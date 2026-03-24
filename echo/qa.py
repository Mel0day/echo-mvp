"""VolcEngine Q&A engine for Echo (OpenAI-compatible API)."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx
from openai import AsyncOpenAI, APIError

from echo.models import Citation, QAMessage, QAResponse, RelatedMemory, SearchResult

VOLC_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-2-0-pro-260215"
TIMEOUT_SECONDS = 30
MAX_RETRIES = 2


def _get_client() -> AsyncOpenAI:
    api_key = os.environ.get("VOLC_API_KEY")
    if not api_key:
        raise RuntimeError("VOLC_API_KEY 未设置")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=VOLC_BASE_URL,
        http_client=httpx.AsyncClient(trust_env=False),
    )


def _build_context_block(results: list[SearchResult]) -> str:
    """Build the context block from search results."""
    parts = []
    for i, r in enumerate(results, 1):
        meta = f"[来源{i}] 文件: {r.title}"
        if r.date:
            meta += f" | 日期: {r.date}"
        if r.section_heading:
            meta += f" | 章节: {r.section_heading}"
        parts.append(f"{meta}\n{r.content}")
    return "\n\n---\n\n".join(parts)


def _build_system_prompt() -> str:
    return """你是 Echo，一个帮助用户回顾和理解自己历史笔记的 AI 助手。

## 核心规则（必须严格遵守）

1. **只基于提供的笔记内容回答**，不要编造或推测用户未写过的内容。
2. **每条回答必须包含来源引用**——格式为：「（来源：[标题] [日期]）」。如果没有可靠来源，直接告知用户未找到相关记录。
3. **禁止无引用输出**——哪怕内容看起来合理，没有来自用户笔记的直接支持就不能作为回答。
4. 用中文回答，语气直接、简洁，不要废话。
5. 如果找到了相关内容，先综合总结用户的观点/记录，再逐一列出具体来源。
6. 引用时请包含原文片段（不超过 2-3 句话），让用户能看到原始内容。

## 回答格式

有结果时：
- 先给出综合性总结（2-4 句）
- 然后用「---」分隔，列出各来源
- 每个来源格式：**[标题] ([日期])**：原文片段...

无结果时：
- 直接说"在你导入的内容中，我没有找到关于 [主题] 的相关记录。"
- 给出 1-2 个改进建议（换一种问法，或者补充数据）"""


def _build_no_results_response(question: str) -> QAResponse:
    """Build a graceful no-results response."""
    suggestions = [
        "换一个更具体的问法，比如加上时间范围或具体项目名",
        "导入更多包含观点和思考的笔记（任务清单类内容检索效果较差）",
        "尝试用关键词替换，比如用英文或缩写试试",
    ]
    answer = (
        f"在你导入的内容中，我没有找到关于「{question}」的相关记录。\n\n"
        "**可能的原因和建议：**\n"
        + "\n".join(f"- {s}" for s in suggestions)
    )
    return QAResponse(
        answer=answer,
        citations=[],
        has_results=False,
        suggestions=suggestions,
    )


async def answer_question(
    question: str,
    search_results: list[SearchResult],
    history: list[QAMessage],
    use_sonnet: bool = False,
) -> QAResponse:
    """
    Generate an answer using VolcEngine API with retrieved context.

    Enforces citation requirement — if no results, returns graceful no-results response.
    """
    if not search_results:
        return _build_no_results_response(question)

    model = DEFAULT_MODEL
    client = _get_client()

    context_block = _build_context_block(search_results)
    system_prompt = _build_system_prompt()

    # Build messages: system + history + current turn
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-6:]:  # keep last 3 turns (6 messages)
        messages.append({"role": msg.role, "content": msg.content})

    user_content = f"""## 我的笔记内容（检索结果）

{context_block}

---

## 我的问题

{question}

请基于以上笔记内容回答，必须包含来源引用。"""

    messages.append({"role": "user", "content": user_content})

    last_error: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                response = await client.chat.completions.create(
                    model=model,
                    max_tokens=1500,
                    messages=messages,
                )
            break
        except TimeoutError:
            return QAResponse(
                answer="回答生成超时（超过 30 秒）。请稍后重试，或尝试更简短的问题。",
                citations=[],
                has_results=True,
                suggestions=["尝试把问题拆成更小的部分", "稍等片刻后重试"],
            )
        except APIError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1.0 * (2 ** attempt))
            else:
                return QAResponse(
                    answer=f"API 暂时不可用，请稍后重试。错误信息：{str(e)[:100]}",
                    citations=[],
                    has_results=True,
                    suggestions=["检查网络连接", "确认 VOLC_API_KEY 是否正确"],
                )
    else:
        return QAResponse(
            answer="API 调用失败，请稍后重试。",
            citations=[],
            has_results=True,
            suggestions=[],
        )

    answer_text = response.choices[0].message.content

    # Top 3 results become citations; rank 4-8 become related_memories
    TOP_N = 3
    citations = []
    for r in search_results[:TOP_N]:
        snippet = r.content[:150].replace('\n', ' ').strip()
        if len(r.content) > 150:
            snippet += "..."
        citations.append(Citation(
            source_file=r.source_file,
            title=r.title,
            date=r.date,
            snippet=snippet,
        ))

    related_memories = []
    for r in search_results[TOP_N:TOP_N + 5]:  # ranks 4-8
        snippet = r.content[:80].replace('\n', ' ').strip()
        if len(r.content) > 80:
            snippet += "..."
        related_memories.append(RelatedMemory(
            title=r.title,
            snippet=snippet,
            source_file=r.source_file,
            date=r.date,
        ))
        if len(related_memories) >= 2:
            break

    return QAResponse(
        answer=answer_text,
        citations=citations,
        has_results=True,
        related_memories=related_memories,
    )


async def generate_recommendations(sample_chunks: list[SearchResult]) -> list[str]:
    """
    Generate 3 personalized recommended questions based on actual content sample.
    """
    if not sample_chunks:
        return [
            "我对哪些话题记录最多？",
            "我最近在思考什么问题？",
            "我有哪些尚未解决的疑问？",
        ]

    client = _get_client()

    content_sample = "\n\n".join([
        f"[{c.title}] {c.content[:200]}"
        for c in sample_chunks[:10]
    ])

    prompt = f"""以下是用户导入的部分笔记内容样本：

{content_sample}

---

请根据这些实际内容，生成 3 个用户可能会想问的个性化问题。

要求：
1. 问题必须基于实际出现的主题和内容，不要编造
2. 问题应该是用户探索自己思考历史的角度，例如"我之前怎么看...？"、"我研究过哪些...？"、"我对...的核心判断是什么？"
3. 每个问题一行，直接输出问题文本，不要编号或额外说明
4. 用中文输出"""

    try:
        async with asyncio.timeout(20):
            response = await client.chat.completions.create(
                model=DEFAULT_MODEL,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
        text = response.choices[0].message.content.strip()
        questions = [q.strip() for q in text.split('\n') if q.strip()]
        return questions[:3] if len(questions) >= 3 else questions + [
            "我最近在思考什么问题？"
        ]
    except Exception:
        return [
            "我之前对这个领域的核心判断是什么？",
            "我研究过哪些相关主题？",
            "我有哪些还没有解决的问题？",
        ]

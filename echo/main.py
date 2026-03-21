"""FastAPI application for Echo."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from echo import indexer, retriever
from echo.chunker import chunk_documents
from echo.embedder import embed_chunks
from echo.models import (
    FeedbackRequest,
    ImportStatus,
    PrivacyConsent,
    QARequest,
    QAResponse,
)
from echo.parser import parse_notion_zip
from echo.qa import answer_question, generate_recommendations

app = FastAPI(title="Echo", description="AI-native cognitive co-pilot", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store (sufficient for single-user local app)
_jobs: dict[str, ImportStatus] = {}

# Static files
STATIC_DIR = Path(__file__).parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the single-file frontend."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Echo</h1><p>Frontend not found. Check static/index.html.</p>")
    return HTMLResponse(index_path.read_text())


# ─── Privacy Consent ────────────────────────────────────────────────────────

@app.get("/consent")
async def get_consent_status():
    """Get current privacy consent status."""
    consent = indexer.get_consent()
    return {"consented": consent["consented"] if consent else None, "timestamp": consent.get("timestamp") if consent else None}


@app.post("/consent")
async def set_consent(body: PrivacyConsent):
    """Record user's privacy consent."""
    indexer.save_consent(body.consented)
    return {"ok": True}


# ─── Import ─────────────────────────────────────────────────────────────────

@app.post("/import")
async def start_import(file: UploadFile = File(...)):
    """Upload a Notion zip file and start import processing."""
    if not file.filename or not file.filename.endswith('.zip'):
        raise HTTPException(400, "请上传 .zip 格式的 Notion 导出文件")

    job_id = str(uuid.uuid4())
    status = ImportStatus(job_id=job_id, status="pending")
    _jobs[job_id] = status

    # Read file contents
    contents = await file.read()

    # Start background processing
    asyncio.create_task(_process_import(job_id, contents, file.filename))

    return {"job_id": job_id}


async def _process_import(job_id: str, zip_bytes: bytes, filename: str) -> None:
    """Background task: parse → chunk → embed → index."""
    status = _jobs[job_id]

    try:
        # Phase 1: Parse
        status.status = "processing"
        status.message = "正在解析 Notion 文件..."

        documents, failed_files = parse_notion_zip(zip_bytes)
        status.total_files = len(documents) + len(failed_files)
        status.failed_docs = len(failed_files)
        status.failed_files = failed_files
        status.successful_docs = len(documents)

        if not documents:
            status.status = "error"
            status.error = "没有找到可解析的文档。请确认上传的是 Notion 导出的 zip 文件。"
            return

        status.message = f"解析完成，共 {len(documents)} 篇文档，开始分块..."

        # Phase 2: Chunk
        chunks, low_content_warning = chunk_documents(documents)
        status.total_chunks = len(chunks)
        status.message = f"已生成 {len(chunks)} 个文本块，开始向量化..."

        if low_content_warning:
            status.message += f"（提示：内容较少，建议导入更多笔记以获得更好体验）"

        # Phase 3: Embed with progress
        embedded_count = 0

        async def progress_cb(done: int, total: int):
            nonlocal embedded_count
            embedded_count = done
            status.processed_files = done
            status.message = f"向量化进度：{done}/{total} 块..."

        chunks_with_embeddings = await embed_chunks(chunks, progress_callback=progress_cb)

        status.message = "向量化完成，正在写入索引..."

        # Phase 4: Index
        import_id = indexer.record_import(
            filename=filename,
            doc_count=len(documents),
            chunk_count=len(chunks),
        )
        indexer.add_chunks(chunks_with_embeddings, import_id=import_id)

        status.status = "done"
        status.message = (
            f"导入完成！共处理 {len(documents)} 篇文档，"
            f"生成 {len(chunks)} 个索引块。"
            + (f" {len(failed_files)} 个文件解析失败。" if failed_files else "")
        )

    except Exception as e:
        status.status = "error"
        status.error = f"导入过程出现错误：{str(e)}"
        status.message = "导入失败，请检查文件格式后重试。"


@app.get("/import/status/{job_id}")
async def get_import_status_sse(job_id: str):
    """SSE stream for import progress."""
    if job_id not in _jobs:
        raise HTTPException(404, "找不到该导入任务")

    async def event_generator() -> AsyncIterator[str]:
        while True:
            status = _jobs.get(job_id)
            if not status:
                break
            data = status.model_dump()
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            if status.status in ("done", "error"):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/import/history")
async def get_import_history():
    """List past imports."""
    history = indexer.get_import_history()
    return [h.model_dump() for h in history]


@app.delete("/import/{import_id}")
async def delete_import(import_id: str):
    """Delete a specific import's data and remove from history."""
    record = indexer.get_import_by_id(import_id)
    if not record:
        raise HTTPException(404, "找不到该导入记录")

    deleted = indexer.delete_import(import_id)
    indexer.remove_import_record(import_id)

    return {"ok": True, "deleted_chunks": deleted}


# ─── Index Stats ─────────────────────────────────────────────────────────────

@app.get("/index/stats")
async def get_index_stats():
    """Return current index statistics."""
    stats = indexer.get_stats()
    return stats.model_dump()


# ─── Q&A ─────────────────────────────────────────────────────────────────────

@app.post("/qa")
async def ask_question(body: QARequest) -> dict:
    """Answer a natural language question using hybrid search + Claude."""
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")

    stats = indexer.get_stats()
    if stats.total_chunks == 0:
        return QAResponse(
            answer="你还没有导入任何笔记。请先导入 Notion 导出的 zip 文件，然后再来提问。",
            citations=[],
            has_results=False,
            suggestions=["点击「导入」按钮上传你的 Notion 导出文件"],
        ).model_dump()

    try:
        results = await retriever.hybrid_search(body.question)
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    response = await answer_question(
        question=body.question,
        search_results=results,
        history=body.history,
        use_sonnet=body.use_sonnet,
    )

    return response.model_dump()


@app.get("/qa/recommendations")
async def get_recommendations():
    """Generate 3 personalized recommended questions based on indexed content."""
    stats = indexer.get_stats()
    if stats.total_chunks == 0:
        return {"questions": []}

    # Get a sample of chunks for recommendation generation
    all_chunks = indexer.get_all_chunks()
    if not all_chunks:
        return {"questions": []}

    # Sample diverse chunks
    import random
    sample = random.sample(all_chunks, min(15, len(all_chunks)))

    from echo.models import SearchResult
    sample_results = [
        SearchResult(
            chunk_id=c["id"],
            content=c["content"],
            source_file=c["source_file"],
            title=c["title"],
            date=c.get("date"),
            section_heading=c.get("section_heading"),
            score=1.0,
        )
        for c in sample
    ]

    questions = await generate_recommendations(sample_results)
    return {"questions": questions}


# ─── Feedback ────────────────────────────────────────────────────────────────

@app.post("/qa/feedback")
async def submit_feedback(body: FeedbackRequest):
    """Record per-answer feedback (thumbs up/down)."""
    # In MVP: just log to a local file
    feedback_dir = indexer._get_data_dir()
    feedback_dir.mkdir(parents=True, exist_ok=True)
    feedback_path = feedback_dir / "feedback.jsonl"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "question": body.question,
        "answer": body.answer[:200],
        "helpful": body.helpful,
    }
    with open(feedback_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"ok": True}


# ─── Data Management ─────────────────────────────────────────────────────────

@app.delete("/data/all")
async def delete_all_data():
    """Delete all local data including vector index."""
    indexer.delete_all_data()
    return {"ok": True, "message": "所有数据已清除"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=True)

"""Hybrid search retriever for Echo — combines semantic (vector) + BM25 keyword search."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from openai import AsyncOpenAI
from rank_bm25 import BM25Okapi

from echo.indexer import get_all_chunks, search_by_vector
from echo.models import SearchResult

# Weights for hybrid scoring
SEMANTIC_WEIGHT = 0.7
BM25_WEIGHT = 0.3
RELEVANCE_THRESHOLD = 0.25
TOP_K_RETRIEVE = 15  # retrieve more, then filter
TOP_K_RETURN = 5

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0


def _get_openai_client() -> AsyncOpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 未设置")
    return AsyncOpenAI(api_key=api_key)


async def _embed_query(query: str) -> list[float]:
    """Embed a single query string with retry."""
    client = _get_openai_client()
    last_error: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.embeddings.create(
                model="text-embedding-3-small",
                input=[query],
            )
            return response.data[0].embedding
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))

    raise RuntimeError(f"查询向量化失败: {last_error}")


def _tokenize(text: str) -> list[str]:
    """Simple tokenizer — split on whitespace and punctuation."""
    import re
    # Handle both Chinese (char-level) and English (word-level)
    tokens = re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text.lower())
    return tokens


def _bm25_search(query: str, all_chunks: list[dict], top_k: int) -> dict[str, float]:
    """
    Run BM25 search over all chunks.
    Returns dict of chunk_id -> normalized BM25 score (0-1).
    """
    if not all_chunks:
        return {}

    corpus = [_tokenize(c["content"]) for c in all_chunks]
    bm25 = BM25Okapi(corpus)
    query_tokens = _tokenize(query)
    scores = bm25.get_scores(query_tokens)

    # Normalize to 0-1
    max_score = max(scores) if scores.max() > 0 else 1.0
    normalized = scores / max_score

    # Build id->score map, only for top candidates
    id_score: dict[str, float] = {}
    for i, chunk in enumerate(all_chunks):
        if normalized[i] > 0.01:  # ignore near-zero scores
            id_score[chunk["id"]] = float(normalized[i])

    return id_score


async def hybrid_search(query: str, top_k: int = TOP_K_RETURN) -> list[SearchResult]:
    """
    Perform hybrid search: semantic (vector) + BM25 keyword.

    Returns top-k SearchResult objects above relevance threshold.
    """
    # Fetch all chunks for BM25 (LanceDB is embedded, this is fast for <100K chunks)
    all_chunks = get_all_chunks()
    if not all_chunks:
        return []

    # Embed query
    query_embedding = await _embed_query(query)

    # Semantic search
    semantic_results = search_by_vector(query_embedding, top_k=TOP_K_RETRIEVE)
    semantic_scores: dict[str, float] = {r.chunk_id: r.score for r in semantic_results}

    # BM25 search
    bm25_scores = _bm25_search(query, all_chunks, top_k=TOP_K_RETRIEVE)

    # Combine: collect all candidate chunk IDs
    all_ids = set(semantic_scores.keys()) | set(bm25_scores.keys())

    # Build lookup for semantic result details
    semantic_detail: dict[str, SearchResult] = {r.chunk_id: r for r in semantic_results}

    # Also build lookup from all_chunks for BM25-only hits
    chunk_detail: dict[str, dict] = {c["id"]: c for c in all_chunks}

    # Score candidates
    combined: list[tuple[float, SearchResult]] = []
    for chunk_id in all_ids:
        sem_score = semantic_scores.get(chunk_id, 0.0)
        bm25_score = bm25_scores.get(chunk_id, 0.0)
        combined_score = SEMANTIC_WEIGHT * sem_score + BM25_WEIGHT * bm25_score

        if chunk_id in semantic_detail:
            result = semantic_detail[chunk_id]
            result = result.model_copy(update={"score": combined_score})
        elif chunk_id in chunk_detail:
            c = chunk_detail[chunk_id]
            result = SearchResult(
                chunk_id=chunk_id,
                content=c["content"],
                source_file=c["source_file"],
                title=c["title"],
                date=c.get("date") or None,
                section_heading=c.get("section_heading") or None,
                score=combined_score,
            )
        else:
            continue

        combined.append((combined_score, result))

    # Sort descending
    combined.sort(key=lambda x: x[0], reverse=True)

    # Filter by threshold and return top-k
    filtered = [r for score, r in combined if score >= RELEVANCE_THRESHOLD]
    return filtered[:top_k]

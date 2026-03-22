"""BM25-only retriever for Echo."""
from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

from echo.indexer import get_all_chunks
from echo.models import SearchResult

RELEVANCE_THRESHOLD = 0.1
TOP_K_RETURN = 5


def _tokenize(text: str) -> list[str]:
    """Tokenize for both Chinese (char-level) and English (word-level)."""
    return re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text.lower())


async def hybrid_search(query: str, top_k: int = TOP_K_RETURN) -> list[SearchResult]:
    """BM25 keyword search over all indexed chunks."""
    all_chunks = get_all_chunks()
    if not all_chunks:
        return []

    corpus = [_tokenize(c["content"]) for c in all_chunks]
    bm25 = BM25Okapi(corpus)
    query_tokens = _tokenize(query)
    scores = bm25.get_scores(query_tokens)

    # BM25Okapi scores can be negative (IDF sign flip for common terms in small corpora).
    # Normalize by range so all scores land in [0, 1].
    min_score = float(scores.min())
    max_score = float(scores.max())
    score_range = max_score - min_score

    if score_range < 1e-9:
        # All docs score identically — BM25 can't distinguish.
        # Fall back to token-overlap ratio as score.
        query_set = set(query_tokens)
        overlap_scores = []
        for chunk in all_chunks:
            doc_tokens = set(_tokenize(chunk["content"]))
            overlap = len(query_set & doc_tokens) / max(len(query_set), 1)
            overlap_scores.append(overlap)
        import numpy as np
        scores = np.array(overlap_scores, dtype=float)
        min_score = float(scores.min())
        max_score = float(scores.max())
        score_range = max_score - min_score
        if score_range < 1e-9:
            # All identical — return top results by overlap score directly
            normalized = scores
        else:
            normalized = (scores - min_score) / score_range
    else:
        normalized = (scores - min_score) / score_range

    results: list[tuple[float, SearchResult]] = []
    for i, chunk in enumerate(all_chunks):
        score = float(normalized[i])
        if score >= RELEVANCE_THRESHOLD:
            results.append((score, SearchResult(
                chunk_id=chunk["id"],
                content=chunk["content"],
                source_file=chunk["source_file"],
                title=chunk["title"],
                date=chunk.get("date") or None,
                section_heading=chunk.get("section_heading") or None,
                score=score,
            )))

    results.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in results[:top_k]]

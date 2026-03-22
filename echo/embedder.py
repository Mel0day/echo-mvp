"""Dummy embedder for Echo — BM25-only mode (no embedding API required)."""
from __future__ import annotations

from echo.models import Chunk

# Dummy embedding dimension matching LanceDB schema
EMBEDDING_DIM = 1536


async def embed_chunks(
    chunks: list[Chunk],
    progress_callback=None,
) -> list[tuple[Chunk, list[float]]]:
    """
    Return (chunk, dummy_embedding) pairs.

    BM25-only mode: embeddings are zero vectors. Search uses keyword matching only.
    """
    results = []
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        results.append((chunk, [0.0] * EMBEDDING_DIM))
        if progress_callback and (i + 1) % 10 == 0:
            await progress_callback(i + 1, total)

    if progress_callback and total > 0:
        await progress_callback(total, total)

    return results

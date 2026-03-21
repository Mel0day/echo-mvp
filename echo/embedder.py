"""OpenAI embedding with retry logic for Echo."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from openai import AsyncOpenAI, RateLimitError, APIError

from echo.models import Chunk

EMBEDDING_MODEL = "text-embedding-3-small"
BATCH_SIZE = 100
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds


def _get_client() -> AsyncOpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 未设置")
    return AsyncOpenAI(api_key=api_key)


async def _embed_batch(client: AsyncOpenAI, texts: list[str]) -> list[list[float]]:
    """Embed a single batch with exponential backoff retry."""
    last_error: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except (RateLimitError, APIError) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
        except Exception as e:
            raise RuntimeError(f"Embedding API 错误: {e}") from e

    raise RuntimeError(
        f"Embedding API 在 {MAX_RETRIES} 次重试后仍然失败: {last_error}"
    )


async def embed_chunks(
    chunks: list[Chunk],
    progress_callback=None,
) -> list[tuple[Chunk, list[float]]]:
    """
    Embed all chunks and return (chunk, embedding) pairs.

    Args:
        chunks: List of Chunk objects to embed.
        progress_callback: Optional async callable(done, total) for progress.

    Returns:
        List of (Chunk, embedding_vector) tuples.
    """
    client = _get_client()
    results: list[tuple[Chunk, list[float]]] = []
    total = len(chunks)
    done = 0

    for i in range(0, total, BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE]
        texts = [c.content for c in batch]
        embeddings = await _embed_batch(client, texts)
        for chunk, emb in zip(batch, embeddings):
            results.append((chunk, emb))
        done += len(batch)
        if progress_callback:
            await progress_callback(done, total)

    return results

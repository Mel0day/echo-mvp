"""Journal entry storage — converts user text directly into indexed chunks."""
from __future__ import annotations

import uuid
from datetime import date

from echo.models import Chunk


def entry_to_chunks(text: str, entry_id: str | None = None) -> list[Chunk]:
    """
    Convert a user journal entry into one or more Chunk objects for indexing.

    Long entries are split at paragraph boundaries. Short entries become one chunk.
    """
    if entry_id is None:
        entry_id = str(uuid.uuid4())

    today = date.today().isoformat()
    # Use first 60 chars as title
    title = text[:60].strip().replace("\n", " ")
    if len(text) > 60:
        title += "..."

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    chunks = []
    for i, para in enumerate(paragraphs):
        if not para:
            continue
        chunks.append(Chunk(
            id=str(uuid.uuid4()),
            content=para,
            source_file=entry_id,
            title=title,
            date=today,
            section_heading=None,
            chunk_index=i,
        ))
    return chunks

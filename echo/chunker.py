"""Text chunker for Echo — splits documents into semantic chunks."""
from __future__ import annotations

import re
import uuid
from typing import Optional

import tiktoken

from echo.models import Chunk, Document

# Target chunk size in tokens
CHUNK_MIN_TOKENS = 100
CHUNK_TARGET_TOKENS = 350
CHUNK_MAX_TOKENS = 512

# Warn threshold
MIN_CHUNKS_WARNING = 50

_enc = None


def _get_encoder() -> tiktoken.Encoding:
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding("cl100k_base")
    return _enc


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def _split_into_sections(content: str) -> list[tuple[Optional[str], str]]:
    """
    Split content by markdown headings.
    Returns list of (heading, text) tuples.
    """
    # Split by H2/H3 headings
    pattern = re.compile(r'^(#{2,3}\s+.+)$', re.MULTILINE)
    parts = pattern.split(content)

    sections: list[tuple[Optional[str], str]] = []

    if not parts:
        return [(None, content)]

    # parts alternates between text and heading captures
    # First part is preamble (before first heading)
    if parts[0].strip():
        sections.append((None, parts[0].strip()))

    i = 1
    while i < len(parts):
        heading = parts[i].lstrip('#').strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ''
        if body:
            sections.append((heading, body))
        elif heading:
            # Heading with no body — attach to previous or skip
            if sections:
                prev_heading, prev_body = sections[-1]
                sections[-1] = (prev_heading, prev_body + '\n\n' + heading)
        i += 2

    if not sections:
        sections = [(None, content)]

    return sections


def _split_section_into_chunks(
    section_text: str,
    heading: Optional[str],
    source_file: str,
    title: str,
    date: Optional[str],
    start_index: int,
) -> list[Chunk]:
    """Split a single section's text into token-sized chunks."""
    chunks: list[Chunk] = []

    # Split by paragraphs first
    paragraphs = re.split(r'\n\n+', section_text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    current_parts: list[str] = []
    current_tokens = 0
    chunk_index = start_index

    def flush() -> None:
        nonlocal current_tokens, chunk_index
        if not current_parts:
            return
        text = '\n\n'.join(current_parts)
        token_count = _count_tokens(text)
        if token_count < 10:
            return
        chunks.append(Chunk(
            id=str(uuid.uuid4()),
            content=text,
            source_file=source_file,
            title=title,
            date=date,
            section_heading=heading,
            chunk_index=chunk_index,
            token_count=token_count,
        ))
        chunk_index += 1
        current_parts.clear()
        current_tokens = 0

    for para in paragraphs:
        para_tokens = _count_tokens(para)

        # If single paragraph exceeds max, split by sentences
        if para_tokens > CHUNK_MAX_TOKENS:
            flush()
            sentences = re.split(r'(?<=[。！？.!?])\s*', para)
            sentence_buf: list[str] = []
            sentence_tokens = 0
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                st = _count_tokens(sent)
                if sentence_tokens + st > CHUNK_MAX_TOKENS and sentence_buf:
                    chunks.append(Chunk(
                        id=str(uuid.uuid4()),
                        content=' '.join(sentence_buf),
                        source_file=source_file,
                        title=title,
                        date=date,
                        section_heading=heading,
                        chunk_index=chunk_index,
                        token_count=sentence_tokens,
                    ))
                    chunk_index += 1
                    sentence_buf = [sent]
                    sentence_tokens = st
                else:
                    sentence_buf.append(sent)
                    sentence_tokens += st
            if sentence_buf:
                text = ' '.join(sentence_buf)
                chunks.append(Chunk(
                    id=str(uuid.uuid4()),
                    content=text,
                    source_file=source_file,
                    title=title,
                    date=date,
                    section_heading=heading,
                    chunk_index=chunk_index,
                    token_count=_count_tokens(text),
                ))
                chunk_index += 1
            continue

        # Normal accumulation
        if current_tokens + para_tokens > CHUNK_MAX_TOKENS and current_parts:
            flush()

        current_parts.append(para)
        current_tokens += para_tokens

        if current_tokens >= CHUNK_TARGET_TOKENS:
            flush()

    flush()
    return chunks


def chunk_documents(documents: list[Document]) -> tuple[list[Chunk], bool]:
    """
    Chunk a list of documents into token-sized pieces.

    Returns:
        (chunks, low_content_warning) — warning=True if fewer than 50 chunks.
    """
    all_chunks: list[Chunk] = []

    for doc in documents:
        sections = _split_into_sections(doc.content)
        chunk_index = 0
        for heading, section_text in sections:
            new_chunks = _split_section_into_chunks(
                section_text=section_text,
                heading=heading,
                source_file=doc.source_file,
                title=doc.title,
                date=doc.date,
                start_index=chunk_index,
            )
            all_chunks.extend(new_chunks)
            chunk_index += len(new_chunks)

    low_content_warning = len(all_chunks) < MIN_CHUNKS_WARNING
    return all_chunks, low_content_warning

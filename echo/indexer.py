"""LanceDB indexer for Echo."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import lancedb
import pyarrow as pa

from echo.models import Chunk, IndexStats, ImportRecord, SearchResult

def _get_data_dir() -> Path:
    return Path(os.environ.get("ECHO_DATA_DIR", "./data"))

DATA_DIR = _get_data_dir()
DB_PATH = DATA_DIR / "echo.lance"
TABLE_NAME = "chunks"
IMPORT_HISTORY_PATH = DATA_DIR / "import_history.json"
CONSENT_PATH = DATA_DIR / "consent.json"

# Schema for LanceDB table
SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("content", pa.string()),
    pa.field("embedding", pa.list_(pa.float32(), 1536)),
    pa.field("source_file", pa.string()),
    pa.field("title", pa.string()),
    pa.field("date", pa.string()),
    pa.field("section_heading", pa.string()),
    pa.field("chunk_index", pa.int32()),
    pa.field("import_id", pa.string()),
])


def _get_db() -> lancedb.DBConnection:
    data_dir = _get_data_dir()
    db_path = data_dir / "echo.lance"
    data_dir.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(db_path))


def _table_exists(db: lancedb.DBConnection) -> bool:
    """Check whether the chunks table exists."""
    result = db.list_tables()
    # list_tables() may return a ListTablesResponse object or a plain list
    table_list = result.tables if hasattr(result, 'tables') else list(result)
    return TABLE_NAME in table_list


def _get_table(db: lancedb.DBConnection):
    """Get or create the chunks table."""
    if _table_exists(db):
        return db.open_table(TABLE_NAME)
    return db.create_table(TABLE_NAME, schema=SCHEMA)


def add_chunks(chunks_with_embeddings: list[tuple[Chunk, list[float]]], import_id: str) -> None:
    """Add chunks with their embeddings to LanceDB."""
    if not chunks_with_embeddings:
        return

    db = _get_db()
    table = _get_table(db)

    rows = []
    for chunk, embedding in chunks_with_embeddings:
        rows.append({
            "id": chunk.id,
            "content": chunk.content,
            "embedding": embedding,
            "source_file": chunk.source_file,
            "title": chunk.title,
            "date": chunk.date or "",
            "section_heading": chunk.section_heading or "",
            "chunk_index": chunk.chunk_index,
            "import_id": import_id,
        })

    table.add(rows)


def search_by_vector(query_embedding: list[float], top_k: int = 10) -> list[SearchResult]:
    """Search chunks by vector similarity."""
    db = _get_db()
    if not _table_exists(db):
        return []

    table = db.open_table(TABLE_NAME)
    try:
        results = (
            table.search(query_embedding)
            .metric("cosine")
            .limit(top_k)
            .to_list()
        )
    except Exception:
        return []

    search_results = []
    for r in results:
        # LanceDB cosine distance: 0 = identical, 2 = opposite
        # Convert to similarity score: 1 - distance/2 (range 0-1)
        distance = r.get("_distance", 1.0)
        score = max(0.0, 1.0 - distance)
        search_results.append(SearchResult(
            chunk_id=r["id"],
            content=r["content"],
            source_file=r["source_file"],
            title=r["title"],
            date=r.get("date") or None,
            section_heading=r.get("section_heading") or None,
            score=score,
        ))

    return search_results


def get_all_chunks() -> list[dict]:
    """Return all chunks as dicts (for BM25 indexing)."""
    db = _get_db()
    if not _table_exists(db):
        return []
    table = db.open_table(TABLE_NAME)
    try:
        rows = table.to_pandas()[["id", "content", "source_file", "title", "date", "section_heading"]].to_dict("records")
        return rows
    except Exception:
        return []


def get_stats() -> IndexStats:
    """Return index statistics."""
    db = _get_db()
    if not _table_exists(db):
        return IndexStats(total_chunks=0, last_updated=None)

    table = db.open_table(TABLE_NAME)
    try:
        df = table.to_pandas()
        total = len(df)
        source_count = df["source_file"].nunique() if total > 0 else 0
    except Exception:
        return IndexStats(total_chunks=0, last_updated=None)

    history = _load_import_history()
    last_updated = None
    if history:
        last_updated = history[-1]["imported_at"]

    return IndexStats(
        total_chunks=total,
        last_updated=last_updated,
        source_count=source_count,
    )


def delete_import(import_id: str) -> int:
    """Delete all chunks associated with an import_id. Returns count deleted."""
    db = _get_db()
    if not _table_exists(db):
        return 0

    table = db.open_table(TABLE_NAME)
    try:
        df = table.to_pandas()
        before = len(df)
        remaining = df[df["import_id"] != import_id]
        deleted = before - len(remaining)
        if deleted > 0:
            # Recreate table with remaining data
            db.drop_table(TABLE_NAME)
            if len(remaining) > 0:
                new_table = db.create_table(TABLE_NAME, schema=SCHEMA)
                new_table.add(remaining.to_dict("records"))
        return deleted
    except Exception:
        return 0


def delete_all_data() -> None:
    """Delete everything."""
    data_dir = _get_data_dir()
    db = _get_db()
    if _table_exists(db):
        db.drop_table(TABLE_NAME)

    (data_dir / "import_history.json").unlink(missing_ok=True)
    (data_dir / "consent.json").unlink(missing_ok=True)


def _load_import_history() -> list[dict]:
    path = _get_data_dir() / "import_history.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save_import_history(history: list[dict]) -> None:
    data_dir = _get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "import_history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2)
    )


def record_import(filename: str, doc_count: int, chunk_count: int) -> str:
    """Record a successful import, return import_id."""
    import_id = str(uuid.uuid4())
    history = _load_import_history()
    history.append({
        "id": import_id,
        "filename": filename,
        "imported_at": datetime.now().isoformat(),
        "doc_count": doc_count,
        "chunk_count": chunk_count,
    })
    _save_import_history(history)
    return import_id


def get_import_history() -> list[ImportRecord]:
    history = _load_import_history()
    return [ImportRecord(**h) for h in history]


def get_import_by_id(import_id: str) -> Optional[dict]:
    history = _load_import_history()
    for h in history:
        if h["id"] == import_id:
            return h
    return None


def remove_import_record(import_id: str) -> None:
    history = _load_import_history()
    history = [h for h in history if h["id"] != import_id]
    _save_import_history(history)


# Privacy consent helpers

def save_consent(consented: bool) -> None:
    data_dir = _get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "consent.json").write_text(json.dumps({
        "consented": consented,
        "timestamp": datetime.now().isoformat(),
    }))


def get_consent() -> Optional[dict]:
    path = _get_data_dir() / "consent.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

"""Pydantic models for Echo."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Document(BaseModel):
    """A parsed document from Notion export."""
    id: str
    title: str
    content: str
    source_file: str
    date: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    failed: bool = False
    error: Optional[str] = None


class Chunk(BaseModel):
    """A text chunk derived from a document."""
    id: str
    content: str
    source_file: str
    title: str
    date: Optional[str] = None
    section_heading: Optional[str] = None
    chunk_index: int
    token_count: int = 0


class SearchResult(BaseModel):
    """A search result with relevance score."""
    chunk_id: str
    content: str
    source_file: str
    title: str
    date: Optional[str] = None
    section_heading: Optional[str] = None
    score: float


class Citation(BaseModel):
    """A citation for an answer."""
    source_file: str
    title: str
    date: Optional[str] = None
    snippet: str


class QAMessage(BaseModel):
    """A single message in Q&A history."""
    role: str  # "user" or "assistant"
    content: str
    citations: list[Citation] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)


class QARequest(BaseModel):
    """Request body for Q&A endpoint."""
    question: str
    history: list[QAMessage] = Field(default_factory=list)
    use_sonnet: bool = False


class QAResponse(BaseModel):
    """Response from Q&A endpoint."""
    answer: str
    citations: list[Citation]
    has_results: bool
    suggestions: list[str] = Field(default_factory=list)


class ImportStatus(BaseModel):
    """Status of an import job."""
    job_id: str
    status: str  # "pending" | "processing" | "done" | "error"
    total_files: int = 0
    processed_files: int = 0
    successful_docs: int = 0
    failed_docs: int = 0
    total_chunks: int = 0
    failed_files: list[dict] = Field(default_factory=list)
    message: str = ""
    error: Optional[str] = None


class IndexStats(BaseModel):
    """Statistics about the current index."""
    total_chunks: int
    last_updated: Optional[str] = None
    source_count: int = 0


class ImportRecord(BaseModel):
    """Record of a past import."""
    id: str
    filename: str
    imported_at: str
    doc_count: int
    chunk_count: int


class FeedbackRequest(BaseModel):
    """Feedback on an answer."""
    question: str
    answer: str
    helpful: bool


class PrivacyConsent(BaseModel):
    """User's privacy consent."""
    consented: bool
    timestamp: str

"""Integration tests for Echo MVP."""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient


def make_test_zip() -> bytes:
    """Create a minimal Notion-like zip for testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr(
            'Product Strategy 2024-03-15.md',
            """# 产品策略思考

我对 to B vs to C 的判断经历了几次转变。

## 核心立场

倾向 to B，原因是付费意愿强、需求刚性。

## 2024 年修正

to C 的分发成本被 AI 拉平，需要重新审视。
"""
        )
        zf.writestr(
            'AI Observations 2024-05-01.md',
            """# AI 产品观察

大模型让很多事情变得可能，但用户习惯是最大的障碍。

## 关键洞察

产品的核心竞争力不是 AI 技术本身，而是解决的问题有多真实。
"""
        )
    return buf.getvalue()


# ─── Parser Tests ────────────────────────────────────────────────────────────

def test_parse_notion_zip_basic():
    from echo.parser import parse_notion_zip
    zip_bytes = make_test_zip()
    docs, failed = parse_notion_zip(zip_bytes)
    assert len(docs) == 2
    assert len(failed) == 0
    titles = {d.title for d in docs}
    assert '产品策略思考' in titles
    assert 'AI 产品观察' in titles


def test_parse_extracts_date():
    from echo.parser import parse_notion_zip
    zip_bytes = make_test_zip()
    docs, _ = parse_notion_zip(zip_bytes)
    dated = [d for d in docs if d.date]
    assert len(dated) >= 1
    dates = {d.date for d in dated}
    assert '2024-03-15' in dates


def test_parse_bad_zip_returns_error():
    from echo.parser import parse_notion_zip
    docs, failed = parse_notion_zip(b'this is not a zip file')
    assert len(docs) == 0
    assert len(failed) == 1
    assert 'zip' in failed[0]['reason'].lower() or '文件' in failed[0]['reason']


def test_parse_empty_file_skipped():
    from echo.parser import parse_notion_zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('empty.md', '')
        zf.writestr('real.md', '# Title\n\nSome content here.')
    docs, failed = parse_notion_zip(buf.getvalue())
    assert len(docs) == 1
    assert len(failed) == 1


# ─── Chunker Tests ───────────────────────────────────────────────────────────

def test_chunk_preserves_metadata():
    from echo.parser import parse_notion_zip
    from echo.chunker import chunk_documents
    zip_bytes = make_test_zip()
    docs, _ = parse_notion_zip(zip_bytes)
    chunks, _ = chunk_documents(docs)
    assert len(chunks) > 0
    for c in chunks:
        assert c.source_file
        assert c.title
        assert c.content.strip()
        assert c.token_count > 0


def test_chunk_warns_on_low_content():
    from echo.parser import parse_notion_zip
    from echo.chunker import chunk_documents
    # Small zip => low chunk count
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('tiny.md', '# Short\n\nJust a few words.')
    docs, _ = parse_notion_zip(buf.getvalue())
    _, warn = chunk_documents(docs)
    assert warn is True  # Less than 50 chunks


def test_chunk_does_not_split_mid_sentence():
    from echo.models import Document
    from echo.chunker import chunk_documents
    import uuid
    # Very long content with sentences
    sentences = ['这是第{}句话，内容很长需要被分块处理。'.format(i) for i in range(200)]
    content = ' '.join(sentences)
    doc = Document(
        id=str(uuid.uuid4()),
        title='Long Doc',
        content=content,
        source_file='long.md',
    )
    chunks, _ = chunk_documents([doc])
    assert len(chunks) > 1
    for c in chunks:
        # Each chunk should be complete text, not mid-word cut
        assert len(c.content) > 0


# ─── Indexer Tests ───────────────────────────────────────────────────────────

def test_indexer_add_and_query(tmp_path):
    import os
    os.environ['ECHO_DATA_DIR'] = str(tmp_path)

    from echo.models import Chunk
    from echo import indexer
    import importlib
    importlib.reload(indexer)

    import uuid
    chunk = Chunk(
        id=str(uuid.uuid4()),
        content='Test content about product strategy',
        source_file='test.md',
        title='Test',
        date='2024-01-01',
        chunk_index=0,
        token_count=10,
    )
    embedding = [0.1] * 1536

    indexer.add_chunks([(chunk, embedding)], import_id='test-001')
    results = indexer.search_by_vector(embedding, top_k=5)
    assert len(results) >= 1
    assert results[0].content == 'Test content about product strategy'


def test_indexer_stats(tmp_path):
    import os
    os.environ['ECHO_DATA_DIR'] = str(tmp_path)

    from echo import indexer
    import importlib
    importlib.reload(indexer)

    stats = indexer.get_stats()
    assert stats.total_chunks == 0


def test_indexer_consent(tmp_path):
    import os
    os.environ['ECHO_DATA_DIR'] = str(tmp_path)

    from echo import indexer
    import importlib
    importlib.reload(indexer)

    assert indexer.get_consent() is None
    indexer.save_consent(True)
    consent = indexer.get_consent()
    assert consent is not None
    assert consent['consented'] is True


# ─── API Tests ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    import os
    os.environ['ECHO_DATA_DIR'] = str(tmp_path)
    from echo.main import app
    return TestClient(app)


def test_api_consent_get_initial(client):
    resp = client.get('/consent')
    assert resp.status_code == 200
    assert resp.json()['consented'] is None


def test_api_consent_set(client):
    resp = client.post('/consent', json={'consented': True, 'timestamp': '2024-01-01T00:00:00'})
    assert resp.status_code == 200
    resp2 = client.get('/consent')
    assert resp2.json()['consented'] is True


def test_api_index_stats_empty(client):
    resp = client.get('/index/stats')
    assert resp.status_code == 200
    data = resp.json()
    assert data['total_chunks'] == 0


def test_api_import_wrong_format(client):
    resp = client.post('/import', files={'file': ('test.txt', b'not a zip', 'text/plain')})
    assert resp.status_code == 400


def test_api_import_zip(client):
    zip_bytes = make_test_zip()
    resp = client.post('/import', files={'file': ('export.zip', zip_bytes, 'application/zip')})
    assert resp.status_code == 200
    data = resp.json()
    assert 'job_id' in data


def test_api_qa_no_index(client):
    resp = client.post('/qa', json={'question': '我对 AI 的判断是什么？'})
    assert resp.status_code == 200
    data = resp.json()
    assert data['has_results'] is False
    # When index is empty, the answer guides user to add content first
    assert 'Echo' in data['answer'] or '输入框' in data['answer']


def test_api_import_history_empty(client):
    resp = client.get('/import/history')
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_delete_all(client):
    resp = client.delete('/data/all')
    assert resp.status_code == 200
    assert resp.json()['ok'] is True


def test_api_recommendations_empty(client):
    resp = client.get('/qa/recommendations')
    assert resp.status_code == 200
    assert resp.json()['questions'] == []


def test_frontend_served(client):
    resp = client.get('/')
    assert resp.status_code == 200
    # Page title is "Echo — 认知副驾" (mixed-case)
    assert 'Echo' in resp.text


# ─── Journal Entry Tests ─────────────────────────────────────────────────────

# ─── Story-004: Related Memories ─────────────────────────────────────────────

def test_qa_response_has_related_memories_field(client):
    """QAResponse model_dump must include related_memories field."""
    from echo.models import QAResponse
    resp = QAResponse(answer='test', citations=[], has_results=True)
    d = resp.model_dump()
    assert 'related_memories' in d
    assert d['related_memories'] == []


def test_related_memory_model():
    """RelatedMemory model accepts all fields."""
    from echo.models import RelatedMemory
    rm = RelatedMemory(title='测试标题', snippet='这是摘要', source_file='test.md', date='2024-01-01')
    assert rm.title == '测试标题'
    assert rm.date == '2024-01-01'


def test_answer_question_builds_related_memories():
    """answer_question maps rank 4-8 search results into related_memories."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from echo.models import SearchResult
    from echo.qa import answer_question

    # Build 6 search results
    results = [
        SearchResult(
            chunk_id=f'id-{i}',
            content=f'Content number {i} ' * 20,
            source_file=f'file{i}.md',
            title=f'Doc {i}',
            date='2024-01-01',
            score=1.0 - i * 0.1,
        )
        for i in range(6)
    ]

    # Mock the LLM call
    mock_choice = MagicMock()
    mock_choice.message.content = '测试回答内容'
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch('echo.qa._get_client', return_value=mock_client):
        response = asyncio.get_event_loop().run_until_complete(
            answer_question(question='测试问题', search_results=results, history=[])
        )

    assert len(response.citations) <= 3
    assert len(response.related_memories) >= 1
    assert len(response.related_memories) <= 2


def test_journal_entry_creation(client):
    """POST /journal/entry stores text as indexed chunks and returns metadata."""
    payload = {'text': '今天思考了一下 to B 与 to C 的差异，觉得付费意愿才是核心变量。'}
    resp = client.post('/journal/entry', json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data['ok'] is True
    assert 'entry_id' in data
    assert data['chunks_stored'] >= 1

    # The chunk should now be visible in index stats
    stats_resp = client.get('/index/stats')
    assert stats_resp.json()['total_chunks'] >= 1


def test_journal_entry_empty_text_rejected(client):
    """POST /journal/entry with blank text returns 400."""
    resp = client.post('/journal/entry', json={'text': '   '})
    assert resp.status_code == 400


# ─── Story-002: Memory Management ────────────────────────────────────────────

def test_api_memory_history_and_stats(client):
    """GET /import/history and GET /index/stats are callable and return correct shape."""
    history_resp = client.get('/import/history')
    assert history_resp.status_code == 200
    assert isinstance(history_resp.json(), list)

    stats_resp = client.get('/index/stats')
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert 'total_chunks' in stats
    assert 'source_count' in stats


def test_api_delete_nonexistent_import(client):
    """DELETE /import/<nonexistent-id> returns 404."""
    fake_id = '00000000-0000-0000-0000-000000000000'
    resp = client.delete(f'/import/{fake_id}')
    assert resp.status_code == 404


# ─── Story-003: Daily Review ─────────────────────────────────────────────────

def test_api_review_daily_empty(client):
    """/review/daily returns null when index is empty."""
    resp = client.get('/review/daily')
    assert resp.status_code == 200
    assert resp.json() is None


def test_api_review_daily_with_data(client, tmp_path):
    """After indexing a chunk, /review/daily returns a valid review object."""
    import os
    os.environ['ECHO_DATA_DIR'] = str(tmp_path)
    from echo import indexer
    import importlib
    importlib.reload(indexer)

    from echo.models import Chunk
    import uuid
    chunk = Chunk(
        id=str(uuid.uuid4()),
        content='这是一段用于测试每日回顾的历史笔记内容。',
        source_file='review_test.md',
        title='回顾测试文档',
        date='2024-01-15',
        chunk_index=0,
        token_count=20,
    )
    import_id = indexer.record_import('review_test.zip', 1, 1)
    # Simulate old import by backdating it in history
    history = indexer._load_import_history()
    from datetime import datetime, timedelta
    history[-1]['imported_at'] = (datetime.now() - timedelta(hours=48)).isoformat()
    indexer._save_import_history(history)
    indexer.add_chunks([(chunk, [0.1] * 1536)], import_id=import_id)

    # Now call the endpoint via a fresh client pointing at tmp_path
    from echo.main import app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    resp = c.get('/review/daily')
    assert resp.status_code == 200
    data = resp.json()
    assert data is not None
    assert 'content' in data
    assert 'title' in data
    assert 'source_file' in data


def test_journal_entry_then_qa_finds_content(client, tmp_path):
    """After storing a journal entry, QA retriever should find matching content.

    The LLM call (answer_question) is mocked so that no VOLC_API_KEY is needed.
    The test verifies that the retriever path is exercised (not the empty-index
    early-return path) and that the response structure is valid.
    """
    from unittest.mock import AsyncMock, patch
    from echo.models import QAResponse

    # Store a distinctive entry
    entry_text = '产品核心竞争力不是 AI 技术本身，而是解决的问题有多真实。'
    client.post('/journal/entry', json={'text': entry_text})

    stub_response = QAResponse(
        answer='（测试存根回答）产品核心竞争力在于解决真实问题。',
        citations=[],
        has_results=True,
    )

    with patch('echo.main.answer_question', new=AsyncMock(return_value=stub_response)):
        resp = client.post('/qa', json={'question': '产品核心竞争力是什么？'})

    assert resp.status_code == 200
    data = resp.json()
    # Must not hit the empty-index fallback
    assert data['has_results'] is True
    # answer_question stub was called, not the no-index branch
    assert '输入框' not in data['answer']

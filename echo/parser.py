"""Notion zip parser for Echo."""
from __future__ import annotations

import io
import re
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from echo.models import Document


def _extract_date_from_filename(filename: str) -> Optional[str]:
    """Extract date from Notion export filename pattern like 'Title YYYY-MM-DD...'."""
    # Notion exports often embed date in the filename as hex hash, or use a date pattern
    match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if match:
        return match.group(1)
    return None


def _parse_markdown(content: str, filename: str) -> tuple[str, str, list[str]]:
    """Parse markdown content, extract title, clean text, extract tags."""
    lines = content.split('\n')
    title = Path(filename).stem
    tags: list[str] = []
    body_lines: list[str] = []
    found_title = False

    for line in lines:
        stripped = line.strip()
        # First H1 becomes title
        if not found_title and stripped.startswith('# '):
            title = stripped[2:].strip()
            found_title = True
            continue
        # Extract tags from lines like "Tags: tag1, tag2" or YAML front matter
        if re.match(r'^[Tt]ags?:\s*(.+)', stripped):
            tag_str = re.match(r'^[Tt]ags?:\s*(.+)', stripped).group(1)
            tags = [t.strip() for t in tag_str.split(',') if t.strip()]
            continue
        body_lines.append(line)

    body = '\n'.join(body_lines).strip()
    return title, body, tags


def _parse_html(content: str, filename: str) -> tuple[str, str, list[str]]:
    """Parse HTML content, extract title and clean text."""
    soup = BeautifulSoup(content, 'html.parser')

    # Extract title
    title = Path(filename).stem
    title_tag = soup.find('title')
    if title_tag and title_tag.get_text(strip=True):
        title = title_tag.get_text(strip=True)
    h1 = soup.find('h1')
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)

    # Extract tags from meta or specific Notion elements
    tags: list[str] = []

    # Remove script, style, nav elements
    for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
        tag.decompose()

    # Get clean text
    text = soup.get_text(separator='\n')
    # Clean up excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    return title, text, tags


def _parse_zip_entries(zf: zipfile.ZipFile, documents: list, failed_files: list) -> None:
    """Recursively parse entries in a ZipFile, handling nested zips."""
    file_list = zf.namelist()

    target_files = [
        f for f in file_list
        if (f.endswith('.md') or f.endswith('.html'))
        and not Path(f).name.startswith('.')
        and '__MACOSX' not in f
    ]

    # Recurse into nested zips (Notion often wraps export in outer zip)
    nested_zips = [
        f for f in file_list
        if f.endswith('.zip') and '__MACOSX' not in f
    ]
    for nested_path in nested_zips:
        try:
            nested_bytes = zf.read(nested_path)
            inner_zf = zipfile.ZipFile(io.BytesIO(nested_bytes))
            _parse_zip_entries(inner_zf, documents, failed_files)
            inner_zf.close()
        except Exception as e:
            failed_files.append({'filename': nested_path, 'reason': f'内层 zip 解析失败: {str(e)[:80]}'})

    for filepath in target_files:
        filename = Path(filepath).name
        try:
            raw = zf.read(filepath)
            # Try UTF-8, fall back to latin-1
            try:
                content = raw.decode('utf-8')
            except UnicodeDecodeError:
                content = raw.decode('latin-1')

            if not content.strip():
                failed_files.append({'filename': filename, 'reason': '文件内容为空'})
                continue

            date = _extract_date_from_filename(filepath)

            if filepath.endswith('.md'):
                title, body, tags = _parse_markdown(content, filename)
            else:
                title, body, tags = _parse_html(content, filename)

            if not body.strip():
                # Fallback: use title as content (e.g. Notion pages with only a heading)
                body = title

            doc_id = str(uuid.uuid4())
            documents.append(Document(
                id=doc_id,
                title=title,
                content=body,
                source_file=filepath,
                date=date,
                tags=tags,
            ))

        except Exception as e:
            failed_files.append({'filename': filename, 'reason': f'解析错误: {str(e)[:100]}'})


def parse_notion_zip(zip_bytes: bytes) -> tuple[list[Document], list[dict]]:
    """
    Parse a Notion export zip file. Handles nested zips (Notion wraps export in outer zip).

    Returns:
        (documents, failed_files) where failed_files is a list of
        {'filename': str, 'reason': str} dicts.
    """
    documents: list[Document] = []
    failed_files: list[dict] = []

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return [], [{'filename': 'archive', 'reason': '不是有效的 zip 文件'}]

    _parse_zip_entries(zf, documents, failed_files)
    zf.close()

    if not documents and not failed_files:
        failed_files.append({'filename': 'archive', 'reason': '未找到可解析内容'})

    return documents, failed_files

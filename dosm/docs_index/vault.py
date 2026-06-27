"""Documentation vault - authoring, import, and file management.

Vault files live at $DOSM_HOME/docs/<app-slug>/<doc-slug>.md.
Every file has a YAML frontmatter block so metadata survives round-trips
through external editors, git pulls, and file-system copies.

The indexer reads frontmatter to populate application_id on Document rows.
"""
from __future__ import annotations

import io
import re
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from dosm.docs_index.store import DocsStore

UNFILED_SLUG = "_unfiled"


# ── Slug helpers ─────────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    """Convert arbitrary text to a URL/filesystem-safe kebab-case slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text) or "doc"


def find_unique_slug(store: DocsStore, folder_slug: str, base_slug: str) -> str:
    """Return base_slug if no collision in ``folder_slug``, else base_slug-2, …"""
    existing = set(store.child_names(folder_slug))
    if f"{base_slug}.md" not in existing:
        return base_slug
    n = 2
    while f"{base_slug}-{n}.md" in existing:
        n += 1
    return f"{base_slug}-{n}"


# ── Frontmatter ───────────────────────────────────────────────────────────────


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns ({}, text) if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    yaml_block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    try:
        meta = yaml.safe_load(yaml_block) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    return meta, body


def serialize_doc(
    *,
    title: str,
    folder_slug: str,
    body_md: str,
    author: str,
    updated_at: datetime,
) -> str:
    """Compose a markdown document with YAML frontmatter."""
    fm = {
        "title": title,
        "folder": folder_slug,
        "author": author,
        "updated_at": updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body_md.rstrip()}\n"


# ── File I/O (via the docs store) ─────────────────────────────────────────────


def save_doc(
    store: DocsStore,
    *,
    folder_slug: str,
    doc_slug: str,
    title: str,
    body_md: str,
    author: str,
) -> str:
    """Write (or overwrite) a vault markdown doc. Returns the rel POSIX path.

    The store performs the path-safety check and an atomic write where the
    backend supports it.
    """
    rel = store.safe_rel(f"{folder_slug}/{doc_slug}.md")
    content = serialize_doc(
        title=title,
        folder_slug=folder_slug,
        body_md=body_md,
        author=author,
        updated_at=datetime.now(UTC),
    )
    store.write_bytes(rel, content.encode("utf-8"))
    return rel


def delete_doc(store: DocsStore, rel: str) -> None:
    """Delete a vault doc. Raises ValueError on traversal, FileNotFoundError if missing."""
    store.delete(store.safe_rel(rel))


def file_mtime_ms(store: DocsStore, rel: str) -> int:
    """Return modification time as integer milliseconds - used for stale-edit detection."""
    return store.stat(rel).mtime_ms


# ── Importers ────────────────────────────────────────────────────────────────


def import_docx(file_bytes: bytes) -> tuple[str, str]:
    """Convert a .docx file to markdown. Returns (markdown_text, warnings_str).

    Images are not extracted in v1 - they are silently dropped by mammoth.
    """
    try:
        import mammoth  # type: ignore
    except ImportError as e:
        raise ImportError("mammoth is not installed; run: pip install mammoth") from e
    result = mammoth.convert_to_markdown(io.BytesIO(file_bytes))
    warnings = "; ".join(m.message for m in result.messages) if result.messages else ""
    return result.value, warnings


def import_pdf(file_bytes: bytes) -> str:
    """Extract text from a PDF and return as lightly-structured markdown.

    Quality depends entirely on the PDF's text layer. Scanned/image PDFs
    produce garbage - the import UI shows a preview before committing.
    """
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as e:
        raise ImportError("pypdf is not installed") from e
    reader = PdfReader(io.BytesIO(file_bytes))
    pages: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"*Page {i}*\n\n{text}")
        except Exception:
            continue
    return "\n\n---\n\n".join(pages) if pages else ""

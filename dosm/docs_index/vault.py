"""Documentation vault - authoring, import, and file management.

Vault files live at $DOSM_HOME/docs/<app-slug>/<doc-slug>.md.
Every file has a YAML frontmatter block so metadata survives round-trips
through external editors, git pulls, and file-system copies.

The indexer reads frontmatter to populate application_id on Document rows.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from dosm.config import Config

UNFILED_SLUG = "_unfiled"


# ── Slug helpers ─────────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    """Convert arbitrary text to a URL/filesystem-safe kebab-case slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text) or "doc"


def find_unique_slug(app_dir: Path, base_slug: str) -> str:
    """Return base_slug if no collision, else base_slug-2, base_slug-3, …"""
    if not (app_dir / f"{base_slug}.md").exists():
        return base_slug
    n = 2
    while (app_dir / f"{base_slug}-{n}.md").exists():
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


# ── Path safety ───────────────────────────────────────────────────────────────


def resolve_path(cfg: Config, rel: str) -> Path:
    """Resolve a relative doc path safely under docs_dir. Raises ValueError on traversal."""
    docs_root = cfg.docs_dir.resolve()
    target = (docs_root / rel).resolve()
    if not str(target).startswith(str(docs_root) + os.sep) and target != docs_root:
        raise ValueError(f"path traversal rejected: {rel!r}")
    return target


# ── File I/O ─────────────────────────────────────────────────────────────────


def save_doc(
    cfg: Config,
    *,
    folder_slug: str,
    doc_slug: str,
    title: str,
    body_md: str,
    author: str,
) -> Path:
    """Write (or overwrite) a vault markdown doc atomically. Returns the absolute path."""
    docs_root = cfg.docs_dir.resolve()
    folder_dir = (docs_root / folder_slug).resolve()
    if not str(folder_dir).startswith(str(docs_root) + os.sep):
        raise ValueError(f"invalid folder_slug: {folder_slug!r}")
    folder_dir.mkdir(parents=True, exist_ok=True)
    target = folder_dir / f"{doc_slug}.md"
    content = serialize_doc(
        title=title,
        folder_slug=folder_slug,
        body_md=body_md,
        author=author,
        updated_at=datetime.now(UTC),
    )
    fd, tmp_path = tempfile.mkstemp(dir=folder_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def delete_doc(cfg: Config, rel: str) -> None:
    """Delete a vault doc. Raises ValueError on traversal, FileNotFoundError if missing."""
    target = resolve_path(cfg, rel)
    target.unlink()


def file_mtime_ms(path: Path) -> int:
    """Return modification time as integer milliseconds - used for stale-edit detection."""
    return int(path.stat().st_mtime * 1000)


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

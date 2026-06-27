from __future__ import annotations

import io
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dosm.docs_index.store import DocsStore


class ParseError(RuntimeError):
    pass


def _read_text(store: DocsStore, rel: str) -> str:
    try:
        return store.read_text(rel)
    except Exception as e:  # decode/IO failures surface as a parse error
        raise ParseError(f"Could not read {rel}: {e}") from e


def _strip_markdown(text: str) -> str:
    """Shed the worst of markdown so chunks don't waste tokens on syntax.

    Preserves code blocks verbatim. Collapses heavy headings / link syntax.
    """
    out_lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            out_lines.append(line)
            continue
        if in_code:
            out_lines.append(line)
            continue
        # Turn "# Title" into "Title" (keep it as its own line).
        line = re.sub(r"^#{1,6}\s+", "", line)
        # [text](url) -> text (url)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
        # **bold** / *italic*
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
        line = re.sub(r"(\*|_)(.+?)\1", r"\2", line)
        out_lines.append(line)
    return "\n".join(out_lines)


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter block so it is not indexed as content."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:].lstrip("\n")


def parse_markdown(store: DocsStore, rel: str) -> tuple[str, str | None]:
    raw = _read_text(store, rel)
    body = _strip_frontmatter(raw)
    # First H1 or first non-empty line of body becomes the title.
    title: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
        title = stripped[:120]
        break
    return _strip_markdown(body), title


def parse_txt(store: DocsStore, rel: str) -> tuple[str, str | None]:
    raw = _read_text(store, rel)
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            return raw, stripped[:120]
    return raw, None


def parse_pdf(store: DocsStore, rel: str) -> tuple[str, str | None]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ParseError("pypdf is not installed") from e
    try:
        # Buffer in memory so the reader can seek freely (SMB handles seek too,
        # but a BytesIO sidesteps any backend quirks and matches import_pdf).
        reader = PdfReader(io.BytesIO(store.read_bytes(rel)))
    except Exception as e:
        raise ParseError(f"failed to open PDF: {e}") from e
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    title = None
    try:
        meta = reader.metadata
        if meta and meta.title:
            title = str(meta.title)[:120]
    except Exception:
        pass
    if not title:
        for line in text.splitlines():
            if line.strip():
                title = line.strip()[:120]
                break
    return text, title


def parse(store: DocsStore, rel: str) -> tuple[str, str | None]:
    """Dispatch on suffix. Returns (text, title)."""
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(store, rel)
    if suffix == ".pdf":
        return parse_pdf(store, rel)
    return parse_txt(store, rel)

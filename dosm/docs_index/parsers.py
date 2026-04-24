from __future__ import annotations

import re
from pathlib import Path


class ParseError(RuntimeError):
    pass


def _read_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ParseError(f"Could not decode {path}")


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


def parse_markdown(path: Path) -> tuple[str, str | None]:
    raw = _read_text(path)
    # First H1 or first non-empty line becomes the title.
    title: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
        title = stripped[:120]
        break
    return _strip_markdown(raw), title


def parse_txt(path: Path) -> tuple[str, str | None]:
    raw = _read_text(path)
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            return raw, stripped[:120]
    return raw, None


def parse_pdf(path: Path) -> tuple[str, str | None]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ParseError("pypdf is not installed") from e
    try:
        reader = PdfReader(str(path))
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


def parse(path: Path) -> tuple[str, str | None]:
    """Dispatch on suffix. Returns (text, title)."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    return parse_txt(path)

"""Convert Confluence storage/HTML to markdown for the docs index.

Uses ``markdownify`` (the optional ``dosm[confluence]`` extra) when installed;
falls back to a crude tag-strip so base installs still index *something*.
"""
from __future__ import annotations

import re


def html_to_markdown(html: str) -> str:
    if not html or not html.strip():
        return ""
    try:
        from markdownify import markdownify as _md  # type: ignore
    except ImportError:
        return _strip_tags(html)
    try:
        return _md(html, heading_style="ATX").strip()
    except Exception:
        return _strip_tags(html)


_ENTITIES = (
    ("&nbsp;", " "),
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&#39;", "'"),
)


def _strip_tags(html: str) -> str:
    """Last-resort HTML→text: drop script/style, turn block tags into newlines."""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|table|ul|ol)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    for ent, ch in _ENTITIES:
        text = text.replace(ent, ch)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()

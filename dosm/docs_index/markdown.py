"""Markdown → safe HTML rendering for the docs vault view."""
from __future__ import annotations

import nh3  # type: ignore
from markdown_it import MarkdownIt  # type: ignore

_md = MarkdownIt("commonmark").enable("table")

_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr",
    "ul", "ol", "li",
    "blockquote",
    "pre", "code",
    "table", "thead", "tbody", "tr", "th", "td",
    "em", "strong", "del", "s",
    "a", "img",
    "div", "span",
}

_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "th": {"align"},
    "td": {"align"},
    "code": {"class"},
    "pre": {"class"},
    "div": {"class"},
    "span": {"class"},
}


def render(text: str) -> str:
    """Render markdown to sanitized HTML safe for embedding with | safe."""
    raw = _md.render(text)
    return nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )

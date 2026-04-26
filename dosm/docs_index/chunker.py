from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chunk:
    ord: int
    text: str
    start_char: int
    end_char: int


# Split priorities: paragraph > line > sentence > word > char.
_BREAKPOINTS: list[str] = ["\n\n", "\n", ". ", " "]


def _find_break(text: str, target_end: int, min_end: int) -> int:
    """Find the best break point at or before target_end, not earlier than min_end."""
    for sep in _BREAKPOINTS:
        idx = text.rfind(sep, min_end, target_end)
        if idx != -1:
            return idx + len(sep)
    return target_end


def chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[Chunk]:
    """Sliding-window chunker with soft breakpoints.

    - Windows are `chunk_size` chars wide.
    - Each new window starts `chunk_size - overlap` chars into the previous one.
    - We pull window boundaries to the nearest paragraph/line/sentence/word break
      inside a ±overlap slack so chunks don't slice through code blocks.
    """
    text = text.strip()
    if not text:
        return []
    if overlap >= chunk_size:
        overlap = chunk_size // 5
    step = chunk_size - overlap

    out: list[Chunk] = []
    n = len(text)
    start = 0
    ord_ = 0
    while start < n:
        target_end = min(n, start + chunk_size)
        if target_end < n:
            end = _find_break(text, target_end, max(start + step, target_end - overlap))
        else:
            end = n
        piece = text[start:end].strip()
        if piece:
            out.append(Chunk(ord=ord_, text=piece, start_char=start, end_char=end))
            ord_ += 1
        if end >= n:
            break
        start = max(start + step, end - overlap)
    return out

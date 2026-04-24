from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.docs_index.search import SearchHit, search as search_docs


@dataclass
class Citation:
    n: int  # 1-based reference number used in the prompt
    rel_path: str
    ord: int
    score: float
    snippet: str


def retrieve(
    db: Session, cfg: Config, query: str, *, k: int = 5
) -> list[Citation]:
    """Return up to k citations grounded in the local docs index."""
    hits: list[SearchHit] = search_docs(db, cfg, query, limit=k)
    return [
        Citation(
            n=i + 1,
            rel_path=h.rel_path,
            ord=h.ord,
            score=h.score,
            snippet=h.snippet,
        )
        for i, h in enumerate(hits)
    ]


def compose_system_prompt(user: str | None = None) -> str:
    lines = [
        "You are DOSM, the assistant embedded in a DevOps Operations Suite "
        "Manager for on-prem infrastructure (Service Fabric, Dynatrace "
        "ActiveGates, SAS Linux servers, and other modular integrations).",
        "Ground every answer in the supplied CONTEXT passages. Cite sources "
        "inline as [n] matching the passage number. If the context does not "
        "contain the answer, say so plainly rather than guessing.",
        "Prefer concise, operational responses. Include commands, expected "
        "output, and recovery steps when relevant. Use fenced code blocks "
        "for commands.",
    ]
    if user:
        lines.append(f"The operator speaking with you is {user}.")
    return "\n\n".join(lines)


def compose_context_block(citations: list[Citation]) -> str:
    if not citations:
        return "CONTEXT:\n(no local documentation matched — answer from general knowledge, or ask the user for more detail.)"
    parts = ["CONTEXT:"]
    for c in citations:
        parts.append(f"[{c.n}] source: {c.rel_path} (chunk #{c.ord}, score {c.score:.3f})\n{c.snippet}")
    return "\n\n".join(parts)


def citations_to_json(citations: list[Citation]) -> list[dict]:
    return [asdict(c) for c in citations]

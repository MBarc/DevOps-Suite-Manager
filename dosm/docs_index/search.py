from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.docs_index.embedder import Embedder, NoEmbedder
from dosm.docs_index.indexer import _get_embedder
from dosm.models import DocChunk, Document


@dataclass
class SearchHit:
    doc_id: int
    chunk_id: int
    rel_path: str
    title: str | None
    ord: int
    score: float
    snippet: str
    mode: str  # "vector" | "like"


_HIGHLIGHT_WINDOW = 240


def _snippet(text: str, query: str) -> str:
    """Return ~240 chars of `text` centered on the first query-term hit."""
    t = text
    q = query.strip().lower()
    if not q:
        return t[:_HIGHLIGHT_WINDOW].strip() + ("…" if len(t) > _HIGHLIGHT_WINDOW else "")
    idx = -1
    for term in re.findall(r"\w+", q):
        if len(term) < 2:
            continue
        pos = t.lower().find(term)
        if pos != -1:
            idx = pos
            break
    if idx == -1:
        return t[:_HIGHLIGHT_WINDOW].strip() + ("…" if len(t) > _HIGHLIGHT_WINDOW else "")
    half = _HIGHLIGHT_WINDOW // 2
    start = max(0, idx - half)
    end = min(len(t), idx + half)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(t) else ""
    return prefix + t[start:end].strip() + suffix


def _like_search(db: Session, query: str, limit: int) -> list[SearchHit]:
    q = f"%{query}%"
    stmt = (
        select(DocChunk, Document)
        .join(Document, Document.id == DocChunk.doc_id)
        .where(or_(DocChunk.text.ilike(q), Document.rel_path.ilike(q)))
        .limit(limit)
    )
    hits: list[SearchHit] = []
    for chunk, doc in db.execute(stmt).all():
        hits.append(
            SearchHit(
                doc_id=doc.id,
                chunk_id=chunk.id,
                rel_path=doc.rel_path,
                title=doc.title,
                ord=chunk.ord,
                score=1.0,  # no scoring in LIKE mode
                snippet=_snippet(chunk.text, query),
                mode="like",
            )
        )
    return hits


def _vector_search(
    db: Session, embedder: Embedder, query: str, limit: int
) -> list[SearchHit]:
    qvec = embedder.embed_query(query)  # (dim,)

    stmt = (
        select(DocChunk.id, DocChunk.doc_id, DocChunk.ord, DocChunk.text,
               DocChunk.embedding, Document.rel_path, Document.title)
        .join(Document, Document.id == DocChunk.doc_id)
        .where(DocChunk.embedding.is_not(None))
    )
    rows = db.execute(stmt).all()
    if not rows:
        return []

    # Stack embeddings (they are already L2-normalized by the embedder).
    mat = np.frombuffer(
        b"".join(r.embedding for r in rows), dtype=np.float32
    ).reshape(len(rows), embedder.dim)
    scores = mat @ qvec  # cosine (both sides normalized)
    top_idx = np.argsort(-scores)[:limit]

    hits: list[SearchHit] = []
    for i in top_idx:
        r = rows[int(i)]
        hits.append(
            SearchHit(
                doc_id=r.doc_id,
                chunk_id=r.id,
                rel_path=r.rel_path,
                title=r.title,
                ord=r.ord,
                score=float(scores[int(i)]),
                snippet=_snippet(r.text, query),
                mode="vector",
            )
        )
    return hits


def search(db: Session, cfg: Config, query: str, *, limit: int = 10) -> list[SearchHit]:
    """Vector search if we have an embedder + embedded chunks; else LIKE.

    Never blocks a request on first-time embedder initialization — uses
    block=False so cold requests get LIKE results immediately while a
    background warmer (or CLI `docs reindex`) primes the embedder.
    """
    query = (query or "").strip()
    if not query:
        return []
    embedder = _get_embedder(cfg, block=False)
    if isinstance(embedder, NoEmbedder):
        return _like_search(db, query, limit)
    try:
        hits = _vector_search(db, embedder, query, limit)
        if hits:
            return hits
    except Exception:
        pass
    return _like_search(db, query, limit)

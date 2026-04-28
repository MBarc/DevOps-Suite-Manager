from __future__ import annotations

import fnmatch
import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import delete, select

from dosm.config import Config
from dosm.db import session_scope
from dosm.docs_index.chunker import Chunk, chunk_text
from dosm.docs_index.embedder import Embedder, NoEmbedder, make_embedder
from dosm.docs_index.parsers import ParseError, parse
from dosm.models import DocChunk, Document, Folder


@dataclass
class IndexStats:
    running: bool = False
    total_files: int = 0
    processed: int = 0
    indexed: int = 0
    skipped_unchanged: int = 0
    errors: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    embedder_name: str = "none"
    messages: list[str] = field(default_factory=list)


_status = IndexStats()
_status_lock = threading.Lock()
_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


def get_index_status() -> IndexStats:
    with _status_lock:
        # Return a shallow copy so callers don't race on the live object.
        return IndexStats(
            running=_status.running,
            total_files=_status.total_files,
            processed=_status.processed,
            indexed=_status.indexed,
            skipped_unchanged=_status.skipped_unchanged,
            errors=_status.errors,
            started_at=_status.started_at,
            finished_at=_status.finished_at,
            last_error=_status.last_error,
            embedder_name=_status.embedder_name,
            messages=list(_status.messages[-20:]),
        )


def _update(**kwargs) -> None:
    with _status_lock:
        for k, v in kwargs.items():
            setattr(_status, k, v)


def _log(msg: str) -> None:
    with _status_lock:
        _status.messages.append(f"{time.strftime('%H:%M:%S')} {msg}")


def _get_embedder(cfg: Config, *, block: bool = True) -> Embedder:
    """Return the cached embedder.

    `block=False` callers (e.g. an HTTP request) skip the slow first-time init
    and get a temporary NoEmbedder so the request can fall back to LIKE search
    immediately. The first blocking caller (startup warmer or `dosm docs
    reindex`) primes the cache for everyone.
    """
    global _embedder
    if _embedder is not None:
        return _embedder
    if not block:
        return NoEmbedder()
    with _embedder_lock:
        if _embedder is not None:
            return _embedder
        _embedder = make_embedder(
            cfg.docs_index.embedder,
            cfg.docs_index.embedder_model,
            cfg.docs_index.embedding_dim,
        )
    return _embedder


def warm_embedder_async(cfg: Config) -> None:
    """Trigger embedder initialization in a daemon thread so the cost is paid
    once at startup rather than on the first user request."""
    if _embedder is not None:
        return
    threading.Thread(
        target=_get_embedder, args=(cfg,), kwargs={"block": True}, daemon=True
    ).start()


def _matches_any(rel: str, patterns: list[str]) -> bool:
    for p in patterns:
        if fnmatch.fnmatch(rel, p):
            return True
        # fnmatch's ** doesn't mean "any depth"; allow bare-filename matches
        # by also testing the pattern with a leading '**/' stripped.
        if p.startswith("**/") and fnmatch.fnmatch(rel, p[3:]):
            return True
    return False


def _iter_doc_files(cfg: Config) -> list[Path]:
    root = cfg.docs_dir
    if not root.exists():
        return []
    includes = cfg.docs_index.include_globs
    excludes = cfg.docs_index.exclude_globs
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if not _matches_any(rel, includes):
            continue
        if _matches_any(rel, excludes):
            continue
        found.append(path)
    return sorted(found)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _embedding_to_bytes(vec: np.ndarray) -> bytes:
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


def _index_one(
    cfg: Config, path: Path, *, embedder: Embedder, force: bool
) -> str:
    """Index a single file. Returns one of: 'indexed', 'unchanged', 'error'."""
    rel = path.relative_to(cfg.docs_dir).as_posix()
    stat = path.stat()
    size = stat.st_size
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(tzinfo=None)
    digest = _sha256(path)

    # Read frontmatter metadata for markdown files before opening the DB session,
    # so the session stays short (no file I/O inside the transaction).
    app_id: int | None = None
    fm_title: str | None = None
    if path.suffix.lower() in {".md", ".markdown"}:
        try:
            from dosm.docs_index.vault import parse_frontmatter
            raw_text = path.read_text(encoding="utf-8", errors="replace")
            fm, _ = parse_frontmatter(raw_text)
            fm_title = str(fm["title"])[:255] if fm.get("title") else None
            _fm_app_slug = fm.get("folder")
        except Exception:
            _fm_app_slug = None
    else:
        _fm_app_slug = None

    with session_scope() as s:
        doc = s.execute(
            select(Document).where(Document.rel_path == rel)
        ).scalar_one_or_none()
        if doc is not None and not force and doc.sha256 == digest and doc.status == "indexed":
            return "unchanged"

        # Resolve folder slug → id inside the session.
        if _fm_app_slug:
            folder_row = s.execute(
                select(Folder).where(Folder.slug == _fm_app_slug)
            ).scalar_one_or_none()
            if folder_row is not None:
                app_id = folder_row.id
            else:
                _log(f"unknown folder slug {_fm_app_slug!r} in {rel}")

        try:
            text, title = parse(path)
        except ParseError as e:
            if doc is None:
                doc = Document(rel_path=rel, sha256=digest, size_bytes=size, modified_at=mtime)
                s.add(doc)
            doc.status = "error"
            doc.error = str(e)
            doc.sha256 = digest
            doc.size_bytes = size
            doc.modified_at = mtime
            doc.indexed_at = datetime.now(timezone.utc)
            doc.chunk_count = 0
            doc.folder_id = app_id
            doc.frontmatter_title = fm_title
            s.flush()
            s.execute(delete(DocChunk).where(DocChunk.doc_id == doc.id))
            return "error"

        chunks: list[Chunk] = chunk_text(
            text,
            chunk_size=cfg.docs_index.chunk_size_chars,
            overlap=cfg.docs_index.chunk_overlap_chars,
        )

        # Embed in one batch per doc.
        vectors: list[bytes | None]
        if isinstance(embedder, NoEmbedder) or not chunks:
            vectors = [None] * len(chunks)
        else:
            try:
                arr = embedder.embed([c.text for c in chunks])
                vectors = [_embedding_to_bytes(arr[i]) for i in range(len(chunks))]
            except Exception as e:
                _log(f"embed failed for {rel}: {e}")
                vectors = [None] * len(chunks)

        display_title = title or fm_title
        if doc is None:
            doc = Document(
                rel_path=rel,
                sha256=digest,
                size_bytes=size,
                modified_at=mtime,
                title=display_title,
            )
            s.add(doc)
            s.flush()
        else:
            doc.sha256 = digest
            doc.size_bytes = size
            doc.modified_at = mtime
            doc.title = display_title

        doc.folder_id = app_id
        doc.frontmatter_title = fm_title
        s.execute(delete(DocChunk).where(DocChunk.doc_id == doc.id))
        for c, v in zip(chunks, vectors, strict=True):
            s.add(
                DocChunk(
                    doc_id=doc.id,
                    ord=c.ord,
                    text=c.text,
                    start_char=c.start_char,
                    end_char=c.end_char,
                    embedding=v,
                )
            )
        doc.chunk_count = len(chunks)
        doc.status = "indexed"
        doc.error = None
        doc.indexed_at = datetime.now(timezone.utc)
    return "indexed"


def _remove_deleted(cfg: Config, on_disk_rel_paths: set[str]) -> int:
    with session_scope() as s:
        all_paths = set(s.execute(select(Document.rel_path)).scalars().all())
        stale = all_paths - on_disk_rel_paths
        if not stale:
            return 0
        docs = s.execute(select(Document).where(Document.rel_path.in_(stale))).scalars().all()
        for d in docs:
            s.execute(delete(DocChunk).where(DocChunk.doc_id == d.id))
            s.delete(d)
        return len(stale)


def reindex(cfg: Config, *, force: bool = False) -> IndexStats:
    """Scan docs/, parse+chunk+embed new/changed files, prune deletions."""
    with _status_lock:
        if _status.running:
            return get_index_status()
        _status.running = True
        _status.started_at = datetime.now(timezone.utc)
        _status.finished_at = None
        _status.total_files = 0
        _status.processed = 0
        _status.indexed = 0
        _status.skipped_unchanged = 0
        _status.errors = 0
        _status.last_error = None
        _status.messages.clear()

    try:
        embedder = _get_embedder(cfg)
        _update(embedder_name=embedder.name)
        files = _iter_doc_files(cfg)
        _update(total_files=len(files))
        _log(f"scanning {len(files)} docs (embedder={embedder.name}, force={force})")

        on_disk: set[str] = set()
        for path in files:
            rel = path.relative_to(cfg.docs_dir).as_posix()
            on_disk.add(rel)
            try:
                outcome = _index_one(cfg, path, embedder=embedder, force=force)
            except Exception as e:
                outcome = "error"
                _log(f"error {rel}: {e}")
                with _status_lock:
                    _status.last_error = f"{rel}: {e}"

            with _status_lock:
                _status.processed += 1
                if outcome == "indexed":
                    _status.indexed += 1
                elif outcome == "unchanged":
                    _status.skipped_unchanged += 1
                else:
                    _status.errors += 1

        removed = _remove_deleted(cfg, on_disk)
        if removed:
            _log(f"pruned {removed} deleted files")
    finally:
        _update(running=False, finished_at=datetime.now(timezone.utc))
    return get_index_status()


def reindex_async(cfg: Config, *, force: bool = False) -> None:
    """Kick off `reindex` in a daemon thread. Silently no-ops if already running."""
    with _status_lock:
        if _status.running:
            return
    t = threading.Thread(target=reindex, args=(cfg,), kwargs={"force": force}, daemon=True)
    t.start()

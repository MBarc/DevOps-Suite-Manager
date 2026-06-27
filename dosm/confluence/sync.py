"""Reconcile one listener's Confluence space into the docs store.

Pages become markdown docs (so the AI learns the text); attachments are written
raw. Change-detection skips unchanged versions; mirror-delete removes anything
that disappeared from Confluence. A single ``reindex_async`` is triggered after
writes so the docs index / agent pick up the changes right away.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.confluence import make_confluence_client
from dosm.confluence.html import html_to_markdown
from dosm.docs_index.indexer import reindex_async
from dosm.docs_index.store import make_docs_store
from dosm.docs_index.vault import serialize_doc, slugify
from dosm.models import (
    DEFAULT_TENANT_SLUG,
    ConfluenceListener,
    ConfluenceSyncItem,
    Folder,
    Tenant,
)

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    pages_written: int = 0
    attachments_written: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.pages_written + self.attachments_written + self.deleted


def _default_tenant_id(db: Session) -> int:
    return db.execute(
        select(Tenant.id).where(Tenant.slug == DEFAULT_TENANT_SLUG)
    ).scalar_one()


def _ensure_folder(db: Session, tenant_id: int, slug: str, name: str) -> None:
    """Ensure a Folder row exists so the space's docs group under one label."""
    existing = db.execute(
        select(Folder).where(Folder.tenant_id == tenant_id, Folder.slug == slug)
    ).scalar_one_or_none()
    if existing is None:
        db.add(Folder(tenant_id=tenant_id, name=name, slug=slug))
        db.flush()


def _upsert_item(
    db: Session,
    listener_id: int,
    kind: str,
    confluence_id: str,
    rel: str,
    version: str,
    title: str,
    item: ConfluenceSyncItem | None,
) -> None:
    now = datetime.now(UTC)
    if item is None:
        db.add(
            ConfluenceSyncItem(
                listener_id=listener_id,
                kind=kind,
                confluence_id=confluence_id,
                rel_path=rel,
                version=version,
                title=title,
                last_seen_at=now,
            )
        )
    else:
        item.rel_path = rel
        item.version = version
        item.title = title
        item.last_seen_at = now


async def sync_listener(
    cfg: Config, listener: ConfluenceListener, db: Session
) -> SyncResult:
    """Reconcile ``listener``'s space into the docs store. Mirror-deletes removed
    items. The caller owns ``db`` (and its commit)."""
    result = SyncResult()
    client = make_confluence_client(cfg, listener)
    store = make_docs_store(cfg)
    ns = listener.slug

    # Group docs under a Folder in the Default tenant (where background-indexed
    # docs land - see indexer._default_tenant_id).
    default_tid = _default_tenant_id(db)
    _ensure_folder(db, default_tid, ns, f"Confluence: {listener.name}")

    existing = {
        (it.kind, it.confluence_id): it
        for it in db.execute(
            select(ConfluenceSyncItem).where(
                ConfluenceSyncItem.listener_id == listener.id
            )
        ).scalars()
    }
    seen: set[tuple[str, str]] = set()

    pages = await client.list_pages()
    for page in pages:
        if listener.sync_pages:
            seen.add(("page", page.id))
            item = existing.get(("page", page.id))
            rel = store.safe_rel(f"confluence/{ns}/{slugify(page.title)}-{page.id}.md")
            if item is not None and item.version == page.version and item.rel_path == rel:
                result.unchanged += 1
                item.last_seen_at = datetime.now(UTC)
            else:
                try:
                    html = await client.get_page_html(page.id)
                    content = serialize_doc(
                        title=page.title or page.id,
                        folder_slug=ns,
                        body_md=html_to_markdown(html),
                        author=f"confluence:{listener.space_key}",
                        updated_at=datetime.now(UTC),
                    )
                    # A retitled page changes its slug -> drop the old file.
                    if item is not None and item.rel_path != rel:
                        _safe_delete(store, item.rel_path, result)
                    store.write_bytes(rel, content.encode("utf-8"))
                    _upsert_item(db, listener.id, "page", page.id, rel, page.version, page.title, item)
                    result.pages_written += 1
                except Exception as e:  # one bad page must not sink the run
                    result.errors.append(f"page {page.id}: {e}")

        if listener.sync_attachments:
            try:
                attachments = await client.list_attachments(page.id)
            except Exception as e:
                result.errors.append(f"attachments of {page.id}: {e}")
                attachments = []
            for att in attachments:
                seen.add(("attachment", att.id))
                a_item = existing.get(("attachment", att.id))
                a_rel = store.safe_rel(
                    f"confluence/{ns}/attachments/{att.id}-{att.filename}"
                )
                if a_item is not None and a_item.version == att.version and a_item.rel_path == a_rel:
                    result.unchanged += 1
                    a_item.last_seen_at = datetime.now(UTC)
                    continue
                try:
                    data = await client.download(att)
                    if a_item is not None and a_item.rel_path != a_rel:
                        _safe_delete(store, a_item.rel_path, result)
                    store.write_bytes(a_rel, data)
                    _upsert_item(
                        db, listener.id, "attachment", att.id, a_rel, att.version, att.filename, a_item
                    )
                    result.attachments_written += 1
                except Exception as e:
                    result.errors.append(f"attachment {att.id}: {e}")

    # Mirror delete: anything we tracked but didn't see this run.
    for key, item in existing.items():
        if key in seen:
            continue
        _safe_delete(store, item.rel_path, result)
        db.delete(item)
        result.deleted += 1

    listener.last_synced_at = datetime.now(UTC)
    listener.last_status = "error" if result.errors else "ok"
    listener.last_error = "; ".join(result.errors[:5]) if result.errors else None
    db.flush()

    if result.changed:
        reindex_async(cfg, force=False)
    return result


def _safe_delete(store, rel: str, result: SyncResult) -> None:
    try:
        store.delete(rel)
    except FileNotFoundError:
        pass
    except Exception as e:
        result.errors.append(f"delete {rel}: {e}")

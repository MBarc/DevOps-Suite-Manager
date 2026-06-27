from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from dosm.confluence.client import (
    AttachMeta,
    CloudConfluenceClient,
    PageMeta,
    ServerConfluenceClient,
)
from dosm.confluence.html import _strip_tags, html_to_markdown
from dosm.confluence.sync import sync_listener
from dosm.models import ConfluenceListener


# ── Client auth shaping (no network) ───────────────────────────────────────


def test_cloud_client_uses_basic_auth():
    c = CloudConfluenceClient("https://x.atlassian.net/wiki", "OPS", "me@x.com", "tok")
    assert c._client_kwargs() == {"auth": ("me@x.com", "tok")}
    assert c._api("/content") == "https://x.atlassian.net/wiki/rest/api/content"


def test_server_client_uses_bearer():
    c = ServerConfluenceClient("https://confluence.corp/", "OPS", "pat-123")
    assert c._client_kwargs() == {"headers": {"Authorization": "Bearer pat-123"}}
    # trailing slash on base_url is normalized
    assert c._api("/space/OPS") == "https://confluence.corp/rest/api/space/OPS"


# ── HTML → markdown ─────────────────────────────────────────────────────────


def test_strip_tags_fallback():
    out = _strip_tags("<h1>Title</h1><p>body &amp; more</p>")
    assert "Title" in out
    assert "body & more" in out
    assert "<" not in out


def test_html_to_markdown_nonempty():
    assert "Hello" in html_to_markdown("<p>Hello world</p>")
    assert html_to_markdown("") == ""


# ── Sync reconcile (fake client + store) ────────────────────────────────────


class FakeStore:
    """In-memory DocsStore stand-in capturing writes/deletes."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def safe_rel(self, rel: str) -> str:
        return rel

    def write_bytes(self, rel: str, data: bytes) -> None:
        self.files[rel] = data

    def delete(self, rel: str) -> None:
        if rel not in self.files:
            raise FileNotFoundError(rel)
        del self.files[rel]


class FakeClient:
    """Controllable ConfluenceClient: ``pages`` maps page-id -> dict."""

    def __init__(self, pages: dict):
        # pages = {pid: {"title", "version", "html", "attachments": [AttachMeta...]}}
        self.pages = pages

    async def list_pages(self):
        return [
            PageMeta(id=pid, title=p["title"], version=p["version"])
            for pid, p in self.pages.items()
        ]

    async def get_page_html(self, page_id: str) -> str:
        return self.pages[page_id].get("html", "<p>body</p>")

    async def list_attachments(self, page_id: str):
        return list(self.pages[page_id].get("attachments", []))

    async def download(self, att: AttachMeta) -> bytes:
        return f"data:{att.id}:{att.version}".encode()


@pytest.fixture
def listener(db, default_tenant):
    li = ConfluenceListener(
        tenant_id=default_tenant["id"],
        name="Ops",
        deployment="cloud",
        base_url="https://x.atlassian.net/wiki",
        space_key="OPS",
        slug="ops",
        credential_id=None,
        sync_pages=True,
        sync_attachments=True,
        enabled=True,
    )
    db.add(li)
    db.flush()
    return li


def _patch(monkeypatch, client, store):
    monkeypatch.setattr("dosm.confluence.sync.make_confluence_client", lambda cfg, l: client)
    monkeypatch.setattr("dosm.confluence.sync.make_docs_store", lambda cfg: store)
    monkeypatch.setattr("dosm.confluence.sync.reindex_async", lambda cfg, force=False: None)


def test_sync_create_update_mirror_delete(monkeypatch, test_config, db, listener):
    store = FakeStore()
    att1 = AttachMeta(id="a1", filename="spec.pdf", version="1", download_url="http://d/a1", media_type="application/pdf")
    client = FakeClient({
        "p1": {"title": "Runbook", "version": "1", "html": "<p>v1</p>", "attachments": [att1]},
    })
    _patch(monkeypatch, client, store)

    # Round 1: fresh sync writes the page + attachment.
    r1 = asyncio.run(sync_listener(test_config, listener, db))
    assert (r1.pages_written, r1.attachments_written, r1.deleted) == (1, 1, 0)
    page_rel = "confluence/ops/runbook-p1.md"
    att_rel = "confluence/ops/attachments/a1-spec.pdf"
    assert page_rel in store.files
    assert att_rel in store.files
    assert listener.last_status == "ok"

    # Round 2: nothing changed -> no rewrites, both counted unchanged.
    r2 = asyncio.run(sync_listener(test_config, listener, db))
    assert (r2.pages_written, r2.attachments_written, r2.deleted) == (0, 0, 0)
    assert r2.unchanged == 2

    # Round 3: page version bumps -> page rewritten, attachment unchanged.
    client.pages["p1"]["version"] = "2"
    client.pages["p1"]["html"] = "<p>v2</p>"
    r3 = asyncio.run(sync_listener(test_config, listener, db))
    assert r3.pages_written == 1
    assert b"v2" in store.files[page_rel]

    # Round 4: page removed from Confluence -> mirror-delete page + attachment.
    client.pages.clear()
    r4 = asyncio.run(sync_listener(test_config, listener, db))
    assert r4.deleted == 2
    assert store.files == {}


def test_sync_pages_only(monkeypatch, test_config, db, listener):
    listener.sync_attachments = False
    db.flush()
    store = FakeStore()
    att = AttachMeta(id="a9", filename="x.pdf", version="1", download_url="http://d/a9", media_type=None)
    client = FakeClient({"p1": {"title": "Doc", "version": "1", "html": "<p>x</p>", "attachments": [att]}})
    _patch(monkeypatch, client, store)

    r = asyncio.run(sync_listener(test_config, listener, db))
    assert r.pages_written == 1
    assert r.attachments_written == 0
    assert all("attachments" not in rel for rel in store.files)


# ── Web routes ───────────────────────────────────────────────────────────────


def test_settings_page_renders(auth_client):
    resp = auth_client.get("/settings/confluence")
    assert resp.status_code == 200
    assert "Confluence listeners" in resp.text


def test_create_listener_via_web(auth_client, db, default_tenant):
    from dosm.models import Credential

    cred = Credential(
        tenant_id=default_tenant["id"], name="cf-cred", kind="login",
        username="me@x.com", secret_ref="t/default/credentials/cf-cred",
    )
    db.add(cred)
    db.commit()
    resp = auth_client.post(
        "/settings/confluence/new",
        data={
            "name": "Ops space", "deployment": "cloud",
            "base_url": "https://x.atlassian.net/wiki", "space_key": "OPS",
            "credential_id": str(cred.id), "sync_pages": "1", "sync_attachments": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = db.execute(
        select(ConfluenceListener).where(ConfluenceListener.space_key == "OPS")
    ).scalar_one()
    assert row.slug == "ops-space"
    assert row.deployment == "cloud"


def test_create_rejects_bad_deployment(auth_client, db, default_tenant):
    from dosm.models import Credential

    cred = Credential(
        tenant_id=default_tenant["id"], name="cf2", kind="login",
        username="me@x", secret_ref="x",
    )
    db.add(cred)
    db.commit()
    resp = auth_client.post(
        "/settings/confluence/new",
        data={
            "name": "Bad", "deployment": "bogus",
            "base_url": "https://x", "space_key": "B", "credential_id": str(cred.id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400

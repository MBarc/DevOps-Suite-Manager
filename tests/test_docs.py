"""Docs vault and folder route tests."""
import io

from sqlalchemy import select

from dosm.models import AuditLog, Document, Folder


def _create_folder(client, name="Runbooks"):
    return client.post("/docs/folders", data={"name": name}, follow_redirects=False)


def _get_folder(session_factory, name: str) -> Folder | None:
    with session_factory() as s:
        return s.execute(select(Folder).where(Folder.name == name)).scalar_one_or_none()


def _save_doc(client, *, title="Test Doc", body="# Hello\nContent here.", app_slug="_unfiled"):
    return client.post(
        "/docs/save",
        data={"title": title, "body": body, "app_slug": app_slug, "path": "", "original_mtime": ""},
        follow_redirects=False,
    )


# ── Page rendering ────────────────────────────────────────────────────────────


def test_docs_home_returns_200(auth_client):
    resp = auth_client.get("/docs")
    assert resp.status_code == 200


def test_docs_home_requires_auth(anon_client):
    resp = anon_client.get("/docs", follow_redirects=False)
    assert resp.status_code == 303


def test_docs_new_form_returns_200(auth_client):
    resp = auth_client.get("/docs/new")
    assert resp.status_code == 200


def test_docs_search_empty_query_returns_200(auth_client):
    resp = auth_client.get("/docs/search")
    assert resp.status_code == 200


def test_docs_search_with_query_returns_200(auth_client):
    resp = auth_client.get("/docs/search?q=runbook")
    assert resp.status_code == 200


# ── Folders ───────────────────────────────────────────────────────────────────


def test_create_folder_redirects(auth_client):
    resp = _create_folder(auth_client)
    assert resp.status_code == 303


def test_create_folder_persists_in_db(auth_client, session_factory):
    _create_folder(auth_client, name="Service Fabric")
    folder = _get_folder(session_factory, "Service Fabric")
    assert folder is not None
    assert folder.slug == "service-fabric"


def test_create_folder_creates_audit_log(auth_client, session_factory):
    _create_folder(auth_client, name="Dynatrace")
    with session_factory() as s:
        entry = s.execute(
            select(AuditLog).where(AuditLog.action == "folder.create")
        ).scalar_one_or_none()
    assert entry is not None


def test_create_duplicate_folder_does_not_crash(auth_client):
    _create_folder(auth_client, name="DupFolder")
    resp = _create_folder(auth_client, name="DupFolder")
    assert resp.status_code < 500


def test_folder_detail_returns_200(auth_client, session_factory):
    _create_folder(auth_client, name="Monitoring")
    folder = _get_folder(session_factory, "Monitoring")
    assert folder is not None
    resp = auth_client.get(f"/docs/folders/{folder.slug}")
    assert resp.status_code == 200
    assert "Monitoring" in resp.text


def test_folder_detail_404_for_missing(auth_client):
    resp = auth_client.get("/docs/folders/does-not-exist")
    assert resp.status_code == 404


def test_delete_folder_redirects(auth_client, session_factory):
    _create_folder(auth_client, name="ToDelete")
    folder = _get_folder(session_factory, "ToDelete")
    resp = auth_client.post(f"/docs/folders/{folder.slug}/delete", follow_redirects=False)
    assert resp.status_code == 303


def test_delete_folder_removes_from_db(auth_client, session_factory):
    _create_folder(auth_client, name="GoneFolder")
    folder = _get_folder(session_factory, "GoneFolder")
    slug = folder.slug
    auth_client.post(f"/docs/folders/{slug}/delete", follow_redirects=False)
    with session_factory() as s:
        gone = s.execute(select(Folder).where(Folder.slug == slug)).scalar_one_or_none()
    assert gone is None


# ── Docs ─────────────────────────────────────────────────────────────────────


def test_save_new_doc_redirects(auth_client):
    resp = _save_doc(auth_client)
    assert resp.status_code == 303


def test_save_new_doc_creates_file(auth_client, test_config):
    _save_doc(auth_client, title="My Runbook", body="# My Runbook\nContent.")
    docs_dir = test_config.docs_dir
    # Should land in the _unfiled folder
    unfiled_dir = docs_dir / "_unfiled"
    md_files = list(unfiled_dir.glob("*.md")) if unfiled_dir.exists() else []
    assert len(md_files) >= 1


def test_save_doc_creates_audit_log(auth_client, session_factory):
    _save_doc(auth_client, title="Audit Test Doc")
    with session_factory() as s:
        entry = s.execute(
            select(AuditLog).where(AuditLog.action == "docs.create")
        ).scalar_one_or_none()
    assert entry is not None


def test_docs_preview_returns_html(auth_client):
    resp = auth_client.post("/docs/preview", data={"body": "# Hello\nWorld"})
    assert resp.status_code == 200
    data = resp.json()
    assert "html" in data
    assert "<h1" in data["html"]


def test_docs_convert_markdown(auth_client):
    md_content = b"# Imported\n\nThis is imported content."
    resp = auth_client.post(
        "/docs/convert",
        files={"file": ("test.md", io.BytesIO(md_content), "text/markdown")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "body_md" in data
    assert "Imported" in data["body_md"]


def test_docs_convert_unsupported_type_returns_400(auth_client):
    resp = auth_client.post(
        "/docs/convert",
        files={"file": ("test.exe", io.BytesIO(b"binary"), "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_delete_doc_redirects(auth_client, test_config):
    # Create a doc first, then delete it.
    resp = _save_doc(auth_client, title="Delete Me")
    assert resp.status_code == 303
    # The redirect URL contains the doc path.
    location = resp.headers["location"]
    # location is e.g. /docs/view?path=_unfiled/delete-me.md
    path = location.split("path=")[-1]

    del_resp = auth_client.post("/docs/delete", data={"path": path}, follow_redirects=False)
    assert del_resp.status_code == 303

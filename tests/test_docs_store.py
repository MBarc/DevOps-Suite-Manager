"""Tests for the docs storage abstraction (local backend, factory, migration).

The SMB backend needs a live share and is not exercised here; the shared
DocsStore contract is covered via LocalDocsStore + an in-memory fake.
"""
from __future__ import annotations

import hashlib
import io
import os

import pytest

from dosm.config import Config, DocsIndexConfig
from dosm.docs_index.parsers import parse
from dosm.docs_index.store import (
    DocsStore,
    FileStat,
    LocalDocsStore,
    MigrationResult,
    last_store_error,
    make_docs_store,
    migrate_docs,
    store_fell_back,
)

# ── LocalDocsStore contract ───────────────────────────────────────────────────


def test_local_store_round_trip(tmp_path):
    store = LocalDocsStore(tmp_path)
    assert store.exists()

    store.write_bytes("a/b/doc.md", b"# Title\nhello")
    assert store.is_file("a/b/doc.md")
    assert store.read_bytes("a/b/doc.md") == b"# Title\nhello"
    assert store.read_text("a/b/doc.md") == "# Title\nhello"

    st = store.stat("a/b/doc.md")
    assert isinstance(st, FileStat)
    assert st.size == len(b"# Title\nhello")
    assert st.mtime_ms == int((tmp_path / "a/b/doc.md").stat().st_mtime * 1000)

    assert store.sha256("a/b/doc.md") == hashlib.sha256(b"# Title\nhello").hexdigest()

    files = list(store.iter_files())
    assert files == ["a/b/doc.md"]

    with store.open_binary("a/b/doc.md") as fh:
        assert fh.read() == b"# Title\nhello"

    store.delete("a/b/doc.md")
    assert not store.is_file("a/b/doc.md")
    with pytest.raises(FileNotFoundError):
        store.delete("a/b/doc.md")


def test_local_store_child_names(tmp_path):
    store = LocalDocsStore(tmp_path)
    store.write_bytes("notes/one.md", b"x")
    store.write_bytes("notes/two.md", b"y")
    names = set(store.child_names("notes"))
    assert names == {"one.md", "two.md"}
    assert store.child_names("does-not-exist") == []


def test_local_store_write_is_atomic_overwrite(tmp_path):
    store = LocalDocsStore(tmp_path)
    store.write_bytes("d.md", b"first")
    store.write_bytes("d.md", b"second")
    assert store.read_bytes("d.md") == b"second"
    # No stray temp files left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["d.md"]


@pytest.mark.parametrize("bad", ["../escape.md", "a/../../b.md", "/etc/passwd"])
def test_local_safe_rel_rejects_traversal(tmp_path, bad):
    store = LocalDocsStore(tmp_path)
    with pytest.raises(ValueError):
        store.safe_rel(bad)


def test_local_safe_rel_normalizes(tmp_path):
    store = LocalDocsStore(tmp_path)
    assert store.safe_rel("a\\b\\c.md") == "a/b/c.md"
    assert store.safe_rel("./a/./b.md") == "a/b.md"


# ── Factory ───────────────────────────────────────────────────────────────────


def test_factory_local(tmp_path):
    cfg = Config(home=tmp_path)
    store = make_docs_store(cfg)
    assert isinstance(store, LocalDocsStore)
    assert not store_fell_back(cfg, store)


def test_factory_smb_unresolvable_falls_back(tmp_path):
    # source=smb but no server/share/credential -> never raises, falls back local.
    cfg = Config(home=tmp_path, docs_index=DocsIndexConfig(source="smb"))
    store = make_docs_store(cfg)
    assert isinstance(store, LocalDocsStore)
    assert store_fell_back(cfg, store)
    assert last_store_error() is not None


# ── parsers via a store ───────────────────────────────────────────────────────


def test_parse_markdown_from_store(tmp_path):
    store = LocalDocsStore(tmp_path)
    store.write_bytes("doc.md", b"---\ntitle: X\n---\n# Heading\n\nBody text.")
    text, title = parse(store, "doc.md")
    assert title == "Heading"
    assert "Body text." in text


def test_parse_txt_from_store(tmp_path):
    store = LocalDocsStore(tmp_path)
    store.write_bytes("notes.txt", b"First line\nSecond line")
    text, title = parse(store, "notes.txt")
    assert title == "First line"
    assert text == "First line\nSecond line"


# ── Migration ─────────────────────────────────────────────────────────────────


class FakeDocsStore(DocsStore):
    """In-memory store for migration tests."""

    label = "fake"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def exists(self) -> bool:
        return True

    def iter_files(self):
        yield from sorted(self.files)

    def is_file(self, rel):
        return self.safe_rel(rel) in self.files

    def stat(self, rel):
        data = self.files[self.safe_rel(rel)]
        return FileStat(size=len(data), mtime_ms=0)

    def child_names(self, rel_dir):
        prefix = (rel_dir.strip("/") + "/") if rel_dir.strip("/") else ""
        out = set()
        for rel in self.files:
            if rel.startswith(prefix):
                out.add(rel[len(prefix):].split("/", 1)[0])
        return list(out)

    def read_bytes(self, rel):
        return self.files[self.safe_rel(rel)]

    def open_binary(self, rel):
        return io.BytesIO(self.read_bytes(rel))

    def sha256(self, rel):
        return hashlib.sha256(self.read_bytes(rel)).hexdigest()

    def write_bytes(self, rel, data):
        self.files[self.safe_rel(rel)] = data

    def delete(self, rel):
        rel = self.safe_rel(rel)
        if rel not in self.files:
            raise FileNotFoundError(rel)
        del self.files[rel]


def _seed_local(tmp_path):
    src = LocalDocsStore(tmp_path)
    src.write_bytes("a.md", b"alpha")
    src.write_bytes("sub/b.md", b"bravo")
    src.write_bytes("sub/c.pdf", b"%PDF-1.4 fake")
    return src


def test_migrate_copies_everything(tmp_path):
    src = _seed_local(tmp_path)
    dst = FakeDocsStore()
    result = migrate_docs(src, dst)
    assert isinstance(result, MigrationResult)
    assert set(result.copied) == {"a.md", "sub/b.md", "sub/c.pdf"}
    assert not result.skipped and not result.errors
    assert dst.files["a.md"] == b"alpha"
    assert dst.files["sub/c.pdf"] == b"%PDF-1.4 fake"


def test_migrate_dry_run_writes_nothing(tmp_path):
    src = _seed_local(tmp_path)
    dst = FakeDocsStore()
    result = migrate_docs(src, dst, dry_run=True)
    assert len(result.copied) == 3
    assert dst.files == {}


def test_migrate_is_idempotent(tmp_path):
    src = _seed_local(tmp_path)
    dst = FakeDocsStore()
    migrate_docs(src, dst)
    # Change source content, re-run without overwrite -> all skipped, dst unchanged.
    src.write_bytes("a.md", b"ALPHA-2")
    result = migrate_docs(src, dst)
    assert set(result.skipped) == {"a.md", "sub/b.md", "sub/c.pdf"}
    assert not result.copied
    assert dst.files["a.md"] == b"alpha"


def test_migrate_overwrite_recopies(tmp_path):
    src = _seed_local(tmp_path)
    dst = FakeDocsStore()
    migrate_docs(src, dst)
    src.write_bytes("a.md", b"ALPHA-2")
    result = migrate_docs(src, dst, overwrite=True)
    assert "a.md" in result.copied
    assert dst.files["a.md"] == b"ALPHA-2"


def test_migrate_collects_errors(tmp_path):
    src = _seed_local(tmp_path)

    class FailingDst(FakeDocsStore):
        def write_bytes(self, rel, data):
            if rel == "sub/b.md":
                raise OSError("disk full")
            super().write_bytes(rel, data)

    dst = FailingDst()
    result = migrate_docs(src, dst)
    assert len(result.copied) == 2
    assert len(result.errors) == 1
    assert result.errors[0][0] == "sub/b.md"
    assert os.path.sep not in result.errors[0][0]  # rel stays posix

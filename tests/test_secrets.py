"""LocalEncryptedBackend: encrypt/decrypt, CRUD, prefix listing."""
import pytest

from dosm.secrets.base import SecretNotFound
from dosm.secrets.local import LocalEncryptedBackend


@pytest.fixture
def backend(tmp_path, session_factory):
    """Fresh backend with its own key file and the shared test session factory."""
    key_file = tmp_path / "secrets.key"
    return LocalEncryptedBackend(key_file=key_file, session_factory=session_factory)


# ── Encrypt / decrypt roundtrip ───────────────────────────────────────────────


def test_set_and_get_returns_same_value(backend):
    backend.set("test/key1", b"super-secret")
    assert backend.get("test/key1") == b"super-secret"


def test_set_and_get_unicode_value(backend):
    value = "p@ssw0rd!€".encode()
    backend.set("test/unicode", value)
    assert backend.get("test/unicode") == value


def test_overwrite_returns_new_value(backend):
    backend.set("test/overwrite", b"original")
    backend.set("test/overwrite", b"updated")
    assert backend.get("test/overwrite") == b"updated"


# ── Missing key ───────────────────────────────────────────────────────────────


def test_get_missing_key_raises(backend):
    with pytest.raises(SecretNotFound):
        backend.get("does/not/exist")


# ── Delete ────────────────────────────────────────────────────────────────────


def test_delete_then_get_raises(backend):
    backend.set("test/todelete", b"value")
    backend.delete("test/todelete")
    with pytest.raises(SecretNotFound):
        backend.get("test/todelete")


def test_delete_missing_key_raises(backend):
    with pytest.raises(SecretNotFound):
        backend.delete("test/nonexistent")


# ── List ──────────────────────────────────────────────────────────────────────


def test_list_with_prefix(backend):
    backend.set("myapp/alpha", b"a")
    backend.set("myapp/beta", b"b")
    backend.set("other/gamma", b"c")
    keys = backend.list("myapp/")
    assert "myapp/alpha" in keys
    assert "myapp/beta" in keys
    assert "other/gamma" not in keys


def test_list_all(backend):
    backend.set("list/one", b"1")
    backend.set("list/two", b"2")
    keys = backend.list()
    assert "list/one" in keys
    assert "list/two" in keys


def test_list_empty_prefix_returns_all(backend):
    backend.set("x/key", b"val")
    keys = backend.list("")
    assert "x/key" in keys

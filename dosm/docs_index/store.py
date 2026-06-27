"""Docs source storage backends.

The docs index historically read every file from one local directory
(``$DOSM_HOME/docs``). This module abstracts that source behind a small
``DocsStore`` interface so the same indexer / vault / routes can read and write
docs either from the local filesystem (``LocalDocsStore``) or from an SMB
network share (``SmbDocsStore``), selected by ``cfg.docs_index.source``.

Design notes:
- All paths handed to a store are **relative POSIX strings** (matching
  ``Document.rel_path``). Stores translate them to their own native path shape.
- Network failures surface as a single neutral ``DocsStoreError`` so callers
  never have to know about ``smbprotocol`` internals.
- ``make_docs_store`` NEVER raises: if an SMB source can't be built it logs,
  records the reason, and falls back to ``LocalDocsStore`` so an unreachable
  share can't crash startup or the indexer thread. Mirrors ``make_embedder``.
- ``smbprotocol`` is an optional dependency (``pip install dosm[smb]``), imported
  lazily inside ``SmbDocsStore`` only.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from dosm.config import Config
    from dosm.models import Credential

log = logging.getLogger(__name__)

_READ_BLOCK = 65536


class DocsStoreError(RuntimeError):
    """Any failure reaching or operating on a docs source (esp. SMB)."""


class DocsStoreMissingDependency(DocsStoreError):
    """The backend's optional dependency (smbprotocol) is not installed."""


@dataclass(frozen=True)
class FileStat:
    size: int
    mtime_ms: int  # integer ms since epoch - stable across local/SMB, drives editor conflict detection


def _normalize_rel(rel: str) -> str:
    """Normalize a relative doc path to clean POSIX form, rejecting traversal.

    Pure string normalization (no filesystem access), so it is valid for both
    local and remote stores. ``LocalDocsStore`` layers a resolve()+containment
    check on top to also defeat symlink escapes.
    """
    r = (rel or "").replace("\\", "/").strip()
    if r.startswith("/"):
        raise ValueError(f"absolute path rejected: {rel!r}")
    parts: list[str] = []
    for part in PurePosixPath(r).parts:
        if part == "..":
            raise ValueError(f"path traversal rejected: {rel!r}")
        if part in (".", ""):
            continue
        parts.append(part)
    if not parts:
        raise ValueError(f"empty doc path: {rel!r}")
    return "/".join(parts)


class DocsStore(ABC):
    """Read/write interface over the docs source tree.

    All ``rel`` arguments are relative POSIX paths under the source root.
    """

    label: str = "docs"

    # -- discovery / metadata ------------------------------------------------
    @abstractmethod
    def exists(self) -> bool:
        """True if the source root is reachable."""

    @abstractmethod
    def iter_files(self) -> Iterator[str]:
        """Yield every file under the root, recursively, as rel POSIX paths."""

    @abstractmethod
    def is_file(self, rel: str) -> bool: ...

    @abstractmethod
    def stat(self, rel: str) -> FileStat: ...

    @abstractmethod
    def child_names(self, rel_dir: str) -> list[str]:
        """Names of the immediate children of ``rel_dir`` (for slug collision checks)."""

    # -- reads ---------------------------------------------------------------
    @abstractmethod
    def read_bytes(self, rel: str) -> bytes: ...

    @abstractmethod
    def open_binary(self, rel: str) -> BinaryIO:
        """Open a binary, seekable file-like object (e.g. for ``PdfReader``)."""

    @abstractmethod
    def sha256(self, rel: str) -> str:
        """Streamed content hash."""

    # -- writes --------------------------------------------------------------
    @abstractmethod
    def write_bytes(self, rel: str, data: bytes) -> None:
        """Write ``data`` to ``rel`` atomically where possible, creating parents."""

    @abstractmethod
    def delete(self, rel: str) -> None:
        """Delete ``rel``. Raises ``FileNotFoundError`` if it does not exist."""

    # -- concrete helpers ----------------------------------------------------
    def safe_rel(self, rel: str) -> str:
        """Validate + normalize ``rel``; raise ``ValueError`` on traversal."""
        return _normalize_rel(rel)

    def read_text(self, rel: str) -> str:
        """Read ``rel`` as text, trying common encodings."""
        data = self.read_bytes(rel)
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        raise DocsStoreError(f"could not decode {rel!r}")


# ── Local filesystem ─────────────────────────────────────────────────────────


class LocalDocsStore(DocsStore):
    """The original behavior: docs live under a local directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.label = f"local ({self.root})"

    def safe_rel(self, rel: str) -> str:
        norm = _normalize_rel(rel)
        root = self.root.resolve()
        target = (root / norm).resolve()
        if target != root and not str(target).startswith(str(root) + os.sep):
            raise ValueError(f"path traversal rejected: {rel!r}")
        return norm

    def _abs(self, rel: str) -> Path:
        return self.root / self.safe_rel(rel)

    def exists(self) -> bool:
        return self.root.exists()

    def iter_files(self) -> Iterator[str]:
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if path.is_file():
                yield path.relative_to(self.root).as_posix()

    def is_file(self, rel: str) -> bool:
        try:
            return self._abs(rel).is_file()
        except ValueError:
            return False

    def stat(self, rel: str) -> FileStat:
        st = self._abs(rel).stat()
        return FileStat(size=st.st_size, mtime_ms=int(st.st_mtime * 1000))

    def child_names(self, rel_dir: str) -> list[str]:
        d = self.root / _normalize_rel(rel_dir) if rel_dir.strip("/") else self.root
        if not d.is_dir():
            return []
        return [p.name for p in d.iterdir()]

    def read_bytes(self, rel: str) -> bytes:
        return self._abs(rel).read_bytes()

    def open_binary(self, rel: str) -> BinaryIO:
        return self._abs(rel).open("rb")

    def sha256(self, rel: str) -> str:
        h = hashlib.sha256()
        with self._abs(rel).open("rb") as fh:
            for block in iter(lambda: fh.read(_READ_BLOCK), b""):
                h.update(block)
        return h.hexdigest()

    def write_bytes(self, rel: str, data: bytes) -> None:
        target = self._abs(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, rel: str) -> None:
        self._abs(rel).unlink()  # raises FileNotFoundError if missing


# ── SMB network share ────────────────────────────────────────────────────────


class SmbDocsStore(DocsStore):
    """Docs sourced from an SMB2/3 network share via ``smbprotocol``.

    Talks SMB directly from the app (no OS mount), so it works from inside the
    Linux container. Connection + auth are registered lazily and reused via
    smbclient's global connection pool.
    """

    def __init__(
        self,
        *,
        server: str,
        share: str,
        base_path: str = "",
        username: str,
        domain: str = "",
        password: str,
        port: int = 445,
        encrypt: bool = True,
    ) -> None:
        self.server = server
        self.share = share
        self.base_path = base_path.strip("/\\")
        self.username = username
        self.domain = domain
        self.password = password
        self.port = port
        self.encrypt = encrypt
        root = f"\\\\{server}\\{share}"
        if self.base_path:
            root += "\\" + self.base_path.replace("/", "\\")
        self._root_unc = root
        self.label = f"smb (\\\\{server}\\{share}\\{self.base_path})"
        self._registered = False
        self._lock = threading.Lock()
        self._smbclient = None

    def _client(self):
        """Lazily import smbclient and register the session once."""
        if self._registered:
            return self._smbclient
        with self._lock:
            if self._registered:
                return self._smbclient
            try:
                import smbclient  # type: ignore
                import smbclient.path  # noqa: F401  (registers smbclient.path)
            except ImportError as e:  # pragma: no cover - exercised only without the extra
                raise DocsStoreMissingDependency(
                    "smbprotocol is not installed; run: pip install 'dosm[smb]'"
                ) from e
            try:
                user = f"{self.domain}\\{self.username}" if self.domain else self.username
                smbclient.register_session(
                    self.server,
                    username=user,
                    password=self.password,
                    port=self.port,
                    encrypt=self.encrypt,
                )
            except Exception as e:
                raise DocsStoreError(f"SMB connection to {self.server!r} failed: {e}") from e
            self._smbclient = smbclient
            self._registered = True
            return smbclient

    def _unc(self, rel: str = "") -> str:
        rel_norm = _normalize_rel(rel) if rel else ""
        if rel_norm:
            return self._root_unc + "\\" + rel_norm.replace("/", "\\")
        return self._root_unc

    @staticmethod
    def _wrap(exc: Exception, what: str) -> DocsStoreError:
        return DocsStoreError(f"SMB {what} failed: {exc}")

    def exists(self) -> bool:
        try:
            smbclient = self._client()
            return smbclient.path.exists(self._root_unc)
        except DocsStoreError:
            raise
        except Exception:
            return False

    def _walk(self, rel_dir: str) -> Iterator[str]:
        smbclient = self._client()
        try:
            entries = list(smbclient.scandir(self._unc(rel_dir)))
        except FileNotFoundError:
            return
        except Exception as e:
            raise self._wrap(e, f"listing {rel_dir or '/'}") from e
        for entry in entries:
            child = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_dir():
                    yield from self._walk(child)
                elif entry.is_file():
                    yield child
            except Exception as e:  # entry.is_dir/is_file can stat under the hood
                raise self._wrap(e, f"stat {child}") from e

    def iter_files(self) -> Iterator[str]:
        yield from self._walk("")

    def is_file(self, rel: str) -> bool:
        try:
            smbclient = self._client()
            return smbclient.path.isfile(self._unc(self.safe_rel(rel)))
        except ValueError:
            return False
        except DocsStoreError:
            raise
        except Exception:
            return False

    def stat(self, rel: str) -> FileStat:
        smbclient = self._client()
        try:
            st = smbclient.stat(self._unc(self.safe_rel(rel)))
        except Exception as e:
            raise self._wrap(e, f"stat {rel}") from e
        return FileStat(size=st.st_size, mtime_ms=int(st.st_mtime * 1000))

    def child_names(self, rel_dir: str) -> list[str]:
        smbclient = self._client()
        try:
            return [e.name for e in smbclient.scandir(self._unc(rel_dir))]
        except FileNotFoundError:
            return []
        except Exception as e:
            raise self._wrap(e, f"listing {rel_dir or '/'}") from e

    def open_binary(self, rel: str) -> BinaryIO:
        smbclient = self._client()
        try:
            return smbclient.open_file(self._unc(self.safe_rel(rel)), mode="rb")
        except Exception as e:
            raise self._wrap(e, f"open {rel}") from e

    def read_bytes(self, rel: str) -> bytes:
        with self.open_binary(rel) as fh:
            return fh.read()

    def sha256(self, rel: str) -> str:
        h = hashlib.sha256()
        with self.open_binary(rel) as fh:
            for block in iter(lambda: fh.read(_READ_BLOCK), b""):
                h.update(block)
        return h.hexdigest()

    def write_bytes(self, rel: str, data: bytes) -> None:
        smbclient = self._client()
        rel = self.safe_rel(rel)
        unc = self._unc(rel)
        import ntpath

        parent = ntpath.dirname(unc)
        try:
            smbclient.makedirs(parent, exist_ok=True)
            tmp = f"{unc}.{os.getpid()}.tmp"
            with smbclient.open_file(tmp, mode="wb") as fh:
                fh.write(data)
            # Atomic replace where supported; fall back to delete+rename.
            try:
                smbclient.replace(tmp, unc)
            except Exception:
                try:
                    smbclient.remove(unc)
                except Exception:
                    pass
                smbclient.rename(tmp, unc)
        except Exception as e:
            raise self._wrap(e, f"write {rel}") from e

    def delete(self, rel: str) -> None:
        smbclient = self._client()
        try:
            smbclient.remove(self._unc(self.safe_rel(rel)))
        except FileNotFoundError:
            raise
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise FileNotFoundError(rel) from e
            raise self._wrap(e, f"delete {rel}") from e
        except Exception as e:
            raise self._wrap(e, f"delete {rel}") from e


# ── Credential resolution + factory ──────────────────────────────────────────


def resolve_login_credential(cfg: Config, cred: Credential) -> tuple[str, str, str]:
    """Resolve a ``login`` credential profile to (username, domain, password).

    The password lives in the secrets backend under ``cred.secret_ref``; the
    username/domain are columns on the row. Mirrors the cert-source pattern of
    authenticating an integration via a stored credential profile.
    """
    from dosm.secrets import get_backend

    password = get_backend(cfg).get_str(cred.secret_ref)
    return (cred.username or ""), (cred.domain or ""), password


# Process-wide cache of a successfully built SMB store (the session registration
# is expensive). Keyed by the connection tuple; a config change needs a restart
# anyway (get_config is lru_cached), so we never invalidate mid-process.
_store_cache: DocsStore | None = None
_store_cache_key: tuple | None = None
_store_lock = threading.Lock()
_last_error: str | None = None


def last_store_error() -> str | None:
    """The reason the most recent SMB store build failed (None if healthy)."""
    return _last_error


def make_docs_store(cfg: Config) -> DocsStore:
    """Build the docs store for ``cfg``. Never raises - falls back to local.

    ``source != "smb"`` always yields a fresh ``LocalDocsStore``. For SMB, a
    build failure (missing credential, unreachable share, missing dependency)
    is logged + recorded in ``last_store_error()`` and a ``LocalDocsStore`` is
    returned, so the indexer/routes degrade gracefully instead of crashing.
    """
    global _store_cache, _store_cache_key, _last_error

    if cfg.docs_index.source != "smb":
        return LocalDocsStore(cfg.docs_dir)

    smb = cfg.docs_index.smb
    key = (smb.server, smb.share, smb.base_path, smb.port, smb.encrypt, smb.credential_id)
    with _store_lock:
        if _store_cache is not None and _store_cache_key == key:
            return _store_cache
        try:
            store = build_smb_store(cfg, smb)
            _store_cache = store
            _store_cache_key = key
            _last_error = None
            return store
        except Exception as e:
            _last_error = str(e)
            log.warning("docs SMB source unavailable, falling back to local: %s", e)
            return LocalDocsStore(cfg.docs_dir)


def build_smb_store(cfg: Config, smb) -> SmbDocsStore:
    """Build an ``SmbDocsStore`` from an ``SmbDocsConfig`` (or compatible object),
    resolving its login credential. Raises ``DocsStoreError`` on bad config."""
    from dosm.db import session_scope
    from dosm.models import Credential

    if not smb.server or not smb.share:
        raise DocsStoreError("SMB source requires server and share")
    if smb.credential_id is None:
        raise DocsStoreError("SMB source requires a login credential")
    with session_scope() as s:
        cred = s.get(Credential, smb.credential_id)
        if cred is None:
            raise DocsStoreError(f"credential id {smb.credential_id} not found")
        if cred.kind != "login":
            raise DocsStoreError(f"credential {cred.name!r} is not a login credential")
        username, domain, password = resolve_login_credential(cfg, cred)
    return SmbDocsStore(
        server=smb.server,
        share=smb.share,
        base_path=smb.base_path,
        username=username,
        domain=domain,
        password=password,
        port=smb.port,
        encrypt=smb.encrypt,
    )


def store_fell_back(cfg: Config, store: DocsStore) -> bool:
    """True if ``store`` is a local fallback while SMB was requested."""
    return cfg.docs_index.source == "smb" and isinstance(store, LocalDocsStore)


# ── Migration + probe helpers ─────────────────────────────────────────────────


@dataclass
class MigrationResult:
    copied: list[str]
    skipped: list[str]
    errors: list[tuple[str, str]]


def migrate_docs(
    src: DocsStore, dst: DocsStore, *, dry_run: bool = False, overwrite: bool = False
) -> MigrationResult:
    """Copy every file from ``src`` to ``dst``. Idempotent.

    Existing files at the destination are skipped unless ``overwrite`` is set.
    Per-file failures are collected (the run continues) rather than aborting.
    Takes explicit stores so it is unit-testable with in-memory fakes.
    """
    copied: list[str] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []
    for rel in src.iter_files():
        try:
            if dry_run:
                copied.append(rel)
                continue
            if not overwrite and dst.is_file(rel):
                skipped.append(rel)
                continue
            dst.write_bytes(rel, src.read_bytes(rel))
            copied.append(rel)
        except Exception as e:  # noqa: BLE001 - collect and keep going
            errors.append((rel, str(e)))
    return MigrationResult(copied=copied, skipped=skipped, errors=errors)


def probe_store(store: DocsStore) -> tuple[bool, str, list[str]]:
    """Reachability check for an already-built store.

    Returns ``(ok, message, sample_files)``: ``message`` is the source label on
    success, or the failure reason otherwise.
    """
    try:
        if not store.exists():
            return False, f"{store.label}: not reachable", []
        sample: list[str] = []
        for i, rel in enumerate(store.iter_files()):
            sample.append(rel)
            if i >= 4:
                break
    except Exception as e:  # noqa: BLE001
        return False, f"{store.label}: {e}", []
    return True, store.label, sample


def probe_source(cfg: Config) -> tuple[bool, str, list[str]]:
    """Reachability check for the configured docs source. Shared by
    ``dosm docs test-source`` and the Settings "Test connection" button."""
    store = make_docs_store(cfg)
    if store_fell_back(cfg, store):
        return False, last_store_error() or "SMB source unavailable", []
    return probe_store(store)

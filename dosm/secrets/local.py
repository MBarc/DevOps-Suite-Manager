from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dosm.models import SecretBlob
from dosm.secrets.base import SecretNotFound, SecretsBackend


class LocalEncryptedBackend(SecretsBackend):
    """Fernet-encrypted secrets stored as blobs in the app's SQLite DB.

    The symmetric key lives in a file under $DOSM_HOME (``config/secrets.key``
    by default). The key file is auto-created on first use with 0600 perms on
    POSIX. Loss of the key file means all existing secrets are unrecoverable.
    """

    name = "local"

    def __init__(self, key_file: Path, session_factory):
        self._key_file = key_file
        self._session_factory = session_factory
        self._fernet = self._load_or_create_key(key_file)

    @staticmethod
    def _load_or_create_key(key_file: Path) -> Fernet:
        if key_file.exists():
            return Fernet(key_file.read_bytes().strip())
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        try:
            key_file.chmod(0o600)
        except OSError:
            # Windows does not honor POSIX modes; skip silently.
            pass
        return Fernet(key)

    def _session(self) -> Session:
        return self._session_factory()

    def get(self, path: str) -> bytes:
        with self._session() as s:
            row = s.get(SecretBlob, path)
            if row is None:
                raise SecretNotFound(path)
            try:
                return self._fernet.decrypt(row.value)
            except InvalidToken as e:
                raise RuntimeError(
                    f"Secret at {path!r} could not be decrypted with the current key. "
                    "The key file may have been rotated or replaced."
                ) from e

    def set(self, path: str, value: bytes) -> None:
        token = self._fernet.encrypt(value)
        with self._session() as s:
            row = s.get(SecretBlob, path)
            if row is None:
                s.add(SecretBlob(path=path, value=token))
            else:
                row.value = token
            s.commit()

    def delete(self, path: str) -> None:
        with self._session() as s:
            result = s.execute(delete(SecretBlob).where(SecretBlob.path == path))
            s.commit()
            if result.rowcount == 0:
                raise SecretNotFound(path)

    def list(self, prefix: str = "") -> list[str]:
        with self._session() as s:
            stmt = select(SecretBlob.path).order_by(SecretBlob.path)
            if prefix:
                stmt = stmt.where(SecretBlob.path.startswith(prefix))
            return [p for (p,) in s.execute(stmt).all()]

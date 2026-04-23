from __future__ import annotations

from abc import ABC, abstractmethod


class SecretNotFound(KeyError):
    """Raised when a secret path does not exist in the backend."""


class SecretsBackend(ABC):
    """Stable interface implemented by LocalEncryptedBackend and VaultBackend.

    Secret paths are slash-delimited strings, e.g. ``ssh/prod/admin`` or
    ``api/dynatrace/token``. Values are opaque bytes.
    """

    name: str = "unknown"

    @abstractmethod
    def get(self, path: str) -> bytes: ...

    @abstractmethod
    def set(self, path: str, value: bytes) -> None: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]: ...

    def get_str(self, path: str) -> str:
        return self.get(path).decode("utf-8")

    def set_str(self, path: str, value: str) -> None:
        self.set(path, value.encode("utf-8"))

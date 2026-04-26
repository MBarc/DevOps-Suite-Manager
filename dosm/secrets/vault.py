from __future__ import annotations

import os

import hvac

from dosm.secrets.base import SecretNotFound, SecretsBackend


class VaultBackend(SecretsBackend):
    """HashiCorp Vault KV v2 backed secrets store.

    Config keys read from ``secrets:`` in ``config.yaml``:
      - ``vault_addr``      : e.g. ``https://vault.internal:8200``
      - ``vault_token_env`` : env var holding the Vault token (default VAULT_TOKEN)
      - ``vault_mount``     : KV v2 mount point (default ``secret``)
      - ``vault_prefix``    : path prefix inside the mount (default ``dosm``)

    Secret values are stored under a single ``value`` field in each KV entry
    to keep the interface opaque-bytes-at-a-path.
    """

    name = "vault"

    def __init__(
        self,
        addr: str,
        token_env: str = "VAULT_TOKEN",
        mount: str = "secret",
        prefix: str = "dosm",
    ):
        token = os.environ.get(token_env)
        if not token:
            raise RuntimeError(
                f"Vault backend selected but ${token_env} is not set. "
                "Export a token before starting DOSM."
            )
        self._client = hvac.Client(url=addr, token=token)
        if not self._client.is_authenticated():
            raise RuntimeError("Vault token is not authenticated.")
        self._mount = mount
        self._prefix = prefix.strip("/")

    def _full_path(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self._prefix}/{path}" if self._prefix else path

    def get(self, path: str) -> bytes:
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=self._full_path(path), mount_point=self._mount, raise_on_deleted_version=True
            )
        except hvac.exceptions.InvalidPath as e:
            raise SecretNotFound(path) from e
        data = resp["data"]["data"]
        if "value" not in data:
            raise RuntimeError(f"Vault secret at {path!r} is missing a 'value' field.")
        value = data["value"]
        return value.encode("utf-8") if isinstance(value, str) else bytes(value)

    def set(self, path: str, value: bytes) -> None:
        self._client.secrets.kv.v2.create_or_update_secret(
            path=self._full_path(path),
            secret={"value": value.decode("utf-8")},
            mount_point=self._mount,
        )

    def delete(self, path: str) -> None:
        self._client.secrets.kv.v2.delete_metadata_and_all_versions(
            path=self._full_path(path), mount_point=self._mount
        )

    def list(self, prefix: str = "") -> list[str]:
        full = self._full_path(prefix).rstrip("/")
        try:
            resp = self._client.secrets.kv.v2.list_secrets(
                path=full, mount_point=self._mount
            )
        except hvac.exceptions.InvalidPath:
            return []
        keys: list[str] = resp.get("data", {}).get("keys", [])
        # Strip trailing slashes (Vault uses them to indicate sub-paths).
        base = prefix.rstrip("/") + "/" if prefix else ""
        return sorted(base + k.rstrip("/") for k in keys)

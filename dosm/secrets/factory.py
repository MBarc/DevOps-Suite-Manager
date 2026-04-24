from __future__ import annotations

from dosm.config import Config, get_config
from dosm.db import init_engine
from dosm.secrets.base import SecretsBackend
from dosm.secrets.local import LocalEncryptedBackend
from dosm.secrets.vault import VaultBackend

# Module-level cache. Keyed by ($DOSM_HOME, backend kind) so a different
# DOSM_HOME (tests, multi-instance) gets its own backend.
_backends: dict[tuple[str, str], SecretsBackend] = {}


def get_backend(cfg: Config | None = None) -> SecretsBackend:
    cfg = cfg or get_config()
    key = (str(cfg.home), cfg.secrets.backend.lower())
    cached = _backends.get(key)
    if cached is not None:
        return cached
    kind = cfg.secrets.backend.lower()
    if kind == "local":
        init_engine(cfg)
        from sqlalchemy.orm import sessionmaker

        from dosm.db import get_engine

        Session = sessionmaker(bind=get_engine(), future=True)
        key_file = cfg.home / cfg.secrets.local_key_file
        backend: SecretsBackend = LocalEncryptedBackend(key_file=key_file, session_factory=Session)
    elif kind == "vault":
        if not cfg.secrets.vault_addr:
            raise RuntimeError("secrets.backend=vault requires secrets.vault_addr in config.yaml")
        backend = VaultBackend(
            addr=cfg.secrets.vault_addr,
            token_env=cfg.secrets.vault_token_env,
            mount=cfg.secrets.vault_mount,
            prefix=cfg.secrets.vault_prefix,
        )
    else:
        raise ValueError(f"Unknown secrets backend: {cfg.secrets.backend!r}")
    _backends[key] = backend
    return backend

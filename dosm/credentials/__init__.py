"""Credential profile UI: named profiles tied to secrets in the backend.

Each Credential row is a user-facing variable like 'Service Fabric Japan
Model'. The actual secret material lives in the configured secrets backend
(local Fernet-encrypted blob or Vault) and is referenced by `secret_ref`.
"""
from dosm.credentials.routes import router as credentials_router

__all__ = ["credentials_router"]

"""Confluence space listeners: pull pages/attachments into the docs index.

A ``ConfluenceListener`` row (per-tenant) subscribes to one Confluence space; a
background poller (:mod:`dosm.confluence.poller`) reconciles each enabled
listener into the docs store and triggers a reindex so the AI learns the content.
"""
from __future__ import annotations

from dosm.config import Config
from dosm.confluence.client import (
    CloudConfluenceClient,
    ConfluenceClient,
    ConfluenceError,
    ServerConfluenceClient,
)

# deployment value -> human label (drives the Settings dropdown)
DEPLOYMENTS = {
    "cloud": "Confluence Cloud",
    "server": "Server / Data Center",
}


def resolve_confluence_credential(cfg: Config, cred) -> tuple[str, str]:
    """Resolve a credential to ``(username/email, token)``.

    The token (API token / PAT) lives in the secrets backend under
    ``cred.secret_ref``; the username/email is a column on the row. Mirrors
    ``docs_index.store.resolve_login_credential``.
    """
    from dosm.secrets import get_backend

    token = get_backend(cfg).get_str(cred.secret_ref)
    return (cred.username or ""), token


def make_confluence_client(cfg: Config, listener) -> ConfluenceClient:
    """Build the async client for ``listener``, resolving its credential.

    Raises ``ConfluenceError`` on missing/invalid config (mirrors
    ``build_smb_store``).
    """
    from dosm.db import session_scope
    from dosm.models import Credential

    if not listener.base_url or not listener.space_key:
        raise ConfluenceError("listener requires a base URL and space key")
    if listener.credential_id is None:
        raise ConfluenceError("listener requires a credential")
    with session_scope() as s:
        cred = s.get(Credential, listener.credential_id)
        if cred is None:
            raise ConfluenceError(f"credential id {listener.credential_id} not found")
        username, token = resolve_confluence_credential(cfg, cred)
    if not token:
        raise ConfluenceError(f"credential {cred.name!r} has no stored token")

    if listener.deployment == "cloud":
        if not username:
            raise ConfluenceError(
                "Confluence Cloud needs an email in the credential username"
            )
        return CloudConfluenceClient(
            listener.base_url, listener.space_key, username, token
        )
    if listener.deployment == "server":
        return ServerConfluenceClient(listener.base_url, listener.space_key, token)
    raise ConfluenceError(f"unknown deployment {listener.deployment!r}")

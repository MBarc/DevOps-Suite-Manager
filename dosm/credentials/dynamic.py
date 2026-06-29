"""Dynamic (per-user / PIM) credentials.

A credential of kind ``dynamic`` is a *placeholder* on a host - it has no shared
secret. Each user stores their OWN username+password for it, kept in the secrets
backend at a per-user path. At connect / file-transfer time the *connecting* user
is identified by a request-scoped ContextVar (set at the entry routes, mirroring
the agent's tenant var), and the resolver chokepoints fetch that user's material.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from dosm.config import Config
from dosm.models import Credential
from dosm.secrets import SecretNotFound, get_backend

DYNAMIC_KIND = "dynamic"


class DynamicCredentialError(RuntimeError):
    """A dynamic credential could not be resolved for the connecting user."""


_UNSET: object = object()
_CONNECTING_USER: ContextVar[object] = ContextVar("dosm_connecting_user", default=_UNSET)


def connecting_user_id() -> int | None:
    """Id of the user initiating the current connection / transfer, or None when
    unset (background / agent context)."""
    val = _CONNECTING_USER.get()
    return None if val is _UNSET else val  # type: ignore[return-value]


@contextmanager
def connecting_user(uid: int | None) -> Iterator[None]:
    """Scope a connect / file-transfer request to the connecting user."""
    token = _CONNECTING_USER.set(uid)
    try:
        yield
    finally:
        _CONNECTING_USER.reset(token)


def set_connecting_user(uid: int | None) -> None:
    """Pin the connecting user for the rest of the current context (no reset) -
    for long-lived single-task flows such as a terminal websocket session. Each
    runs in its own task with an isolated context, so the value doesn't leak."""
    _CONNECTING_USER.set(uid)


def is_dynamic(cred: Credential | None) -> bool:
    return cred is not None and cred.kind == DYNAMIC_KIND


def user_path(cred: Credential, uid: int) -> str:
    """Secrets-backend path holding ``uid``'s material for dynamic ``cred``."""
    return f"{cred.secret_ref}/u/{uid}"


def set_user_material(cfg: Config, cred: Credential, uid: int, username: str, password: str) -> None:
    get_backend(cfg).set_str(
        user_path(cred, uid), json.dumps({"username": username, "password": password})
    )


def clear_user_material(cfg: Config, cred: Credential, uid: int) -> None:
    try:
        get_backend(cfg).delete(user_path(cred, uid))
    except SecretNotFound:
        pass


def get_user_material(cfg: Config, cred: Credential, uid: int | None) -> tuple[str, str] | None:
    """(username, password) for ``uid``, or None if they haven't stored it."""
    if uid is None:
        return None
    try:
        raw = get_backend(cfg).get_str(user_path(cred, uid))
    except SecretNotFound:
        return None
    try:
        data = json.loads(raw)
        return (data.get("username") or "", data.get("password") or "")
    except (ValueError, TypeError):
        return None


def has_user_material(cfg: Config, cred: Credential, uid: int | None) -> bool:
    return get_user_material(cfg, cred, uid) is not None


def resolve_dynamic(cfg: Config, cred: Credential) -> tuple[str, str]:
    """(username, password) for the *connecting* user. Raises DynamicCredentialError
    when no connecting user is set or that user hasn't stored their credentials."""
    uid = connecting_user_id()
    if uid is None:
        raise DynamicCredentialError(
            f"{cred.name!r} is a per-user (PIM) credential and can't be used "
            f"without a signed-in user."
        )
    material = get_user_material(cfg, cred, uid)
    if material is None:
        raise DynamicCredentialError(
            f"You haven't set up your credentials for {cred.name!r}. Open "
            f"My Credentials to add your username and password."
        )
    return material

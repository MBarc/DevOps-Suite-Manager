from __future__ import annotations

import secrets
from pathlib import Path

from starlette.middleware.sessions import SessionMiddleware

from dosm.config import Config


def _load_or_create_secret(path: Path) -> str:
    if path.exists():
        data = path.read_text().strip()
        if data:
            return data
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    path.write_text(token)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def install_session_middleware(app, cfg: Config) -> None:
    secret_file = cfg.home / cfg.auth.session_secret_file
    secret = _load_or_create_secret(secret_file)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=cfg.auth.session_cookie,
        max_age=cfg.auth.session_max_age_seconds,
        same_site="lax",
        https_only=False,
    )

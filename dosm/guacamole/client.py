from __future__ import annotations

import httpx


class GuacamoleClientError(RuntimeError):
    pass


class GuacamoleUnreachable(GuacamoleClientError):
    pass


async def fetch_session_token(base_url: str, encoded_data: str, *, timeout: float = 10.0) -> str:
    """Exchange a signed auth-json blob for a Guacamole session token.

    Returns the `authToken` string. Raises GuacamoleUnreachable if the
    Guacamole webapp can't be contacted, GuacamoleClientError for any other
    failure (bad credentials, malformed blob, missing extension, ...).
    """
    url = base_url.rstrip("/") + "/api/tokens"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, data={"data": encoded_data})
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        raise GuacamoleUnreachable(f"cannot reach Guacamole at {base_url}: {e}") from e
    if r.status_code != 200:
        raise GuacamoleClientError(
            f"Guacamole rejected auth-json blob: {r.status_code} {r.text[:200]}"
        )
    body = r.json()
    token = body.get("authToken")
    if not token:
        raise GuacamoleClientError(f"Guacamole response missing authToken: {body}")
    return token

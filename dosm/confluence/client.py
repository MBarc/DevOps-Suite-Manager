"""Async REST client for one Confluence space (v1 content API).

Confluence Cloud and Server/Data Center expose the same ``/rest/api`` content
surface; the subclasses differ only in how they authenticate (Cloud = HTTP Basic
``email:api_token``; Server/DC = ``Authorization: Bearer <PAT>``). The base URL
carries the right context path already (Cloud: ``https://x.atlassian.net/wiki``;
Server: ``https://confluence.corp``).

Network paths are unvalidated against a live instance (same caveat as the SMB /
cloud-cert adapters) until smoke-tested.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
PAGE_LIMIT = 50


class ConfluenceError(RuntimeError):
    """Any Confluence API failure (auth, not-found, bad status)."""


class ConfluenceUnreachable(ConfluenceError):
    """The Confluence host could not be reached (connect/timeout)."""


@dataclass(frozen=True)
class PageMeta:
    id: str
    title: str
    version: str


@dataclass(frozen=True)
class AttachMeta:
    id: str
    filename: str
    version: str
    download_url: str  # absolute
    media_type: str | None


class ConfluenceClient(ABC):
    """Async client bound to one space. Subclasses supply only auth."""

    def __init__(self, base_url: str, space_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.space_key = space_key

    # -- deployment-specific auth ------------------------------------------------
    @abstractmethod
    def _client_kwargs(self) -> dict:
        """httpx.AsyncClient kwargs carrying auth (``auth=`` or ``headers=``)."""

    def _api(self, path: str) -> str:
        return f"{self.base_url}/rest/api{path}"

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, params: dict | None = None
    ) -> dict:
        try:
            resp = await client.get(url, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise ConfluenceUnreachable(f"Confluence unreachable: {e}") from e
        if resp.status_code in (401, 403):
            raise ConfluenceError(
                f"Confluence rejected the credential ({resp.status_code})"
            )
        if resp.status_code == 404:
            raise ConfluenceError(f"Not found: {url}")
        if resp.status_code >= 400:
            raise ConfluenceError(
                f"Confluence error {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    async def test_connection(self) -> tuple[bool, str]:
        """Verify auth + that the configured space is reachable."""
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, **self._client_kwargs()) as c:
                data = await self._get_json(c, self._api(f"/space/{self.space_key}"))
            name = data.get("name") or self.space_key
            return True, f"Reached space {name!r} ({self.space_key})"
        except ConfluenceError as e:
            return False, str(e)
        except Exception as e:  # pragma: no cover - defensive
            return False, f"Unexpected error: {e}"

    async def list_pages(self) -> list[PageMeta]:
        """All current pages in the space, with version numbers (paginated)."""
        pages: list[PageMeta] = []
        start = 0
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, **self._client_kwargs()) as c:
            while True:
                data = await self._get_json(
                    c,
                    self._api("/content"),
                    params={
                        "spaceKey": self.space_key,
                        "type": "page",
                        "status": "current",
                        "expand": "version",
                        "limit": PAGE_LIMIT,
                        "start": start,
                    },
                )
                results = data.get("results", [])
                for r in results:
                    pages.append(
                        PageMeta(
                            id=str(r["id"]),
                            title=r.get("title", ""),
                            version=str((r.get("version") or {}).get("number", "")),
                        )
                    )
                if len(results) < PAGE_LIMIT:
                    break
                start += PAGE_LIMIT
        return pages

    async def get_page_html(self, page_id: str) -> str:
        """The page body in Confluence storage (XHTML) format."""
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, **self._client_kwargs()) as c:
            data = await self._get_json(
                c, self._api(f"/content/{page_id}"), params={"expand": "body.storage"}
            )
        return (((data.get("body") or {}).get("storage") or {}).get("value")) or ""

    async def list_attachments(self, page_id: str) -> list[AttachMeta]:
        atts: list[AttachMeta] = []
        start = 0
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, **self._client_kwargs()) as c:
            while True:
                data = await self._get_json(
                    c,
                    self._api(f"/content/{page_id}/child/attachment"),
                    params={"expand": "version", "limit": PAGE_LIMIT, "start": start},
                )
                results = data.get("results", [])
                link_base = (data.get("_links") or {}).get("base") or self.base_url
                for r in results:
                    download = ((r.get("_links") or {}).get("download")) or ""
                    atts.append(
                        AttachMeta(
                            id=str(r["id"]),
                            filename=r.get("title", str(r["id"])),
                            version=str((r.get("version") or {}).get("number", "")),
                            download_url=f"{link_base}{download}" if download else "",
                            media_type=((r.get("metadata") or {}).get("mediaType")),
                        )
                    )
                if len(results) < PAGE_LIMIT:
                    break
                start += PAGE_LIMIT
        return atts

    async def download(self, att: AttachMeta) -> bytes:
        if not att.download_url:
            raise ConfluenceError(f"attachment {att.filename!r} has no download link")
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, follow_redirects=True, **self._client_kwargs()
        ) as c:
            try:
                resp = await c.get(att.download_url)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                raise ConfluenceUnreachable(f"Confluence unreachable: {e}") from e
        if resp.status_code >= 400:
            raise ConfluenceError(
                f"download failed ({resp.status_code}) for {att.filename!r}"
            )
        return resp.content


class CloudConfluenceClient(ConfluenceClient):
    """Confluence Cloud - HTTP Basic with email + API token."""

    def __init__(self, base_url: str, space_key: str, email: str, api_token: str) -> None:
        super().__init__(base_url, space_key)
        self._email = email
        self._token = api_token

    def _client_kwargs(self) -> dict:
        return {"auth": (self._email, self._token)}


class ServerConfluenceClient(ConfluenceClient):
    """Confluence Server / Data Center - Bearer personal access token."""

    def __init__(self, base_url: str, space_key: str, token: str) -> None:
        super().__init__(base_url, space_key)
        self._token = token

    def _client_kwargs(self) -> dict:
        return {"headers": {"Authorization": f"Bearer {self._token}"}}

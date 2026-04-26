from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


class OllamaError(RuntimeError):
    pass


class OllamaUnreachable(OllamaError):
    pass


@dataclass
class ChatDelta:
    """One streamed token/chunk from Ollama's /api/chat endpoint."""

    content: str
    done: bool
    raw: dict


class OllamaClient:
    """Minimal async client for Ollama's chat + embeddings API."""

    def __init__(self, base_url: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout

    async def ping(self) -> dict:
        """Returns /api/tags. Raises OllamaUnreachable if we can't reach the server."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                return r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise OllamaUnreachable(f"cannot reach Ollama at {self.base_url}: {e}") from e
        except httpx.HTTPStatusError as e:
            raise OllamaError(f"Ollama {e.response.status_code}: {e.response.text[:200]}") from e

    async def has_model(self, name: str | None = None) -> bool:
        try:
            tags = await self.ping()
        except OllamaError:
            return False
        target = name or self.model
        for m in tags.get("models", []):
            if m.get("name") == target or m.get("name", "").split(":")[0] == target.split(":")[0]:
                return True
        return False

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.2,
        num_ctx: int | None = None,
    ) -> AsyncIterator[ChatDelta]:
        """Stream a chat completion as ChatDelta objects.

        `messages` is a list of {"role": ..., "content": ...} entries.
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if num_ctx is not None:
            payload["options"]["num_ctx"] = num_ctx

        async with httpx.AsyncClient(timeout=None) as c:
            try:
                async with c.stream(
                    "POST", f"{self.base_url}/api/chat", json=payload
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = ""
                        msg = obj.get("message")
                        if isinstance(msg, dict):
                            content = msg.get("content", "") or ""
                        elif "response" in obj:
                            content = obj.get("response") or ""
                        yield ChatDelta(
                            content=content, done=bool(obj.get("done")), raw=obj
                        )
                        if obj.get("done"):
                            return
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise OllamaUnreachable(str(e)) from e
            except httpx.HTTPStatusError as e:
                body = ""
                try:
                    body = (await e.response.aread()).decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                raise OllamaError(f"Ollama {e.response.status_code}: {body}") from e

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

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


@dataclass
class ToolCall:
    """A single tool invocation returned by the model."""

    name: str
    arguments: dict  # already parsed by Ollama - never a raw JSON string


@dataclass
class ChatResponse:
    """Structured response from a non-streaming chat completion."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The raw message dict as returned by Ollama. Re-insert into the history
    # before appending tool results so the model sees its own tool_calls.
    raw_message: dict = field(default_factory=dict)


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

    async def complete_chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.2,
        num_ctx: int | None = None,
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        """Non-streaming chat completion. Returns a ChatResponse with content
        and any tool_calls the model emitted."""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if num_ctx is not None:
            payload["options"]["num_ctx"] = num_ctx
        if tools:
            payload["tools"] = tools
        # Use a split timeout: fail fast on connect (Ollama down), but allow
        # unlimited read time so large models on CPU can finish generating.
        split_timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=5.0)
        async with httpx.AsyncClient(timeout=split_timeout) as c:
            try:
                r = await c.post(f"{self.base_url}/api/chat", json=payload)
                r.raise_for_status()
                obj = r.json()
                msg = obj.get("message") or {}
                content = str(msg.get("content") or "")
                raw_tool_calls = msg.get("tool_calls") or []
                tool_calls = []
                for tc in raw_tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if name:
                        tool_calls.append(ToolCall(name=name, arguments=args))
                return ChatResponse(
                    content=content,
                    tool_calls=tool_calls,
                    raw_message=msg,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise OllamaUnreachable(str(e)) from e
            except httpx.HTTPStatusError as e:
                body = ""
                try:
                    body = (await e.response.aread()).decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                raise OllamaError(f"Ollama {e.response.status_code}: {body}") from e

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

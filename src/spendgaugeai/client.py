"""
client.py — SpendGaugeAIClient, wrap(), and .log() (docs/DESIGN.md §8a).

Two integration patterns, both best-effort and silent on failure — reporting
usage must never be able to break the real app using it:

- wrap(anthropic_client, ...) — patches `messages.create` and `messages.stream`
  on the given client object (works for both `Anthropic` and `AsyncAnthropic`,
  detected at wrap time) so every call through it reports itself automatically.
  Patching at this shared choke point means `client.beta.messages.tool_runner`,
  which calls the same underlying method internally, is covered too — no
  special-casing needed.
- SpendGaugeAIClient(...).log(...) / .alog(...) — manual, explicit reporting.

session_id/project flow through a contextvars.ContextVar (asyncio-task-local),
never a mutable attribute — a shared long-lived client handling concurrent
requests can't safely store per-request state as an instance attribute.
"""
import contextvars
import functools
import inspect
import logging
import uuid

import httpx

logger = logging.getLogger("spendgaugeai")

_session_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("spendgauge_session", default=None)


class SpendGaugeAIClient:
    """Thin HTTP client for POST /usage/log. Every method is best-effort: a
    short timeout, every exception caught internally, never raised."""

    def __init__(self, base_url: str, api_key: str, project: str = "default", timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.project = project
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _payload(self, *, model, input_tokens=0, cache_write_tokens=0, cache_read_tokens=0,
                 output_tokens=0, web_search_requests=0, tools_used=None,
                 session_id=None, project=None) -> dict:
        ctx = _session_ctx.get()
        return {
            "project": project or (ctx.get("project") if ctx else None) or self.project,
            "session_id": session_id or (ctx.get("session_id") if ctx else None),
            "model": model,
            "input_tokens": input_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_read_tokens": cache_read_tokens,
            "output_tokens": output_tokens,
            "web_search_requests": web_search_requests,
            "tools_used": tools_used or [],
        }

    def log(self, **kwargs) -> None:
        """Synchronous, best-effort report. Never raises."""
        payload = self._payload(**kwargs)
        try:
            httpx.post(f"{self.base_url}/usage/log", json=payload, headers=self._headers(), timeout=self.timeout)
        except Exception as e:
            logger.debug(f"[spendgaugeai] usage report failed: {e}")

    async def alog(self, **kwargs) -> None:
        """Async, best-effort report. Never raises."""
        payload = self._payload(**kwargs)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                await http.post(f"{self.base_url}/usage/log", json=payload, headers=self._headers())
        except Exception as e:
            logger.debug(f"[spendgaugeai] usage report failed: {e}")

    def spendgauge_session(self, session_id: str | None = None, project: str | None = None) -> "_SessionContext":
        """Scope session_id/project to the current (async) task, e.g.:

            with client.spendgauge_session(session_id=session_id, project="my-app"):
                response = await client.messages.create(...)

        No context set → each call falls back to a fresh server-generated UUID.
        """
        return _SessionContext(session_id=session_id, project=project)


class _SessionContext:
    def __init__(self, session_id: str | None, project: str | None):
        self.session_id = session_id or str(uuid.uuid4())
        self.project = project
        self._token = None

    def __enter__(self) -> "_SessionContext":
        self._token = _session_ctx.set({"session_id": self.session_id, "project": self.project})
        return self

    def __exit__(self, *exc_info) -> None:
        _session_ctx.reset(self._token)

    async def __aenter__(self) -> "_SessionContext":
        return self.__enter__()

    async def __aexit__(self, *exc_info) -> None:
        self.__exit__(*exc_info)


# ── wrap() ──────────────────────────────────────────────────────────────────

def _extract_usage(message) -> dict:
    usage = getattr(message, "usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }


def _extract_tools_used(message) -> list[str]:
    return [b.name for b in getattr(message, "content", None) or [] if getattr(b, "type", None) == "tool_use"]


def _extract_web_search_requests(message) -> int:
    usage = getattr(message, "usage", None)
    server_tool_use = getattr(usage, "server_tool_use", None)
    return getattr(server_tool_use, "web_search_requests", 0) or 0


def _report_kwargs(message, model: str) -> dict:
    return {
        "model": getattr(message, "model", None) or model,
        "tools_used": _extract_tools_used(message),
        "web_search_requests": _extract_web_search_requests(message),
        **_extract_usage(message),
    }


class _SyncStreamWrapper:
    """Wraps the context manager returned by `messages.stream(...)`. Forwards
    every event unchanged; reports from the final accumulated message once the
    `with` block exits, in a finally, so it fires whether the caller consumes
    the whole stream, breaks early, or the stream errors."""

    def __init__(self, manager, spendgauge: SpendGaugeAIClient, model: str):
        self._manager = manager
        self._spendgauge = spendgauge
        self._model = model
        self._stream = None

    def __enter__(self):
        self._stream = self._manager.__enter__()
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        try:
            try:
                final_message = self._stream.get_final_message()
                self._spendgauge.log(**_report_kwargs(final_message, self._model))
            except Exception as e:
                logger.debug(f"[spendgaugeai] stream report failed: {e}")
        finally:
            return self._manager.__exit__(exc_type, exc, tb)


class _AsyncStreamWrapper:
    def __init__(self, manager, spendgauge: SpendGaugeAIClient, model: str):
        self._manager = manager
        self._spendgauge = spendgauge
        self._model = model
        self._stream = None

    async def __aenter__(self):
        self._stream = await self._manager.__aenter__()
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        try:
            try:
                final_message = await self._stream.get_final_message()
                await self._spendgauge.alog(**_report_kwargs(final_message, self._model))
            except Exception as e:
                logger.debug(f"[spendgaugeai] stream report failed: {e}")
        finally:
            return await self._manager.__aexit__(exc_type, exc, tb)


def wrap(anthropic_client, *, base_url: str, api_key: str, project: str = "default", timeout: float = 2.0):
    """Patch `messages.create` and `messages.stream` on `anthropic_client` (an
    `Anthropic` or `AsyncAnthropic` instance — detected automatically) so every
    call through it reports itself to SpendGaugeAI. Returns the same client
    object, mutated in place, for `client = wrap(Anthropic())`-style use."""
    spendgauge = SpendGaugeAIClient(base_url=base_url, api_key=api_key, project=project, timeout=timeout)
    messages_obj = anthropic_client.messages
    original_create = messages_obj.create
    original_stream = messages_obj.stream
    is_async_client = inspect.iscoroutinefunction(original_create)

    if is_async_client:
        @functools.wraps(original_create)
        async def patched_create(*args, **kwargs):
            response = await original_create(*args, **kwargs)
            try:
                await spendgauge.alog(**_report_kwargs(response, kwargs.get("model", "unknown")))
            except Exception as e:
                logger.debug(f"[spendgaugeai] report failed: {e}")
            return response

        @functools.wraps(original_stream)
        def patched_stream(*args, **kwargs):
            manager = original_stream(*args, **kwargs)
            return _AsyncStreamWrapper(manager, spendgauge, kwargs.get("model", "unknown"))
    else:
        @functools.wraps(original_create)
        def patched_create(*args, **kwargs):
            response = original_create(*args, **kwargs)
            try:
                spendgauge.log(**_report_kwargs(response, kwargs.get("model", "unknown")))
            except Exception as e:
                logger.debug(f"[spendgaugeai] report failed: {e}")
            return response

        @functools.wraps(original_stream)
        def patched_stream(*args, **kwargs):
            manager = original_stream(*args, **kwargs)
            return _SyncStreamWrapper(manager, spendgauge, kwargs.get("model", "unknown"))

    messages_obj.create = patched_create
    messages_obj.stream = patched_stream
    return anthropic_client

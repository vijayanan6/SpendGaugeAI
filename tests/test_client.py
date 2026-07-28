"""Tests for client.py's wrap() — patches messages.create/.stream on a
fake Anthropic-shaped client. Uses SimpleNamespace since wrap()'s extraction
helpers only ever use getattr(), matching real SDK response objects without
needing the anthropic package installed."""
from types import SimpleNamespace

import pytest

from spendgaugeai.client import SpendGaugeAIBudgetExceededError, SpendGaugeAIClient, wrap


class _FakeMessages:
    def __init__(self, create_fn, stream_fn=None):
        self.create = create_fn
        self.stream = stream_fn or (lambda *a, **k: None)


class _FakeClient:
    def __init__(self, create_fn, stream_fn=None):
        self.messages = _FakeMessages(create_fn, stream_fn)


def _fake_message(**overrides):
    defaults = dict(
        model="claude-sonnet-4-6",
        content=[
            SimpleNamespace(type="text", text="hello"),
            SimpleNamespace(type="tool_use", name="search_docs"),
        ],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=20,
            server_tool_use=SimpleNamespace(web_search_requests=2),
        ),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_raw_events():
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                model="claude-sonnet-4-6",
                usage=SimpleNamespace(input_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=20),
            ),
        ),
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use", name="search_docs")),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=50, server_tool_use=SimpleNamespace(web_search_requests=2))),
        SimpleNamespace(type="message_stop"),
    ]


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(SpendGaugeAIClient, "log", lambda self, **kwargs: calls.append(kwargs))

    async def fake_alog(self, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(SpendGaugeAIClient, "alog", fake_alog)
    return calls


def test_sync_create_reports_normally(captured):
    fake_client = _FakeClient(create_fn=lambda *a, **k: _fake_message())
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k")

    response = wrapped.messages.create(model="claude-sonnet-4-6")
    assert response.content[0].text == "hello"  # forwarded unchanged

    assert len(captured) == 1
    assert captured[0]["input_tokens"] == 100
    assert captured[0]["output_tokens"] == 50
    assert captured[0]["tools_used"] == ["search_docs"]
    assert captured[0]["web_search_requests"] == 2


def test_sync_create_with_stream_true_reports_real_usage_not_zero(captured):
    # Regression: create(stream=True) returns a raw event iterator, not a
    # populated Message — reading .usage/.content directly off it (as the
    # non-streaming path does) silently produced an all-zero report.
    events = _fake_raw_events()
    fake_client = _FakeClient(create_fn=lambda *a, **k: iter(events))
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k")

    stream = wrapped.messages.create(model="claude-sonnet-4-6", stream=True)
    seen = list(stream)
    assert seen == events  # every event forwarded unchanged

    assert len(captured) == 1
    assert captured[0]["input_tokens"] == 100
    assert captured[0]["output_tokens"] == 50
    assert captured[0]["cache_read_tokens"] == 20
    assert captured[0]["web_search_requests"] == 2
    assert captured[0]["tools_used"] == ["search_docs"]


def test_sync_create_with_stream_true_reports_even_on_early_break(captured):
    events = _fake_raw_events()
    fake_client = _FakeClient(create_fn=lambda *a, **k: iter(events))
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k")

    stream = wrapped.messages.create(model="claude-sonnet-4-6", stream=True)
    for event in stream:
        if event.type == "content_block_start":
            break  # caller stops consuming before message_delta/message_stop

    # Even though output_tokens (from message_delta) was never seen, the
    # partial report still fires — reporting must never depend on the caller
    # draining the whole stream.
    assert len(captured) == 1
    assert captured[0]["input_tokens"] == 100


@pytest.mark.asyncio
async def test_async_create_with_stream_true_reports_real_usage_not_zero(captured):
    events = _fake_raw_events()

    async def async_create(*a, **k):
        async def gen():
            for e in events:
                yield e
        return gen()

    fake_client = _FakeClient(create_fn=async_create)
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k")

    stream = await wrapped.messages.create(model="claude-sonnet-4-6", stream=True)
    seen = [e async for e in stream]
    assert seen == events

    assert len(captured) == 1
    assert captured[0]["input_tokens"] == 100
    assert captured[0]["output_tokens"] == 50
    assert captured[0]["tools_used"] == ["search_docs"]


# ── check_budget() / acheck_budget() ───────────────────────────────────────

class _FakeBudgetResponse:
    def __init__(self, exceeded: bool):
        self._exceeded = exceeded

    def raise_for_status(self):
        pass

    def json(self):
        return {"exceeded": self._exceeded, "remaining_usd": 0.0, "starting_balance": 5.0}


def test_check_budget_true_when_server_reports_exceeded(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        return _FakeBudgetResponse(exceeded=True)

    monkeypatch.setattr("spendgaugeai.client.httpx.get", fake_get)
    client = SpendGaugeAIClient(base_url="http://localhost:8000", api_key="k")
    assert client.check_budget() is True
    assert calls[0][0] == "http://localhost:8000/usage/budget"
    assert calls[0][1] is None  # no project passed -> global check


def test_check_budget_passes_project_as_query_param(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return _FakeBudgetResponse(exceeded=False)

    monkeypatch.setattr("spendgaugeai.client.httpx.get", fake_get)
    client = SpendGaugeAIClient(base_url="http://localhost:8000", api_key="k")
    client.check_budget(project="demo-app")
    assert calls[0] == {"project": "demo-app"}


def test_check_budget_none_on_network_failure_fails_open(monkeypatch):
    def fake_get(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("spendgaugeai.client.httpx.get", fake_get)
    client = SpendGaugeAIClient(base_url="http://localhost:8000", api_key="k")
    assert client.check_budget() is None


def test_check_budget_caches_within_ttl(monkeypatch):
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return _FakeBudgetResponse(exceeded=False)

    monkeypatch.setattr("spendgaugeai.client.httpx.get", fake_get)
    client = SpendGaugeAIClient(base_url="http://localhost:8000", api_key="k")
    assert client.check_budget(cache_seconds=60) is False
    assert client.check_budget(cache_seconds=60) is False
    assert len(calls) == 1  # second call served from cache


@pytest.mark.asyncio
async def test_acheck_budget_true_when_server_reports_exceeded(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, params=None, headers=None):
            return _FakeBudgetResponse(exceeded=True)

    monkeypatch.setattr("spendgaugeai.client.httpx.AsyncClient", _FakeAsyncClient)
    client = SpendGaugeAIClient(base_url="http://localhost:8000", api_key="k")
    assert await client.acheck_budget() is True


@pytest.mark.asyncio
async def test_acheck_budget_none_on_network_failure_fails_open(monkeypatch):
    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr("spendgaugeai.client.httpx.AsyncClient", _FakeAsyncClient)
    client = SpendGaugeAIClient(base_url="http://localhost:8000", api_key="k")
    assert await client.acheck_budget() is None


# ── wrap(..., enforce=True) ────────────────────────────────────────────────

class _FakeSyncStreamManager:
    def __init__(self, final_message):
        self._final_message = final_message
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self._final_message


class _FakeAsyncStreamManager:
    def __init__(self, final_message):
        self._final_message = final_message
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_final_message(self):
        return self._final_message


def test_enforce_off_by_default_never_checks_budget(captured, monkeypatch):
    check_calls = []
    monkeypatch.setattr(SpendGaugeAIClient, "check_budget", lambda self, **k: check_calls.append(1) or False)
    fake_client = _FakeClient(create_fn=lambda *a, **k: _fake_message())
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k")

    wrapped.messages.create(model="claude-sonnet-4-6")
    assert check_calls == []
    assert len(captured) == 1


def test_enforce_raises_before_sync_create_when_exceeded(captured, monkeypatch):
    monkeypatch.setattr(SpendGaugeAIClient, "check_budget", lambda self, **k: True)
    real_call_made = []
    fake_client = _FakeClient(create_fn=lambda *a, **k: real_call_made.append(1) or _fake_message())
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k", enforce=True)

    with pytest.raises(SpendGaugeAIBudgetExceededError):
        wrapped.messages.create(model="claude-sonnet-4-6")
    assert real_call_made == []  # blocked before the real Anthropic call
    assert captured == []  # never reached reporting either


def test_enforce_raises_before_sync_create_stream_true_when_exceeded(monkeypatch):
    monkeypatch.setattr(SpendGaugeAIClient, "check_budget", lambda self, **k: True)
    real_call_made = []
    fake_client = _FakeClient(create_fn=lambda *a, **k: real_call_made.append(1) or iter(_fake_raw_events()))
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k", enforce=True)

    with pytest.raises(SpendGaugeAIBudgetExceededError):
        wrapped.messages.create(model="claude-sonnet-4-6", stream=True)
    assert real_call_made == []


def test_enforce_proceeds_when_not_exceeded(captured, monkeypatch):
    monkeypatch.setattr(SpendGaugeAIClient, "check_budget", lambda self, **k: False)
    fake_client = _FakeClient(create_fn=lambda *a, **k: _fake_message())
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k", enforce=True)

    response = wrapped.messages.create(model="claude-sonnet-4-6")
    assert response.content[0].text == "hello"
    assert len(captured) == 1


def test_enforce_fails_open_when_check_result_is_none(captured, monkeypatch):
    monkeypatch.setattr(SpendGaugeAIClient, "check_budget", lambda self, **k: None)
    fake_client = _FakeClient(create_fn=lambda *a, **k: _fake_message())
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k", enforce=True)

    response = wrapped.messages.create(model="claude-sonnet-4-6")
    assert response is not None
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_enforce_raises_before_async_create_when_exceeded(monkeypatch):
    async def fake_acheck(self, **k):
        return True

    monkeypatch.setattr(SpendGaugeAIClient, "acheck_budget", fake_acheck)
    real_call_made = []

    async def async_create(*a, **k):
        real_call_made.append(1)
        return _fake_message()

    fake_client = _FakeClient(create_fn=async_create)
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k", enforce=True)

    with pytest.raises(SpendGaugeAIBudgetExceededError):
        await wrapped.messages.create(model="claude-sonnet-4-6")
    assert real_call_made == []


def test_enforce_raises_before_sync_stream_enter_when_exceeded(monkeypatch):
    monkeypatch.setattr(SpendGaugeAIClient, "check_budget", lambda self, **k: True)
    manager = _FakeSyncStreamManager(_fake_message())
    fake_client = _FakeClient(create_fn=lambda *a, **k: None, stream_fn=lambda *a, **k: manager)
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k", enforce=True)

    stream_wrapper = wrapped.messages.stream(model="claude-sonnet-4-6")
    with pytest.raises(SpendGaugeAIBudgetExceededError):
        with stream_wrapper:
            pass
    assert manager.entered is False  # the real stream's __enter__ (the network call) never ran


@pytest.mark.asyncio
async def test_enforce_raises_before_async_stream_enter_when_exceeded(monkeypatch):
    async def fake_acheck(self, **k):
        return True

    monkeypatch.setattr(SpendGaugeAIClient, "acheck_budget", fake_acheck)

    async def async_create(*a, **k):
        return _fake_message()

    manager = _FakeAsyncStreamManager(_fake_message())
    fake_client = _FakeClient(create_fn=async_create, stream_fn=lambda *a, **k: manager)
    wrapped = wrap(fake_client, base_url="http://localhost:8000", api_key="k", enforce=True)

    stream_wrapper = wrapped.messages.stream(model="claude-sonnet-4-6")
    with pytest.raises(SpendGaugeAIBudgetExceededError):
        async with stream_wrapper:
            pass
    assert manager.entered is False

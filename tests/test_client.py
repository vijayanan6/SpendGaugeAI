"""Tests for client.py's wrap() — patches messages.create/.stream on a
fake Anthropic-shaped client. Uses SimpleNamespace since wrap()'s extraction
helpers only ever use getattr(), matching real SDK response objects without
needing the anthropic package installed."""
from types import SimpleNamespace

import pytest

from spendgaugeai.client import SpendGaugeAIClient, wrap


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

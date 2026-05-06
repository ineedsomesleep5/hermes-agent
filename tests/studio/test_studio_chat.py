"""Tests for tools/studio_chat.py — gateway-forwarded streaming chat.

studio_chat now forwards every Studio chat turn to the running Hermes
gateway (127.0.0.1:8642) so the user's *real* configured agent runs the
turn — same model, same tools, same skills, same memory. We test the
event-translation layer with a stub stream factory so we don't need a
live gateway during unit tests.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def chat(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("tools.studio_chat", None)
    import tools.studio_chat as sc_mod
    importlib.reload(sc_mod)
    yield sc_mod


# ---------------------------------------------------------------------------
# Stub stream factory — produces (event_name, data_dict) tuples
# ---------------------------------------------------------------------------

def _make_stream_factory(events):
    last_call: dict = {}

    async def factory(base_url, payload, headers):
        last_call["base_url"] = base_url
        last_call["payload"] = payload
        last_call["headers"] = headers
        for ev in events:
            yield ev

    factory.last_call = last_call
    return factory


# ---------------------------------------------------------------------------
# Studio prompt guardrails
# ---------------------------------------------------------------------------

def test_studio_context_treats_gratitude_as_noop(chat):
    context = chat.STUDIO_CONTEXT.lower()
    assert "gratitude or approval" in context
    assert "do not call tools" in context
    assert "stale studio" in context
    assert "annotations" in context
    assert "never call studio_widget_read" in context
    assert "live widget edits" in context
    assert "page reload" in context
    assert "studio shell itself" in context


# ---------------------------------------------------------------------------
# Sessions + studio context injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_turn_seeds_studio_context(chat, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "stub-key")
    factory = _make_stream_factory(
        [
            (None, {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        ]
    )

    events = []
    async for ev in chat.stream_chat(
        "s1", "hi", "default", stream_factory=factory
    ):
        events.append(ev)

    payload = factory.last_call["payload"]
    msgs = payload["messages"]
    # Order: Studio context, live canvas grounding, then the user turn.
    assert msgs[0]["role"] == "system"
    assert "studio canvas" in msgs[0]["content"].lower()
    assert msgs[1]["role"] == "system"
    assert "canvas state" in msgs[1]["content"].lower()
    assert msgs[2] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_no_api_key_emits_error(chat, monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("STUDIO_GATEWAY_API_KEY", raising=False)
    events = []
    async for ev in chat.stream_chat("s2", "hi", "default"):
        events.append(ev)
    assert any(e["type"] == "error" for e in events)


# ---------------------------------------------------------------------------
# Text streaming
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_deltas_pass_through(chat, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "stub-key")
    factory = _make_stream_factory(
        [
            (None, {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]}),
            (None, {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]}),
            (None, {"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        ]
    )

    events = []
    async for ev in chat.stream_chat(
        "s3", "say hi", "default", stream_factory=factory
    ):
        events.append(ev)

    types = [e["type"] for e in events]
    assert types[0] == "user_echo"
    text_deltas = [e["delta"] for e in events if e["type"] == "text"]
    assert "".join(text_deltas) == "Hello world"
    assert types[-1] == "done"


@pytest.mark.asyncio
async def test_assistant_text_is_appended_to_history(chat, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "stub-key")
    factory = _make_stream_factory(
        [
            (None, {"choices": [{"delta": {"content": "Pong"}, "finish_reason": "stop"}]}),
        ]
    )
    async for _ in chat.stream_chat(
        "history-test", "ping", "default", stream_factory=factory
    ):
        pass

    history = chat.get_history("history-test")
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "Pong"


# ---------------------------------------------------------------------------
# Tool progress translation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hermes_tool_progress_becomes_tool_call_event(chat, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "stub-key")
    factory = _make_stream_factory(
        [
            ("hermes.tool.progress", {"tool": "studio_widget_create", "label": "studio_widget_create"}),
            # Same tool emits multiple progress events while running — we
            # should only emit ONE tool_call_start for it.
            ("hermes.tool.progress", {"tool": "studio_widget_create", "label": "studio_widget_create"}),
            (None, {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]}),
        ]
    )

    events = []
    async for ev in chat.stream_chat(
        "s4", "make a widget", "default", stream_factory=factory
    ):
        events.append(ev)

    starts = [e for e in events if e["type"] == "tool_call_start"]
    results = [e for e in events if e["type"] == "tool_call_result"]
    assert len(starts) == 1
    assert starts[0]["name"] == "studio_widget_create"
    # Each tool that fired gets a synthetic completion event so the UI
    # can stop showing it as "running".
    assert len(results) == 1
    assert results[0]["name"] == "studio_widget_create"


@pytest.mark.asyncio
async def test_multiple_tools_each_get_own_event(chat, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "stub-key")
    factory = _make_stream_factory(
        [
            ("hermes.tool.progress", {"tool": "skills_list"}),
            ("hermes.tool.progress", {"tool": "studio_widget_create"}),
            (None, {"choices": [{"delta": {"content": "Built."}, "finish_reason": "stop"}]}),
        ]
    )

    events = []
    async for ev in chat.stream_chat(
        "s5", "make skills widget", "default", stream_factory=factory
    ):
        events.append(ev)

    started_names = [e["name"] for e in events if e["type"] == "tool_call_start"]
    assert started_names == ["skills_list", "studio_widget_create"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_factory_exception_emits_error(chat, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "stub-key")

    async def broken_factory(base_url, payload, headers):
        raise RuntimeError("gateway down")
        yield  # noqa — keep type checker happy that this is a generator

    events = []
    async for ev in chat.stream_chat(
        "s6", "hi", "default", stream_factory=broken_factory
    ):
        events.append(ev)
    err = [e for e in events if e["type"] == "error"]
    assert err and "gateway down" in err[0]["message"]


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bearer_auth_header_is_set(chat, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "secret-123")
    factory = _make_stream_factory(
        [
            (None, {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}),
        ]
    )
    async for _ in chat.stream_chat(
        "s7", "hi", "default", stream_factory=factory
    ):
        pass
    assert factory.last_call["headers"]["Authorization"] == "Bearer secret-123"


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def test_reset_session_clears_history(chat):
    chat._sessions["zzz"] = [{"role": "user", "content": "old"}]
    chat.reset_session("zzz")
    assert chat.get_history("zzz") == []

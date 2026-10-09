"""The request loop in agents/llm.py, against a fake streaming client (no network)."""
import asyncio
import types

import anthropic
import httpx2 as httpx
import pytest

from agents import llm
from agents.schema import STR, obj

B = lambda **k: types.SimpleNamespace(**k)  # noqa: E731
USAGE = B(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0,
          server_tool_use=B(web_search_requests=1, web_fetch_requests=0))
SCHEMA = obj(a=STR)
REQ = httpx.Request("POST", "https://api.example")


def submit(inp):
    return B(type="tool_use", name="submit", input=inp)


SEARCH = B(type="server_tool_use", name="web_search", id="s1")
RESULT_ERR = B(type="web_search_tool_result", tool_use_id="s1", content=B(error_code="max_uses_exceeded"))


def msg(stop, *content):
    return B(stop_reason=stop, content=list(content), usage=USAGE)


class _Stream:
    def __init__(self, item):
        self.item = item

    async def __aenter__(self):
        if isinstance(self.item, Exception):
            raise self.item
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self.item


@pytest.fixture
def client(monkeypatch):
    sent: list[dict] = []
    script: list = []

    def stream(**kw):
        sent.append(kw)
        return _Stream(script.pop(0))

    monkeypatch.setattr(llm, "_client", B(beta=B(messages=B(stream=stream))))
    monkeypatch.setattr(llm, "_sems", {"heavy": asyncio.Semaphore(2), "light": asyncio.Semaphore(2)})
    monkeypatch.setattr(llm, "errors", [])
    monkeypatch.setattr(llm, "STRICT_400", False)
    monkeypatch.setattr(llm, "DIRECT_SEARCH", False)
    return script, sent


def run(**kw):
    return asyncio.run(llm.run_structured("sys", "prompt", SCHEMA, label="t", **kw))


def roles(kw):
    return [m["role"] for m in kw["messages"]]


def test_plain_submit(client):
    script, sent = client
    script.append(msg("tool_use", submit({"a": "ok"})))
    assert run() == {"a": "ok"}


def test_truncated_submit_is_discarded_not_edited(client):
    script, sent = client
    script += [msg("max_tokens", SEARCH, submit({})), msg("tool_use", submit({"a": "ok"}))]
    assert run() == {"a": "ok"}
    assert roles(sent[1]) == ["user", "user"]  # bad turn dropped whole, then a nudge


def test_pause_turn_resumes_with_full_content(client):
    script, sent = client
    script += [msg("pause_turn", SEARCH, RESULT_ERR), msg("tool_use", submit({"a": "ok"}))]
    assert run(web_searches=2) == {"a": "ok"}
    assert roles(sent[1]) == ["user", "assistant"]
    assert any("max_uses_exceeded" in e for e in llm.errors)


def test_pause_turn_with_complete_submit_returns_it(client):
    script, _ = client
    script.append(msg("pause_turn", SEARCH, RESULT_ERR, submit({"a": "done"})))
    assert run(web_searches=1) == {"a": "done"}


def test_empty_submit_alongside_unrun_search_is_rejected():
    s = obj(items={"type": "array", "items": STR})
    assert llm._premature({"items": []}) and not llm._premature({"items": ["x"]})
    assert not llm._premature({"a": "x"})
    assert s["required"] == ["items"]


def test_400_fatal_before_preflight_only(client, monkeypatch):
    script, _ = client
    err = anthropic.BadRequestError("prompt is too long", response=httpx.Response(400, request=REQ), body=None)
    script.append(err)
    assert run() is None  # after preflight: just this call
    monkeypatch.setattr(llm, "STRICT_400", True)
    script.append(err)
    with pytest.raises(anthropic.BadRequestError):
        run()


def test_auth_error_always_fatal(client):
    script, _ = client
    script.append(anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=REQ), body=None))
    with pytest.raises(anthropic.AuthenticationError):
        run()


def test_server_error_returns_none(client):
    script, _ = client
    script.append(anthropic.InternalServerError("boom", response=httpx.Response(500, request=REQ), body=None))
    assert run() is None


def test_model_routing_caching_and_fallbacks(client):
    script, sent = client
    script += [msg("tool_use", submit({"a": "x"})), msg("tool_use", submit({"a": "y"}))]
    run(model="light", web_searches=1, effort="low")
    run(model="heavy")
    light, heavy = sent
    assert light["model"] == llm.MODELS["light"] and "fallbacks" not in light and light["cache_control"]
    assert light["output_config"] == {"effort": "low"}
    assert heavy["model"] == llm.MODELS["heavy"] and heavy["fallbacks"] == "default" and "cache_control" not in heavy


def test_preflight_switches_to_direct_search(client):
    script, sent = client
    script += [anthropic.BadRequestError("tools.0: allowed_callers must be ['direct']",
                                         response=httpx.Response(400, request=REQ), body=None),
               msg("tool_use", submit({"ok": True}))]
    llm.STRICT_400 = True
    asyncio.run(llm.preflight())
    assert llm.DIRECT_SEARCH and not llm.STRICT_400
    assert sent[-1]["tools"][0]["allowed_callers"] == ["direct"]

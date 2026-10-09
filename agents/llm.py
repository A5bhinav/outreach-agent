"""Shared Claude call helper.

Every agent step calls `run_structured`: Claude may use web search / web fetch
(server-side tools), then must hand back its answer by calling a strict
`submit` tool whose input schema is the step's output schema.
"""
import asyncio
import datetime
import sys
import time

import anthropic

# Judgement-heavy steps (research, people, writing, review) use the heavy model;
# routing, sourcing and contact lookup use the lighter one.
MODELS = {"heavy": "claude-opus-5-5", "light": "claude-sonnet-5"}
# Models that take server-side refusal fallbacks.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5"}
EFFORT = "high"  # overridden by --effort in main.py
CONCURRENCY = 5
DIRECT_SEARCH = False  # set by preflight() if the org can't use dynamic-filtering search
TODAY = datetime.date.today().isoformat()

_client: anthropic.AsyncAnthropic | None = None
_sem: asyncio.Semaphore | None = None
_t0 = time.time()
usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "web_searches": 0, "web_fetches": 0, "calls": 0}
errors: list[str] = []

# Errors that will hit every call (bad key, web search not enabled, bad params): stop the run.
FATAL = (anthropic.BadRequestError, anthropic.AuthenticationError,
         anthropic.PermissionDeniedError, anthropic.NotFoundError)


def log(msg: str) -> None:
    if "  ! " in msg:
        errors.append(msg.strip())
    print(f"[{time.time() - _t0:6.1f}s] {msg}", file=sys.stderr, flush=True)


def _get():
    global _client, _sem
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=3, timeout=anthropic.Timeout(900, connect=15))
        _sem = asyncio.Semaphore(CONCURRENCY)
    return _client, _sem


def _web_tools(max_uses: int, fetch: bool) -> list[dict]:
    extra = {"allowed_callers": ["direct"]} if DIRECT_SEARCH else {}
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses, **extra}]
    if fetch:
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_uses, **extra})
    return tools


def _track(resp, label: str) -> None:
    usage["calls"] += 1
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        usage[k] += getattr(resp.usage, k, 0) or 0
    stu = getattr(resp.usage, "server_tool_use", None)
    if stu:
        usage["web_searches"] += getattr(stu, "web_search_requests", 0) or 0
        usage["web_fetches"] += getattr(stu, "web_fetch_requests", 0) or 0
    # Server-tool failures come back as result blocks inside a 200, not as exceptions.
    for b in resp.content:
        if b.type in ("web_search_tool_result", "web_fetch_tool_result") and not isinstance(b.content, list):
            code = getattr(b.content, "error_code", None)
            if code:
                log(f"  ! {label}: {b.type} error: {code}")


def _unrun_searches(content: list) -> set[str]:
    """IDs of server_tool_use blocks with no matching result block in the same response."""
    called = {b.id for b in content if b.type == "server_tool_use"}
    answered = {getattr(b, "tool_use_id", None) for b in content if b.type.endswith("_tool_result")}
    return called - answered


async def _create(client, model: str, **params):
    kw = dict(model=model, max_tokens=32000, thinking={"type": "adaptive"}, output_config={"effort": EFFORT}, **params)
    if model in FALLBACK_MODELS:
        kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    async with client.beta.messages.stream(**kw) as stream:
        return await stream.get_final_message()


async def run_structured(
    system: str,
    prompt: str,
    schema: dict,
    *,
    web_searches: int = 0,
    web_fetch: bool = False,
    model: str = "heavy",
    label: str = "",
) -> dict | None:
    """Run one agent step and return the `submit` tool input (or None on failure).

    Raises on configuration errors (400/401/403/404) so a broken setup fails fast
    instead of producing an empty run. History is append-only: a bad turn is
    discarded whole and retried, never edited.
    """
    client, sem = _get()
    submit = {
        "name": "submit",
        "description": "Submit your final answer. Call this exactly once, when you are done, in a turn of its own.",
        "strict": True,
        "input_schema": schema,
    }
    tools = [submit]
    if web_searches:
        tools = _web_tools(web_searches, web_fetch) + tools

    system = f"{system}\n\nToday's date is {TODAY}."
    messages: list[dict] = [{"role": "user", "content": prompt}]
    async with sem:
        for _ in range(6):
            try:
                resp = await _create(client, MODELS[model], system=system, tools=tools, messages=messages)
            except FATAL as e:
                log(f"  ! {label}: API error {e.status_code}: {e.message}")
                raise
            except anthropic.APIStatusError as e:
                log(f"  ! {label}: API error {e.status_code}: {e.message}")
                return None
            except anthropic.APIConnectionError as e:
                log(f"  ! {label}: connection error: {e}")
                return None

            _track(resp, label)

            if resp.stop_reason == "refusal":
                log(f"  ! {label}: refused")
                return None
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue  # server-side tool loop paused; resend to resume

            truncated = resp.stop_reason == "max_tokens"
            unrun = _unrun_searches(resp.content)
            subs = [b for b in resp.content if b.type == "tool_use" and b.name == "submit"]
            if subs and not truncated and not unrun and set(schema["required"]) <= set(subs[0].input):
                return subs[0].input

            # The turn can't be sent back as-is (a cut-off answer, a submit without a result, or a
            # search that never ran), so discard it and ask again.
            if truncated:
                nudge = "Your last answer was cut off. Be more concise and call the submit tool with your complete answer."
            elif unrun:
                nudge = "Your searches in that turn didn't run because you also called submit. Search first; submit in a separate turn."
            else:
                if not subs:
                    messages.append({"role": "assistant", "content": resp.content})
                nudge = "Call the submit tool now with your complete answer, using only what you found."
            messages.append({"role": "user", "content": nudge})
    log(f"  ! {label}: no structured answer")
    return None


async def preflight() -> None:
    """One cheap search call so a broken setup fails in seconds, not mid-run.

    If the org refuses dynamic-filtering search, switch every later call to direct search.
    """
    global DIRECT_SEARCH, EFFORT
    effort, EFFORT = EFFORT, "low"
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    res = None
    try:
        for attempt in range(2):
            try:
                res = await run_structured("Run one web search, then submit ok=true.", "Search for: anthropic",
                                     schema, web_searches=1, model="light", label="preflight")
                break
            except anthropic.BadRequestError as e:
                if attempt == 0 and not DIRECT_SEARCH and "allowed_callers" in str(e.message):
                    log("preflight: dynamic-filtering search unavailable; using direct search")
                    DIRECT_SEARCH = True
                    continue
                raise
    finally:
        EFFORT = effort
    if res is None:
        raise SystemExit(f"preflight failed: {errors[-1] if errors else 'no answer'}")
    log("preflight: web search OK")

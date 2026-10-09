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
EFFORT = "high"  # overridden by --effort in main.py; steps may pass their own
CONCURRENCY = 8  # heavy-model calls in flight; light calls get twice as many (separate rate-limit buckets)
DIRECT_SEARCH = False  # set by preflight() if the org can't use dynamic-filtering search
# Before preflight passes, any 400 means the setup is broken; after, a 400 is specific to one call.
STRICT_400 = True
TODAY = datetime.date.today().isoformat()

_client: anthropic.AsyncAnthropic | None = None
_sems: dict[str, asyncio.Semaphore] = {}
_t0 = time.time()
usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "web_searches": 0, "web_fetches": 0, "calls": 0}
by_step: dict[str, dict] = {}
errors: list[str] = []
# Optional per-run log capture (the web app streams these lines to the browser).
sink: list[str] | None = None

# Errors that will hit every call (bad key, no access, unknown model): stop the run.
FATAL = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)


def log(msg: str) -> None:
    if "  ! " in msg:
        errors.append(msg.strip())
    line = f"[{time.time() - _t0:6.1f}s] {msg}"
    if sink is not None:
        sink.append(line)
    print(line, file=sys.stderr, flush=True)


def reset() -> None:
    """Fresh client, limits and counters for a new run (each run gets its own event loop)."""
    global _client, _t0, STRICT_400, DIRECT_SEARCH
    _client = None
    _sems.clear()
    _t0 = time.time()
    STRICT_400, DIRECT_SEARCH = True, False
    for k in usage:
        usage[k] = 0
    by_step.clear()
    errors.clear()


def _get():
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=5, timeout=anthropic.Timeout(900, connect=15))
        _sems["heavy"] = asyncio.Semaphore(CONCURRENCY)
        _sems["light"] = asyncio.Semaphore(CONCURRENCY * 2)
    return _client


def _web_tools(searches: int, fetches: int) -> list[dict]:
    extra = {"allowed_callers": ["direct"]} if DIRECT_SEARCH else {}
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": searches, **extra}]
    if fetches:
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches, **extra})
    return tools


def _track(resp, label: str) -> None:
    step = label.split("[")[0] or "other"
    s = by_step.setdefault(step, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "web_searches": 0})
    usage["calls"] += 1
    s["calls"] += 1
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = getattr(resp.usage, k, 0) or 0
        usage[k] += v
        if k in s:
            s[k] += v
    stu = getattr(resp.usage, "server_tool_use", None)
    if stu:
        usage["web_searches"] += getattr(stu, "web_search_requests", 0) or 0
        s["web_searches"] += getattr(stu, "web_search_requests", 0) or 0
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


def _premature(answer: dict) -> bool:
    """A submit whose list fields are all empty, sent alongside searches that never ran, skipped its own research."""
    lists = [v for v in answer.values() if isinstance(v, list)]
    return bool(lists) and not any(lists)


async def _create(client, model: str, effort: str, max_tokens: int, cache: bool, **params):
    kw = dict(model=model, max_tokens=max_tokens, thinking={"type": "adaptive"}, output_config={"effort": effort}, **params)
    if cache:
        # Web-tool loops resend large search results; with caching on, the server also caches after
        # each tool result, so later iterations read at the cache rate.
        kw["cache_control"] = {"type": "ephemeral"}
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
    web_fetches: int = 0,
    model: str = "heavy",
    effort: str | None = None,
    max_tokens: int = 32000,
    label: str = "",
) -> dict | None:
    """Run one agent step and return the `submit` tool input (or None on failure).

    Raises on configuration errors so a broken setup fails fast instead of producing an empty
    run. History is append-only: a turn that can't be sent back is discarded whole, never edited.
    """
    client = _get()
    submit = {
        "name": "submit",
        "description": "Submit your final answer. Call this exactly once, when you are done, in a turn of its own.",
        "strict": True,
        "input_schema": schema,
    }
    tools = [submit]
    if web_searches:
        tools = _web_tools(web_searches, web_fetches) + tools

    system = f"{system}\n\nToday's date is {TODAY}."
    messages: list[dict] = [{"role": "user", "content": prompt}]
    async with _sems[model]:
        for _ in range(8):
            try:
                resp = await _create(client, MODELS[model], effort or EFFORT, max_tokens, bool(web_searches),
                                     system=system, tools=tools, messages=messages)
            except FATAL as e:
                log(f"  ! {label}: API error {e.status_code}: {e.message}")
                raise
            except anthropic.BadRequestError as e:
                log(f"  ! {label}: API error 400: {e.message}")
                if STRICT_400:
                    raise
                return None
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

            truncated = resp.stop_reason == "max_tokens"
            unrun = _unrun_searches(resp.content)
            subs = [b for b in resp.content if b.type == "tool_use" and b.name == "submit"]
            if subs and not truncated and set(schema["required"]) <= set(subs[0].input):
                if not (unrun and _premature(subs[0].input)):
                    return subs[0].input

            if resp.stop_reason == "pause_turn" and not subs:
                messages.append({"role": "assistant", "content": resp.content})
                continue  # server-side tool loop paused; resend to resume

            # The turn can't be sent back as-is (a cut-off answer, a submit without a tool_result, or a
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
    global DIRECT_SEARCH, STRICT_400
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    res = None
    for attempt in range(2):
        try:
            res = await run_structured("Run one web search, then submit ok=true.", "Search for: anthropic", schema,
                                       web_searches=1, model="light", effort="low", max_tokens=4000, label="preflight")
            break
        except anthropic.BadRequestError as e:
            if attempt == 0 and not DIRECT_SEARCH and "allowed_callers" in str(e.message):
                log("preflight: dynamic-filtering search unavailable; using direct search")
                DIRECT_SEARCH = True
                continue
            raise
    if res is None:
        raise SystemExit(f"preflight failed: {errors[-1] if errors else 'no answer'}")
    STRICT_400 = False
    log("preflight: web search OK")


def summary() -> str:
    u = usage
    lines = [f"usage: {u['calls']} calls, {u['input_tokens']:,} in ({u['cache_read_input_tokens']:,} cache reads, "
             f"{u['cache_creation_input_tokens']:,} cache writes) / {u['output_tokens']:,} out tokens, "
             f"{u['web_searches']} web searches, {u['web_fetches']} web fetches"]
    for step, s in sorted(by_step.items(), key=lambda kv: -kv[1]["input_tokens"]):
        lines.append(f"  {step:<10} {s['calls']:>4} calls {s['input_tokens']:>12,} in {s['output_tokens']:>10,} out "
                     f"{s['web_searches']:>4} searches")
    return "\n".join(lines)

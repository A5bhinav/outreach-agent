"""Shared Claude call helper.

Every agent step calls `run_structured`: Claude may use web search / web fetch
(server-side tools), then must hand back its answer by calling a strict
`submit` tool whose input schema is the step's output schema.
"""
import asyncio
import sys
import time

import anthropic

MODEL = "claude-opus-5"
EFFORT = "high"  # overridden by --effort in main.py
CONCURRENCY = 5

_client: anthropic.AsyncAnthropic | None = None
_sem: asyncio.Semaphore | None = None
_t0 = time.time()
usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "calls": 0}
errors: list[str] = []


def log(msg: str) -> None:
    if "  ! " in msg:
        errors.append(msg.strip())
    print(f"[{time.time() - _t0:6.1f}s] {msg}", file=sys.stderr, flush=True)


def _get():
    global _client, _sem
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=4)
        _sem = asyncio.Semaphore(CONCURRENCY)
    return _client, _sem


def _web_tools(max_uses: int, fetch: bool) -> list[dict]:
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}]
    if fetch:
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_uses})
    return tools


async def run_structured(
    system: str,
    prompt: str,
    schema: dict,
    *,
    web_searches: int = 0,
    web_fetch: bool = False,
    label: str = "",
) -> dict | None:
    """Run one agent step and return the `submit` tool input (or None on failure)."""
    client, sem = _get()
    submit = {
        "name": "submit",
        "description": "Submit your final answer. Call this exactly once, when you are done.",
        "strict": True,
        "input_schema": schema,
    }
    tools = [submit]
    if web_searches:
        tools = _web_tools(web_searches, web_fetch) + tools

    messages: list[dict] = [{"role": "user", "content": prompt}]
    async with sem:
        for _ in range(6):
            try:
                resp = await client.beta.messages.create(
                    model=MODEL,
                    max_tokens=16000,
                    system=system,
                    tools=tools,
                    messages=messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": EFFORT},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.APIStatusError as e:
                log(f"  ! {label}: API error {e.status_code}: {e.message}")
                return None
            except anthropic.APIConnectionError as e:
                log(f"  ! {label}: connection error: {e}")
                return None

            usage["calls"] += 1
            usage["input_tokens"] += resp.usage.input_tokens
            usage["output_tokens"] += resp.usage.output_tokens
            stu = getattr(resp.usage, "server_tool_use", None)
            if stu and getattr(stu, "web_search_requests", None):
                usage["web_searches"] += stu.web_search_requests

            if resp.stop_reason == "refusal":
                log(f"  ! {label}: refused")
                return None

            for block in resp.content:
                if block.type == "tool_use" and block.name == "submit":
                    return block.input

            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue  # server-side tool loop paused; resend to resume
            # Ended (or hit max_tokens) without submitting: nudge once more.
            messages.append(
                {"role": "user", "content": "Call the submit tool now with your answer, using only what you found."}
            )
    log(f"  ! {label}: no structured answer")
    return None

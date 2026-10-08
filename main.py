"""outreach-agent: find matching companies and draft cold emails for human review.

Usage:
    python main.py "find general contractors ... US" --n 20 [--config startup.yaml]

This tool never sends email. It writes outputs/results.csv and outputs/results.md.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import yaml

from agents import llm
from agents.contacts import find_contacts
from agents.planner import plan
from agents.researcher import research
from agents.reviewer import review
from agents.sourcer import source
from agents.writer import write
from output import write_outputs

OUT = Path(__file__).parent / "outputs"


async def run(request: str, startup: dict, n: int) -> list[dict]:
    p = await plan(request, startup)
    cands = await source(p, request, n)
    cands = cands[: max(n * 2, n + 5)]  # cap research cost
    top = await research(cands, p, n)
    top = await find_contacts(top, startup)
    top = await write(top, startup)
    top = await review(top, startup)
    OUT.mkdir(exist_ok=True)
    (OUT / "run.json").write_text(json.dumps({"request": request, "plan": p, "companies": top, "errors": llm.errors, "usage": llm.usage}, indent=2))
    return write_outputs(top, request, OUT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", help="who you want to meet, in plain English")
    ap.add_argument("--n", type=int, default=20, help="number of companies to keep (default 20)")
    ap.add_argument("--config", default="startup.yaml")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--concurrency", type=int, default=5)
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first.")
    startup = yaml.safe_load(Path(args.config).read_text())
    llm.EFFORT, llm.CONCURRENCY = args.effort, args.concurrency

    rows = asyncio.run(run(args.request, startup, args.n))
    u = llm.usage
    llm.log(f"done: {len(rows)} drafts -> {OUT}/results.csv, results.md")
    llm.log(f"usage: {u['calls']} calls, {u['input_tokens']:,} in / {u['output_tokens']:,} out tokens, "
            f"{u['web_searches']} web searches")


if __name__ == "__main__":
    main()

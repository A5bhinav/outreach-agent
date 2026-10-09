"""outreach-agent: turn a portfolio support request into researched, send-ready outreach drafts.

Usage:
    python main.py "find general contractors ... US" --n 20 --config portfolio/acme.yaml
    python main.py "find systems engineers who've deployed AMR fleets" --n 10 --config portfolio/acme.yaml
    python main.py --opt-out someone@example.com     # never contact again, for any portfolio company

The planner decides whether the request is about meeting companies (customers, partners)
or finding people (e.g. a hire). This tool never sends email. Each run writes
results.csv, results.md, drafts/*.eml and run.json to its own folder under outputs/,
and records every draft in the shared ledger so nobody is contacted twice.
"""
import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

import anthropic
import yaml

from agents import llm
from agents.compliance import Ledger, missing_legal
from agents.contacts import find_contacts
from agents.people import find_people
from agents.planner import more_queries, plan
from agents.researcher import research
from agents.reviewer import review
from agents.sourcer import domain, name_key, source
from agents.writer import write
from output import write_outputs

ROOT = Path(__file__).parent
REQUIRED = ["company_name", "pitch", "sender"]
REQUIRED_SENDER = ["name"]


def _checkpoint(outdir: Path, state: dict) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "run.json").write_text(
        json.dumps({**state, "errors": llm.errors, "usage": llm.usage}, indent=2, default=str), encoding="utf-8")


class Seen:
    """Candidates already handled this run, plus the ledger's exclusions."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.keys: set[str] = set()

    def _keys(self, c: dict) -> set[str]:
        ks = {"n:" + name_key(c["name"])}
        if c.get("domain") and c["domain"] != "unknown":
            ks.add("d:" + c["domain"])
        return ks

    def add(self, cs: list[dict]) -> None:
        for c in cs:
            self.keys |= self._keys(c)

    def __call__(self, c: dict) -> bool:
        return bool(self._keys(c) & self.keys) or self.ledger.skip(c)


async def _companies(request: str, startup: dict, p: dict, n: int, seen: Seen, state: dict, outdir: Path) -> list[dict]:
    cands = (await source(p, request, n, skip=seen))[: max(n * 2, n + 5)]  # cap research cost
    if not cands:
        raise SystemExit(f"sourcer found 0 new candidates; errors: {llm.errors[:3] or 'none logged'}")
    seen.add(cands)
    state["candidates"] = cands
    _checkpoint(outdir, state)
    top = await research(cands, p, n)
    if len(top) < n:  # second round from new angles
        extra_q = await more_queries(p, request, p["queries"],
                                     f"{len(top)} of {len(cands)} researched companies qualified; {n} wanted")
        if extra_q:
            more = (await source(p, request, n - len(top), queries=extra_q, skip=seen))[: max(2 * (n - len(top)), 5)]
            seen.add(more)
            state["candidates"] += more
            state["extra_queries"] = extra_q
            sites = {domain(c["website"]) for c in top if c["website"].startswith("http")}
            extra = [c for c in await research(more, p, n - len(top))
                     if not (c["website"].startswith("http") and domain(c["website"]) in sites)]
            top = sorted(top + extra, key=lambda c: c["fit_score"], reverse=True)[:n]
    if top:
        top = await find_contacts(top, startup, p)
        # A contact found now may match the ledger by email.
        top = [t for t in top if not seen.ledger.skip(t)]
    return top


async def _people(request: str, p: dict, n: int, seen: Seen) -> list[dict]:
    pools = (await source(p, request, n))[: max(5, n)]  # talent-pool companies; the ledger applies to people
    if not pools:
        raise SystemExit(f"sourcer found 0 talent-pool companies; errors: {llm.errors[:3] or 'none logged'}")
    top = await find_people(pools, p, n, skip=seen.ledger.skip)
    if len(top) < n:
        extra_q = await more_queries(p, request, p["queries"], f"{len(top)} people found across {len(pools)} companies; {n} wanted")
        if extra_q:
            names = {name_key(c["name"]) for c in pools}
            more = [c for c in await source(p, request, n, queries=extra_q) if name_key(c["name"]) not in names][: max(5, n)]
            got = {t["name"].lower() for t in top}
            extra = await find_people(more, p, n - len(top),
                                      skip=lambda t: t["name"].lower() in got or seen.ledger.skip(t))
            top += extra
    return top


async def run(request: str, startup: dict, n: int, outdir: Path, ledger: Ledger, preflight: bool = True) -> list[dict]:
    state: dict = {"request": request}
    try:
        if preflight:
            await llm.preflight()
        p = await plan(request, startup)
        state["plan"] = p
        _checkpoint(outdir, state)

        seen = Seen(ledger)
        if p["mode"] == "people":
            top = await _people(request, p, n, seen)
        else:
            top = await _companies(request, startup, p, n, seen, state, outdir)
        if not top:
            raise SystemExit("no usable targets after research (none had fresh sourced facts and met the must-haves)")
        state["targets"] = top
        _checkpoint(outdir, state)

        top = await write(top, startup, p)
        top = await review(top, startup, p)
        state["targets"] = top
        rows = write_outputs(top, request, outdir, startup)
        ledger.record(top)
        return rows
    finally:
        _checkpoint(outdir, state)


def _pos_int(s: str) -> int:
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return v


def _load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists() and not p.is_absolute():
        p = ROOT / path
    if not p.exists():
        sys.exit(f"config not found: {path}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED if not cfg.get(k)]
    missing += [f"sender.{k}" for k in REQUIRED_SENDER if not (cfg.get("sender") or {}).get(k)]
    if missing:
        sys.exit(f"config {p} is missing: {', '.join(missing)}")
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", nargs="?", help="who you want to meet or find, in plain English")
    ap.add_argument("--n", type=_pos_int, default=20, help="number of targets to keep (default 20)")
    ap.add_argument("--config", default="startup.yaml", help="portfolio company profile (YAML)")
    ap.add_argument("--out", default=None, help="output folder (default outputs/<timestamp>)")
    ap.add_argument("--ledger", default=str(ROOT / "ledger.csv"), help="shared contact ledger (default ledger.csv)")
    ap.add_argument("--opt-out", metavar="EMAIL_OR_DOMAIN", help="record an opt-out in the ledger and exit")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--concurrency", type=_pos_int, default=5)
    ap.add_argument("--skip-preflight", action="store_true", help="skip the startup web-search check")
    args = ap.parse_args()

    if args.opt_out:
        Ledger(Path(args.ledger), "").opt_out(args.opt_out)
        print(f"recorded opt-out for {args.opt_out} in {args.ledger}")
        return
    if not args.request:
        ap.error("a request is required")

    startup = _load_config(args.config)
    if "PLACEHOLDER" in yaml.safe_dump(startup):
        llm.log("warning: config still has PLACEHOLDER values; emails using them will be marked not ready to send")
    if missing := missing_legal(startup):
        llm.log(f"warning: config has no {', '.join('sender.' + m for m in missing)}; every email will be blocked "
                "until the legal footer is complete")
    llm.EFFORT, llm.CONCURRENCY = args.effort, args.concurrency
    outdir = Path(args.out) if args.out else ROOT / "outputs" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    ledger = Ledger(Path(args.ledger), startup["company_name"])

    try:
        rows = asyncio.run(run(args.request, startup, args.n, outdir, ledger, preflight=not args.skip_preflight))
    except anthropic.AuthenticationError:
        sys.exit("no valid Anthropic credentials (set ANTHROPIC_API_KEY or run `ant auth login`)")
    except llm.FATAL as e:
        sys.exit(f"stopping: API configuration error ({e.status_code}): {e.message}")
    u = llm.usage
    ready = sum(r["ready_to_send"] == "yes" for r in rows)
    llm.log(f"done: {len(rows)} drafts ({ready} ready to send) -> {outdir}/results.md, results.csv, drafts/")
    llm.log(f"usage: {u['calls']} calls, {u['input_tokens']:,} in / {u['output_tokens']:,} out tokens, "
            f"{u['web_searches']} web searches, {u['web_fetches']} web fetches")


if __name__ == "__main__":
    main()

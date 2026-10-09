"""outreach-agent: turn a portfolio support request into researched, send-ready outreach drafts.

Usage:
    python main.py "find general contractors ... US" --config startup.yaml
    python main.py "find systems engineers who've deployed AMR fleets" --n 10 --config startup.yaml
    python main.py --mark-sent outputs/<run>          # after sending: record them in the ledger
    python main.py --opt-out someone@example.com      # never contact again, for any portfolio company

The planner decides whether the request is about meeting companies (customers, partners)
or finding people (e.g. a hire), and shows you its plan before spending on research. This
tool never sends anything. Each run writes results.html (open it in a browser), results.md,
results.csv, drafts/*.eml and run.json to its own folder under outputs/, updated as each
target finishes.
"""
import argparse
import asyncio
import datetime
import json
import math
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
from agents.reviewer import flag_similar, review_one
from agents.sourcer import name_key, real_domain, source
from agents.writer import write_one
from output import write_outputs

ROOT = Path(__file__).parent
REQUIRED = ["company_name", "pitch", "sender"]
REQUIRED_SENDER = ["name"]


def _checkpoint(outdir: Path, state: dict) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "run.json").write_text(
        json.dumps({**state, "errors": llm.errors, "usage": llm.usage, "usage_by_step": llm.by_step},
                   indent=2, default=str), encoding="utf-8")


class Seen:
    """Candidates already handled this run, plus the ledger's exclusions."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.keys: set[str] = set()

    def _keys(self, c: dict) -> set[str]:
        ks = {"n:" + name_key(c["name"])}
        if d := real_domain(c.get("website", "")):
            ks.add("d:" + d)
        return ks

    def add(self, cs: list[dict]) -> None:
        for c in cs:
            self.keys |= self._keys(c)

    def __call__(self, c: dict) -> bool:
        return bool(self._keys(c) & self.keys) or self.ledger.skip(c)


class Finisher:
    """Per-target pipeline after selection: (contacts →) write → review, with results written as each lands.

    Targets don't wait for each other, and a second search round runs while the first targets finish.
    """

    def __init__(self, request: str, startup: dict, p: dict, outdir: Path, ledger: Ledger, state: dict):
        self.request, self.startup, self.p, self.outdir, self.ledger, self.state = request, startup, p, outdir, ledger, state
        self.tasks: list[asyncio.Task] = []
        self.done: list[dict] = []
        self.expected = 0

    def start(self, targets: list[dict], need_contacts: bool) -> None:
        self.expected += len(targets)
        self.tasks += [asyncio.create_task(self._one(t, need_contacts)) for t in targets]

    async def _one(self, t: dict, need_contacts: bool) -> dict | None:
        if need_contacts:
            t = (await find_contacts([t], self.startup, self.p))[0]
            if reason := self.ledger.skip_reason(t):  # a contact found now may match the ledger by email
                llm.log(f"skip: {t['name']}: {reason}")
                self.expected -= 1
                return None
        t = await write_one(t, self.startup, self.p)
        t = await review_one(t, self.startup, self.p)
        self.done.append(t)
        rows = write_outputs(self.done, self.request, self.outdir, self.startup)
        row = next(r for r in rows if r["target"] == t["name"])
        ready = sum(r["status"] == "ready" for r in rows)
        llm.log(f"{'✓' if row['status'] == 'ready' else '·'} {len(self.done)}/{self.expected} done, {ready} ready: "
                f"{t['name']} ({row['channel']}: {row['status']})")
        return t

    async def finish(self) -> list[dict]:
        for r in await asyncio.gather(*self.tasks, return_exceptions=True):
            if isinstance(r, BaseException):
                llm.log(f"  ! finishing a target failed: {r!r}")
        flag_similar(self.done)
        self.state["targets"] = self.done
        return write_outputs(self.done, self.request, self.outdir, self.startup) if self.done else []


def _not_excluded(targets: list[dict], seen: Seen) -> list[dict]:
    """Research can confirm a homepage the sourcer didn't have; re-check the ledger before paying for contacts."""
    kept = []
    for t in targets:
        if reason := seen.ledger.skip_reason(t):
            llm.log(f"skip: {t['name']}: {reason}")
        else:
            kept.append(t)
    return kept


async def _companies(request: str, p: dict, n: int, seen: Seen, fin: Finisher, state: dict, outdir: Path) -> int:
    cands = await source(p, request, n, skip=seen)
    if not cands:
        raise SystemExit(f"sourcer found 0 new candidates; errors: {llm.errors[:3] or 'none logged'}")
    seen.add(cands)
    cap = max(math.ceil(1.5 * n) + 2, n + 3)  # best likely-fit first; the rest is a reserve for round 2
    first, reserve = cands[:cap], cands[cap:]
    state["candidates"] = cands
    _checkpoint(outdir, state)
    top = _not_excluded(await research(first, p, n), seen)
    fin.start(top, need_contacts=True)  # writing starts now; round 2 (if any) runs alongside
    got = len(top)
    sites = {real_domain(c["website"]) for c in top} - {""}

    async def more(cs: list[dict], want: int) -> int:
        nonlocal sites
        extra = [c for c in _not_excluded(await research(cs, p, want), seen)
                 if real_domain(c["website"]) not in sites][:want]
        sites |= {real_domain(c["website"]) for c in extra} - {""}
        fin.start(extra, need_contacts=True)
        return len(extra)

    if got < n and reserve:
        got += await more(reserve[: 2 * (n - got) + 2], n - got)
    if got < n:
        extra_q = await more_queries(p, request, p["queries"], f"{got} of {len(cands)} candidates qualified; {n} wanted")
        if extra_q:
            new = await source(p, request, n - got, queries=extra_q, skip=seen)
            seen.add(new)
            state["candidates"] += new
            state["extra_queries"] = extra_q
            got += await more(new[: 2 * (n - got) + 2], n - got)
    return got


async def _people(request: str, p: dict, n: int, seen: Seen, fin: Finisher) -> int:
    pools = (await source(p, request, n))[: max(5, n // 2 + 2)]  # talent-pool companies; the ledger applies to people
    if not pools:
        raise SystemExit(f"sourcer found 0 talent-pool companies; errors: {llm.errors[:3] or 'none logged'}")
    top = await find_people(pools, p, n, skip=seen.ledger.skip)
    fin.start(top, need_contacts=False)
    got = len(top)
    if got < n:
        extra_q = await more_queries(p, request, p["queries"], f"{got} people found across {len(pools)} companies; {n} wanted")
        if extra_q:
            names = {name_key(c["name"]) for c in pools}
            short = n - got
            more = [c for c in await source(p, request, short, queries=extra_q) if name_key(c["name"]) not in names]
            have = {t["name"].lower() for t in top}
            extra = await find_people(more[: max(3, math.ceil(short / 2) + 1)], p, short,
                                      skip=lambda t: t["name"].lower() in have or seen.ledger.skip(t))
            fin.start(extra, need_contacts=False)
            got += len(extra)
    return got


def _show_plan(p: dict, n: int) -> None:
    lines = [f"\nPlan (mode: {p['mode']}, keeping up to {n}):", "  Queries:"]
    lines += [f"    - {q}" for q in p["queries"]]
    lines += ["  Rubric (* = must-have):"] + [f"    {'*' if r['must_have'] else ' '} {r['criterion']}" for r in p["rubric"]]
    lines += [f"  Disqualifiers: {'; '.join(p['disqualifiers'])}", f"  Target roles: {', '.join(p['target_titles'])}",
              f"  Ask: {p['ask']}"]
    heavy = (math.ceil(1.5 * n) + 2 if p["mode"] == "companies" else max(5, n // 2 + 2)) + 3 * n
    lines.append(f"  Estimate: ~{heavy + len(p['queries']) + n} Claude calls ({heavy} on the heavy model), "
                 f"~{4 * heavy} web searches, {max(5, n)}-{3 * max(5, n)} minutes.\n")
    print("\n".join(lines), file=sys.stderr, flush=True)


async def run(request: str, startup: dict, n: int, outdir: Path, ledger: Ledger, preflight: bool = True,
              confirm=None) -> list[dict]:
    state: dict = {"request": request}
    try:
        if preflight:
            await llm.preflight()
        else:
            llm.STRICT_400 = False
        p = await plan(request, startup)
        state["plan"] = p
        _checkpoint(outdir, state)
        if confirm and not confirm(p):
            raise SystemExit("stopped after planning; nothing else was spent")

        seen = Seen(ledger)
        fin = Finisher(request, startup, p, outdir, ledger, state)
        if p["mode"] == "people":
            await _people(request, p, n, seen, fin)
        else:
            await _companies(request, p, n, seen, fin, state, outdir)
        rows = await fin.finish()
        if not rows:
            raise SystemExit("no usable targets after research (none had fresh sourced facts and met the must-haves)")
        ready = {r["target"] for r in rows if r["status"] == "ready"}
        ledger.record([t for t in fin.done if t["name"] in ready], run=str(outdir.resolve()))
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


def _confirm(n: int, yes: bool, plan_only: bool):
    def ask(_p: dict) -> bool:
        _show_plan(_p, n)
        if plan_only:
            return False
        if yes or not sys.stdin.isatty():
            return True
        return input("Proceed? [Y/n] ").strip().lower() in ("", "y", "yes")
    return ask


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", nargs="?", help="who you want to meet or find, in plain English")
    ap.add_argument("--n", type=_pos_int, default=5, help="number of targets to keep (default 5)")
    ap.add_argument("--config", default="startup.yaml", help="portfolio company profile (YAML)")
    ap.add_argument("--out", default=None, help="output folder (default outputs/<timestamp>)")
    ap.add_argument("--ledger", default=str(ROOT / "ledger.csv"), help="shared contact ledger (default ledger.csv)")
    ap.add_argument("--opt-out", metavar="EMAIL_OR_DOMAIN", help="record an opt-out in the ledger and exit")
    ap.add_argument("--mark-sent", metavar="RUN_DIR", help="mark a run's ready drafts as sent in the ledger and exit")
    ap.add_argument("--only", default="", help="with --mark-sent: comma-separated target names (default all)")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"],
                    help="effort for research and people steps (others use fixed, cheaper levels)")
    ap.add_argument("--concurrency", type=_pos_int, default=8)
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation after the plan")
    ap.add_argument("--plan-only", action="store_true", help="show the plan and stop")
    ap.add_argument("--allow-placeholders", action="store_true",
                    help="run even though the config has PLACEHOLDER values (every email will be blocked)")
    ap.add_argument("--skip-preflight", action="store_true", help="skip the startup web-search check")
    args = ap.parse_args()

    if args.opt_out:
        Ledger(Path(args.ledger), "").opt_out(args.opt_out)
        print(f"recorded opt-out for {args.opt_out} in {args.ledger}")
        return
    if args.mark_sent:
        only = {s.strip() for s in args.only.split(",") if s.strip()} or None
        n = Ledger(Path(args.ledger), "").mark_sent(str(Path(args.mark_sent).resolve()), only)
        print(f"marked {n} draft(s) from {args.mark_sent} as sent")
        return
    if not args.request:
        ap.error("a request is required")

    startup = _load_config(args.config)
    problems = []
    if "PLACEHOLDER" in yaml.safe_dump(startup):
        problems.append("it still has PLACEHOLDER values")
    if missing := missing_legal(startup):
        problems.append(f"the email footer needs {', '.join(missing)}")
    if problems and not (args.allow_placeholders or args.plan_only):
        sys.exit(f"fix {args.config} first: {'; '.join(problems)}. Every email would be blocked, so nothing was "
                 "spent. (Use --plan-only to preview, or --allow-placeholders for a dry run.)")
    llm.EFFORT, llm.CONCURRENCY = args.effort, args.concurrency
    outdir = Path(args.out) if args.out else ROOT / "outputs" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    ledger = Ledger(Path(args.ledger), startup["company_name"])

    try:
        rows = asyncio.run(run(args.request, startup, args.n, outdir, ledger, preflight=not args.skip_preflight,
                               confirm=_confirm(args.n, args.yes, args.plan_only)))
    except anthropic.AuthenticationError:
        sys.exit("no valid Anthropic credentials (set ANTHROPIC_API_KEY or run `ant auth login`)")
    except (*llm.FATAL, anthropic.BadRequestError) as e:
        sys.exit(f"stopping: API configuration error ({e.status_code}): {e.message}")
    ready = sum(r["status"] == "ready" for r in rows)
    llm.log(f"done: {len(rows)} targets, {ready} ready to send -> open {outdir / 'results.html'}")
    llm.log(llm.summary())


if __name__ == "__main__":
    main()

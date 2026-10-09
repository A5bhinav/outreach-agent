"""Step 3 (companies mode): pull recent, sourced facts per candidate and judge each rubric criterion."""
import asyncio

from .evidence import fresh, judge, numbered
from .llm import log, run_structured
from .schema import ASSESSMENTS, FACTS, STR, obj
from .sourcer import domain

SCHEMA = obj(
    website=STR,
    facts=FACTS,
    assessments=ASSESSMENTS,
    location=STR,
    size_hint=STR,
    fit_reason=STR,
    disqualified=STR,
)

SYSTEM = (
    "You research one company from public web pages. Find 2-3 specific facts relevant to the rubric "
    "and the research signals, preferring recent events (the last 18 months) that give a reason to "
    "reach out now. Each fact must be something you actually read, stated plainly, with the exact URL "
    "it came from and its date as shown on the page (else \"undated\"). Never infer or embellish.\n"
    "assessments: one entry per numbered rubric criterion: verdict yes (a page you read shows it; give "
    "its URL), no (a page shows it doesn't hold; give its URL), or unclear (not enough evidence; "
    "evidence_url \"none\"). Don't guess a yes.\n"
    "fit_reason is one line. disqualified is \"no\" unless a listed disqualifier clearly applies, in "
    "which case name it. website is the company's own homepage URL (confirm or find it), or \"unknown\"."
)


def _dq(c: dict) -> bool:
    return c.get("disqualified", "no").strip().lower() not in ("", "no", "none")


async def _one(c: dict, p: dict, i: int, total: int) -> dict:
    prompt = (
        f"Company: {c['name']}\nWebsite: {c['website']}\nFound via: {', '.join(c['source_urls'])}\n"
        f"Known location: {c.get('location', 'unknown')}; size hint: {c.get('size_hint', 'unknown')}\n\n"
        f"Rubric:\n{numbered(p['rubric'])}\nResearch signals: {p['research_signals']}\n"
        f"Disqualifiers: {p['disqualifiers']}\n"
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=4, web_fetch=True, label=f"research[{c['name']}]")
    if not out:
        return {**c, "facts": [], "fit": judge([], p["rubric"]), "fit_score": 0,
                "fit_reason": "research failed", "disqualified": "no"}
    out["facts"] = fresh(out["facts"])  # sourced and not stale
    fit = judge(out.pop("assessments"), p["rubric"])
    if not out["website"].startswith("http"):
        out["website"] = c["website"]
    merged = {**c, **out, "fit": fit, "fit_score": fit["fit_score"], "source_urls": list(c["source_urls"])}
    for f in out["facts"]:
        if f["source_url"] not in merged["source_urls"]:
            merged["source_urls"].append(f["source_url"])
    log(f"researcher: ({i}/{total}) {c['name']}: {fit['met']}/{fit['total']} criteria, "
        f"must-haves {'met' if fit['must_haves_met'] else 'NOT met'}, {len(out['facts'])} fresh facts"
        + (f", disqualified: {out['disqualified']}" if _dq(out) else ""))
    return merged


async def research(cands: list[dict], p: dict, n: int) -> list[dict]:
    """Research every candidate; return those that pass: fresh sourced facts, every must-have, no disqualifier."""
    log(f"researcher: researching {len(cands)} candidates")
    total = len(cands)
    done = await asyncio.gather(*(_one(c, p, i + 1, total) for i, c in enumerate(cands)))
    usable = [c for c in done if c["facts"] and not _dq(c) and c["fit"]["must_haves_met"]]
    # Several unconfirmed-homepage entries can resolve to the same site; keep the best.
    seen, deduped = set(), []
    for c in sorted(usable, key=lambda c: c["fit_score"], reverse=True):
        d = domain(c["website"]) if c["website"].startswith("http") else c["name"].lower()
        if d not in seen:
            seen.add(d)
            deduped.append(c)
    log(f"researcher: {len(deduped)} of {total} pass (fresh sourced facts, all must-haves, not disqualified)")
    return deduped[:n]

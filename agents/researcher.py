"""Step 3: pull recent, sourced facts per candidate and score fit against the rubric."""
import asyncio

from .llm import log, run_structured
from .schema import INT, STR, arr, obj

SCHEMA = obj(
    facts=arr(obj(fact=STR, date=STR, source_url=STR)),
    location=STR,
    size_hint=STR,
    fit_score=INT,
    fit_reason=STR,
)

SYSTEM = (
    "You research one company from public web pages. Find 2-3 specific, recent facts relevant to "
    "the rubric (recent projects, expansions, hiring, news, warehouse/yard/logistics operations, "
    "prefab or materials-handling activity). Each fact must be something you actually read, stated "
    "plainly, with the exact URL it came from and its date if shown (else \"undated\"). Never infer "
    "or embellish. If you find little, return fewer facts and score lower. fit_score is 1-10 "
    "against the rubric; fit_reason is one line."
)


async def _one(c: dict, rubric: list, disq: list, i: int, total: int) -> dict:
    prompt = (
        f"Company: {c['name']}\nWebsite: {c['website']}\nFound via: {', '.join(c['source_urls'])}\n"
        f"Known location: {c.get('location', 'unknown')}; size hint: {c.get('size_hint', 'unknown')}\n\n"
        f"Fit rubric: {rubric}\nDisqualifiers: {disq}\n"
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=4, web_fetch=True, label=f"research[{c['name']}]")
    if not out:
        return {**c, "facts": [], "fit_score": 0, "fit_reason": "research failed"}
    out["fit_score"] = max(1, min(10, out["fit_score"]))
    log(f"researcher: ({i}/{total}) {c['name']}: fit {out['fit_score']}, {len(out['facts'])} facts")
    merged = {**c, **out}
    for f in out["facts"]:
        if f["source_url"] and f["source_url"] not in merged["source_urls"]:
            merged["source_urls"].append(f["source_url"])
    return merged


async def research(cands: list[dict], plan: dict, n: int) -> list[dict]:
    log(f"researcher: researching {len(cands)} candidates")
    total = len(cands)
    done = await asyncio.gather(
        *(_one(c, plan["rubric"], plan["disqualifiers"], i + 1, total) for i, c in enumerate(cands))
    )
    # A company with no sourced facts can't get a grounded email, so it's dropped.
    usable = [c for c in done if c["facts"]]
    usable.sort(key=lambda c: c["fit_score"], reverse=True)
    top = usable[:n]
    log(f"researcher: kept top {len(top)} of {len(usable)} with sourced facts")
    return top

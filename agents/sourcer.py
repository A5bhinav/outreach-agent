"""Step 2: run each query with web search, collect candidate companies, dedupe by domain and name."""
import asyncio
import re
from itertools import zip_longest
from typing import Callable
from urllib.parse import urlparse

from .llm import log, run_structured
from .schema import STR, arr, obj

SCHEMA = obj(
    companies=arr(
        obj(name=STR, website=STR, location=STR, size_hint=STR, source_url=STR)
    )
)

SYSTEM = (
    "You find real companies on the web. Only list a company if you saw it in a search result or "
    "page during this task. website must be the company's own homepage URL (not LinkedIn, a news "
    "site or a directory), or \"unknown\" if you didn't see it. source_url must be the page where you found it. Use \"unknown\" for "
    "location or size_hint if no page states it. Never guess or invent anything."
)

# Hosts that are never a company's own site; a "website" on one of these would merge unrelated firms.
AGGREGATORS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "wikipedia.org", "enr.com", "bizjournals.com", "crunchbase.com", "zoominfo.com", "bloomberg.com",
    "finance.yahoo.com", "glassdoor.com", "indeed.com", "github.com", "medium.com", "google.com",
}
_SUFFIX = re.compile(r"\b(inc|llc|ltd|co|corp|corporation|company|group|the)\b|[^a-z0-9]")


def domain(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def _aggregator(d: str) -> bool:
    return any(d == a or d.endswith("." + a) for a in AGGREGATORS)


def name_key(name: str) -> str:
    return _SUFFIX.sub("", name.lower())


async def _one(query: str, request: str, p: dict, per_query: int) -> list[dict]:
    what = ("companies that match the goal" if p["mode"] == "companies"
            else "companies or teams likely to employ (or have employed) the kind of person sought")
    prompt = (
        f"Overall goal: {request}\n"
        f"Fit rubric: {p['rubric']}\nDisqualifiers (skip these): {p['disqualifiers']}\n\n"
        f"Search query to run (you may refine it): {query}\n\n"
        f"Return up to {per_query} individual {what}. Skip directories, associations, publishers and "
        "list sites themselves."
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=4, model="light", label=f"sourcer[{query[:40]}]")
    if out is None:
        log(f"  ! sourcer: call failed for '{query[:60]}'")
        return []
    found = out["companies"]
    log(f"sourcer: {len(found):2d} from '{query[:60]}'")
    return found


async def source(p: dict, request: str, n: int, queries: list[str] | None = None,
                 skip: Callable[[dict], bool] | None = None) -> list[dict]:
    """Run the queries; `skip` drops candidates already seen or excluded (ledger, earlier rounds)."""
    queries = queries or p["queries"]
    per_query = max(5, (2 * n) // len(queries) + 2)
    log(f"sourcer: running {len(queries)} queries concurrently")
    batches = await asyncio.gather(*(_one(q, request, p, per_query) for q in queries))

    # Interleave so a later cap keeps candidates from every query angle, not just the first ones.
    flat = [c for row in zip_longest(*batches) for c in row if c]
    seen: dict[str, dict] = {}
    names: dict[str, str] = {}
    for c in flat:
        nk = name_key(c["name"])
        d = domain(c.get("website", ""))
        if not d or "." not in d or _aggregator(d):
            # No confirmed homepage yet: keep it (the researcher finds the site), keyed by name.
            if not nk:
                continue
            c = {**c, "website": "unknown"}
            d = names.get(nk, "name:" + nk)
        key = names.get(nk, d) if nk else d
        if key in seen:
            if c["source_url"] not in seen[key]["source_urls"]:
                seen[key]["source_urls"].append(c["source_url"])
            continue
        seen[d] = {**c, "domain": d if not d.startswith("name:") else "unknown", "source_urls": [c["source_url"]]}
        if nk:
            names[nk] = d
    cands = list(seen.values())
    if skip:
        before = len(cands)
        cands = [c for c in cands if not skip(c)]
        if before - len(cands):
            log(f"sourcer: skipped {before - len(cands)} already seen, contacted or opted out")
    log(f"sourcer: {len(cands)} unique candidates after dedupe")
    return cands

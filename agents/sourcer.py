"""Step 2: run each query with web search, collect candidate companies, dedupe by domain and name, rank."""
import asyncio
import re
from itertools import zip_longest
from typing import Callable
from urllib.parse import urlparse

from .llm import log, run_structured
from .schema import STR, arr, enum, obj

SCHEMA = obj(
    companies=arr(
        obj(name=STR, website=STR, location=STR, size_hint=STR, source_url=STR, likely_fit=enum("high", "medium", "low"))
    )
)

SYSTEM = (
    "You find real companies on the web. Only list a company if you saw it in a search result or "
    "page during this task. website must be the company's own homepage URL (not LinkedIn, a news "
    "site, a press-release wire or a directory), or \"unknown\" if you didn't see it. source_url must "
    "be the page where you found it. Use \"unknown\" for location or size_hint if no page states it. "
    "likely_fit is your quick read of how well it matches the goal and rubric from what you saw "
    "(high, medium, low). Never guess or invent anything."
)

# Hosts that are never a company's own site; a "website" on one of these would merge unrelated firms.
AGGREGATORS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "wikipedia.org", "enr.com", "bizjournals.com", "crunchbase.com", "zoominfo.com", "bloomberg.com",
    "finance.yahoo.com", "glassdoor.com", "indeed.com", "github.com", "medium.com", "google.com",
    "prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com", "einpresswire.com",
    "yelp.com", "bbb.org", "houzz.com", "dnb.com", "manta.com", "buildzoom.com", "angi.com",
    "mapquest.com", "yellowpages.com", "apollo.io", "rocketreach.co", "owler.com", "pitchbook.com",
}
_SUFFIX = re.compile(r"\b(inc|llc|ltd|co|corp|corporation|company|group|the|gmbh|ag|plc|sa|bv)\b|[^\w]")
_RANK = {"high": 0, "medium": 1, "low": 2}


def domain(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def real_domain(url: str) -> str:
    """The company domain for a URL, or "" if it isn't a usable company homepage."""
    if not url or url == "unknown":
        return ""
    d = domain(url)
    return d if "." in d and not any(d == a or d.endswith("." + a) for a in AGGREGATORS) else ""


def name_key(name: str) -> str:
    """Normalized company name for dedupe; falls back to the lowercased name when only suffixes remain."""
    k = _SUFFIX.sub("", name.lower()).replace("_", "")
    return k or re.sub(r"\s+", " ", name.lower().strip())


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
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=3, model="light", effort="low",
                               max_tokens=8000, label=f"sourcer[{query[:40]}]")
    if out is None:
        log(f"  ! sourcer: call failed for '{query[:60]}'")
        return []
    found = out["companies"]
    log(f"sourcer: {len(found):2d} from '{query[:60]}'")
    return found


async def source(p: dict, request: str, n: int, queries: list[str] | None = None,
                 skip: Callable[[dict], bool] | None = None) -> list[dict]:
    """Run the queries and return deduped candidates, best likely fit first.

    `skip` drops candidates already seen or excluded (ledger, earlier rounds).
    """
    queries = queries or p["queries"]
    per_query = max(5, (2 * n) // len(queries) + 2)
    log(f"sourcer: running {len(queries)} queries concurrently")
    batches = await asyncio.gather(*(_one(q, request, p, per_query) for q in queries))

    # Interleave so every query angle is represented before ranking.
    flat = [c for row in zip_longest(*batches) for c in row if c]
    seen: dict[str, dict] = {}
    names: dict[str, str] = {}  # name key -> seen key
    for c in flat:
        nk, d = name_key(c["name"]), real_domain(c.get("website", ""))
        if d and d in seen:
            key = d
        elif nk in names:
            key = names[nk]
        else:
            key = d or "name:" + nk
        if key in seen:
            e = seen[key]
            if c["source_url"] not in e["source_urls"]:
                e["source_urls"].append(c["source_url"])
            e["likely_fit"] = min(e["likely_fit"], c.get("likely_fit", "low"), key=_RANK.get)
            if d and e["domain"] == "unknown":
                # A later result confirmed the homepage: upgrade the name-only entry and re-key it.
                del seen[key]
                e.update(website=c["website"], domain=d)
                seen[d] = e
                names[nk] = d
            continue
        seen[key] = {**c, "website": c["website"] if d else "unknown", "domain": d or "unknown",
                     "source_urls": [c["source_url"]], "likely_fit": c.get("likely_fit", "low")}
        names[nk] = key
    cands = list(seen.values())
    if skip:
        before = len(cands)
        cands = [c for c in cands if not skip(c)]
        if before - len(cands):
            log(f"sourcer: skipped {before - len(cands)} already seen, contacted or opted out")
    # Best likely fit first, then the most corroborated; stable, so query interleave breaks ties.
    cands.sort(key=lambda c: (_RANK[c["likely_fit"]], -len(c["source_urls"])))
    log(f"sourcer: {len(cands)} unique candidates after dedupe "
        f"({sum(c['likely_fit'] == 'high' for c in cands)} likely high fit)")
    return cands

"""Step 2: run each query with web search, collect candidate companies, dedupe by domain."""
import asyncio
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
    "page during this task. website must be the company's own homepage URL. source_url must be the "
    "page where you found it. Use \"unknown\" for location or size_hint if no page states it. "
    "Never guess or invent anything."
)


def domain(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower().split(":")[0]
    return host.removeprefix("www.")


async def _one(query: str, request: str, rubric: list, per_query: int) -> list[dict]:
    prompt = (
        f"Overall goal: {request}\n"
        f"Fit rubric: {rubric}\n\n"
        f"Search query to run (you may refine it): {query}\n\n"
        f"Return up to {per_query} individual companies that match the goal. Skip directories, "
        "associations, publishers and software vendors."
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=4, label=f"sourcer[{query[:40]}]")
    found = (out or {}).get("companies", [])
    log(f"sourcer: {len(found):2d} from '{query[:60]}'")
    return found


async def source(plan: dict, request: str, n: int) -> list[dict]:
    per_query = max(5, (2 * n) // len(plan["queries"]) + 2)
    log(f"sourcer: running {len(plan['queries'])} queries concurrently")
    batches = await asyncio.gather(*(_one(q, request, plan["rubric"], per_query) for q in plan["queries"]))

    seen: dict[str, dict] = {}
    for c in (c for b in batches for c in b):
        d = domain(c.get("website", ""))
        if not d or "." not in d:
            continue
        if d in seen:
            if c["source_url"] not in seen[d]["source_urls"]:
                seen[d]["source_urls"].append(c["source_url"])
            continue
        seen[d] = {**c, "domain": d, "source_urls": [c["source_url"]]}
    cands = list(seen.values())
    log(f"sourcer: {len(cands)} unique candidates after domain dedupe")
    return cands

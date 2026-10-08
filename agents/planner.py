"""Step 1: turn the request into search queries and a fit rubric."""
import yaml

from .llm import log, run_structured
from .schema import STR, arr, obj

SCHEMA = obj(
    queries=arr(STR),
    rubric=arr(obj(criterion=STR, why=STR)),
    disqualifiers=arr(STR),
)

SYSTEM = (
    "You plan B2B prospecting research. Write web search queries that surface individual "
    "companies (their own websites, trade press, project announcements, 'top firms' lists), "
    "not generic articles. Each query should come at the target from a different angle "
    "(region, sector, project type, signal such as expansion or hiring)."
)


async def plan(request: str, startup: dict) -> dict:
    log("planner: building queries + rubric")
    prompt = (
        f"Startup profile:\n{yaml.safe_dump(startup, sort_keys=False)}\n"
        f"Request: {request}\n\n"
        "Produce 5-8 distinct search queries, a fit rubric of 4-6 criteria describing what makes a "
        "company a strong pilot customer or partner for this startup, and a short list of disqualifiers."
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, label="planner")
    if not out:
        raise SystemExit("planner failed; see log above")
    out["queries"] = out["queries"][:8]
    log(f"planner: {len(out['queries'])} queries, {len(out['rubric'])} rubric criteria")
    return out

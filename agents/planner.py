"""Step 1: turn the request into a mode, search queries, a fit rubric and who/what to target."""
import yaml

from .llm import log, run_structured
from .schema import BOOL, STR, arr, enum, obj

SCHEMA = obj(
    mode=enum("companies", "people"),
    queries=arr(STR),
    rubric=arr(obj(criterion=STR, why=STR, must_have=BOOL)),
    disqualifiers=arr(STR),
    target_titles=arr(STR),
    research_signals=arr(STR),
    ask=STR,
)

SYSTEM = (
    "You plan prospecting research for a portfolio startup. First decide the mode:\n"
    "- companies: the startup wants to meet businesses (customers, pilot sites, partners, channels). "
    "Queries surface individual target companies (their own sites, trade press, project "
    "announcements, 'top firms' lists), not generic articles.\n"
    "- people: the startup wants to hire or meet a specific kind of person (e.g. an engineer with a "
    "given background). Queries surface companies and teams that would employ or have employed "
    "people like that (talent pools), so individual profiles can be found there next.\n"
    "Each query should come at the target from a different angle (region, sector, project type, a "
    "technology used). At least two queries must look for recent trigger events (expansions, new "
    "facilities, project wins, leadership hires, funding, relevant job postings) so each email has a "
    "reason to write now.\n"
    "rubric: 4-6 checkable criteria for a strong target (a company in companies mode, a person in "
    "people mode). Mark must_have=true only for the 1-3 criteria without which the target is pointless "
    "to contact. In people mode, criteria are about demonstrated work and skills only, never about "
    "age, gender, ethnicity or other personal traits.\n"
    "disqualifiers: short list of things that rule a target out.\n"
    "target_titles: in companies mode, the roles of the decision-makers worth contacting; in people "
    "mode, the job titles of the people being sought.\n"
    "research_signals: the specific, checkable things worth looking for about each target.\n"
    "ask: one concrete, low-friction ask that fits the request, such as whether they'd like an intro "
    "to the founder for a short call. Don't presume facts about the recipient in it."
)

MORE = (
    "You extend a prospecting search that found too few qualifying targets. Write new web search "
    "queries that come at the same goal from angles not yet tried (other regions, adjacent sectors, "
    "other trigger events, other lists or sources). Don't repeat or lightly reword earlier queries."
)


async def plan(request: str, startup: dict) -> dict:
    log("planner: building queries + rubric")
    prompt = (
        f"Startup profile:\n{yaml.safe_dump(startup, sort_keys=False)}\n"
        f"Request: {request}\n\n"
        "Produce the plan: 5-8 distinct search queries plus the other fields."
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, model="light", label="planner")
    if not out:
        raise SystemExit("planner failed; see log above")
    out["queries"] = [q for q in out["queries"] if q.strip()][:8]
    if not out["queries"]:
        raise SystemExit("planner returned no search queries")
    if not out["rubric"]:
        raise SystemExit("planner returned no rubric")
    log(f"planner: mode={out['mode']}, {len(out['queries'])} queries, {len(out['rubric'])} rubric criteria "
        f"({sum(r['must_have'] for r in out['rubric'])} must-have)")
    return out


async def more_queries(p: dict, request: str, tried: list[str], shortfall: str) -> list[str]:
    prompt = (
        f"Request: {request}\nFit rubric: {p['rubric']}\nDisqualifiers: {p['disqualifiers']}\n"
        f"Queries already run:\n- " + "\n- ".join(tried) + f"\n\nWhat happened: {shortfall}\n\n"
        "Write 3-5 new queries."
    )
    out = await run_structured(MORE, prompt, obj(queries=arr(STR)), model="light", label="planner[more]")
    new = [q for q in (out or {}).get("queries", []) if q.strip() and q not in tried][:5]
    log(f"planner: {len(new)} extra queries for a second round")
    return new

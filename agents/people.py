"""Step 3 (people mode): find individual people at each talent-pool company, with dated, sourced evidence.

This is a sourcing aid, not a screening tool: each person gets per-criterion evidence, not a
score, nobody is rejected on an "unclear", and a human decides whom to contact. LinkedIn
profiles are only ever shown as links; GitHub is evidence of work, never an email source.
"""
import asyncio

from .contacts import email_allowed, verify_contact
from .evidence import fresh, judge, numbered
from .llm import log, run_structured
from .schema import ASSESSMENTS, FACTS, STR, arr, obj

SCHEMA = obj(
    people=arr(
        obj(
            name=STR,
            current_title=STR,
            current_company=STR,
            location=STR,
            profile_url=STR,
            email=STR,
            email_source_url=STR,
            facts=FACTS,
            assessments=ASSESSMENTS,
            fit_reason=STR,
        )
    )
)

SYSTEM = (
    "You find specific people whose background fits a role, starting from one company or team where "
    "people like that work or have worked. Use public pages: company team pages, engineering blogs, "
    "conference talks, papers, patents, GitHub, personal sites, news. Only report a person whose name "
    "and work you actually read on a page during this task; profile_url is the best page about them "
    "(a LinkedIn URL from search results is fine as a link, but don't take facts from LinkedIn). For "
    "each, give 1-3 facts about their work (what they built, shipped, wrote or presented) with the "
    "exact URL each came from and its date as shown (else \"undated\"); prefer recent work. "
    "current_company may differ from the starting company (alumni count).\n"
    "assessments: one entry per numbered rubric criterion: yes (a page shows it; give its URL), no "
    "(a page shows it doesn't hold), or unclear (evidence_url \"none\").\n"
    "email: only an address you read verbatim on a public page, with that page in email_source_url; "
    "never take one from GitHub or LinkedIn and never construct one from a name pattern; otherwise "
    "\"unknown\" for both. Don't infer or mention gender, age, ethnicity or other personal traits. "
    "fit_reason is one line about their work. Never guess or invent anything."
)


async def _one(c: dict, p: dict, per_company: int) -> list[dict]:
    prompt = (
        f"Starting company/team: {c['name']} ({c['website']})\n"
        f"Role sought: {p['target_titles']}\nRubric:\n{numbered(p['rubric'])}\n"
        f"Signals worth finding: {p['research_signals']}\nDisqualifiers: {p['disqualifiers']}\n\n"
        f"Return up to {per_company} people."
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=5, web_fetch=True, label=f"people[{c['name']}]")
    if out is None:
        log(f"  ! people: call failed for {c['name']}")
        return []
    targets = []
    for person in out["people"]:
        facts = fresh(person["facts"])
        if not facts or not person["profile_url"].startswith("http"):
            continue
        fit = judge(person["assessments"], p["rubric"])
        if fit["must_have_failed"]:  # only a clear "no" on a must-have rules someone out
            continue
        contact = {
            "name": person["name"], "title": person["current_title"], "source_url": person["profile_url"],
            "email": person["email"] or "unknown", "email_source_url": person["email_source_url"] or "unknown",
            "suggested_title": person["current_title"], "verified": "no",
        }
        if not email_allowed(contact["email"], contact["email_source_url"]):
            contact.update(email="unknown", email_source_url="unknown")
        targets.append({
            "name": person["name"],
            "company": person["current_company"],
            "website": person["profile_url"],
            "domain": "",
            "location": person["location"],
            "found_via": c["name"],
            "source_urls": [person["profile_url"]] + [f["source_url"] for f in facts if f["source_url"] != person["profile_url"]],
            "facts": facts,
            "fit": fit,
            "fit_score": fit["fit_score"],  # used only to order the list; not shown as a score
            "fit_reason": person["fit_reason"],
            "contact": contact,
        })
    log(f"people: {len(targets)} sourced people via {c['name']}")
    return targets


async def find_people(companies: list[dict], p: dict, n: int, skip=None) -> list[dict]:
    per_company = max(2, min(5, (2 * n) // max(1, len(companies)) + 1))
    log(f"people: searching {len(companies)} talent-pool companies, up to {per_company} each")
    batches = await asyncio.gather(*(_one(c, p, per_company) for c in companies))
    seen: dict[str, dict] = {}
    names: set[str] = set()
    for t in (t for b in batches for t in b):
        key = t["contact"]["source_url"].rstrip("/").lower()
        name_key = t["name"].lower().strip()
        if key in seen or name_key in names or (skip and skip(t)):
            continue
        seen[key] = t
        names.add(name_key)
    ranked = sorted(seen.values(), key=lambda t: (t["fit"]["must_haves_met"], t["fit"]["met"]), reverse=True)
    top = ranked[:n]
    for t, ct in zip(top, await asyncio.gather(*(verify_contact(t["contact"]) for t in top))):
        t["contact"] = ct
    log(f"people: kept {len(top)} of {len(seen)} unique people")
    return top

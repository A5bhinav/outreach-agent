"""Step 4: find a named decision-maker only if a public page shows one. No emails, ever."""
import asyncio
import re

from .llm import log, run_structured
from .schema import STR, obj

SCHEMA = obj(name=STR, title=STR, source_url=STR, suggested_title=STR)

SYSTEM = (
    "You look for one plausible decision-maker at a company for a robotics / operations pilot "
    "(e.g. COO, VP Operations, Director of Preconstruction, Logistics or Equipment Manager, "
    "Innovation lead). Only report a person whose name AND title you actually read on a public page "
    "(company about/team/leadership page, press release, news article) and give that page's URL. "
    "If you did not find one, set name, title and source_url to \"unknown\". Always set "
    "suggested_title to the role worth targeting. Never guess names. Never look for or output "
    "email addresses or phone numbers."
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


async def _one(c: dict, pitch: str) -> dict:
    prompt = f"Company: {c['name']}\nWebsite: {c['website']}\nWhat the startup offers: {pitch}"
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=3, web_fetch=True, label=f"contact[{c['name']}]")
    out = out or {"name": "unknown", "title": "unknown", "source_url": "unknown", "suggested_title": "VP Operations"}
    # Belt and braces: an unsourced name is treated as not found, and no emails survive.
    if not out["source_url"].startswith("http") or out["name"].lower() == "unknown":
        out.update(name="unknown", title="unknown", source_url="unknown")
    for k in out:
        out[k] = _EMAIL.sub("", out[k]).strip() or "unknown"
    log(f"contacts: {c['name']}: {out['name']} ({out['title'] if out['name'] != 'unknown' else 'target ' + out['suggested_title']})")
    return {**c, "contact": out}


async def find_contacts(companies: list[dict], startup: dict) -> list[dict]:
    log(f"contacts: searching {len(companies)} companies")
    return list(await asyncio.gather(*(_one(c, startup["pitch"]) for c in companies)))

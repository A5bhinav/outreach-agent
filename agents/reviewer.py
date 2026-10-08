"""Step 6: flag any claim not supported by the research notes or startup profile, and remove it."""
import asyncio
import json

import yaml

from .llm import log, run_structured
from .schema import STR, arr, obj

SCHEMA = obj(
    flags=arr(obj(claim=STR, problem=STR)),
    subject=STR,
    body=STR,
)

SYSTEM = (
    "You are a strict fact-checker for cold emails. Go through the draft sentence by sentence. "
    "Every factual claim about the recipient's company must be supported by the research notes; "
    "every claim about the sender's company must be supported by the startup profile. Flag each "
    "unsupported or exaggerated claim (including wrong names, numbers, dates, or implied "
    "familiarity), and anything over 120 words or with more than one ask. Then return the "
    "corrected subject and body with those claims removed or softened to exactly what the sources "
    "say. Change nothing else. If nothing is wrong, return an empty flags list and the draft "
    "unchanged."
)


async def _one(c: dict, startup: dict) -> dict:
    d = c["draft"]
    if not d["body"]:
        return {**c, "final": {"subject": "", "body": ""}, "flags": ["writer failed: no draft"]}
    prompt = (
        f"Startup profile:\n{yaml.safe_dump(startup, sort_keys=False)}\n"
        f"Research notes:\n{json.dumps({'facts': c['facts'], 'contact': c['contact']}, indent=2)}\n\n"
        f"Draft subject: {d['subject']}\nDraft body:\n{d['body']}"
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, label=f"review[{c['name']}]")
    if not out:
        return {**c, "final": {"subject": d["subject"], "body": d["body"]}, "flags": ["review failed: check manually"]}
    flags = [f"{f['claim']} -> {f['problem']}" for f in out["flags"]]
    words = len(out["body"].split())
    if words > 120:
        flags.append(f"still {words} words after review")
    log(f"reviewer: {c['name']}: {len(flags)} flag(s)")
    return {**c, "final": {"subject": out["subject"], "body": out["body"]}, "flags": flags}


async def review(companies: list[dict], startup: dict) -> list[dict]:
    log(f"reviewer: checking {len(companies)} drafts")
    return list(await asyncio.gather(*(_one(c, startup) for c in companies)))

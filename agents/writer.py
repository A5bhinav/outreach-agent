"""Step 5: draft a short cold email per company grounded in one researched fact."""
import asyncio
import json

import yaml

from .llm import log, run_structured
from .schema import STR, obj

SCHEMA = obj(subject=STR, body=STR, fact_used=STR)

SYSTEM = (
    "You write short cold emails that sound like a real founder typed them. Rules:\n"
    "- Under 120 words in the body, including greeting and sign-off.\n"
    "- Open with ONE specific fact from the research notes, stated accurately and without exaggeration.\n"
    "- Connect that fact to what the startup does in a sentence or two. Proof points may only come "
    "from the startup profile; skip any marked PLACEHOLDER.\n"
    "- End with the single ask from the profile. One ask, no alternatives.\n"
    "- Casual and plain. No buzzwords (synergy, leverage, revolutionize, cutting-edge, game-changer, "
    "streamline, etc.), no 'I hope this finds you well', no flattery, no exclamation marks.\n"
    "- Greet the contact by first name only if a name is given; otherwise use 'Hi there' or the team.\n"
    "- Don't claim anything about the company that isn't in the research notes.\n"
    "- Subject line: short, lowercase is fine, no clickbait.\n"
    "- Sign off with the sender's name."
)


async def _one(c: dict, startup: dict) -> dict:
    notes = {"company": c["name"], "contact": c["contact"], "facts": c["facts"], "fit_reason": c["fit_reason"]}
    prompt = (
        f"Startup profile:\n{yaml.safe_dump(startup, sort_keys=False)}\n"
        f"Research notes:\n{json.dumps(notes, indent=2)}\n\nWrite the email."
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, label=f"writer[{c['name']}]")
    out = out or {"subject": "", "body": "", "fact_used": ""}
    log(f"writer: {c['name']}: {len(out['body'].split())} words")
    return {**c, "draft": out}


async def write(companies: list[dict], startup: dict) -> list[dict]:
    log(f"writer: drafting {len(companies)} emails")
    return list(await asyncio.gather(*(_one(c, startup) for c in companies)))

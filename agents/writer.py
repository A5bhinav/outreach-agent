"""Step 5: draft a short, send-ready email per target grounded in one researched fact."""
import asyncio
import json

import yaml

from .llm import log, run_structured
from .schema import STR, obj

SCHEMA = obj(subject=STR, body=STR, fact_used=STR, fact_source_url=STR, followup_body=STR)

RULES = (
    "Rules:\n"
    "- 50-110 words in the body, including greeting and sign-off. Plain text, no links or formatting.\n"
    "- Open with ONE specific fact from the research notes, stated accurately and without exaggeration. "
    "Prefer the most recent dated fact (lowest age_months); it's the reason to write now.\n"
    "- Connect that fact to the startup in a sentence or two. Any guess about the recipient's needs "
    "must be phrased as a guess or question (\"curious whether...\", \"I'd guess...\"), never as fact. "
    "Proof points may only come from the startup profile; skip any marked PLACEHOLDER.\n"
    "- End with the single ask given. One ask, no alternatives. If it mentions something the notes "
    "don't show the recipient has, adapt it so it doesn't presume.\n"
    "- Casual and plain. No buzzwords (synergy, leverage, revolutionize, cutting-edge, game-changer, "
    "streamline, etc.), no 'I hope this finds you well', no flattery, no exclamation marks.\n"
    "- Greet the contact by first name if a name is given; otherwise use 'Hi {company} team'. Never "
    "write a bracketed placeholder like [Name].\n"
    "- Don't claim anything about the recipient that isn't in the research notes.\n"
    "- Subject line: short, lowercase is fine, no clickbait.\n"
    "- fact_used / fact_source_url: the fact you opened with and its URL, copied from the notes.\n"
    "- Don't add an unsubscribe line, address or legal text; a footer is added automatically.\n"
    "- followup_body: a 30-60 word follow-up to send about five business days later if there's no "
    "reply. Same thread (no subject), references the first note in a few words, adds nothing the "
    "notes don't support, repeats the single ask, same sign-off."
)

VOICE = {
    "investor": (
        "You write short intro emails from an investor on behalf of a portfolio company. The sender "
        "is the investor named in the profile's sender block; refer to the startup and its founder in "
        "the third person. It's a double opt-in intro: ask whether they'd like to be introduced to the "
        "founder, and make clear nothing happens unless they say yes. Lead with what's useful to the "
        "recipient, not the startup's pitch. Sign off with the sender's name, title and firm."
    ),
    "founder": (
        "You write short cold emails that sound like a real founder typed them. The sender is named in "
        "the profile's sender block. Sign off with the sender's name."
    ),
}

PURPOSE = {
    "companies": "The recipient is a decision-maker at a company the startup wants to meet.",
    "people": (
        "The recipient is a person the startup may want to hire. Open with something specific they "
        "built, shipped, wrote or presented, say briefly why the startup's work might interest them, "
        "and keep it respectful of their current job. No job-description dumps."
    ),
}


def system_for(startup: dict, mode: str) -> str:
    voice = startup.get("sender", {}).get("voice", "investor")
    return f"{VOICE.get(voice, VOICE['investor'])}\n{PURPOSE[mode]}\n{RULES}"


def notes_for(c: dict) -> dict:
    ct = c["contact"]
    contact = ({"name": ct["name"], "title": ct["title"]} if ct["name"] != "unknown"
               else {"name": "not found", "target_role": ct.get("suggested_title", "unknown")})
    return {"recipient_company": c.get("company", c["name"]), "contact": contact, "facts": c["facts"]}


async def write_one(c: dict, startup: dict, p: dict, feedback: list[str] | None = None) -> dict:
    prompt = (
        f"Startup profile:\n{yaml.safe_dump(startup, sort_keys=False)}\n"
        f"Ask: {p['ask']}\n"
        f"Research notes:\n{json.dumps(notes_for(c), indent=2)}\n\n"
    )
    if feedback:
        prompt += ("A fact-checker flagged these problems in your previous draft; write a new draft that "
                   "fixes them while staying specific:\n- " + "\n- ".join(feedback) +
                   f"\n\nPrevious draft:\n{c['draft']['body']}\n\nPrevious follow-up:\n{c['draft']['followup_body']}\n")
    else:
        prompt += "Write the email."
    out = await run_structured(system_for(startup, p["mode"]), prompt, SCHEMA, label=f"writer[{c['name']}]")
    out = out or {"subject": "", "body": "", "fact_used": "", "fact_source_url": "", "followup_body": ""}
    log(f"writer: {c['name']}: {len(out['body'].split())} words" + (" (rewrite)" if feedback else ""))
    return {**c, "draft": out}


async def write(targets: list[dict], startup: dict, p: dict) -> list[dict]:
    log(f"writer: drafting {len(targets)} emails")
    return list(await asyncio.gather(*(write_one(c, startup, p) for c in targets)))

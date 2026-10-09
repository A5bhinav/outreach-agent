"""Step 5: draft a short, send-ready intro per target grounded in a researched fact, plus a follow-up
and a LinkedIn-length note for targets without an email address."""
import asyncio
import json

import yaml

from .llm import log, run_structured
from .schema import STR, obj

SCHEMA = obj(subject=STR, body=STR, fact_used=STR, fact_source_url=STR, followup_body=STR, linkedin_note=STR)

RULES = (
    "Rules:\n"
    "- 50-110 words in the body, including greeting and sign-off. Plain text, no links or formatting.\n"
    "- Open with ONE specific fact from the research notes, stated accurately and without exaggeration: "
    "start with the fact itself or a question about it, never \"I came across\", \"I read\", \"I noticed\", "
    "\"I saw\" or \"I was reading\". Prefer the most recent dated fact (lowest age_months); it's the reason "
    "to write now.\n"
    "- Describe the startup in one clause, in terms of this recipient's situation (their project, site, "
    "team or problem). Never paste the pitch from the profile. Any guess about the recipient's needs "
    "must be phrased as a guess or question (\"curious whether...\", \"I'd guess...\"), never as fact. "
    "Proof points and other startup facts may only come from the startup profile; skip anything marked "
    "PLACEHOLDER.\n"
    "- End with the single ask given. One ask, no alternatives. If it mentions something the notes "
    "don't show the recipient has, adapt it so it doesn't presume.\n"
    "- Plain and human, like a busy person typed it. No buzzwords (synergy, leverage, revolutionize, "
    "cutting-edge, game-changer, streamline, etc.), no 'I hope this finds you well', no flattery, no "
    "exclamation marks.\n"
    "- Greeting: first name if a contact name is given. If the address is a shared inbox (email_kind "
    "generic) and a person is named, greet the team and ask them to pass it to that person by name and "
    "title. With no name, greet the company's team. Never write a bracketed placeholder like [Name].\n"
    "- Don't claim anything about the recipient that isn't in the research notes.\n"
    "- fact_used / fact_source_url: the fact you opened with and its URL, copied from the notes.\n"
    "- Don't add an unsubscribe line, address or legal text; a footer is added automatically.\n"
    "- followup_body: 30-60 words to send about five business days later in the same thread if there's "
    "no reply. Use a different fact from the notes than the opener (or, if there's only one, a new angle "
    "on it), then repeat the ask in a few words. Same sign-off. Never \"just bumping this\".\n"
    "- linkedin_note: under 280 characters, for a LinkedIn connection request: the hook, who you are, "
    "the ask. No links, no sign-off block."
)

VOICE = {
    "investor": (
        "You write short intro emails from an investor on behalf of a portfolio company. The sender is "
        "the investor named in the profile's sender block; refer to the startup and its founder in the "
        "third person. It's a double opt-in intro: ask whether they'd like to be connected with the "
        "founder (e.g. \"Want me to connect you?\"). In one line say why you're writing: your firm backs "
        "the founder, plus one credibility point from the profile (investor_note, stage_and_backers, "
        "founder_bio) if given. Lead with what's useful to the recipient. Sign off with the sender's "
        "name, title and firm.\n"
        "Subject: an intro subject naming the founder and startup and tied to the recipient's situation, "
        "e.g. \"Intro to {founder} ({company}) re: your Kasson prefab shop\". Sentence case, no clickbait."
    ),
    "founder": (
        "You write short cold emails that sound like a real founder typed them. The sender is named in "
        "the profile's sender block. Sign off with the sender's name.\n"
        "Subject: short and specific to the recipient's situation, sentence case, no clickbait."
    ),
}

PURPOSE = {
    "companies": "The recipient is a decision-maker at a company the startup wants to meet.",
    "people": (
        "The recipient is a person the startup may want to hire. Open with something specific they "
        "built, shipped, wrote or presented, say briefly why the startup's work (and the role, if the "
        "profile's roles block describes one) might interest them, and keep it respectful of their "
        "current job. No job-description dumps. The ask is a low-key chat, not an application."
    ),
}

# Profile fields the writer actually uses; the rest (address, email, ICP notes) would only add noise.
_WRITER_FIELDS = ("company_name", "founder_name", "founder_bio", "pitch", "stage_and_backers", "investor_note",
                  "offer", "proof_points", "roles")


def system_for(startup: dict, mode: str) -> str:
    voice = startup.get("sender", {}).get("voice", "investor")
    return f"{VOICE.get(voice, VOICE['investor'])}\n{PURPOSE[mode]}\n{RULES}"


def profile_for(startup: dict) -> str:
    s = startup.get("sender", {})
    prof = {k: startup[k] for k in _WRITER_FIELDS if startup.get(k)}
    prof["sender"] = {k: s[k] for k in ("name", "title", "firm", "voice") if s.get(k)}
    return yaml.safe_dump(prof, sort_keys=False, allow_unicode=True)


def notes_for(c: dict) -> dict:
    ct = c["contact"]
    contact = ({"name": ct["name"], "title": ct["title"]} if ct["name"] != "unknown"
               else {"name": "not found", "target_role": ct.get("suggested_title", "unknown")})
    contact["email_kind"] = ct.get("email_kind", "none")
    return {"recipient_company": c.get("company", c["name"]), "contact": contact, "facts": c["facts"]}


async def write_one(c: dict, startup: dict, p: dict, feedback: list[str] | None = None) -> dict:
    prompt = (
        f"Startup profile:\n{profile_for(startup)}\n"
        f"Ask: {p['ask']}\n"
        f"Research notes:\n{json.dumps(notes_for(c), indent=2)}\n\n"
    )
    if feedback:
        prompt += ("A reviewer flagged these problems in your previous draft; write a new draft that "
                   "fixes them while staying specific:\n- " + "\n- ".join(feedback) +
                   f"\n\nPrevious draft:\n{c['draft']['body']}\n\nPrevious follow-up:\n{c['draft']['followup_body']}\n")
    else:
        prompt += "Write the email."
    out = await run_structured(system_for(startup, p["mode"]), prompt, SCHEMA, effort="medium", max_tokens=8000,
                               label=f"writer[{c['name']}]")
    out = out or {"subject": "", "body": "", "fact_used": "", "fact_source_url": "", "followup_body": "", "linkedin_note": ""}
    log(f"writer: {c['name']}: {len(out['body'].split())} words" + (" (rewrite)" if feedback else ""))
    return {**c, "draft": out}


async def write(targets: list[dict], startup: dict, p: dict) -> list[dict]:
    log(f"writer: drafting {len(targets)} emails")
    return list(await asyncio.gather(*(write_one(c, startup, p) for c in targets)))

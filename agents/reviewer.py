"""Step 6: fact- and style-check each draft and follow-up, send problems back to the writer once,
re-check, add the legal footer, and flag anything that blocks sending."""
import asyncio
import json
import re

import yaml

from .compliance import footer, missing_legal
from .llm import log, run_structured
from .schema import STR, arr, obj
from .writer import notes_for, write_one

SCHEMA = obj(
    flags=arr(obj(claim=STR, problem=STR)),
    subject=STR,
    body=STR,
    followup_body=STR,
)

SYSTEM = (
    "You are a strict reviewer for outreach emails: an email and its follow-up. Go through both "
    "sentence by sentence.\n"
    "Facts: every claim about the recipient must be supported by the research notes; every claim "
    "about the startup or sender must be supported by the startup profile. A guess clearly phrased as "
    "a guess or question (\"I'd guess...\", \"curious whether...\") is fine. Flag unsupported or "
    "exaggerated claims, wrong names, numbers or dates, and implied familiarity.\n"
    "Style: flag an email body outside 50-110 words or a follow-up over 60, more than one ask, an ask "
    "that presumes something the notes don't show (e.g. a facility they may not have), buzzwords "
    "(synergy, leverage, revolutionize, cutting-edge, game-changer, streamline, etc.), flattery, "
    "exclamation marks, and placeholder text.\n"
    "Then return the corrected subject, body and followup_body with those problems fixed by removing "
    "or softening to exactly what the sources say. Change nothing else. If nothing is wrong, return "
    "an empty flags list and the text unchanged."
)

BODY_WORDS = (40, 120)  # writer targets 50-110; small slack before flagging
FOLLOWUP_MAX = 70
BUZZWORDS = re.compile(r"\b(synerg\w*|leverag\w*|revolutioni[sz]\w*|cutting[- ]edge|game[- ]chang\w*|"
                       r"streamlin\w*|best[- ]in[- ]class|disrupt\w*|paradigm|hope this (email )?finds you)\b", re.I)
_PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|PLACEHOLDER|\bunknown\b|\bnot found\b", re.I)


async def _check(c: dict, startup: dict) -> dict | None:
    d = c["draft"]
    prompt = (
        f"Startup profile:\n{yaml.safe_dump(startup, sort_keys=False)}\n"
        f"Research notes:\n{json.dumps(notes_for(c), indent=2)}\n\n"
        f"Draft subject: {d['subject']}\nDraft body:\n{d['body']}\n\nDraft follow-up:\n{d['followup_body']}"
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, label=f"review[{c['name']}]")
    if out and not out["body"].strip():
        return None
    return out


def _placeholders(startup: dict) -> list[str]:
    """Config values still marked as placeholders; they must never reach a sendable email."""
    vals = []
    for v in [*startup.values(), *startup.get("sender", {}).values()]:
        if isinstance(v, str) and "PLACEHOLDER" in v:
            vals.append(v.replace("PLACEHOLDER:", "").strip())
    return [v for v in vals if v]


def _style_flags(subject: str, body: str, followup: str, startup: dict) -> list[str]:
    flags = []
    words = len(body.split())
    if not BODY_WORDS[0] <= words <= BODY_WORDS[1]:
        flags.append(f"body is {words} words (target 50-110)")
    if len(followup.split()) > FOLLOWUP_MAX:
        flags.append(f"follow-up is {len(followup.split())} words (target under 60)")
    text = "\n".join((subject, body, followup))
    if hits := sorted({m.group(0).lower() for m in BUZZWORDS.finditer(text)}):
        flags.append(f"buzzwords: {hits}")
    if "!" in text:
        flags.append("exclamation mark")
    hits = _PLACEHOLDER.findall(text) + [p for p in _placeholders(startup) if p.lower() in text.lower()]
    if hits:
        flags.append(f"BLOCKER: placeholder text in email: {sorted(set(hits))}")
    return flags


async def _one(c: dict, startup: dict, p: dict) -> dict:
    if not c["draft"]["body"].strip():
        return {**c, "final": {"subject": "", "body": "", "followup_body": "", "footer": ""},
                "flags": ["BLOCKER: writer failed, no draft"]}
    out = await _check(c, startup)
    d = c["draft"]
    style = _style_flags(d["subject"], d["body"], d["followup_body"], startup)
    if (out and out["flags"]) or [f for f in style if not f.startswith("BLOCKER")]:
        # One rewrite with the reviewer's feedback, then a fresh check of the rewrite.
        feedback = [f"{f['claim']} -> {f['problem']}" for f in (out or {}).get("flags", [])] + style
        rewritten = await write_one(c, startup, p, feedback=feedback)
        if rewritten["draft"]["body"].strip():
            c = rewritten
            out = await _check(c, startup)
    if not out:
        d = c["draft"]
        return {**c, "final": {"subject": d["subject"], "body": d["body"], "followup_body": d["followup_body"],
                               "footer": footer(startup, c)},
                "flags": ["BLOCKER: review failed, check manually"]}

    flags = [f"{f['claim']} -> {f['problem']}" for f in out["flags"]]
    flags += _style_flags(out["subject"], out["body"], out["followup_body"], startup)
    if missing := missing_legal(startup):
        flags.append(f"BLOCKER: legal footer incomplete, set sender.{', sender.'.join(missing)} in the config")
    log(f"reviewer: {c['name']}: {len(flags)} flag(s)")
    final = {"subject": out["subject"], "body": out["body"], "followup_body": out["followup_body"],
             "footer": footer(startup, c)}
    return {**c, "final": final, "flags": flags}


async def review(targets: list[dict], startup: dict, p: dict) -> list[dict]:
    log(f"reviewer: checking {len(targets)} drafts")
    return list(await asyncio.gather(*(_one(c, startup, p) for c in targets)))

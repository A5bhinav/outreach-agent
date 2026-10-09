"""Step 6: fact- and style-check each draft, rewrite only what the check couldn't fix, verify the
opening fact against its source page, add the legal footer, and flag anything that needs a human.

Flags come in two kinds: "fixed" (problems the reviewer already corrected in the text; shown for
transparency) and "check" (things the investor must look at). A "check" flag starting with
BLOCKER stops the email from being marked ready to send.
"""
import asyncio
import json
import re

from .compliance import footer, missing_legal
from .contacts import page_text
from .llm import log, run_structured
from .schema import STR, arr, obj
from .writer import notes_for, profile_for, write_one

SCHEMA = obj(
    flags=arr(obj(claim=STR, problem=STR)),
    subject=STR,
    body=STR,
    followup_body=STR,
    linkedin_note=STR,
)

SYSTEM = (
    "You are a strict reviewer for outreach messages: an email, its follow-up and a LinkedIn note. Go "
    "through each sentence by sentence.\n"
    "Facts: every claim about the recipient must be supported by the research notes; every claim "
    "about the startup or sender must be supported by the startup profile. A guess clearly phrased as "
    "a guess or question (\"I'd guess...\", \"curious whether...\") is fine. Flag unsupported or "
    "exaggerated claims, wrong names, numbers or dates, and implied familiarity.\n"
    "Style: flag an email body outside 50-110 words, a follow-up over 60, a LinkedIn note over 280 "
    "characters, more than one ask, an ask that presumes something the notes don't show (e.g. a "
    "facility they may not have), buzzwords, flattery, exclamation marks, placeholder text, and a "
    "pasted product pitch.\n"
    "Then return the corrected subject, body, followup_body and linkedin_note with those problems "
    "fixed by removing or softening to exactly what the sources say, keeping the message specific. "
    "Change nothing else. If nothing is wrong, return an empty flags list and the text unchanged."
)

BODY_WORDS = (40, 120)  # writer targets 50-110; small slack before flagging
FOLLOWUP_MAX = 70
NOTE_MAX = 300
WEAK_HOOK_MONTHS = 12
SIMILAR = 0.35  # 3-gram Jaccard between two bodies above which they read as one template
BUZZWORDS = re.compile(r"\b(synerg\w*|leverag\w*|revolutioni[sz]\w*|cutting[- ]edge|game[- ]chang\w*|"
                       r"streamlin\w*|best[- ]in[- ]class|disrupt\w*|paradigm|hope this (email )?finds you)\b", re.I)
_PLACEHOLDER = re.compile(r"\[[A-Za-z][^\]]{0,30}\]|\{[A-Za-z_][^}]{0,30}\}|PLACEHOLDER|\b(hi|dear|hello) (unknown|not found)\b", re.I)


async def _check(c: dict, startup: dict) -> dict | None:
    d = c["draft"]
    prompt = (
        f"Startup profile:\n{profile_for(startup)}\n"
        f"Research notes:\n{json.dumps(notes_for(c), indent=2)}\n\n"
        f"Draft subject: {d['subject']}\nDraft body:\n{d['body']}\n\nDraft follow-up:\n{d['followup_body']}\n\n"
        f"Draft LinkedIn note:\n{d['linkedin_note']}"
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, effort="medium", max_tokens=8000, label=f"review[{c['name']}]")
    if out and not out["body"].strip():
        return None
    return out


def _strings(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def _placeholders(startup: dict) -> list[str]:
    """Config values (at any depth) still marked as placeholders; they must never reach a sendable email."""
    return [s.replace("PLACEHOLDER:", "").strip() for s in _strings(startup) if "PLACEHOLDER" in s
            and s.replace("PLACEHOLDER:", "").strip()]


def _style_flags(m: dict, startup: dict) -> list[str]:
    flags = []
    words = len(m["body"].split())
    if not BODY_WORDS[0] <= words <= BODY_WORDS[1]:
        flags.append(f"body is {words} words (target 50-110)")
    if len(m["followup_body"].split()) > FOLLOWUP_MAX:
        flags.append(f"follow-up is {len(m['followup_body'].split())} words (target under 60)")
    if len(m["linkedin_note"]) > NOTE_MAX:
        flags.append(f"LinkedIn note is {len(m['linkedin_note'])} characters (limit 300)")
    text = "\n".join((m["subject"], m["body"], m["followup_body"], m["linkedin_note"]))
    if hits := sorted({x.group(0).lower() for x in BUZZWORDS.finditer(text)}):
        flags.append(f"buzzwords: {hits}")
    if "!" in text:
        flags.append("exclamation mark")
    return flags


def _blockers(m: dict, startup: dict, foot: str) -> list[str]:
    text = "\n".join((m["subject"], m["body"], m["followup_body"], m["linkedin_note"], foot))
    hits = [x.group(0) for x in _PLACEHOLDER.finditer(text)]
    hits += [p for p in _placeholders(startup) if p.lower() in text.lower()]
    out = [f"BLOCKER: placeholder text in message: {sorted(set(hits))}"] if hits else []
    if missing := missing_legal(startup):
        out.append(f"BLOCKER: legal footer incomplete, set {', '.join('sender.' + k if k != 'company_name' else k for k in missing)} in the config")
    return out


def _tokens(fact: str) -> list[str]:
    """Checkable details of a fact: numbers, then capitalized words that aren't sentence starts."""
    nums = [n.replace(",", "") for n in re.findall(r"\d[\d,.]*\d|\d", fact) if len(n.replace(",", "")) >= 2]
    nouns = [w for w in re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", fact) if not fact.startswith(w)]
    return (nums + nouns)[:4]


async def _fact_check(draft: dict) -> str:
    """yes / unchecked / NO: are the opening fact's key details on its cited page?"""
    toks = _tokens(draft.get("fact_used", ""))
    if not toks:
        return "unchecked"
    text = await page_text(draft.get("fact_source_url", ""))
    if text is None:
        return "unchecked"
    flat = text.replace(",", "")
    found = sum(t.lower() in flat for t in toks)
    return "yes" if found else ("NO" if len(toks) >= 2 else "unchecked")


def _hook_age(c: dict) -> int | None:
    url = c["draft"].get("fact_source_url", "")
    ages = [f.get("age_months") for f in c["facts"] if f["source_url"] == url]
    return ages[0] if ages else None


async def _one(c: dict, startup: dict, p: dict) -> dict:
    empty = {"subject": "", "body": "", "followup_body": "", "linkedin_note": "", "footer": ""}
    if not c["draft"]["body"].strip():
        return {**c, "final": empty, "fixed": [], "flags": ["BLOCKER: writer failed, no draft"]}
    out = await _check(c, startup)
    # Rewrite only when the check failed, left style problems, or had to gut the email.
    remaining = _style_flags(out, startup) if out else ["review failed"]
    gutted = bool(out) and len(out["body"].split()) < 0.65 * len(c["draft"]["body"].split())
    if remaining or gutted:
        feedback = [f"{f['claim']} -> {f['problem']}" for f in (out or {}).get("flags", [])] + remaining
        if gutted:
            feedback.append("too much had to be cut; rebuild the email around a supported fact")
        rewritten = await write_one(c, startup, p, feedback=feedback)
        if rewritten["draft"]["body"].strip():
            c = rewritten
            out = await _check(c, startup)
    if not out:
        d = c["draft"]
        final = {**{k: d.get(k, "") for k in empty}, "footer": footer(startup, c)}
        return {**c, "final": final, "fixed": [], "flags": ["BLOCKER: review failed, check manually"]}

    final = {"subject": out["subject"].strip(), "body": out["body"].strip(), "followup_body": out["followup_body"].strip(),
             "linkedin_note": out["linkedin_note"].strip(), "footer": footer(startup, c)}
    fixed = [f"{f['claim']} -> {f['problem']}" for f in out["flags"]]
    flags = _blockers(final, startup, final["footer"]) + _style_flags(final, startup)
    fact = await _fact_check(c["draft"])
    if fact == "NO":
        flags.append(f"BLOCKER: opening fact's details not found on its source page ({c['draft'].get('fact_source_url')}); check it")
    age = _hook_age(c)
    if age is None or age > WEAK_HOOK_MONTHS:
        flags.append("weak hook: the opening fact is " + ("undated" if age is None else f"{age} months old"))
    if not c["contact"].get("email_domain_match", True):
        flags.append(f"email {c['contact']['email']} isn't on the company's domain; confirm it reaches them")
    log(f"reviewer: {c['name']}: {len(fixed)} fixed, {len(flags)} to check")
    return {**c, "final": final, "fact_check": fact, "fixed": fixed, "flags": flags}


def _grams(text: str) -> set[tuple]:
    w = re.findall(r"[a-z']+", text.lower())
    return set(zip(w, w[1:], w[2:]))


def flag_similar(targets: list[dict]) -> None:
    """Flag emails that read like the same template as another in the batch."""
    grams = [_grams(t["final"]["body"]) for t in targets]
    for i, t in enumerate(targets):
        for j in range(len(targets)):
            if i != j and grams[i] and grams[j]:
                sim = len(grams[i] & grams[j]) / len(grams[i] | grams[j])
                if sim > SIMILAR:
                    t["flags"].append(f"reads like the email to {targets[j]['name']} ({sim:.0%} overlap); vary it")
                    break


async def review_one(c: dict, startup: dict, p: dict) -> dict:
    return await _one(c, startup, p)


async def review(targets: list[dict], startup: dict, p: dict) -> list[dict]:
    log(f"reviewer: checking {len(targets)} drafts")
    done = list(await asyncio.gather(*(_one(c, startup, p) for c in targets)))
    flag_similar(done)
    return done

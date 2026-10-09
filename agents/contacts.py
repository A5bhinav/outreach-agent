"""Step 4 (companies mode): find a named decision-maker and a published email only if a public page shows them.

If APOLLO_API_KEY is set and no published address was found, the named contact is looked up in
Apollo and only an address Apollo marks "verified" is kept. Addresses are never guessed.
Also holds the page check used to verify any name/email an agent reports against its source URL.
"""
import asyncio
import json
import os
import re
import urllib.parse
import urllib.request

from .llm import log, run_structured
from .schema import STR, obj

SCHEMA = obj(name=STR, title=STR, source_url=STR, email=STR, email_source_url=STR, suggested_title=STR)

SYSTEM = (
    "You look for one decision-maker at a company who would own the conversation described. Only "
    "report a person whose name AND title you actually read on a public page (company "
    "about/team/leadership page, press release, news article) and give that page's URL. Prefer a "
    "person already named in the research notes if their role fits the target roles. If you did not "
    "find someone in a fitting role, set name, title and source_url to \"unknown\". Always set "
    "suggested_title to the role worth targeting. Never guess names.\n"
    "email: only an address you read verbatim on a public page (for that person, or else a general "
    "business contact address such as info@ or sales@ on the company's own site), with that page's "
    "URL in email_source_url. Never construct or guess an address from a name pattern. Otherwise set "
    "both to \"unknown\"."
)

# LinkedIn's terms bar automated fetching; GitHub's bar using its data for unsolicited email.
NO_FETCH = ("linkedin.com",)
NO_EMAIL_SOURCE = ("linkedin.com", "github.com", "githubusercontent.com")
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
UNKNOWN_CONTACT = {"name": "unknown", "title": "unknown", "source_url": "unknown", "email": "unknown",
                   "email_source_url": "unknown", "verified": "no"}


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (outreach-agent verifier)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(2_000_000).decode("utf-8", errors="ignore").lower()


async def page_mentions(url: str, needle: str) -> bool | None:
    """True/False if the page does/doesn't contain `needle`; None if the page couldn't be fetched."""
    if not url.startswith("http") or not needle:
        return False
    if _on(url, NO_FETCH):
        return None
    try:
        text = await asyncio.to_thread(_fetch, url)
    except Exception:
        return None
    return needle.lower() in text


def _on(url: str, hosts: tuple) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def email_allowed(email: str, source_url: str) -> bool:
    return bool(_EMAIL.match(email)) and source_url.startswith("http") and not _on(source_url, NO_EMAIL_SOURCE)


def verdict(ok: bool | None) -> str:
    return {True: "yes", False: "NO - not found on source page", None: "unchecked (page not fetchable)"}[ok]


async def verify_contact(ct: dict) -> dict:
    """Check the surname and email against their source pages. Drops anything the page contradicts."""
    ct = {**ct}
    if ct["name"] != "unknown":
        ok = await page_mentions(ct["source_url"], ct["name"].split()[-1])
        ct["verified"] = verdict(ok)
    if ct["email"] != "unknown":
        ok = await page_mentions(ct["email_source_url"], ct["email"])
        if ok is False:
            ct.update(email="unknown", email_source_url="unknown")
    return ct


def _apollo_match(first: str, last: str, company: str, dom: str, key: str) -> dict:
    body = {"first_name": first, "last_name": last, "organization_name": company}
    if dom:
        body["domain"] = dom
    req = urllib.request.Request(
        "https://api.apollo.io/api/v1/people/match", data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("person") or {}


async def apollo_email(name: str, company: str, dom: str) -> tuple[str, str] | None:
    """(email, source label) for a verified Apollo match, else None. No-op without APOLLO_API_KEY."""
    key = os.environ.get("APOLLO_API_KEY")
    parts = name.split()
    if not key or len(parts) < 2:
        return None
    try:
        person = await asyncio.to_thread(_apollo_match, parts[0], parts[-1], company, dom, key)
    except Exception as e:
        log(f"  ! apollo: lookup failed for {name}: {e}")
        return None
    email, status = person.get("email") or "", person.get("email_status") or ""
    if status == "verified" and _EMAIL.match(email):
        return email, "apollo (verified)"
    return None


async def _one(c: dict, startup: dict, p: dict) -> dict:
    prompt = (
        f"Company: {c['name']}\nWebsite: {c['website']}\n"
        f"What the startup offers: {startup['pitch']}\nTarget roles: {p['target_titles']}\n"
        f"Research notes: {c['facts']}"
    )
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=3, web_fetch=True, model="light", label=f"contact[{c['name']}]")
    out = out or {**UNKNOWN_CONTACT, "suggested_title": (p["target_titles"] or ["unknown"])[0]}
    out = {k: v.strip() or "unknown" for k, v in out.items()}
    # Belt and braces: an unsourced name is treated as not found; an unsourced or malformed email is dropped.
    if not out["source_url"].startswith("http") or out["name"].lower() == "unknown":
        out.update(name="unknown", title="unknown", source_url="unknown")
    if not email_allowed(out["email"], out["email_source_url"]):
        out.update(email="unknown", email_source_url="unknown")
    out["verified"] = "no"
    out = await verify_contact(out)
    if out["email"] == "unknown" and out["name"] != "unknown" and not out["verified"].startswith("NO"):
        hit = await apollo_email(out["name"], c["name"], c.get("domain", ""))
        if hit:
            out["email"], out["email_source_url"] = hit
    log(f"contacts: {c['name']}: {out['name']} ({out['title'] if out['name'] != 'unknown' else 'target ' + out['suggested_title']})"
        f", email {out['email']}")
    return {**c, "contact": out}


async def find_contacts(companies: list[dict], startup: dict, p: dict) -> list[dict]:
    log(f"contacts: searching {len(companies)} companies")
    return list(await asyncio.gather(*(_one(c, startup, p) for c in companies)))

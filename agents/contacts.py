"""Step 4 (companies mode): find a named decision-maker, a published email and the company's contact form.

Only names and addresses read on a public page are kept, and each is checked against that page.
If APOLLO_API_KEY is set and no personal address was found (none, or only a generic inbox such as
info@), the named contact is looked up in Apollo and only an address Apollo marks "verified" is
kept. Addresses are never guessed. Also holds the page checks used by other steps.
"""
import asyncio
import html
import json
import os
import re
import urllib.parse
import urllib.request

from .llm import log, run_structured
from .schema import STR, obj

SCHEMA = obj(name=STR, title=STR, source_url=STR, email=STR, email_source_url=STR, contact_form_url=STR,
             suggested_title=STR)

SYSTEM = (
    "You look for one decision-maker at a company who would own the conversation described. Only "
    "report a person whose name AND title you actually read on a public page (company "
    "about/team/leadership page, press release, news article) and give that page's URL. Prefer a "
    "person already named in the research notes if their role fits the target roles. If you did not "
    "find someone in a fitting role, set name, title and source_url to \"unknown\". Always set "
    "suggested_title to the role worth targeting. Never guess names.\n"
    "email: only an address you read verbatim on a public page, preferably that person's own; else a "
    "general business address (info@, sales@) on the company's own site. Put that page's URL in "
    "email_source_url. Never construct or guess an address from a name pattern. Otherwise set both "
    "to \"unknown\".\n"
    "contact_form_url: the company's own contact or inquiry form page if you saw one, else \"unknown\"."
)

# LinkedIn's terms bar automated fetching; GitHub's bar using its data for unsolicited email.
NO_FETCH = ("linkedin.com",)
NO_EMAIL_SOURCE = ("linkedin.com", "github.com", "githubusercontent.com")
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
GENERIC = re.compile(r"^(info|sales|contact|contactus|hello|hi|office|admin|marketing|support|help|inquir\w*|"
                     r"enquir\w*|bids?|estimating|careers|jobs|hr|team|general|mail|media|press|pr|service)@", re.I)
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "pe", "phd", "mba", "cpa", "pmp", "leed", "ap", "esq", "md", "dr", "mr",
             "mrs", "ms", "prof"}
UNKNOWN_CONTACT = {"name": "unknown", "title": "unknown", "source_url": "unknown", "email": "unknown",
                   "email_source_url": "unknown", "contact_form_url": "unknown", "verified": "no"}


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (outreach-agent verifier)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(3_000_000).decode("utf-8", errors="ignore")


def _cf_emails(raw: str) -> list[str]:
    """Decode Cloudflare-obfuscated addresses (data-cfemail / #hex links)."""
    out = []
    for h in re.findall(r'(?:data-cfemail="|email-protection#)([0-9a-fA-F]{6,})', raw):
        try:
            key = int(h[:2], 16)
            out.append("".join(chr(int(h[i:i + 2], 16) ^ key) for i in range(2, len(h), 2)))
        except ValueError:
            pass
    return out


def _host_in(url: str, hosts: tuple) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


async def page_text(url: str) -> str | None:
    """Visible text of a page (tags stripped, entities decoded, lowercased), or None if it can't be checked.

    None covers fetch failures, LinkedIn (not fetched by policy) and near-empty, script-rendered pages,
    so a missing name there reads as "unchecked" rather than "not on the page".
    """
    if not url.startswith("http") or _host_in(url, NO_FETCH):
        return None
    try:
        raw = await asyncio.to_thread(_fetch, url)
    except Exception:
        return None
    body = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).lower()
    if len(text) < 500:
        return None
    # mailto: links and Cloudflare-protected addresses aren't visible text but are on the page.
    extra = re.findall(r"mailto:([^\"'?>\s]+)", raw) + _cf_emails(raw)
    return text + " " + " ".join(urllib.parse.unquote(e).lower() for e in extra)


def name_parts(name: str) -> list[str]:
    """Name tokens without honorifics, suffixes or credentials ("Dr. Jane Doe, PE" -> ["jane", "doe"])."""
    toks = [t.strip(".,").lower() for t in re.split(r"[\s,]+", name) if t.strip(".,")]
    return [t for t in toks if t not in _SUFFIXES]


def mentions_name(text: str, name: str) -> bool:
    """Full name (allowing a middle name or initial), or a distinctive surname, as whole words."""
    parts = name_parts(name)
    if not parts:
        return False
    full = r"\b" + r"\W+(?:\w+\W+)?".join(map(re.escape, parts)) + r"\b"
    if re.search(full, text):
        return True
    return len(parts[-1]) > 3 and bool(re.search(rf"\b{re.escape(parts[-1])}\b", text))


def email_allowed(email: str, source_url: str) -> bool:
    if not _EMAIL.match(email):
        return False
    if source_url.startswith("apollo"):
        return True
    return source_url.startswith("http") and not _host_in(source_url, NO_EMAIL_SOURCE)


def email_kind(email: str, name: str) -> str:
    """personal (looks like the named contact's own address), generic (shared inbox), other, or none."""
    if email == "unknown":
        return "none"
    if GENERIC.match(email):
        return "generic"
    local = email.split("@")[0].lower()
    parts = name_parts(name) if name != "unknown" else []
    if parts and (any(len(p) > 2 and p in local for p in parts) or local.startswith(parts[0][0] + parts[-1])):
        return "personal"
    return "other"


def _base(d: str) -> str:
    return ".".join(d.lower().removeprefix("www.").split(".")[-2:])


def verdict(ok: bool | None) -> str:
    return {True: "yes", False: "NO - not found on source page", None: "unchecked (page not fetchable)"}[ok]


async def verify_contact(ct: dict) -> dict:
    """Check the name and email against their source pages. Drops an email the page contradicts."""
    ct = {**ct}
    if ct["name"] != "unknown":
        text = await page_text(ct["source_url"])
        ct["verified"] = verdict(None if text is None else mentions_name(text, ct["name"]))
    if ct["email"] != "unknown" and ct["email_source_url"].startswith("http"):
        text = await page_text(ct["email_source_url"])
        if text is not None and ct["email"].lower() not in text:
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
    parts = name_parts(name)
    if not key or len(parts) < 2:
        return None
    try:
        person = await asyncio.to_thread(_apollo_match, parts[0].title(), parts[-1].title(), company, dom, key)
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
    out = await run_structured(SYSTEM, prompt, SCHEMA, web_searches=2, web_fetches=3, model="light", effort="low",
                               max_tokens=8000, label=f"contact[{c['name']}]")
    out = out or {**UNKNOWN_CONTACT, "suggested_title": (p["target_titles"] or ["unknown"])[0]}
    out = {k: v.strip() or "unknown" for k, v in out.items()}
    # Belt and braces: an unsourced name is treated as not found; an unsourced or malformed email is dropped.
    if not out["source_url"].startswith("http") or out["name"].lower() == "unknown":
        out.update(name="unknown", title="unknown", source_url="unknown")
    if not email_allowed(out["email"], out["email_source_url"]):
        out.update(email="unknown", email_source_url="unknown")
    if not out["contact_form_url"].startswith("http"):
        out["contact_form_url"] = "unknown"
    out["verified"] = "no"
    out = await verify_contact(out)
    dom = c.get("domain") if c.get("domain") not in (None, "", "unknown") else ""
    # A shared inbox shouldn't stop a lookup for the named person's own verified address.
    if (email_kind(out["email"], out["name"]) in ("none", "generic") and out["name"] != "unknown"
            and not out["verified"].startswith("NO")):
        if hit := await apollo_email(out["name"], c["name"], dom):
            out["email"], out["email_source_url"] = hit
    out["email_kind"] = email_kind(out["email"], out["name"])
    # An address on another domain (e.g. a PR agency's media@ from a press release) needs a human look.
    out["email_domain_match"] = out["email"] == "unknown" or not dom or _base(out["email"].split("@")[-1]) == _base(dom)
    log(f"contacts: {c['name']}: {out['name']} ({out['title'] if out['name'] != 'unknown' else 'target ' + out['suggested_title']})"
        f", email {out['email']} [{out['email_kind']}]")
    return {**c, "contact": out}


async def find_contacts(companies: list[dict], startup: dict, p: dict) -> list[dict]:
    if len(companies) > 1:
        log(f"contacts: searching {len(companies)} companies")
    return list(await asyncio.gather(*(_one(c, startup, p) for c in companies)))

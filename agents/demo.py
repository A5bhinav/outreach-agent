"""Offline demo: the real pipeline end to end with a scripted model and fictional data.

`python main.py --demo` needs no API key, profile or sender file. Every agent step still runs
through the real code (dedupe, criteria gate, contact checks, reviewer rewrite policy, ledger,
footer, output files); only the model calls and page fetches are replaced with canned answers.
All companies, people and URLs are fictional (.example domains).
"""
import re

from . import contacts, llm, people, planner, researcher, reviewer, sourcer, writer

PROFILE = {
    "company_name": "Example Robotics (demo)",
    "founder_name": "Jordan Lee",
    "founder_bio": "led AMR deployments at a large 3PL before founding Example Robotics",
    "stage_and_backers": "Seed, $6M, led by Demo Ventures",
    "investor_note": "their robots already run in live warehouse operations, not just pilots",
    "pitch": "Autonomous mobile robots that move pallets, carts and materials around warehouses and yards; "
             "now expanding into construction laydown yards and prefab shops.",
    "icp_notes": "Mid-size US general contractors with their own yard, warehouse or prefab shop.",
    "proof_points": ["robots running in live warehouse deployments today"],
    "roles": [{"title": "Senior Systems Engineer, Field Deployments", "location": "Remote (US) or Boston",
               "why_interesting": "owns fleet bring-up end to end at customer sites"}],
    "sender": {"voice": "investor", "name": "Sam Rivera", "title": "Partner", "firm": "Demo Ventures",
               "email": "sam@demoventures.example", "firm_address": "1 Demo Plaza, Suite 100, San Francisco, CA 94105"},
}

DEFAULT_REQUEST = ("find general contractors and construction firms that might want robotics for materials "
                   "handling, mid-size, US")

_PEOPLE_WORDS = re.compile(r"\b(engineer|engineers|hire|hiring|recruit|candidate|candidates|developer|scientist)\b", re.I)

# Fictional companies: (name, slug, city, likely_fit, fact, date, contact, title, email kind, outcome)
COMPANIES = [
    ("Northfield Builders", "northfield", "Columbus, OH", "high",
     "Opened a 60,000 sq ft prefab and modular assembly facility in Groveport", "2026-06",
     "Dana Whitaker", "VP of Operations", "personal", "pass"),
    ("Granite Peak Construction", "granitepeak", "Denver, CO", "high",
     "Broke ground on a 400,000 sq ft distribution center for a regional grocer", "2026-08",
     "Marcus Bell", "Director of Preconstruction", "generic", "pass"),
    ("Harbor & Lane Contractors", "harborlane", "Tacoma, WA", "medium",
     "Moved its equipment yard to a new 12-acre site near the Port of Tacoma", "2026-04",
     "Priya Nandakumar", "Equipment and Logistics Manager", "personal", "pass"),
    ("Redstone Industrial", "redstone", "Huntsville, AL", "medium",
     "Hired its first Director of Construction Technology to lead a VDC team", "2026-02",
     "unknown", "unknown", "form", "pass"),
    ("Summit Megabuild Group", "summitmega", "Dallas, TX", "low",
     "Ranked among the ten largest US general contractors by revenue", "2026-05",
     "unknown", "unknown", "none", "too_big"),
    ("Brightline Homes", "brightline", "Phoenix, AZ", "low",
     "Builds single-family homes across three Phoenix subdivisions", "2025-11",
     "unknown", "unknown", "none", "no_yard"),
]

ENGINEERS = [
    ("Riley Chen", "Systems Engineer, Fleet Deployments", "Ridgeway Logistics Robotics", "Boston, MA",
     "Presented a talk on bringing up 150-robot AMR fleets in live warehouses at a robotics conference", "2026-05"),
    ("Morgan Adeyemi", "Senior Robotics Systems Engineer", "CartFlow Automation", "Pittsburgh, PA",
     "Co-authored a paper on multi-robot traffic control in mixed human-robot warehouses", "2025-12"),
    ("Alex Novak", "Field Robotics Lead", "Yardbot Systems", "Austin, TX",
     "Wrote an engineering blog post on commissioning autonomous tuggers at three distribution centers", "2026-03"),
]

# Hand-written demo copy per company: (opener, bridge, follow-up angle). Each opener is the
# researched fact; each bridge ties the startup to that specific situation as a guess.
COPY = {
    "northfield": ("Congrats on the new 60,000 sq ft prefab facility in Groveport.",
                   "A shop that size usually means a lot of carts and pallets crossing the floor between stations, and "
                   "that's exactly the hauling Example Robotics' robots already do in live warehouses.",
                   "prefab lines tend to be the easiest first pilot, since the routes are fixed"),
    "granitepeak": ("Your 400,000 sq ft distribution center groundbreaking for the regional grocer looks like a big one.",
                    "I'd guess the laydown yard on a build that size keeps a crew busy just moving material, which is "
                    "the work Example Robotics automates today in warehouses.",
                    "the same robots could stay useful for the grocer once the building is handed over"),
    "harborlane": ("Moving your equipment yard to the new 12-acre site by the Port of Tacoma is a big shift.",
                   "Curious whether a new layout is a chance to rethink how equipment and materials get moved around, "
                   "since that's what Example Robotics' robots handle at warehouse sites now.",
                   "a new yard layout is the cheapest moment to plan robot routes"),
    "redstone": ("Bringing on your first Director of Construction Technology to lead a VDC team caught my eye.",
                 "A new tech lead is often looking for a first automation pilot, and Example Robotics' yard and "
                 "warehouse robots are already in live use.",
                 "a VDC team could map robot routes straight from its models"),
}

PEOPLE_COPY = {
    "Riley Chen": ("your conference talk on bringing up 150-robot fleets in live warehouses was the most practical "
                   "take on commissioning I've seen.",
                   "Example Robotics is about to do exactly that at customer yards, with no lab buffer."),
    "Morgan Adeyemi": ("your paper on traffic control for mixed human-robot warehouses tackles the problem most "
                       "deployments trip over.",
                       "Example Robotics runs robots alongside crews every day, so that work would land directly."),
    "Alex Novak": ("your post on commissioning autonomous tuggers across three distribution centers read like a "
                   "field manual.",
                   "Example Robotics needs someone who has done that bring-up for real, more than once."),
}


def _first_lower(s: str) -> str:
    return s[0].lower() + s[1:]


def _site(slug: str) -> str:
    return f"https://www.{slug}.example"


def _page(slug: str) -> str:
    return f"https://www.{slug}.example/news"


class DemoModel:
    """Scripted answers keyed on the step label, shaped like the real model's submit payloads."""

    def __init__(self, request: str):
        self.mode = "people" if _PEOPLE_WORDS.search(request) else "companies"
        self.drafts: dict[str, int] = {}

    async def __call__(self, system, prompt, schema, *, web_searches=0, web_fetches=0, model="heavy", effort=None,
                       max_tokens=0, label=""):
        step, _, arg = label.partition("[")
        arg = arg.rstrip("]")
        return getattr(self, "_" + step)(arg, prompt)

    # --- steps ---
    def _preflight(self, arg, prompt):
        return {"ok": True}

    def _planner(self, arg, prompt):
        if arg == "more":
            return {"queries": ["general contractor new equipment yard 2026"]}
        if self.mode == "people":
            return {"mode": "people",
                    "queries": ["AMR fleet deployment engineering team", "warehouse robotics integrator engineers",
                                "multi-robot traffic control research"],
                    "rubric": [{"criterion": "hands-on deployment of mobile robot fleets", "why": "core of the role", "must_have": True},
                               {"criterion": "systems-level work across robots, software and site", "why": "role scope", "must_have": False},
                               {"criterion": "public evidence of recent work", "why": "a specific opener", "must_have": False}],
                    "disqualifiers": ["pure research with no deployments"],
                    "target_titles": ["Systems Engineer", "Field Robotics Engineer"],
                    "research_signals": ["talks", "papers", "engineering blog posts"],
                    "ask": "would you be open to a short, no-pressure chat with the founder?"}
        return {"mode": "companies",
                "queries": ["mid-size general contractor opens prefab facility 2026", "contractor new equipment yard",
                            "general contractor distribution center groundbreaking", "construction VDC innovation hire"],
                "rubric": [{"criterion": "runs its own yard, warehouse or prefab shop", "why": "where robots would work", "must_have": True},
                           {"criterion": "mid-size (roughly $50M-$1B revenue)", "why": "budget and fast decisions", "must_have": True},
                           {"criterion": "recent expansion or construction-technology hire", "why": "a reason to talk now", "must_have": False}],
                "disqualifiers": ["top-20 national GC", "residential-only builder"],
                "target_titles": ["VP of Operations", "Equipment Manager", "Director of Preconstruction"],
                "research_signals": ["new facilities", "equipment yards", "VDC teams"],
                "ask": "would you like an intro to the founder for a short call?"}

    def _sourcer(self, arg, prompt):
        pool = COMPANIES if self.mode == "companies" else COMPANIES[:4]
        i = sum(map(ord, arg)) % len(pool)  # deterministic across runs
        picks = pool[i:] + pool[:i]
        out = []
        for name, slug, city, fit, *_ in picks[:4]:
            # Some results only show the LinkedIn page; the researcher finds the homepage later.
            web = f"https://www.linkedin.com/company/{slug}" if slug == "harborlane" and "yard" not in arg else _site(slug)
            out.append({"name": name, "website": web, "location": city, "size_hint": "mid-size",
                        "source_url": f"https://news.example/{slug}", "likely_fit": fit})
        return {"companies": out}

    def _research(self, arg, prompt):
        c = next(c for c in COMPANIES if c[0] == arg)
        name, slug, city, _, fact, date, *_, outcome = c
        verdicts = {"pass": ["yes", "yes", "yes"], "too_big": ["yes", "no", "yes"], "no_yard": ["no", "yes", "unclear"]}[outcome]
        return {"website": _site(slug),
                "facts": [{"fact": fact, "date": date, "source_url": _page(slug)},
                          {"fact": f"Lists {city.split(',')[0]} as its headquarters", "date": "undated", "source_url": _site(slug)},
                          {"fact": "Completed a parking structure", "date": "2019", "source_url": _site(slug) + "/projects"}],
                "assessments": [{"criterion_number": i + 1, "verdict": v,
                                 "evidence_url": _page(slug) if v == "yes" else "none"} for i, v in enumerate(verdicts)],
                "location": city, "size_hint": "mid-size", "fit_reason": f"{fact.split(' for ')[0]}.",
                "disqualified": "top-20 national GC" if outcome == "too_big" else "no"}

    def _contact(self, arg, prompt):
        c = next(c for c in COMPANIES if c[0] == arg)
        name, slug, *_, person, title, kind, _ = c
        site = _site(slug)
        first, last = (person.split()[0].lower(), person.split()[-1].lower()) if person != "unknown" else ("", "")
        email = {"personal": f"{first}.{last}@{slug}.example", "generic": f"info@{slug}.example"}.get(kind, "unknown")
        return {"name": person, "title": title, "source_url": site + "/team" if person != "unknown" else "unknown",
                "email": email, "email_source_url": site + "/team" if email != "unknown" else "unknown",
                "contact_form_url": site + "/contact" if kind == "form" else "unknown", "suggested_title": "VP of Operations"}

    def _people(self, arg, prompt):
        i = [c[0] for c in COMPANIES].index(arg) % len(ENGINEERS)
        name, title, company, city, fact, date = ENGINEERS[i]
        handle = name.lower().replace(" ", "")
        return {"people": [{"name": name, "current_title": title, "current_company": company, "location": city,
                            "profile_url": f"https://www.linkedin.com/in/{handle}-demo", "email": "unknown",
                            "email_source_url": "unknown",
                            "facts": [{"fact": fact, "date": date, "source_url": f"https://{handle}.example/work"}],
                            "assessments": [{"criterion_number": 1, "verdict": "yes", "evidence_url": f"https://{handle}.example/work"},
                                            {"criterion_number": 2, "verdict": "yes", "evidence_url": f"https://{handle}.example/work"},
                                            {"criterion_number": 3, "verdict": "yes", "evidence_url": f"https://{handle}.example/work"}],
                            "fit_reason": fact}]}

    def _writer(self, arg, prompt):
        n = self.drafts[arg] = self.drafts.get(arg, 0) + 1
        if self.mode == "people":
            return self._write_person(arg, n)
        c = next(c for c in COMPANIES if c[0] == arg)
        name, slug, city, _, fact, date, person, title, kind, _ = c
        opener, bridge, angle = COPY[slug]
        first = person.split()[0] if person != "unknown" else None
        greet = f"Hi {name} team," if kind == "generic" or not first else f"Hi {first},"
        forward = f" Could you pass this to {person}, your {title}?" if kind == "generic" and first else ""
        if n == 1 and slug == "harborlane":  # first draft over-claims; the review sends it back once
            body = (f"{greet} {opener} You clearly struggle to move materials around the new site, and our "
                    "cutting-edge robots will revolutionize your yard! Want an intro?")
        else:
            body = (f"{greet} {opener}{forward} {bridge} My firm, Demo Ventures, backs the founder, Jordan Lee, "
                    "who ran AMR deployments at a large 3PL. Would you like an intro to Jordan for a short call?"
                    "\n\nSam Rivera\nPartner, Demo Ventures")
        return {"subject": f"Intro to Jordan Lee (Example Robotics) re: your {city.split(',')[0]} {'shop' if 'prefab' in fact else 'site'}",
                "body": body, "fact_used": fact, "fact_source_url": _page(slug),
                "followup_body": f"{greet} one more thought: {angle}. Still happy to connect you with Jordan if useful."
                                 "\n\nSam",
                "linkedin_note": f"{opener} I back Example Robotics (robots that haul materials in yards and "
                                 "warehouses). Open to an intro to the founder?"}

    def _write_person(self, arg, n):
        p = next(e for e in ENGINEERS if e[0] == arg)
        name, title, company, city, fact, date = p
        first = name.split()[0]
        hook, link = PEOPLE_COPY[name]
        body = (f"Hi {first}, {hook} {link} I'm a partner at Demo Ventures; we back Example Robotics, and the founder, "
                "Jordan Lee, is hiring a Senior Systems Engineer to own fleet bring-up at customer sites. Would you be "
                "open to a short, no-pressure chat with Jordan?\n\nSam Rivera\nPartner, Demo Ventures")
        return {"subject": f"{first}, a field deployment role at Example Robotics", "body": body, "fact_used": fact,
                "fact_source_url": f"https://{name.lower().replace(' ', '')}.example/work",
                "followup_body": f"Hi {first}, no pressure at all. Happy to send the role details first if that's easier."
                                 "\n\nSam",
                "linkedin_note": f"Hi {first}, {hook} I back Example Robotics, hiring for fleet bring-up. Open to a "
                                 "short chat with the founder?"}

    def _review(self, arg, prompt):
        def part(start, end=None):
            s = prompt.split(start, 1)[1]
            return s.split(end, 1)[0] if end else s
        draft = {"subject": part("Draft subject: ", "\n"), "body": part("Draft body:\n", "\n\nDraft follow-up:\n"),
                 "followup_body": part("Draft follow-up:\n", "\n\nDraft LinkedIn note:\n"),
                 "linkedin_note": part("Draft LinkedIn note:\n")}
        flags = []
        if "clearly struggle" in draft["body"]:
            flags.append({"claim": "You clearly struggle to move materials", "problem": "not in the research notes"})
            draft["body"] = draft["body"].replace(" You clearly struggle to move materials around the new site, and our "
                                                  "cutting-edge robots will revolutionize your yard!", "")
        return {"flags": flags, **draft}

    def _judge(self, arg, prompt):
        return {"specificity": 4, "credibility": 4, "relevance": 4, "human_tone": 4, "would_reply": True,
                "critique": "demo score"}


def _page_text_for(url: str) -> str | None:
    if "linkedin.com" in url:
        return None
    slug = re.sub(r"^https?://(www\.)?", "", url).split(".")[0]
    text = " ".join(" ".join(map(str, c)) for c in COMPANIES if c[1] == slug)
    text += " ".join(" ".join(e) for e in ENGINEERS if e[0].lower().replace(" ", "") == slug)
    if not text:
        return None
    for c in COMPANIES:
        if c[1] == slug and c[6] != "unknown":
            first, last = c[6].split()[0].lower(), c[6].split()[-1].lower()
            text += f" {first}.{last}@{slug}.example info@{slug}.example"
    return ("demo page filler " * 40 + text).lower().replace(",", "")


def install(request: str) -> DemoModel:
    """Swap the model and page fetcher for scripted ones; returns the model for inspection."""
    model = DemoModel(request)
    for m in (planner, sourcer, researcher, contacts, people, writer, reviewer, llm):
        if hasattr(m, "run_structured"):
            m.run_structured = model

    async def page_text(url: str) -> str | None:
        return _page_text_for(url)

    contacts.page_text = page_text
    reviewer.page_text = page_text

    async def no_apollo(*a, **k):
        return None

    contacts.apollo_email = no_apollo
    llm.STRICT_400 = False
    return model

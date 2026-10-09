"""Shared fixtures: a scripted stand-in for Claude so the whole pipeline runs offline.

`fake_model` patches `run_structured` in every agent module with a function that routes by the
step label ("planner", "sourcer[...]", "research[...]", ...) to handlers a test can override.
`pages` patches the page fetcher used for contact and fact verification.
"""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import contacts, llm, people, planner, researcher, reviewer, sourcer, writer  # noqa: E402

MODULES = (planner, sourcer, researcher, contacts, people, writer, reviewer)


def assess(*verdicts: str) -> list[dict]:
    return [{"criterion_number": i + 1, "verdict": v, "evidence_url": "https://ev.example" if v == "yes" else "none"}
            for i, v in enumerate(verdicts)]


def fact(url: str, date: str = "2026-03", text: str = "Opened a 50,000 sq ft prefab shop in Kasson") -> dict:
    return {"fact": text, "date": date, "source_url": url}


GOOD_BODY = ("Hi {name}, your new 50,000 sq ft prefab shop in Kasson opened in March. My firm backs Jordan Lee at "
             "Example Robotics, whose robots haul materials around yards and shops; I'd guess moving material across "
             "a site that size eats crew time. Want me to connect you with Jordan for a short call? Sam, Partner, Fund")


class FakeModel:
    def __init__(self, mode: str = "companies"):
        self.mode = mode
        self.calls: list[tuple[str, str, str | None]] = []
        self.handlers = {
            "planner": self.planner, "sourcer": self.sourcer, "research": self.research, "contact": self.contact,
            "people": self.people, "writer": self.writer, "review": self.review,
        }

    async def __call__(self, system, prompt, schema, *, web_searches=0, web_fetches=0, model="heavy", effort=None,
                       max_tokens=0, label=""):
        step = label.split("[")[0]
        self.calls.append((step, model, effort))
        if label == "planner[more]":
            return {"queries": ["q-extra"]}
        return self.handlers[step](label, prompt)

    # Default handlers; tests replace entries in self.handlers to change behaviour.
    def planner(self, label, prompt):
        return {"mode": self.mode, "queries": ["q1", "q2"],
                "rubric": [{"criterion": "runs its own yard", "why": "w", "must_have": True},
                           {"criterion": "recent expansion", "why": "w", "must_have": False}],
                "disqualifiers": ["national top-20 GC"], "target_titles": ["VP Operations"],
                "research_signals": ["prefab shop"], "ask": "would you like an intro to the founder?"}

    def sourcer(self, label, prompt):
        q = label[len("sourcer["):-1]
        if q == "q-extra":
            return {"companies": [{"name": "Extra GmbH", "website": "https://extra.de", "location": "Frankfurt, Germany",
                                   "size_hint": "?", "source_url": "https://news.example/x", "likely_fit": "medium"}]}
        return {"companies": [{"name": f"Acme {q} Inc", "website": f"https://www.acme{q}.com", "location": "Austin, TX",
                               "size_hint": "mid", "source_url": f"https://news.example/{q}", "likely_fit": "high"}]}

    def research(self, label, prompt):
        return {"website": "unknown", "facts": [fact("https://acme.example/news"), fact("https://old.example", "March 2019")],
                "assessments": assess("yes", "unclear"), "location": "Austin, TX", "size_hint": "mid",
                "fit_reason": "runs a prefab shop", "disqualified": "no"}

    def contact(self, label, prompt):
        return {"name": "Pat Smith", "title": "COO", "source_url": "https://acme.example/team",
                "email": "pat.smith@acme.example", "email_source_url": "https://acme.example/team",
                "contact_form_url": "unknown", "suggested_title": "COO"}

    def people(self, label, prompt):
        return {"people": [{"name": "Riley Chen", "current_title": "Systems Engineer", "current_company": "Locus",
                            "location": "Boston, MA", "profile_url": "https://www.linkedin.com/in/riley",
                            "email": "unknown", "email_source_url": "unknown",
                            "facts": [fact("https://riley.example/talk", text="Presented AMR fleet bring-up at ROSCon 2026")],
                            "assessments": assess("yes", "yes"), "fit_reason": "deployed AMR fleets"}]}

    def writer(self, label, prompt):
        return {"subject": "Intro to Jordan Lee (Example Robotics) re: your Kasson shop", "body": GOOD_BODY.format(name="Pat"),
                "fact_used": "Opened a 50,000 sq ft prefab shop in Kasson", "fact_source_url": "https://acme.example/news",
                "followup_body": "Hi Pat, one more angle: the Kasson shop is hiring yard staff. Want the intro? Sam",
                "linkedin_note": "Hi, I back a robotics startup whose work overlaps yours. Open to an intro to the founder?"}

    def review(self, label, prompt):
        def part(start, end=None):
            s = prompt.split(start, 1)[1]
            return s.split(end, 1)[0] if end else s
        return {"flags": [], "subject": part("Draft subject: ", "\n"), "body": part("Draft body:\n", "\n\nDraft follow-up:\n"),
                "followup_body": part("Draft follow-up:\n", "\n\nDraft LinkedIn note:\n"),
                "linkedin_note": part("Draft LinkedIn note:\n")}


PAGE = ("filler " * 120) + " pat smith coo pat.smith@acme.example 50000 sq ft prefab shop kasson riley chen "


@pytest.fixture
def fake_model(monkeypatch):
    fm = FakeModel()
    for m in MODULES:
        if hasattr(m, "run_structured"):
            monkeypatch.setattr(m, "run_structured", fm)
    monkeypatch.setattr(llm, "STRICT_400", False)
    monkeypatch.setattr(llm, "errors", [])
    return fm


@pytest.fixture
def pages(monkeypatch):
    """Map of URL substring -> page text; anything unmatched is 'not fetchable' (None)."""
    table = {"acme.example": PAGE, "riley.example": PAGE}

    async def page_text(url):
        if "linkedin.com" in url:
            return None
        return next((v for k, v in table.items() if k in url), None)

    monkeypatch.setattr(contacts, "page_text", page_text)
    monkeypatch.setattr(reviewer, "page_text", page_text)
    return table


@pytest.fixture
def profile(tmp_path) -> dict:
    cfg = yaml.safe_load((ROOT / "startup.yaml").read_text())
    text = yaml.safe_dump(cfg).replace("PLACEHOLDER: ", "")
    return yaml.safe_load(text)

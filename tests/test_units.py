"""Unit tests for the pure helpers: dates, criteria, dedupe keys, contact checks, compliance."""
import datetime

import pytest

from agents import compliance, contacts, evidence, sourcer
from conftest import assess

TODAY = datetime.date(2026, 10, 9)


@pytest.mark.parametrize("date,months", [
    ("2025-11-03", 11), ("Nov 2025", 11), ("November 3, 2025", 11), ("11/2025", 11), ("Q3 2024", 26),
    ("2024–2025", 16), ("2024-25", 16), ("2025-26 season", 4), ("Expected 2028", 0), ("Mayor's award, 2025", 16),
    ("Junior league 2025", 16), ("Sept. 2025", 13), ("March 2019", 91), ("undated", None),
])
def test_age_months(date, months):
    assert evidence.age_months(date, TODAY) == months


def test_fresh_drops_stale_and_unsourced_and_sorts_dated_first():
    facts = [{"fact": "a", "date": "undated", "source_url": "https://a"},
             {"fact": "b", "date": "2019", "source_url": "https://b"},
             {"fact": "c", "date": "2026-09", "source_url": "https://c"},
             {"fact": "d", "date": "2026-09", "source_url": "unknown"}]
    assert [f["fact"] for f in evidence.fresh(facts)] == ["c", "a"]


def test_judge_gate():
    rubric = [{"criterion": "a", "must_have": True}, {"criterion": "b", "must_have": True}, {"criterion": "c"}]
    assert evidence.judge(assess("yes", "yes", "no"), rubric)["must_haves_met"]
    one_unclear = evidence.judge(assess("yes", "unclear", "yes"), rubric)
    assert one_unclear["passes"] and not one_unclear["must_haves_met"] and one_unclear["unclear_must_haves"] == ["b"]
    assert not evidence.judge(assess("unclear", "unclear", "yes"), rubric)["passes"]
    assert evidence.judge(assess("no", "yes", "yes"), rubric)["must_have_failed"]
    # A "yes" without an evidence link doesn't count.
    unsourced = [{"criterion_number": 1, "verdict": "yes", "evidence_url": "none"}]
    assert evidence.judge(unsourced, rubric[:1])["verdicts"]["a"] == "unclear"


def test_real_domain_and_name_key():
    assert sourcer.real_domain("https://www.acme.com/about") == "acme.com"
    assert sourcer.real_domain("https://www.linkedin.com/company/acme") == ""
    assert sourcer.real_domain("https://www.prnewswire.com/news/x") == ""
    assert sourcer.real_domain("unknown") == ""
    assert sourcer.name_key("ABC Construction Co.") == sourcer.name_key("abc construction")
    assert sourcer.name_key("The Company") != ""


@pytest.mark.parametrize("name,ok", [("Wei Li", False), ("Jane Doe, PE", True), ("Dr. Jane Doe", True),
                                     ("John Smith Jr.", False), ("Jane Q. Doe", True)])
def test_mentions_name(name, ok):
    text = "<li>our team</li> jane q. doe, director of operations. people page. open roles"
    assert contacts.mentions_name(text.lower(), name) is ok


def test_email_kind_and_allowed():
    assert contacts.email_kind("info@acme.com", "Pat Smith") == "generic"
    assert contacts.email_kind("pat.smith@acme.com", "Pat Smith") == "personal"
    assert contacts.email_kind("psmith@acme.com", "Pat Smith") == "personal"
    assert contacts.email_kind("unknown", "Pat Smith") == "none"
    assert not contacts.email_allowed("a@b.com", "https://github.com/x")
    assert not contacts.email_allowed("a@b.com", "https://www.linkedin.com/in/x")
    assert contacts.email_allowed("a@b.com", "https://b.com/team")
    assert contacts.email_allowed("a@b.com", "apollo (verified)")
    assert not contacts.email_allowed("not-an-email", "https://b.com")


def test_cloudflare_email_decoding():
    email = "pat@acme.com"
    key = 0x42
    enc = f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in email)
    assert contacts._cf_emails(f'<a href="/cdn-cgi/l/email-protection#{enc}">x</a>') == [email]


@pytest.mark.parametrize("loc,eu", [("Frankfurt, Germany", True), ("München, Deutschland", True), ("Manchester", True),
                                    ("unknown", True), ("Austin, TX", False), ("New South Wales, Australia", False)])
def test_eu_uk(loc, eu):
    assert compliance.is_eu_uk(loc) is eu


def test_missing_legal(profile):
    assert compliance.missing_legal(profile) == []
    profile["sender"]["firm_address"] = "PLACEHOLDER: x"
    del profile["sender"]["firm"]
    assert set(compliance.missing_legal(profile)) == {"firm_address", "firm"}


def _target(name="Acme", domain="acme.com", email="pat@acme.com", website="https://acme.com"):
    return {"name": name, "domain": domain, "website": website, "contact": {"email": email, "name": "Pat"}}


def test_ledger_rules(tmp_path):
    path = tmp_path / "ledger.csv"
    led = compliance.Ledger(path, "Robo")
    led.opt_out("https://www.optedout.com/")
    led.opt_out("jane@other.com")
    led.record([_target()], run="/runs/1")

    led = compliance.Ledger(path, "Robo")
    # Domain opt-out covers a target whose only link to the domain is its email address.
    assert led.skip_reason(_target("X", "", "info@optedout.com", "unknown")) == "opted out"
    assert led.skip_reason(_target("Y", "y.com", "jane@other.com")) == "opted out"
    assert "drafted" in led.skip_reason(_target())            # same portfolio company, recent draft
    assert compliance.Ledger(path, "Other").skip_reason(_target()) is None  # drafts don't block other companies
    # Unknown homepages never match each other.
    led.record([_target("Nosite", "unknown", "unknown", "unknown")], run="/runs/1")
    assert compliance.Ledger(path, "Robo").skip_reason(_target("Other nosite", "unknown", "unknown", "unknown")) is None

    assert led.mark_sent("/runs/1", None) == 2
    led2 = compliance.Ledger(path, "Other")
    assert "sent an intro for Robo" in led2.skip_reason(_target())


def test_ledger_drafts_expire(tmp_path):
    path = tmp_path / "ledger.csv"
    led = compliance.Ledger(path, "Robo")
    led.record([_target()])
    led.rows[0]["date"] = (TODAY.today() - datetime.timedelta(days=compliance.DRAFT_DAYS + 1)).isoformat()
    assert led.skip_reason(_target()) is None


def test_ledger_migrates_old_header(tmp_path):
    path = tmp_path / "ledger.csv"
    path.write_text("date,portfolio_company,target,company,domain,email,profile_url,status\n"
                    "2026-10-01,Robo,Acme,Acme,acme.com,,,Opted Out\n")
    led = compliance.Ledger(path, "Robo")
    assert path.read_text().splitlines()[0].endswith(",run")
    assert led.skip_reason(_target()) == "opted out"  # status normalized


def test_footer(profile):
    t = {"location": "Frankfurt, Germany", "website": "https://x.de", "contact": {"name": "unknown"}}
    text = compliance.footer(profile, t)
    assert profile["sender"]["firm_address"] in text and "no thanks" in text and "object" in text
    t["location"] = "Austin, TX"
    assert "object" not in compliance.footer(profile, t)

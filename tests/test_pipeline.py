"""End-to-end runs of main.run with the scripted model: both modes, ledger, round 2, review, output files."""
import asyncio
import csv
import email
import subprocess
import sys

import pytest

import main as M
from agents import compliance
from conftest import ROOT, assess, fact


def run(profile, tmp_path, n=2, ledger=None, confirm=None):
    ledger = ledger or compliance.Ledger(tmp_path / "ledger.csv", profile["company_name"])
    return asyncio.run(M.run("find GCs", profile, n, tmp_path / "out", ledger, preflight=False, confirm=confirm))


def rows_by_target(rows):
    return {r["target"]: r for r in rows}


def test_companies_mode_end_to_end(fake_model, pages, profile, tmp_path):
    rows = run(profile, tmp_path)
    assert {r["target"] for r in rows} == {"Acme q1 Inc", "Acme q2 Inc"}
    r = rows[0]
    assert r["status"] == "ready" and r["channel"] == "email" and r["to_email"] == "pat.smith@acme.example"
    assert r["contact_verified"] == "yes" and r["fact_check"] == "yes"
    assert profile["sender"]["firm_address"] in r["email_body"]           # legal footer attached
    assert r["send_link"].startswith("https://mail.google.com/") and r["followup_link"]
    out = tmp_path / "out"
    assert {p.name for p in out.iterdir()} >= {"results.html", "results.md", "results.csv", "run.json", "drafts"}
    eml = email.message_from_bytes(next((out / "drafts").glob("01-*[!p].eml")).read_bytes())
    assert eml["To"] == "pat.smith@acme.example" and eml["X-Unsent"] == "1"
    # Stale facts never reach the notes; the ledger has the ready messages.
    assert "2019" not in (out / "results.md").read_text()
    assert len(list(csv.DictReader(open(tmp_path / "ledger.csv")))) == 2
    # Per-step model and effort routing.
    assert ("sourcer", "light", "low") in fake_model.calls and ("writer", "heavy", "medium") in fake_model.calls


def test_round_two_backfills_and_eu_notice(fake_model, pages, profile, tmp_path):
    fake_model.handlers["research"] = lambda label, prompt: {
        **fake_model.research(label, prompt), "location": "Frankfurt, Germany" if "Extra" in label else "Austin, TX"}
    rows = rows_by_target(run(profile, tmp_path, n=3))
    assert "Extra GmbH" in rows                                           # came from the extra queries
    assert "object" in rows["Extra GmbH"]["email_body"]                   # GDPR notice for a German recipient
    assert "object" not in rows["Acme q1 Inc"]["email_body"]


def test_opted_out_domain_found_via_linkedin_is_never_contacted(fake_model, pages, profile, tmp_path):
    def sourcer(label, prompt):
        return {"companies": [{"name": "Opted Builders", "website": "https://linkedin.com/company/ob", "location": "?",
                               "size_hint": "?", "source_url": "https://x.example", "likely_fit": "high"}]}

    def research(label, prompt):
        return {**fake_model.research(label, prompt), "website": "https://optedout.com"}

    fake_model.handlers.update(sourcer=sourcer, research=research)
    led = compliance.Ledger(tmp_path / "ledger.csv", profile["company_name"])
    led.opt_out("optedout.com")
    with pytest.raises(SystemExit):
        run(profile, tmp_path, n=1, ledger=led)
    assert not any(step == "contact" for step, _, _ in fake_model.calls)  # skipped before paying for contacts


def test_sourcer_upgrades_homepage_found_later(fake_model, pages, profile, tmp_path):
    def sourcer(label, prompt):
        site = "https://linkedin.com/company/up" if "q1" in label else "https://upgrade.example"
        return {"companies": [{"name": "Upgrade Co", "website": site, "location": "OH", "size_hint": "?",
                               "source_url": f"https://src/{label}", "likely_fit": "high"}]}

    fake_model.handlers["sourcer"] = sourcer
    run(profile, tmp_path, n=1)
    import json
    cands = json.loads((tmp_path / "out" / "run.json").read_text())["candidates"]
    assert [(c["website"], c["domain"]) for c in cands[:1]] == [("https://upgrade.example", "upgrade.example")]


def test_must_have_gate_drops_target(fake_model, pages, profile, tmp_path):
    fake_model.handlers["research"] = lambda label, prompt: {
        **fake_model.research(label, prompt), "assessments": assess("no", "yes")}
    with pytest.raises(SystemExit, match="no usable targets"):
        run(profile, tmp_path, n=1)


def test_reviewer_rewrites_only_what_it_cannot_fix(fake_model, pages, profile, tmp_path):
    bad = {"subject": "intro!", "body": "Hi Pat, leverage our cutting-edge robots! " * 2, "fact_used": "x",
           "fact_source_url": "https://acme.example/news", "followup_body": "Bump.", "linkedin_note": "hi"}
    writes = []

    def writer(label, prompt):
        writes.append(label)
        return fake_model.writer(label, prompt) if "reviewer flagged" in prompt else bad

    fake_model.handlers["writer"] = writer
    rows = run(profile, tmp_path, n=1)
    assert len(writes) == 2 and rows[0]["status"] == "ready" and "leverage" not in rows[0]["email_body"]


def test_shared_inbox_and_placeholders_block(fake_model, pages, profile, tmp_path):
    fake_model.handlers["contact"] = lambda label, prompt: {**fake_model.contact(label, prompt), "email": "info@acme.example"}
    pages["acme.example"] += " info@acme.example"
    profile["proof_points"] = ["PLACEHOLDER: robots in live warehouses"]
    fake_model.handlers["writer"] = lambda label, prompt: {**fake_model.writer(label, prompt),
                                                           "body": fake_model.writer(label, prompt)["body"] + " robots in live warehouses"}
    r = run(profile, tmp_path, n=1)[0]
    assert r["channel"] == "email (shared inbox)" and r["email_kind"] == "generic"
    assert r["status"].startswith("not ready") and "placeholder" in r["status"]
    assert not (tmp_path / "ledger.csv").exists()                         # nothing unsendable is recorded


def test_people_mode_uses_linkedin_note(fake_model, pages, profile, tmp_path):
    fake_model.mode = "people"
    r = run(profile, tmp_path, n=1)[0]
    assert r["target"] == "Riley Chen" and r["channel"] == "LinkedIn note" and r["status"] == "ready"
    assert r["send_link"] == "https://www.linkedin.com/in/riley" and r["linkedin_note"]
    assert r["contact_verified"].startswith("unchecked")                  # LinkedIn is never fetched


def test_plan_only_spends_nothing_after_planning(fake_model, pages, profile, tmp_path):
    with pytest.raises(SystemExit, match="stopped after planning"):
        run(profile, tmp_path, confirm=M._confirm(2, False, True))
    assert [step for step, _, _ in fake_model.calls] == ["planner"]


def test_cli_refuses_placeholder_config(tmp_path):
    res = subprocess.run([sys.executable, str(ROOT / "main.py"), "x", "--ledger", str(tmp_path / "l.csv")],
                         capture_output=True, text=True)
    assert res.returncode == 1 and "PLACEHOLDER" in res.stderr and "nothing was spent" in res.stderr


def test_cli_opt_out_and_mark_sent(tmp_path, fake_model, pages, profile):
    run(profile, tmp_path)
    led = str(tmp_path / "ledger.csv")
    out = subprocess.run([sys.executable, str(ROOT / "main.py"), "--mark-sent", str(tmp_path / "out"), "--ledger", led],
                         capture_output=True, text=True)
    assert "marked 2 draft(s)" in out.stdout
    subprocess.run([sys.executable, str(ROOT / "main.py"), "--opt-out", "x@y.com", "--ledger", led], check=True,
                   capture_output=True)
    statuses = [r["status"] for r in csv.DictReader(open(led))]
    assert statuses == ["sent", "sent", "opted_out"]


def test_fact_check_blocks_unsupported_opener(fake_model, pages, profile, tmp_path):
    fake_model.handlers["writer"] = lambda label, prompt: {
        **fake_model.writer(label, prompt), "fact_used": "Won the 2026 Denver Airport terminal contract worth 900 million"}
    r = run(profile, tmp_path, n=1)[0]
    assert r["fact_check"] == "NO" and r["status"].startswith("not ready")


def test_fact_fixture_unused_guard():
    assert fact("https://x")["source_url"] == "https://x"


def test_resume_finishes_selected_targets_without_redoing_work(fake_model, pages, profile, tmp_path):
    import json
    calls = {"n": 0}
    real_writer = fake_model.writer

    def flaky_writer(label, prompt):  # the second target's writer "crashes" the first run
        calls["n"] += 1
        if "q2" in label and calls["n"] < 10:
            raise RuntimeError("simulated crash")
        return real_writer(label, prompt)

    fake_model.handlers["writer"] = flaky_writer
    rows = run(profile, tmp_path)                       # first run: one target lost to the crash
    assert [r["target"] for r in rows] == ["Acme q1 Inc"]
    state = json.loads((tmp_path / "out" / "run.json").read_text())
    assert len(state["selected"]) == 2 and len(state["done"]) == 1 and state["recorded"]

    # Simulate an interrupted run: nothing recorded yet, one target unfinished.
    state["recorded"] = False
    (tmp_path / "ledger.csv").unlink()
    calls["n"] = 100
    before = len(fake_model.calls)
    led = compliance.Ledger(tmp_path / "ledger.csv", profile["company_name"])
    rows = asyncio.run(M.run(state["request"], profile, 2, tmp_path / "out", led, preflight=False, resume=state))
    assert {r["target"] for r in rows} == {"Acme q1 Inc", "Acme q2 Inc"}
    steps = [s for s, _, _ in fake_model.calls[before:]]
    assert "planner" not in steps and "sourcer" not in steps and "research" not in steps
    assert steps.count("writer") == 1                   # only the unfinished target
    assert len(list(csv.DictReader(open(tmp_path / "ledger.csv")))) == 2


def test_eval_scorer_and_judge(fake_model, pages, profile, tmp_path, monkeypatch):
    import evals.judge as J
    import evals.score as S
    run(profile, tmp_path)
    s = S.score(tmp_path / "out", "companies")
    assert s["mode_ok"] and s["ready"] == 2 and s["ready_by_channel"] == {"email": 2} and s["body_similarity"] > 0.9

    async def fake_judge(system, prompt, schema, **kw):
        return {"specificity": 4, "credibility": 4, "relevance": 3, "human_tone": 5, "would_reply": True, "critique": "ok"}
    monkeypatch.setattr(J.llm, "run_structured", fake_judge)
    j = asyncio.run(J.judge(tmp_path / "out"))
    assert j["judged"] == 2 and j["specificity"] == 4 and j["would_reply_share"] == 1.0

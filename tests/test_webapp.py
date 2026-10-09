"""The local web app (webapp.py): API flow end to end in demo mode, settings writes, and the
same-origin guard that stops other websites from triggering paid runs."""
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

import webapp
from agents import profiles
from conftest import ROOT


@pytest.fixture
def server(tmp_path, monkeypatch):
    port_dir = tmp_path / "portfolio"
    port_dir.mkdir()
    for f in ("_template.yaml", "example-robotics.yaml"):
        shutil.copy(ROOT / "portfolio" / f, port_dir / f)
    monkeypatch.setattr(profiles, "PORTFOLIO", port_dir)
    monkeypatch.setattr(profiles, "TEMPLATE", port_dir / "_template.yaml")
    monkeypatch.setattr(profiles, "SENDER", tmp_path / "sender.yaml")
    orig_companies = profiles.companies
    monkeypatch.setattr(profiles, "companies", lambda portfolio=port_dir: orig_companies(portfolio))
    orig_resolve = profiles.resolve
    monkeypatch.setattr(profiles, "resolve", lambda company=None, config=None, request="", portfolio=port_dir:
                        orig_resolve(company, config, request, portfolio))
    orig_new = profiles.new_company
    monkeypatch.setattr(profiles, "new_company", lambda name, portfolio=port_dir: orig_new(name, portfolio))
    for name, val in (("OUT", tmp_path / "outputs"), ("LEDGER", tmp_path / "ledger.csv"), ("ENV", tmp_path / ".env")):
        monkeypatch.setattr(webapp, name, val)
    (tmp_path / "outputs").mkdir()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", tmp_path
    httpd.shutdown()


def call(base, path, body=None, method=None, headers=None):
    h = {"Content-Type": "application/json", "X-Outreach": "1", **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=h, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read()
            return r.status, (json.loads(data) if "json" in ctype else data)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_done(base, run_id):
    for _ in range(200):
        _, d = call(base, f"/api/runs/{run_id}")
        if d["job"] and d["job"]["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("run didn't finish")


@pytest.mark.parametrize("request_text,mode,channel", [
    ("find general contractors for robotics", "companies", "email"),
    ("find systems engineers who have deployed AMR fleets", "people", "LinkedIn note"),
])
def test_demo_flow_end_to_end(server, request_text, mode, channel):
    base, tmp = server
    code, page = call(base, "/")
    assert code == 200 and b"New request" in page
    code, plan = call(base, "/api/plan", {"request": request_text, "n": 3, "demo": True})
    assert code == 200 and plan["plan"]["mode"] == mode and plan["estimate"]["calls"] > 0
    code, r = call(base, "/api/runs", {"request": request_text, "n": 3, "demo": True, "plan": plan["plan"]})
    assert code == 202 and r["id"].startswith("demo-")
    d = wait_done(base, r["id"])
    assert d["status"] == "done" and d["targets"] == 3 and d["ready"] == 3
    assert any("using the approved plan" in line for line in d["job"]["log"])  # the plan wasn't redone
    assert {t["channel"] for t in d["live"]} >= {channel}
    code, html = call(base, f"/runs/{r['id']}/results.html")
    assert code == 200 and b"Outreach" in html
    _, runs = call(base, "/api/runs")
    assert runs["runs"][0]["id"] == r["id"] and runs["runs"][0]["company"] == "Example Robotics (demo)"
    # Ledger sync from the inbox (demo runs keep their own ledger in the run folder).
    first = d["live"][0]["target"]
    assert call(base, "/api/ledger", {"run": r["id"], "targets": [first], "sent": True})[1] == {"updated": 1}
    assert f"{first},sent" in "".join(l.split(",")[2] + "," + l.split(",")[7] for l in (tmp / "outputs" / r["id"] / "ledger.csv").read_text().splitlines())
    assert call(base, "/api/ledger", {"run": r["id"], "targets": [first], "sent": False})[1] == {"updated": 1}


def test_real_run_requires_setup(server):
    base, _ = server
    code, err = call(base, "/api/plan", {"request": "find GCs", "company": "example-robotics"})
    assert code == 400 and "placeholder" in err["error"] and "API key" in err["error"]


def test_profiles_sender_and_keys(server):
    base, tmp = server
    code, r = call(base, "/api/companies", {"name": "Gamma Grid"})
    assert code == 201 and r["slug"] == "gamma-grid"
    prof = {"company_name": "Gamma Grid", "founder_name": "Ana Ruiz", "pitch": "Grid batteries.",
            "proof_points": ["live at 3 utilities", ""], "roles": [{"title": "Controls engineer", "location": "Remote"}, {"title": ""}]}
    assert call(base, "/api/companies/gamma-grid", {"profile": prof}, method="PUT")[0] == 200
    saved = yaml.safe_load((tmp / "portfolio" / "gamma-grid.yaml").read_text())
    assert saved["proof_points"] == ["live at 3 utilities"] and saved["roles"] == [{"title": "Controls engineer", "location": "Remote"}]
    assert call(base, "/api/companies/gamma-grid", {"profile": {"company_name": "x"}}, method="PUT")[0] == 400
    sender = {"name": "Sam", "title": "Partner", "firm": "Fund", "email": "sam@fund.example", "firm_address": "1 Main St", "voice": "investor"}
    assert call(base, "/api/sender", sender, method="PUT")[0] == 200
    code, st = call(base, "/api/state")
    assert st["sender_ready"] and {c["slug"] for c in st["companies"]} == {"example-robotics", "gamma-grid"}
    assert call(base, "/api/keys", {"anthropic": "sk-ant-test-1234567890", "remember": True})[1]["saved"]
    assert "ANTHROPIC_API_KEY=sk-ant-test-1234567890" in (tmp / ".env").read_text()
    _, st = call(base, "/api/state")
    assert st["keys"]["anthropic"].startswith("sk-ant-") and "1234567890" not in st["keys"]["anthropic"]  # masked


def test_same_origin_guard(server):
    base, _ = server
    assert call(base, "/api/plan", {"request": "x", "demo": True}, headers={"X-Outreach": "0"})[0] == 403
    assert call(base, "/api/plan", {"request": "x", "demo": True}, headers={"Origin": "https://evil.example"})[0] == 403
    assert call(base, "/api/state", headers={"Host": "evil.example"})[0] == 403  # DNS rebinding
    assert call(base, "/runs/../webapp.py")[0] in (400, 404)
    assert call(base, "/runs/nope/..%2F..%2Fwebapp.py")[0] in (400, 404)

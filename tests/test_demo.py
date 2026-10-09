"""The offline demo must work on a fresh checkout: no API key, profile, sender file or network."""
import csv
import os
import subprocess
import sys

import pytest

from conftest import ROOT


@pytest.mark.parametrize("request_text,channels", [
    (None, {"email", "email (shared inbox)", "contact form"}),
    ("find systems engineers who have deployed AMR fleets", {"LinkedIn note"}),
])
def test_demo_runs_without_any_setup(tmp_path, request_text, channels):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "APOLLO"))}
    env["HTTPS_PROXY"] = env["HTTP_PROXY"] = "http://127.0.0.1:9"  # any real network call would fail
    cmd = [sys.executable, str(ROOT / "main.py"), "--demo", "--out", str(tmp_path / "demo")]
    if request_text:
        cmd.insert(3, request_text)
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    rows = list(csv.DictReader(open(tmp_path / "demo" / "results.csv", encoding="utf-8-sig")))
    assert rows and all(r["status"] == "ready" for r in rows)
    assert {r["channel"] for r in rows} == channels
    assert all("reads like" not in r["to_check"] for r in rows)       # demo copy isn't templated
    assert (tmp_path / "demo" / "results.html").exists()
    assert not (ROOT / "ledger.csv").exists() or "Example Robotics (demo)" not in (ROOT / "ledger.csv").read_text()

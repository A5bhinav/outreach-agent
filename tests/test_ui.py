"""Drive the review app (results.html) in headless Chrome: edits, Gmail links, sent state, filters,
keyboard, search, persistence. Skipped when Chrome isn't installed."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

CHROME = next((p for p in (shutil.which("google-chrome"), shutil.which("google-chrome-stable"), shutil.which("chromium"),
                           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome") if p and Path(p).exists()), None)


@pytest.mark.skipif(not CHROME, reason="Chrome not installed")
def test_review_app_interactions(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC")}
    subprocess.run([sys.executable, str(ROOT / "main.py"), "--demo", "--out", str(tmp_path / "run")],
                   check=True, capture_output=True, env=env, timeout=60)
    page = (tmp_path / "run" / "results.html").as_uri()
    harness = tmp_path / "harness.html"
    harness.write_text(f'<body><iframe id=f src="{page}" style="width:1400px;height:900px"></iframe><pre id=out></pre>'
                       f'<script>{(ROOT / "tests" / "ui_harness.js").read_text()}</script></body>')
    dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--allow-file-access-from-files",
                          "--virtual-time-budget=8000", "--dump-dom",
                          harness.as_uri()], capture_output=True, text=True, timeout=90).stdout
    out = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    results = re.findall(r"(PASS|FAIL) ([^\n<]+)", out.group(1) if out else "")
    assert len(results) == 20, out.group(1) if out else dom[-2000:]
    assert [m for s, m in results if s == "FAIL"] == []

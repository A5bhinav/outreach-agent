"""Run the eval requests and score them: python evals/run.py [--n 5] [--only id1,id2] [--config startup.yaml]

Each request runs through main.py into evals/results/<timestamp>/<id>/, with its own throwaway
ledger so eval runs never touch the real one. This makes real API calls and costs money; start
with --n 3 and --only.
"""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score import score  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=5)
ap.add_argument("--only", default="")
ap.add_argument("--config", default="startup.yaml")
ap.add_argument("--effort", default="medium")
args = ap.parse_args()

cases = yaml.safe_load((HERE / "requests.yaml").read_text())
if args.only:
    cases = [c for c in cases if c["id"] in args.only.split(",")]
base = HERE / "results" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
results = []
for c in cases:
    out = base / c["id"]
    cmd = [sys.executable, str(HERE.parent / "main.py"), c["request"], "--n", str(args.n),
           "--config", c.get("config", args.config), "--out", str(out), "--ledger", str(out / "ledger.csv"),
           "--effort", args.effort]
    print(f"== {c['id']}", file=sys.stderr, flush=True)
    rc = subprocess.run(cmd).returncode
    if (out / "run.json").exists():
        r = score(out, c.get("expect_mode"))
        r["exit_code"] = rc
        results.append(r)
        print(json.dumps(r), flush=True)
(base / "scores.json").write_text(json.dumps(results, indent=2))
print(f"scores -> {base / 'scores.json'}", file=sys.stderr)

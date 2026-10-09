"""Score finished runs: python evals/score.py outputs/<run> [outputs/<run> ...]

Reads each run's run.json and results.csv and prints one line of metrics per run, so a change
can be compared against a baseline on the same requests. No API calls.
"""
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.evidence import MAX_AGE_MONTHS  # noqa: E402


def score(run: Path, expect_mode: str | None = None) -> dict:
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    rows = list(csv.DictReader(open(run / "results.csv", encoding="utf-8-sig"))) if (run / "results.csv").exists() else []
    targets = state.get("targets", [])
    facts = [f for t in targets for f in t.get("facts", [])]
    dated = [f for f in facts if f.get("age_months") is not None]
    words = [len(r["email_body"].split("\n\n--")[0].split()) for r in rows if r["email_body"]]
    flags = [r["flags"] for r in rows]
    mode = state.get("plan", {}).get("mode")
    return {
        "run": run.name,
        "mode_ok": None if expect_mode is None else mode == expect_mode,
        "targets": len(rows),
        "ready": sum(r["ready_to_send"] == "yes" for r in rows),
        "with_email": sum(bool(r["to_email"]) for r in rows),
        "named_contact": sum(r["contact_name"] != "unknown" for r in rows),
        "blockers": sum("BLOCKER" in f for f in flags),
        "flagged": sum(f != "none" for f in flags),
        "median_words": statistics.median(words) if words else 0,
        "dated_fact_share": round(len(dated) / len(facts), 2) if facts else 0,
        "stale_facts": sum(f["age_months"] > MAX_AGE_MONTHS for f in dated),
        "distinct_subjects": len({r["subject"] for r in rows}),
        "errors": len(state.get("errors", [])),
        "calls": state.get("usage", {}).get("calls", 0),
        "web_searches": state.get("usage", {}).get("web_searches", 0),
    }


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(json.dumps(score(Path(arg))))

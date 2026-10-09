"""Score finished runs: python evals/score.py outputs/<run> [outputs/<run> ...]

Reads each run's run.json and results.csv and prints one line of metrics per run, so a change
can be compared against a baseline on the same requests. No API calls.
"""
import csv
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.evidence import MAX_AGE_MONTHS  # noqa: E402


def _grams(text: str) -> set:
    w = re.findall(r"[a-z']+", text.lower())
    return set(zip(w, w[1:], w[2:]))


def _similarity(bodies: list[str]) -> float:
    """Mean pairwise 3-gram Jaccard between email bodies (footer excluded); lower = less templated."""
    g = [_grams(b) for b in bodies if b]
    pairs = [(a, b) for i, a in enumerate(g) for b in g[i + 1:] if a and b]
    return round(sum(len(a & b) / len(a | b) for a, b in pairs) / len(pairs), 3) if pairs else 0.0


def score(run: Path, expect_mode: str | None = None) -> dict:
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    rows = list(csv.DictReader(open(run / "results.csv", encoding="utf-8-sig"))) if (run / "results.csv").exists() else []
    targets = state.get("targets", [])
    facts = [f for t in targets for f in t.get("facts", [])]
    dated = [f for f in facts if f.get("age_months") is not None]
    words = [len(r["email_body"].rsplit("\n\n", 1)[0].split()) for r in rows if r["email_body"]]
    flags = [r["to_check"] for r in rows]
    bodies = [r["email_body"].split("\n\n")[0:-1] for r in rows]
    mode = state.get("plan", {}).get("mode")
    channels: dict[str, int] = {}
    for r in rows:
        if r["status"] == "ready":
            channels[r["channel"]] = channels.get(r["channel"], 0) + 1
    return {
        "run": run.name,
        "mode_ok": None if expect_mode is None else mode == expect_mode,
        "targets": len(rows),
        "ready": sum(r["status"] == "ready" for r in rows),
        "ready_by_channel": channels,
        "ready_shared_inbox": sum(r["status"] == "ready" and "shared" in r["channel"] for r in rows),
        "with_email": sum(bool(r["to_email"]) for r in rows),
        "named_contact": sum(r["contact_name"] != "unknown" for r in rows),
        "blockers": sum("BLOCKER" in f for f in flags),
        "flagged": sum(f != "none" for f in flags),
        "weak_hooks": sum("weak hook" in f for f in flags),
        "fact_check_failed": sum(r["fact_check"] == "NO" for r in rows),
        "body_similarity": _similarity(["\n\n".join(b) for b in bodies]),
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

"""Model-graded quality scores for a finished run's messages (real API calls; costs a little).

    python evals/judge.py outputs/<run> [outputs/<run> ...]

For each message, a skeptical reader persona scores specificity, credibility, relevance and
tone (1-5), says whether they'd plausibly reply, and gives a one-line critique. Scores are
written to <run>/judge.json and summarized on stdout, so prompt changes can be compared on
quality, not just counts.
"""
import asyncio
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents import llm  # noqa: E402
from agents.schema import BOOL, INT, STR, obj  # noqa: E402

SCHEMA = obj(specificity=INT, credibility=INT, relevance=INT, human_tone=INT, would_reply=BOOL, critique=STR)

SYSTEM = (
    "You are the busy recipient of an unsolicited intro email or LinkedIn note, and a skeptical one. "
    "Score it 1-5 on each axis:\n"
    "- specificity: does it show it was written for you (a real, specific fact about you or your company), "
    "not a mail merge?\n"
    "- credibility: does the sender and the startup come across as legitimate and worth your time?\n"
    "- relevance: given the research notes, is this plausibly useful to you right now?\n"
    "- human_tone: does it read like a person wrote it, without buzzwords, filler or salesiness?\n"
    "would_reply: would a typical person in your role plausibly reply? critique: one line, the single "
    "biggest improvement."
)


async def _one(row: dict) -> dict:
    msg = row["email_body"] if row["channel"] != "LinkedIn note" else row["linkedin_note"]
    prompt = (f"You are: {row['contact_name']}, {row['contact_title']} at {row['company']}.\n"
              f"Research notes about you: {row['fact_used']} | {row['fit_reason']}\n\n"
              f"Subject: {row['subject']}\n\n{msg}")
    out = await llm.run_structured(SYSTEM, prompt, SCHEMA, model="light", effort="low", max_tokens=4000,
                                   label=f"judge[{row['target']}]")
    return {"target": row["target"], **(out or {"error": "no answer"})}


async def judge(run: Path) -> dict:
    rows = [r for r in csv.DictReader(open(run / "results.csv", encoding="utf-8-sig")) if r["email_body"].strip()]
    llm.STRICT_400 = False
    scores = [s for s in await asyncio.gather(*(_one(r) for r in rows)) if "error" not in s]
    (run / "judge.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")
    axes = ("specificity", "credibility", "relevance", "human_tone")
    return {"run": run.name, "judged": len(scores),
            **{a: round(statistics.mean(s[a] for s in scores), 2) if scores else 0 for a in axes},
            "would_reply_share": round(sum(s["would_reply"] for s in scores) / len(scores), 2) if scores else 0}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(json.dumps(asyncio.run(judge(Path(arg)))))

"""Shared evidence rules: fact freshness and per-criterion fit."""
import datetime
import re

MAX_AGE_MONTHS = 18

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def age_months(date: str, today: datetime.date | None = None) -> int | None:
    """Months between a fact's date string and today; None if no year can be read."""
    today = today or datetime.date.today()
    s = date.lower()
    y = re.search(r"\b(19|20)\d{2}\b", s)
    if not y:
        return None
    year = int(y.group())
    month = 6  # unknown month: assume mid-year
    m = re.search(r"\b(19|20)\d{2}-(\d{1,2})\b", s)
    if m:
        month = int(m.group(2))
    else:
        for name, i in _MONTHS.items():
            if re.search(rf"\b{name}", s):
                month = i
                break
    return (today.year - year) * 12 + (today.month - month)


def fresh(facts: list[dict]) -> list[dict]:
    """Keep sourced facts that are dated within MAX_AGE_MONTHS or undated; dated ones first."""
    kept = []
    for f in facts:
        if not f["source_url"].startswith("http"):
            continue
        age = age_months(f["date"])
        if age is not None and age > MAX_AGE_MONTHS:
            continue
        kept.append({**f, "age_months": age})
    return sorted(kept, key=lambda f: (f["age_months"] is None, f["age_months"] or 0))


def judge(assessments: list[dict], rubric: list[dict]) -> dict:
    """Summarize per-criterion verdicts (criteria numbered from 1). A 'yes' needs an evidence URL to count."""
    by: dict[int, str] = {}
    for a in assessments:
        ok = a["verdict"] != "yes" or a["evidence_url"].startswith("http")
        by[a["criterion_number"]] = a["verdict"] if ok else "unclear"
    nums = range(1, len(rubric) + 1)
    must = [i for i in nums if rubric[i - 1].get("must_have")]
    met = sum(by.get(i) == "yes" for i in nums)
    return {
        "met": met,
        "total": len(rubric),
        "must_haves_met": all(by.get(i) == "yes" for i in must),
        "must_have_failed": any(by.get(i) == "no" for i in must),
        "fit_score": round(10 * met / len(rubric)) if rubric else 0,
        "verdicts": {rubric[i - 1]["criterion"]: by.get(i, "unclear") for i in nums},
    }


def numbered(rubric: list[dict]) -> str:
    return "\n".join(f"{i}. {'[MUST-HAVE] ' if r.get('must_have') else ''}{r['criterion']}"
                     for i, r in enumerate(rubric, 1))

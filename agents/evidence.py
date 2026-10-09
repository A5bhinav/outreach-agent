"""Shared evidence rules: fact freshness and per-criterion fit."""
import datetime
import re

MAX_AGE_MONTHS = 18

_MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)(?:[a-z]*)\.?(?=\s|,|\d|$)", re.I)
_MONTH_NUM = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_FULL = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "sept", "october",
         "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}


def age_months(date: str, today: datetime.date | None = None) -> int | None:
    """Months between a fact's date string and today; None if no year can be read.

    Uses the latest year mentioned (so "2019-2025" or "2024–2025" reads as 2025). Future dates
    (planned or expected events) count as age 0.
    """
    today = today or datetime.date.today()
    s = date.lower()
    years = [int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", s)]
    # Season/fiscal ranges like "2024-25" mean the later year.
    years += [int(y) + 1 for y, yy in re.findall(r"(?<!\d)((?:19|20)\d{2})[-–/](\d{2})(?!\d)", s)
              if int(yy) == (int(y) + 1) % 100]
    if not years:
        return None
    year = max(years)
    month = 6  # unknown month: assume mid-year
    if m := re.search(rf"(?<!\d){year}-(\d{{1,2}})(?!\d)", s):  # 2025-11 or 2025-11-03
        if 1 <= int(m.group(1)) <= 12:
            month = int(m.group(1))
    elif m := re.search(rf"(?<!\d)(\d{{1,2}})/(?:\d{{1,2}}/)?{year}(?!\d)", s):  # 11/2025 or 11/03/2025 (US)
        if 1 <= int(m.group(1)) <= 12:
            month = int(m.group(1))
    else:
        for m in _MONTH.finditer(s):
            word = re.match(r"[a-z]+", s[m.start():]).group(0)
            if word in _FULL:
                month = _MONTH_NUM[m.group(1)[:3]]
                break
        else:
            if q := re.search(r"\bq([1-4])\b", s):
                month = int(q.group(1)) * 3 - 1
    return max(0, (today.year - year) * 12 + (today.month - month))


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
    """Summarize per-criterion verdicts (criteria numbered from 1). A 'yes' needs an evidence URL to count.

    passes: no must-have is a clear "no" and at most one must-have is "unclear" (shown as such,
    so the investor decides). Hard-to-prove criteria shouldn't silently empty the list.
    """
    by: dict[int, str] = {}
    for a in assessments:
        ok = a["verdict"] != "yes" or a["evidence_url"].startswith("http")
        by[a["criterion_number"]] = a["verdict"] if ok else "unclear"
    nums = range(1, len(rubric) + 1)
    must = [i for i in nums if rubric[i - 1].get("must_have")]
    met = sum(by.get(i) == "yes" for i in nums)
    unclear_must = [rubric[i - 1]["criterion"] for i in must if by.get(i, "unclear") == "unclear"]
    failed = any(by.get(i) == "no" for i in must)
    return {
        "met": met,
        "total": len(rubric),
        "must_haves_met": not failed and not unclear_must,
        "must_have_failed": failed,
        "unclear_must_haves": unclear_must,
        "passes": not failed and len(unclear_must) <= 1,
        # Rank confirmed must-haves above unclear ones, then by criteria met.
        "fit_score": round(10 * met / len(rubric)) - 3 * len(unclear_must) if rubric else 0,
        "verdicts": {rubric[i - 1]["criterion"]: by.get(i, "unclear") for i in nums},
    }


def numbered(rubric: list[dict]) -> str:
    return "\n".join(f"{i}. {'[MUST-HAVE] ' if r.get('must_have') else ''}{r['criterion']}"
                     for i, r in enumerate(rubric, 1))

"""Legal footer for every email, and the do-not-contact ledger shared across runs and portfolio companies.

Footer (CAN-SPAM): says it's an intro on behalf of a portfolio company, gives the firm's postal
address and an opt-out. EU/UK recipients (GDPR Art. 14) also get where their details came from
and their right to object. This is a reading of the rules, not legal advice.

Ledger (CSV): one row per drafted email. Statuses: drafted, sent, replied, opted_out. The tool
writes "drafted"; update rows to sent/opted_out by hand (or with --opt-out). Rules on later runs:
- opted_out: never contacted again, for any portfolio company (matched on email, profile or domain
  when no email was recorded).
- any status for the same portfolio company within REPEAT_DAYS: skipped.
- sent for another portfolio company within CROSS_DAYS: skipped, so one business doesn't get
  several of the firm's intros at once.
"""
import csv
import datetime
import re
from pathlib import Path

REPEAT_DAYS = 180
CROSS_DAYS = 30
FIELDS = ["date", "portfolio_company", "target", "company", "domain", "email", "profile_url", "status"]

EU_UK = re.compile(
    r"\b(uk|united kingdom|england|scotland|wales|northern ireland|london|ireland|dublin|germany|berlin|munich|"
    r"france|paris|spain|madrid|barcelona|italy|milan|rome|netherlands|amsterdam|belgium|brussels|"
    r"luxembourg|austria|vienna|switzerland|zurich|geneva|sweden|stockholm|denmark|copenhagen|finland|"
    r"helsinki|norway|oslo|iceland|poland|warsaw|czech|prague|slovakia|hungary|budapest|romania|bulgaria|"
    r"greece|athens|portugal|lisbon|croatia|slovenia|estonia|latvia|lithuania|malta|cyprus|liechtenstein|"
    r"eu|europe)\b", re.I)


def _clean(s: str) -> str:
    return s.replace("PLACEHOLDER:", "").strip()


def footer(startup: dict, target: dict) -> str:
    s = startup.get("sender", {})
    firm = _clean(s.get("firm", ""))
    company = _clean(startup.get("company_name", ""))
    lines = [
        "--",
        f"{firm} is an investor in {company}; this is an introduction on their behalf." if s.get("voice", "investor") == "investor"
        else f"Sent by {company}.",
        "If you'd rather not hear from us, reply \"unsubscribe\" and we won't contact you again.",
    ]
    if s.get("firm_address"):
        lines.append(_clean(s["firm_address"]))
    if EU_UK.search(target.get("location", "") or ""):
        src = target["contact"].get("source_url") if target["contact"]["name"] != "unknown" else target.get("website")
        lines.append(f"We found your details on a public web page ({src}). You can object to us using them, "
                     "and we'll delete them, by replying to this email.")
    return "\n".join(lines)


def missing_legal(startup: dict) -> list[str]:
    s = startup.get("sender", {})
    return [k for k in ("firm_address",) if not s.get(k) or "PLACEHOLDER" in str(s.get(k))]


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")


def _dom(t: dict) -> str:
    d = t.get("domain") or ""
    return "" if d == "unknown" else d


class Ledger:
    def __init__(self, path: Path, portfolio_company: str):
        self.path = path
        self.pc = _clean(portfolio_company)
        self.rows: list[dict] = []
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                self.rows = list(csv.DictReader(f))

    @staticmethod
    def _age(row: dict) -> int:
        try:
            return (datetime.date.today() - datetime.date.fromisoformat(row["date"])).days
        except (KeyError, ValueError):
            return 0

    def _matches(self, row: dict, t: dict) -> bool:
        email = t.get("contact", {}).get("email", "unknown")
        if row.get("email") and email != "unknown" and row["email"].lower() == email.lower():
            return True
        if row.get("profile_url") and t.get("website") and _norm(row["profile_url"]) == _norm(t["website"]):
            return True
        return bool(row.get("domain")) and row["domain"] == _dom(t) and not row.get("email")

    def _company_match(self, row: dict, t: dict) -> bool:
        return (bool(row.get("domain")) and row["domain"] == _dom(t)) or self._matches(row, t)

    def skip_reason(self, t: dict) -> str | None:
        for row in self.rows:
            st = row.get("status", "")
            if st == "opted_out" and self._matches(row, t):
                return "opted out"
            if not self._company_match(row, t):
                continue
            if row.get("portfolio_company") == self.pc and self._age(row) <= REPEAT_DAYS:
                return f"already {st} for {self.pc} on {row.get('date')}"
            if st == "sent" and self._age(row) <= CROSS_DAYS:
                return f"sent an intro for {row.get('portfolio_company')} on {row.get('date')}"
        return None

    def skip(self, t: dict) -> bool:
        return self.skip_reason(t) is not None

    def record(self, targets: list[dict]) -> None:
        today = datetime.date.today().isoformat()
        new = [{
            "date": today, "portfolio_company": self.pc, "target": t["name"], "company": t.get("company", t["name"]),
            "domain": _dom(t),
            "email": t["contact"]["email"] if t["contact"]["email"] != "unknown" else "",
            "profile_url": t["website"] if not _dom(t) else "", "status": "drafted",
        } for t in targets]
        self.rows += new
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if not exists:
                w.writeheader()
            w.writerows(new)

    def opt_out(self, email_or_domain: str) -> None:
        v = email_or_domain.strip().lower()
        row = {k: "" for k in FIELDS}
        row.update(date=datetime.date.today().isoformat(), status="opted_out", target=v,
                   **({"email": v} if "@" in v else {"domain": v.removeprefix("www.")}))
        exists = self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if not exists:
                w.writeheader()
            w.writerow(row)

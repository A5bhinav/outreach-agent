"""Legal footer for every email, and the do-not-contact ledger shared across runs and portfolio companies.

Footer (CAN-SPAM): identifies the firm and that this is an intro for a portfolio company, gives the
firm's postal address and a plain opt-out. EU/UK recipients, and anyone whose location is unknown,
also get where their details came from and their right to object (GDPR Art. 14). This is a reading
of the rules, not legal advice.

Ledger (CSV): the tool records each ready-to-send message as "drafted"; `--mark-sent` turns rows
into "sent"; `--opt-out` records unsubscribes. On later runs:
- opted_out: never contacted again, for any portfolio company (matched on email, profile URL, or
  domain, including any address at an opted-out domain).
- sent / replied for the same portfolio company within REPEAT_DAYS: skipped.
- drafted for the same portfolio company within DRAFT_DAYS: skipped (so a rerun doesn't redo it,
  but a draft that was never sent doesn't hide the target for months).
- sent for another portfolio company within CROSS_DAYS: skipped, so one business doesn't get
  several of the firm's intros at once.
"""
import csv
import datetime
import re
from pathlib import Path

REPEAT_DAYS = 180
DRAFT_DAYS = 14
CROSS_DAYS = 30
FIELDS = ["date", "portfolio_company", "target", "company", "domain", "email", "profile_url", "status", "run"]

EU_UK = re.compile(
    r"\b(uk|u\.k\.|united kingdom|great britain|britain|england|scotland|wales|northern ireland|london|manchester|"
    r"birmingham|leeds|glasgow|edinburgh|bristol|liverpool|cambridge, uk|oxford, uk|ireland|dublin|cork|"
    r"germany|deutschland|berlin|munich|münchen|muenchen|hamburg|frankfurt|cologne|köln|stuttgart|düsseldorf|"
    r"france|paris|lyon|marseille|toulouse|lille|bordeaux|nantes|spain|españa|madrid|barcelona|valencia|"
    r"italy|italia|milan|milano|rome|roma|turin|torino|netherlands|nederland|holland|amsterdam|rotterdam|"
    r"eindhoven|the hague|utrecht|belgium|belgië|belgique|brussels|antwerp|ghent|luxembourg|austria|österreich|"
    r"vienna|wien|switzerland|schweiz|suisse|zurich|zürich|geneva|genève|basel|sweden|sverige|stockholm|"
    r"gothenburg|denmark|danmark|copenhagen|aarhus|finland|suomi|helsinki|norway|norge|oslo|iceland|poland|"
    r"polska|warsaw|kraków|krakow|wrocław|czech|czechia|prague|praha|brno|slovakia|hungary|budapest|romania|"
    r"bucharest|bulgaria|sofia|greece|athens|portugal|lisbon|lisboa|porto|croatia|zagreb|slovenia|estonia|"
    r"tallinn|latvia|riga|lithuania|vilnius|malta|cyprus|liechtenstein|eu|europe|european union)\b", re.I)


def _clean(s: str) -> str:
    return s.replace("PLACEHOLDER:", "").strip()


def is_eu_uk(location: str) -> bool:
    """True for EU/UK/EEA/Swiss locations, and for unknown locations (safer to include the notice)."""
    loc = (location or "").strip()
    if not loc or loc.lower() in ("unknown", "?", "n/a", "remote"):
        return True
    # Over-matching (e.g. Paris, TX) only adds a harmless notice; under-matching would skip a required one.
    return bool(EU_UK.search(loc)) and "new south wales" not in loc.lower()


def footer(startup: dict, target: dict) -> str:
    s = startup.get("sender", {})
    firm, company = _clean(s.get("firm", "")), _clean(startup.get("company_name", ""))
    who = (f"{firm}, {_clean(s['firm_address'])}" if s.get("firm_address") else firm).strip(", ")
    lines = [
        f"{who} · Intro on behalf of {company}, a {firm} portfolio company." if s.get("voice", "investor") == "investor"
        else f"{company}{', ' + _clean(s['firm_address']) if s.get('firm_address') else ''}",
        "Not relevant? Reply \"no thanks\" and I won't follow up or contact you again.",
    ]
    if is_eu_uk(target.get("location", "")):
        src = target["contact"].get("source_url") if target["contact"]["name"] != "unknown" else target.get("website")
        lines.append(f"I found your details on a public web page ({src}); reply to object and I'll delete them.")
    return "\n".join(lines)


def missing_legal(startup: dict) -> list[str]:
    """Config fields the footer needs that are missing or still placeholders."""
    s = startup.get("sender", {})
    need = {"firm_address": s.get("firm_address"), "company_name": startup.get("company_name")}
    if s.get("voice", "investor") == "investor":
        need["firm"] = s.get("firm")
    return [k for k, v in need.items() if not v or "PLACEHOLDER" in str(v)]


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")


def _dom(t: dict) -> str:
    d = t.get("domain") or ""
    return "" if d == "unknown" else d


def _status(row: dict) -> str:
    return (row.get("status") or "").strip().lower().replace(" ", "_").replace("-", "_")


def _email(t: dict) -> str:
    e = (t.get("contact") or {}).get("email", "unknown")
    return "" if e == "unknown" else e.lower()


class Ledger:
    def __init__(self, path: Path, portfolio_company: str):
        self.path = path
        self.pc = _clean(portfolio_company)
        self.rows: list[dict] = []
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self.rows = list(reader)
                header = reader.fieldnames or []
            if header != FIELDS:  # older ledger format: rewrite with the current columns before appending
                self._write(self.rows, mode="w")

    @staticmethod
    def _age(row: dict) -> int:
        try:
            return (datetime.date.today() - datetime.date.fromisoformat(row["date"])).days
        except (KeyError, ValueError, TypeError):
            return 0

    def _person(self, row: dict, t: dict) -> bool:
        """Same recipient: same email, same profile URL, or a domain-wide row covering their address/site."""
        email = _email(t)
        if row.get("email") and email and row["email"].lower() == email:
            return True
        if row.get("profile_url", "").startswith("http") and t.get("website", "").startswith("http") \
                and _norm(row["profile_url"]) == _norm(t["website"]):
            return True
        if row.get("domain") and not row.get("email"):
            d = row["domain"].lower()
            return d == _dom(t) or bool(email) and (email.endswith("@" + d) or email.endswith("." + d))
        return False

    def _company(self, row: dict, t: dict) -> bool:
        return bool(row.get("domain")) and row["domain"].lower() == _dom(t) or self._person(row, t)

    def skip_reason(self, t: dict) -> str | None:
        for row in self.rows:
            st = _status(row)
            if st == "opted_out" and self._person(row, t):
                return "opted out"
            if not self._company(row, t):
                continue
            same = row.get("portfolio_company") == self.pc
            age = self._age(row)
            if same and st in ("sent", "replied") and age <= REPEAT_DAYS:
                return f"already {st} for {self.pc} on {row.get('date')}"
            if same and st == "drafted" and age <= DRAFT_DAYS:
                return f"drafted for {self.pc} on {row.get('date')}"
            if not same and st == "sent" and age <= CROSS_DAYS:
                return f"sent an intro for {row.get('portfolio_company')} on {row.get('date')}"
        return None

    def skip(self, t: dict) -> bool:
        return self.skip_reason(t) is not None

    def _write(self, rows: list[dict], mode: str = "a") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists() and mode == "a"
        with open(self.path, mode, newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerows(rows)

    def record(self, targets: list[dict], run: str = "") -> None:
        today = datetime.date.today().isoformat()
        new = [{
            "date": today, "portfolio_company": self.pc, "target": t["name"], "company": t.get("company", t["name"]),
            "domain": _dom(t), "email": _email(t),
            "profile_url": t["website"] if not _dom(t) and t.get("website", "").startswith("http") else "",
            "status": "drafted", "run": run,
        } for t in targets]
        self.rows += new
        self._write(new)

    def mark_sent(self, run: str, targets: set[str] | None) -> int:
        """Mark a run's drafted rows as sent (all, or those whose target name is in `targets`)."""
        n = 0
        for row in self.rows:
            if row.get("run") == run and _status(row) == "drafted" and (targets is None or row["target"] in targets):
                row["status"] = "sent"
                row["date"] = datetime.date.today().isoformat()
                n += 1
        self._write(self.rows, mode="w")
        return n

    def opt_out(self, email_or_domain: str) -> None:
        from .sourcer import domain
        v = email_or_domain.strip().lower()
        row = {k: "" for k in FIELDS}
        row.update(date=datetime.date.today().isoformat(), status="opted_out", target=v,
                   **({"email": v} if "@" in v else {"domain": domain(v)}))
        self.rows.append(row)
        self._write([row])

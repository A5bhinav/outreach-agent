"""Write the review files for a run. Drafts only; nothing is sent.

- results.csv: one row per target, ready-to-send first.
- results.md: readable review page with research notes and a one-click Gmail compose link per email.
- drafts/NN-name.eml: each first email as a draft file (opens in Mail/Outlook/Thunderbird ready to send).
"""
import csv
import re
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlencode

FIELDS = ["ready_to_send", "to_email", "email_source", "target", "company", "website", "location",
          "contact_name", "contact_title", "contact_source", "contact_verified", "criteria_met", "criteria",
          "fit_reason", "fact_used", "fact_source", "source_urls", "subject", "email_body", "followup_body",
          "gmail_link", "flags"]


def _ready(c: dict) -> str:
    """'yes' only when the email has an address, a named and non-contradicted contact, and no blockers."""
    ct = c["contact"]
    why = []
    if ct["email"] == "unknown":
        why.append("no verified email address")
    if ct["name"] == "unknown":
        why.append("no named contact")
    elif ct.get("verified", "").startswith("NO"):
        why.append("contact name not found on source page")
    if any(f.startswith("BLOCKER") for f in c["flags"]):
        why.append("blocker flag")
    return "yes" if not why else "no: " + "; ".join(why)


def full_body(final: dict) -> str:
    return f"{final['body'].rstrip()}\n\n{final['footer']}" if final.get("footer") else final["body"]


def gmail_link(to: str, subject: str, body: str) -> str:
    return "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body},
                                                         quote_via=quote)


def _row(c: dict) -> dict:
    ct = c["contact"]
    found = ct["name"] != "unknown"
    fit = c["fit"]
    to = ct["email"] if ct["email"] != "unknown" else ""
    body = full_body(c["final"])
    return {
        "ready_to_send": _ready(c),
        "to_email": to,
        "email_source": ct["email_source_url"] if to else "",
        "target": c["name"],
        "company": c.get("company", c["name"]),
        "website": c["website"],
        "location": c.get("location", "unknown"),
        "contact_name": ct["name"],
        "contact_title": ct["title"] if found else f"unknown (target: {ct.get('suggested_title', 'unknown')})",
        "contact_source": ct["source_url"],
        "contact_verified": ct.get("verified", "no"),
        "criteria_met": f"{fit['met']}/{fit['total']}" + ("" if fit["must_haves_met"] else " (must-haves unconfirmed)"),
        "criteria": " | ".join(f"{k}: {v}" for k, v in fit["verdicts"].items()),
        "fit_reason": c["fit_reason"],
        "fact_used": c["draft"].get("fact_used", ""),
        "fact_source": c["draft"].get("fact_source_url", ""),
        "source_urls": " | ".join(c["source_urls"]),
        "subject": c["final"]["subject"],
        "email_body": body,
        "followup_body": c["final"].get("followup_body", ""),
        "gmail_link": gmail_link(to, c["final"]["subject"], body) if body.strip() else "",
        "flags": " | ".join(c["flags"]) or "none",
        "_sort": (_ready(c) == "yes", c.get("fit_score", 0)),
    }


def _eml(r: dict, sender: dict) -> bytes:
    m = EmailMessage()
    m["To"] = r["to_email"]
    m["Subject"] = r["subject"]
    if sender.get("email") and "PLACEHOLDER" not in sender["email"]:
        m["From"] = f"{sender.get('name', '')} <{sender['email']}>"
    m["X-Unsent"] = "1"  # Outlook/Mail open it as an editable draft
    m.set_content(r["email_body"])
    return bytes(m)


def write_outputs(targets: list[dict], request: str, outdir: Path, startup: dict | None = None) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    pairs = sorted(((_row(c), c["facts"]) for c in targets), key=lambda t: t[0]["_sort"], reverse=True)
    for r, _ in pairs:
        r.pop("_sort")
    rows = [r for r, _ in pairs]

    # utf-8-sig so Excel shows dashes and curly quotes correctly.
    with open(outdir / "results.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    drafts = outdir / "drafts"
    drafts.mkdir(exist_ok=True)
    sender = (startup or {}).get("sender", {})
    for i, r in enumerate(rows, 1):
        if r["email_body"].strip():
            slug = re.sub(r"[^a-z0-9]+", "-", r["target"].lower()).strip("-")[:40]
            (drafts / f"{i:02d}-{slug}.eml").write_bytes(_eml(r, sender))

    ready = sum(r["ready_to_send"] == "yes" for r in rows)
    md = [f"# Outreach drafts\n\nRequest: _{request}_\n\n**{ready} of {len(rows)} ready to send. "
          "Nothing has been sent. Read each email before sending.** Each \"Open in Gmail\" link opens a "
          "pre-filled compose window; `drafts/` has the same emails as .eml files. Send the follow-up "
          "about five business days later in the same thread if there's no reply.\n"]
    for i, (r, facts) in enumerate(pairs, 1):
        md.append(f"## {i}. {r['target']} — {r['criteria_met']} criteria — ready to send: {r['ready_to_send']}\n")
        src = r["email_source"]
        md.append(f"- To: {r['to_email'] or '(no verified address found)'}"
                  + (f" ([source]({src}))" if src.startswith("http") else f" ({src})" if src else ""))
        if r["gmail_link"]:
            md.append(f"- [Open in Gmail]({r['gmail_link']})")
        if r["company"] != r["target"]:
            md.append(f"- Company: {r['company']}")
        md.append(f"- Website/profile: {r['website']}  \n- Location: {r['location']}")
        md.append(f"- Contact: {r['contact_name']}, {r['contact_title']}"
                  + (f" ([source]({r['contact_source']}), verified: {r['contact_verified']})" if r["contact_source"] != "unknown" else ""))
        md.append(f"- Why: {r['fit_reason']}")
        md.append(f"- Criteria: {r['criteria']}")
        md.append("- Research notes:")
        for f in facts:
            md.append(f"  - {f['fact']} ({f['date']}) — {f['source_url']}")
        md.append(f"- Reviewer flags: {r['flags']}\n")
        md.append(f"**Subject:** {r['subject']}\n")
        md.append("\n".join("> " + line for line in r["email_body"].splitlines()) + "\n")
        if r["followup_body"]:
            md.append("**Follow-up (≈5 business days later, same thread):**\n")
            md.append("\n".join("> " + line for line in r["followup_body"].splitlines()) + "\n")
    (outdir / "results.md").write_text("\n".join(md), encoding="utf-8")
    return rows

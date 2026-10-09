"""Write the review files for a run. Drafts only; nothing is sent.

- results.html: the review page (open in a browser): summary table with a one-click action per
  target, then each message with copy buttons, the evidence behind it and anything to check.
- results.md: the same, as Markdown.
- results.csv: one row per target, ready-to-send first.
- drafts/NN-name.eml (+ .followup.eml): each email as a draft file for Mail/Outlook/Thunderbird.
"""
import csv
import datetime
import html
import re
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlencode

FIELDS = ["status", "channel", "to_email", "email_kind", "send_link", "target", "company", "website", "location",
          "contact_name", "contact_title", "contact_source", "contact_verified", "criteria_met", "criteria",
          "fit_reason", "fact_used", "fact_source", "fact_check", "subject", "email_body", "followup_body",
          "followup_send_after", "followup_link", "linkedin_note", "profile_link", "contact_form", "source_urls",
          "to_check", "auto_fixed"]


def _business_days(n: int, start: datetime.date | None = None) -> datetime.date:
    d = start or datetime.date.today()
    while n:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def full_body(final: dict) -> str:
    return f"{final['body'].rstrip()}\n\n{final['footer']}" if final.get("footer") else final["body"]


def gmail_link(to: str, subject: str, body: str, account: str = "") -> str:
    q = {"view": "cm", "fs": "1", "to": to, "su": subject, "body": body}
    if account:
        q["authuser"] = account  # open in the sender's Gmail account, not whichever is signed in first
    return "https://mail.google.com/mail/?" + urlencode(q, quote_via=quote)


def _channel(c: dict) -> tuple[str, str]:
    """(channel, status) for a target. Status is 'ready', or 'not ready: why'."""
    ct = c["contact"]
    blockers = [f for f in c["flags"] if f.startswith("BLOCKER")]
    if not c["final"]["body"].strip():
        return "none", "not ready: no draft"
    if ct["name"] != "unknown" and ct.get("verified", "").startswith("NO"):
        return "none", "not ready: contact name not found on its source page"
    if ct["email"] != "unknown":
        ch = "email (shared inbox)" if ct.get("email_kind") == "generic" else "email"
    elif "linkedin.com" in c.get("website", "") or c.get("domain") == "":  # a person: message them on their profile
        ch = "LinkedIn note"
    elif ct.get("contact_form_url", "unknown") != "unknown":
        ch = "contact form"
    else:
        return "none", "not ready: no email, profile or contact form found"
    if blockers:
        return ch, "not ready: " + "; ".join(b.removeprefix("BLOCKER: ") for b in blockers)
    return ch, "ready"


def _row(c: dict, account: str) -> dict:
    ct = c["contact"]
    found = ct["name"] != "unknown"
    fit = c["fit"]
    final = c["final"]
    to = ct["email"] if ct["email"] != "unknown" else ""
    body = full_body(final)
    channel, status = _channel(c)
    person = c.get("domain") == "" and "found_via" in c
    profile = c["website"] if person else ""
    if person and "linkedin.com" not in profile:
        profile_search = "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{ct['name']} {c.get('company', '')}")
    else:
        profile_search = ""
    send = (gmail_link(to, final["subject"], body, account) if channel.startswith("email")
            else (profile if "linkedin.com" in profile else profile_search) if channel == "LinkedIn note"
            else ct.get("contact_form_url", "") if channel == "contact form" else "")
    fu_link = gmail_link(to, "Re: " + final["subject"], final.get("followup_body", ""), account) if to and final.get("followup_body") else ""
    return {
        "status": status,
        "channel": channel,
        "to_email": to,
        "email_kind": ct.get("email_kind", "none"),
        "send_link": send,
        "target": c["name"],
        "company": c.get("company", c["name"]),
        "website": c["website"],
        "location": c.get("location", "unknown"),
        "contact_name": ct["name"],
        "contact_title": ct["title"] if found else f"unknown (target: {ct.get('suggested_title', 'unknown')})",
        "contact_source": ct["source_url"],
        "contact_verified": ct.get("verified", "no"),
        "criteria_met": f"{fit['met']}/{fit['total']}" + (f" (unconfirmed must-have: {fit['unclear_must_haves'][0]})"
                                                         if fit.get("unclear_must_haves") else ""),
        "criteria": " | ".join(f"{k}: {v}" for k, v in fit["verdicts"].items()),
        "fit_reason": c["fit_reason"],
        "fact_used": c["draft"].get("fact_used", ""),
        "fact_source": c["draft"].get("fact_source_url", ""),
        "fact_check": c.get("fact_check", "unchecked"),
        "subject": final["subject"],
        "email_body": body,
        "followup_body": final.get("followup_body", ""),
        "followup_send_after": _business_days(5).isoformat() if final.get("followup_body") else "",
        "followup_link": fu_link,
        "linkedin_note": final.get("linkedin_note", ""),
        "profile_link": profile or profile_search,
        "contact_form": ct.get("contact_form_url", "unknown") if ct.get("contact_form_url", "unknown") != "unknown" else "",
        "source_urls": " | ".join(c["source_urls"]),
        "to_check": " | ".join(c["flags"]) or "none",
        "auto_fixed": " | ".join(c.get("fixed", [])),
        "_sort": (status == "ready", channel.startswith("email") and "shared" not in channel, c.get("fit_score", 0)),
    }


def _eml(to: str, subject: str, body: str, sender: dict) -> bytes:
    m = EmailMessage()
    if to:
        m["To"] = to
    m["Subject"] = re.sub(r"\s+", " ", subject).strip()
    email = sender.get("email", "")
    if email and "PLACEHOLDER" not in email and "@" in email:
        user, dom = email.split("@", 1)
        m["From"] = Address(display_name=sender.get("name", "").replace("PLACEHOLDER:", "").strip(), username=user, domain=dom)
    m["X-Unsent"] = "1"  # Outlook/Mail open it as an editable draft
    m.set_content(body)
    return bytes(m)


def _md(rows: list[dict], facts: list[list[dict]], request: str) -> str:
    ready = sum(r["status"] == "ready" for r in rows)
    by = {}
    for r in rows:
        if r["status"] == "ready":
            by[r["channel"]] = by.get(r["channel"], 0) + 1
    md = [f"# Outreach drafts\n\nRequest: _{request}_\n",
          f"**{ready} of {len(rows)} ready** ({', '.join(f'{v} via {k}' for k, v in by.items()) or 'none'}). "
          "Nothing has been sent. Read each message, click its action, then run "
          "`python main.py --mark-sent <this folder>` so the ledger knows.\n",
          "| # | Target | Contact | Channel | Hook | Status | Action |", "|---|---|---|---|---|---|---|"]
    for i, (r, fs) in enumerate(zip(rows, facts), 1):
        hook = next((f"{f['fact'][:70]} ({f['date']})" for f in fs if f["source_url"] == r["fact_source"]), r["fact_used"][:70])
        action = f"[Open]({r['send_link']})" if r["send_link"] else "—"
        md.append(f"| {i} | {r['target']} | {r['contact_name'] if r['contact_name'] != 'unknown' else '—'} | {r['channel']} "
                  f"| {hook.replace('|', '/')} | {r['status']} | {action} |")
    md.append("")
    for i, (r, fs) in enumerate(zip(rows, facts), 1):
        md.append(f"## {i}. {r['target']} — {r['status']}\n")
        md.append(f"**Channel:** {r['channel']}" + (f" → {r['to_email']}" if r["to_email"] else "")
                  + (f" · [Open]({r['send_link']})" if r["send_link"] else ""))
        if r["to_check"] != "none":
            md.append("\n**Check before sending:**\n" + "\n".join(f"- {f}" for f in r["to_check"].split(" | ")))
        if r["channel"].startswith("email") or r["channel"] in ("contact form", "none"):
            md.append(f"\n**Subject:** {r['subject']}\n")
            md.append("\n".join("> " + line for line in r["email_body"].splitlines()) + "\n")
        if r["linkedin_note"] and r["channel"] in ("LinkedIn note", "none"):
            md.append(f"\n**LinkedIn note** ({len(r['linkedin_note'])} chars)"
                      + (f" · [profile]({r['profile_link']})" if r["profile_link"] else "") + f"\n\n> {r['linkedin_note']}\n")
        if r["followup_body"]:
            md.append(f"**Follow-up** (send {r['followup_send_after']} if no reply, same thread)"
                      + (f" · [Open]({r['followup_link']})" if r["followup_link"] else "") + "\n")
            md.append("\n".join("> " + line for line in r["followup_body"].splitlines()) + "\n")
        md.append("<details><summary>Why this target, and the evidence</summary>\n")
        md.append(f"- Contact: {r['contact_name']}, {r['contact_title']}"
                  + (f" ([source]({r['contact_source']}), verified: {r['contact_verified']})" if r["contact_source"].startswith("http") else ""))
        if r["company"] != r["target"]:
            md.append(f"- Company: {r['company']}")
        md.append(f"- Website/profile: {r['website']} · Location: {r['location']}")
        md.append(f"- Why: {r['fit_reason']} · Criteria met: {r['criteria_met']}")
        md.append(f"- Criteria: {r['criteria']}")
        md.append(f"- Opening fact checked on its source page: {r['fact_check']}")
        md.append("- Research notes:")
        md += [f"  - {f['fact']} ({f['date']}) — {f['source_url']}" for f in fs]
        if r["auto_fixed"]:
            md.append("- Already fixed by the reviewer:\n" + "\n".join(f"  - {f}" for f in r["auto_fixed"].split(" | ")))
        md.append("\n</details>\n")
    return "\n".join(md)


_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--card:#fafafa;--ok:#1a7f37;--warn:#9a6700;--link:#0b57d0}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eaeaea;--muted:#9a9a9a;--line:#2e2e2e;--card:#1c1c1c;--ok:#4ac26b;--warn:#d4a72c;--link:#8ab4f8}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px}
a{color:var(--link)} table{border-collapse:collapse;width:100%;font-size:14px} td,th{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}
.wrap{overflow-x:auto} .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:18px 0}
.ok{color:var(--ok);font-weight:600} .warn{color:var(--warn)} pre{white-space:pre-wrap;font:inherit;margin:6px 0;padding:10px;border-left:3px solid var(--line)}
button{font:inherit;font-size:13px;padding:2px 10px;margin-left:6px;cursor:pointer} .muted{color:var(--muted);font-size:13px}
"""


def _html(rows: list[dict], facts: list[list[dict]], request: str) -> str:
    e = html.escape
    ready = sum(r["status"] == "ready" for r in rows)

    def link(url: str, text: str) -> str:
        return f'<a href="{e(url)}" target="_blank" rel="noopener">{e(text)}</a>' if url else ""

    def block(label: str, text: str, extra: str = "") -> str:
        return (f'<div><b>{e(label)}</b>{extra}<button onclick="navigator.clipboard.writeText(this.parentNode.querySelector(\'pre\').innerText)">Copy</button>'
                f"<pre>{e(text)}</pre></div>")

    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Outreach drafts</title><style>{_CSS}</style>",
           f"<h1>Outreach drafts</h1><p class=muted>{e(request)}</p>",
           f"<p><b>{ready} of {len(rows)} ready.</b> Nothing has been sent. Read each message, use its action, then run "
           f"<code>python main.py --mark-sent &lt;this folder&gt;</code>.</p>",
           "<div class=wrap><table><tr><th>#</th><th>Target</th><th>Contact</th><th>Channel</th><th>Status</th><th>Action</th></tr>"]
    for i, r in enumerate(rows, 1):
        cls = "ok" if r["status"] == "ready" else "warn"
        out.append(f"<tr><td>{i}</td><td><a href='#t{i}'>{e(r['target'])}</a></td><td>{e(r['contact_name'] if r['contact_name'] != 'unknown' else '—')}</td>"
                   f"<td>{e(r['channel'])}</td><td class={cls}>{e(r['status'])}</td><td>{link(r['send_link'], 'Open')}</td></tr>")
    out.append("</table></div>")
    for i, (r, fs) in enumerate(zip(rows, facts), 1):
        cls = "ok" if r["status"] == "ready" else "warn"
        out.append(f"<div class=card id=t{i}><h2>{i}. {e(r['target'])} <span class={cls}>· {e(r['status'])}</span></h2>")
        out.append(f"<p>{e(r['channel'])}{' → ' + e(r['to_email']) if r['to_email'] else ''} {link(r['send_link'], 'Open')}</p>")
        if r["to_check"] != "none":
            out.append("<p class=warn><b>Check before sending:</b></p><ul>" + "".join(f"<li class=warn>{e(f)}</li>" for f in r["to_check"].split(" | ")) + "</ul>")
        if r["channel"].startswith("email") or r["channel"] in ("contact form", "none"):
            out.append(block("Subject", r["subject"]))
            out.append(block("Email", r["email_body"]))
        if r["linkedin_note"] and r["channel"] in ("LinkedIn note", "none"):
            out.append(block(f"LinkedIn note ({len(r['linkedin_note'])} chars)", r["linkedin_note"], " " + link(r["profile_link"], "profile")))
        if r["followup_body"]:
            out.append(block(f"Follow-up (send {r['followup_send_after']} if no reply)", r["followup_body"], " " + link(r["followup_link"], "Open")))
        out.append("<details><summary>Why this target, and the evidence</summary><ul>")
        out.append(f"<li>Contact: {e(r['contact_name'])}, {e(r['contact_title'])} {link(r['contact_source'] if r['contact_source'].startswith('http') else '', 'source')} (verified: {e(r['contact_verified'])})</li>")
        out.append(f"<li>{link(r['website'], r['website'])} · {e(r['location'])}</li>")
        out.append(f"<li>Why: {e(r['fit_reason'])} · Criteria met: {e(r['criteria_met'])}</li>")
        out.append(f"<li>Criteria: {e(r['criteria'])}</li><li>Opening fact checked on its source page: {e(r['fact_check'])}</li>")
        out += [f"<li>{e(f['fact'])} ({e(f['date'])}) {link(f['source_url'], 'source')}</li>" for f in fs]
        if r["auto_fixed"]:
            out.append("<li>Already fixed by the reviewer: " + e(r["auto_fixed"]) + "</li>")
        out.append("</ul></details></div>")
    return "\n".join(out)


def write_outputs(targets: list[dict], request: str, outdir: Path, startup: dict | None = None) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    sender = (startup or {}).get("sender", {})
    account = sender.get("email", "") if "PLACEHOLDER" not in sender.get("email", "") else ""
    pairs = sorted(((_row(c, account), c["facts"]) for c in targets), key=lambda t: t[0]["_sort"], reverse=True)
    for r, _ in pairs:
        r.pop("_sort")
    rows, facts = [r for r, _ in pairs], [f for _, f in pairs]

    # utf-8-sig so Excel shows dashes and curly quotes correctly.
    with open(outdir / "results.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    drafts = outdir / "drafts"
    drafts.mkdir(exist_ok=True)
    for old in drafts.glob("*.eml"):
        old.unlink()
    for i, r in enumerate(rows, 1):
        if r["channel"].startswith("email") and r["email_body"].strip():
            slug = re.sub(r"[^a-z0-9]+", "-", r["target"].lower()).strip("-")[:40]
            (drafts / f"{i:02d}-{slug}.eml").write_bytes(_eml(r["to_email"], r["subject"], r["email_body"], sender))
            if r["followup_body"]:
                (drafts / f"{i:02d}-{slug}.followup.eml").write_bytes(
                    _eml(r["to_email"], "Re: " + r["subject"], r["followup_body"], sender))

    (outdir / "results.md").write_text(_md(rows, facts, request), encoding="utf-8")
    (outdir / "results.html").write_text(_html(rows, facts, request), encoding="utf-8")
    return rows

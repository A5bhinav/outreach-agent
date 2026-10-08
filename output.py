"""Write results.csv and results.md (sorted by fit score). Drafts only; nothing is sent."""
import csv
from pathlib import Path

FIELDS = ["company", "website", "location", "contact_name", "contact_title", "contact_source",
          "fit_score", "fit_reason", "source_urls", "subject", "email_body", "flags"]


def _row(c: dict) -> dict:
    ct = c["contact"]
    found = ct["name"] != "unknown"
    return {
        "company": c["name"],
        "website": c["website"],
        "location": c.get("location", "unknown"),
        "contact_name": ct["name"],
        "contact_title": ct["title"] if found else f"unknown (target: {ct['suggested_title']})",
        "contact_source": ct["source_url"],
        "fit_score": c["fit_score"],
        "fit_reason": c["fit_reason"],
        "source_urls": " | ".join(c["source_urls"]),
        "subject": c["final"]["subject"],
        "email_body": c["final"]["body"],
        "flags": " | ".join(c["flags"]) or "none",
    }


def write_outputs(companies: list[dict], request: str, outdir: Path) -> list[dict]:
    outdir.mkdir(exist_ok=True)
    rows = sorted((_row(c) for c in companies), key=lambda r: r["fit_score"], reverse=True)

    with open(outdir / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    facts = {c["name"]: c["facts"] for c in companies}
    md = [f"# Outreach drafts\n\nRequest: _{request}_\n\n**Drafts only. Review every email before sending.**\n"]
    for i, r in enumerate(rows, 1):
        md.append(f"## {i}. {r['company']} — fit {r['fit_score']}/10\n")
        md.append(f"- Website: {r['website']}  \n- Location: {r['location']}")
        md.append(f"- Contact: {r['contact_name']}, {r['contact_title']}"
                  + (f" ([source]({r['contact_source']}))" if r['contact_source'] != "unknown" else ""))
        md.append(f"- Fit: {r['fit_reason']}")
        md.append("- Research notes:")
        for f in facts[r["company"]]:
            md.append(f"  - {f['fact']} ({f['date']}) — {f['source_url']}")
        md.append(f"- Reviewer flags: {r['flags']}\n")
        md.append(f"**Subject:** {r['subject']}\n")
        md.append("\n".join("> " + line for line in r["email_body"].splitlines()) + "\n")
    (outdir / "results.md").write_text("\n".join(md))
    return rows

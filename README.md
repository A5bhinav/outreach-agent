# outreach-agent

Proof-of-concept CLI for portfolio support requests. Give it a plain-English request and a
portfolio company profile. It works out whether you want to **meet companies** (customers,
pilot sites, partners) or **find people** (e.g. a specific kind of engineer to hire). It then
searches the web for targets and drafts a personalized, fact-checked intro email (plus a
follow-up) for each one, written as you, the investor.
**It never sends anything.** You review the output and send it yourself.

## Pipeline

| Step | Module | What it does |
|---|---|---|
| 0. Preflight | `agents/llm.py` | One cheap web-search call so a broken setup fails in seconds; switches to direct search if the org refuses dynamic filtering |
| 1. Planner | `agents/planner.py` | Request → mode (`companies` / `people`), 5-8 search queries (including trigger-event queries), rubric with must-haves, disqualifiers, target roles, research signals, the ask |
| 2. Sourcer | `agents/sourcer.py` | Web-searches each query concurrently; dedupes by domain and name; keeps companies whose homepage isn't confirmed yet; skips anything in the ledger |
| 3a. Researcher (companies) | `agents/researcher.py` | Dated, sourced facts (none older than 18 months) and a yes/no/unclear verdict with a link for each rubric criterion; keeps only companies that meet every must-have |
| 3b. People finder (people) | `agents/people.py` | Finds people at or from the sourced companies with dated evidence of their work (talks, papers, patents, blogs, team pages, GitHub). Shows criteria evidence, not a score; LinkedIn appears only as a link |
| 2-3 again | `agents/planner.py` | If fewer than `--n` targets qualify, one more round of searches from new angles |
| 4. Contact finder (companies) | `agents/contacts.py` | Named decision-maker and published email only if a public page shows them, checked against that page; optional verified-email lookup in Apollo |
| 5. Writer | `agents/writer.py` | 50-110-word double-opt-in intro opening on the most recent fact, one ask, plus a short follow-up |
| 6. Reviewer | `agents/reviewer.py` | Checks facts and style (length, one ask, nothing presumed, no buzzwords, no placeholders), sends problems back for one rewrite, checks again, adds the legal footer |

Research, people finding, writing and review use `claude-opus-5-5`. Planning, sourcing and
contact lookup use `claude-sonnet-5`. Both are set in `agents/llm.py`. Steps 0-4 use
Anthropic's server-side web search and web fetch tools. Each step returns structured JSON
through a strict `submit` tool. Per-target steps run concurrently (5 at a time by default).
Configuration errors stop the run immediately instead of producing an empty result.

## Setup

Requires Python 3.10+.

```bash
cd outreach-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # or `ant auth login`
export APOLLO_API_KEY=...             # optional: verified emails for named contacts (companies mode)
```

Web search and web fetch must be enabled for your organization in the Claude Console.

Make one profile per portfolio company (copy `startup.yaml`). Fill in the company name,
founder, pitch, ICP notes and proof points, and set the `sender` block to yourself, including
`firm_address`. Every email carries it in its footer, and emails are blocked until it's set.
`voice: investor` (the default) writes a double-opt-in intro from you; `voice: founder` writes
as the founder. Any email that still contains a `PLACEHOLDER` value is marked not ready to send.

## Example runs

```bash
# Meet companies
python main.py "find general contractors and construction firms that might want robotics for materials handling, mid-size, US" --n 20 --config portfolio/acme-robotics.yaml

# Find people
python main.py "find systems engineers who have deployed autonomous mobile robot fleets in warehouses" --n 10 --config portfolio/acme-robotics.yaml

# Someone replied "unsubscribe": never contact them again, for any portfolio company
python main.py --opt-out jane@example.com      # or a domain: --opt-out example.com
```

Progress is logged to stderr. Each run writes to `outputs/<timestamp>/` (or `--out DIR`):

- `results.md`: the review page. For each target it shows the recipient, an **Open in Gmail** link (a pre-filled compose window), the research notes and criteria behind the email, reviewer flags, the email itself and the follow-up.
- `drafts/*.eml`: the same emails as draft files that open in Mail, Outlook or Thunderbird.
- `results.csv`: one row per target, ready-to-send rows first.
- `run.json`: full intermediate data, saved after each stage.

`ready_to_send` is `yes` only when all of these hold:
- a verified email address was found (published on a page and confirmed there, or Apollo-verified);
- a named contact was found and not contradicted by its source page;
- the reviewer raised no blockers.

Otherwise it says why. Send the follow-up about five business days later, in the same thread,
if there's no reply.

Options: `--n`, `--config`, `--out`, `--ledger`, `--opt-out`, `--effort low|medium|high|xhigh|max`
(lower is cheaper and faster), `--concurrency`, `--skip-preflight`.

## Contact ledger

`ledger.csv` (git-ignored, shared across runs and portfolio companies) records every drafted
email. The tool writes `drafted`. Change a row to `sent` once you send it, and use `--opt-out`
for unsubscribes. Later runs skip:
- anyone who opted out, for every portfolio company;
- anyone already drafted or contacted for the same portfolio company in the last 180 days;
- anyone sent an intro for another portfolio company in the last 30 days.

## Evals

`evals/requests.yaml` holds 12 representative requests: 7 about meeting companies, 5 about
finding people. Run them before and after a change:

```bash
python evals/run.py --n 3 --only gc-robotics,amr-systems-eng   # real API calls; costs money
python evals/score.py evals/results/<timestamp>/*              # re-score finished runs
```

Each run is scored on: routing to the right mode, targets found, how many are ready to send,
email and contact coverage, blockers and flags, median length, the share of dated and stale
facts, distinct subjects, errors and cost.

## Cost

A 20-target run makes roughly 100+ Claude calls and a few hundred web searches. Try `--n 5`
first. Web search is billed per search on top of tokens. The final log line prints totals.

## Rules baked in

- Facts, names, emails and URLs come only from pages Claude read during the run; anything else is `unknown`. Facts older than 18 months are dropped.
- Email addresses are never guessed from name patterns. They're never taken from GitHub or LinkedIn either: GitHub's policies bar using its data for unsolicited email, and LinkedIn's bar scraping. LinkedIn pages are never fetched. An address is dropped if it isn't on the page it was attributed to.
- Every email carries an intro disclosure, an opt-out line and the firm's postal address (CAN-SPAM). EU and UK recipients also get where their details came from and their right to object (GDPR Art. 14). This is a reading of the rules, not legal advice; have counsel confirm it.
- The people search reports evidence of work only: no scores shown, no inferred personal traits, and a person decides whom to contact.
- No sending code exists. Review every draft yourself.

`examples/` holds a 5-company demo from an earlier version.

Not built: integrations with Harmonic and Affinity, and creating Gmail drafts through the Gmail
API. The Gmail compose links and `.eml` files cover sending without OAuth setup.

# outreach-agent

Proof-of-concept CLI. Give it a plain-English description of who you want to meet; it finds
~20 matching companies on the web and drafts a personalized cold email for each.
**It never sends anything.** Output is a file for a human to review.

## Pipeline

| Step | Module | What it does |
|---|---|---|
| 1. Planner | `agents/planner.py` | Request → 5-8 search queries + fit rubric |
| 2. Sourcer | `agents/sourcer.py` | Web-searches each query concurrently, dedupes by domain |
| 3. Researcher | `agents/researcher.py` | 2-3 sourced recent facts per company, fit score 1-10, keeps top N |
| 4. Contact finder | `agents/contacts.py` | Named decision-maker only if a public page shows one; else `unknown` + suggested title. No emails. |
| 5. Writer | `agents/writer.py` | <120-word email built on one researched fact, one ask |
| 6. Reviewer | `agents/reviewer.py` | Flags unsupported claims and rewrites them out |

Every step uses Claude (`claude-opus-5`) through `agents/llm.py`. Steps 2-4 use Anthropic's
server-side web search / web fetch tools. Each step returns structured JSON via a strict
`submit` tool. Per-company steps run concurrently with asyncio (default 5 at a time).

## Setup

```bash
cd outreach-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

Edit `startup.yaml` with your real company name, pitch, ICP notes, proof points, sender name
and ask. The writer may only cite proof points from this file.

## Example run

```bash
python main.py "find general contractors and construction firms that might want robotics for materials handling, mid-size, US" --n 20
```

Progress is logged to stderr. Results:

- `outputs/results.csv`: company, website, contact name/title, fit score, fit reason, source URLs, subject, email body, flags
- `outputs/results.md`: same, readable, sorted by fit score, with the research notes behind each email
- `outputs/run.json`: full intermediate data (plan, facts, drafts before review)

Options: `--n` (companies to keep), `--config`, `--effort low|medium|high|xhigh|max`
(lower is cheaper/faster), `--concurrency`.

## Cost

A 20-company run makes roughly 100+ Claude calls and a few hundred web searches. Try `--n 5`
first. Web search is billed per search on top of tokens. The final log line prints totals.

## Rules baked in

- Facts, names and URLs come only from pages Claude read during the run; anything else is `unknown`.
- No email addresses are collected or guessed (and any that slip through are stripped).
- Companies with no sourced facts are dropped rather than written to with generic copy.
- No sending code exists. Review every draft yourself.

# Example outputs (5-company demo)

Request: "find general contractors and construction firms that might want robotics for materials handling, mid-size, US" (`--n 5`)

- `results.md` – readable drafts sorted by fit score, with the sourced research notes behind each email
- `results.csv` – same data, one row per company
- `run.json` – the plan (queries + rubric), research facts, contacts, drafts and reviewer flags

How these were produced:
- **Step 1 (planner)** ran through `main.py` against the Anthropic API.
- **Steps 2-6** were done by hand with Claude Code's web search and fetch tools, following the same rules as the agents in `agents/`. The API run's sourcing step returned 0 companies. The cause is still unconfirmed; the likely one is web search not being enabled for the API organization. `results.csv` and `results.md` were written by `output.py`.

Every fact, name and URL comes from a public page opened during the run. "Example Robotics" / "Alex" are placeholders from `startup.yaml`.

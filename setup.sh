#!/usr/bin/env bash
# One-command setup: virtualenv, dependencies, tests, and an offline demo run.
#   ./setup.sh            set everything up and open the demo review page
#   ./setup.sh --no-demo  skip the demo
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required")'

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt -r requirements-dev.txt
echo "✓ dependencies installed in .venv"

[ -f sender.yaml ] || { cp sender.example.yaml sender.yaml; echo "✓ created sender.yaml (fill in your details before a real run)"; }

.venv/bin/python -m pytest -q
echo "✓ tests pass"

if [ "${1:-}" != "--no-demo" ]; then
  page=$(.venv/bin/python main.py --demo 2>/dev/null | tail -1)
  echo "✓ demo run (offline, fictional data): $page"
  case "$(uname)" in Darwin) open "$page" ;; Linux) command -v xdg-open >/dev/null && xdg-open "$page" || true ;; esac
fi

cat <<'NEXT'

Next, for a real run:
  1. export ANTHROPIC_API_KEY=sk-ant-...         (web search must be enabled for the org)
  2. fill in sender.yaml                          (your name, firm, email, firm postal address)
  3. .venv/bin/python main.py --new-company "Company Name"   then fill in portfolio/<company>.yaml
  4. .venv/bin/python main.py "who you want to meet" --company <company> --plan-only
  5. drop --plan-only to run it; open outputs/<run>/results.html
NEXT

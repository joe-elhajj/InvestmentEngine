#!/bin/bash
# Resolve the checkout independently of launchd's working directory and PATH.
# API keys are inherited from the installed launchd job's environment.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "launcher.sh: Python not found at $PYTHON" >&2
  echo "Create the repository's .venv and install requirements.txt as described in README.md Setup." >&2
  exit 1
fi

exec "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

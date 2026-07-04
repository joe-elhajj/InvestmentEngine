#!/bin/bash
# launcher.sh — starts the Investment Engine web app for launchd.
#
# launchd invokes this script directly (/bin/bash app/launcher.sh), not as
# an interactive or login shell — conda's shell hooks are never sourced and
# PATH is whatever launchd's minimal default is, NOT the user's terminal
# PATH. So this resolves an ABSOLUTE path to the conda base environment's
# uvicorn instead of just calling `uvicorn` and hoping PATH has it.
#
# No secrets live here — just a path resolution and an exec.

set -euo pipefail

cd "/Users/joeelhajj/Projects/Investment Engine"

# Resolve the conda base env's prefix. Prefers `conda info --base` (correct
# even if conda is reinstalled/moved later); falls back to the two standard
# install locations if `conda` itself isn't on this minimal PATH either.
if command -v conda >/dev/null 2>&1; then
  CONDA_BASE="$(conda info --base)"
elif [ -x "/opt/anaconda3/bin/conda" ]; then
  CONDA_BASE="$(/opt/anaconda3/bin/conda info --base)"
elif [ -x "$HOME/anaconda3/bin/conda" ]; then
  CONDA_BASE="$("$HOME/anaconda3/bin/conda" info --base)"
else
  echo "launcher.sh: could not locate a conda installation (checked PATH, /opt/anaconda3, ~/anaconda3)." >&2
  exit 1
fi

UVICORN="$CONDA_BASE/bin/uvicorn"
if [ ! -x "$UVICORN" ]; then
  echo "launcher.sh: uvicorn not found at $UVICORN" >&2
  echo "Install it in the conda base env: \"$CONDA_BASE/bin/pip\" install fastapi \"uvicorn[standard]\"" >&2
  exit 1
fi

exec "$UVICORN" app.main:app --host 127.0.0.1 --port 8000

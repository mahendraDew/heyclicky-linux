#!/usr/bin/env bash
# Run heyclicky straight from the source tree.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "No virtualenv found. Run ./install.sh once, or create one with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

echo "Starting Clicky… the panel opens in a moment."
echo "Tip: if it is hidden, run './run.sh --show-panel'."
exec env PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m heyclicky "$@"

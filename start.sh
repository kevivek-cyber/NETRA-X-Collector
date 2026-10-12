#!/usr/bin/env sh
# NETRA-X collector for Linux / macOS. Needs python3 and tor (apt install tor,
# brew install tor) or a running Tor Browser.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi
if [ -z "$NETRAX_COLLECTOR_TOKEN" ]; then
  echo "Set NETRAX_COLLECTOR_TOKEN first (ask the NETRA-X admin)." >&2
  exit 1
fi
export NETRAX_SERVER="${NETRAX_SERVER:-https://console.onnetra.in}"
exec .venv/bin/python -m workers.collector_agent "$@"

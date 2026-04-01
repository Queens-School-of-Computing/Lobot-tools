#!/bin/bash
# Launcher for lobot-collector service.
# WorkingDirectory must be the tools directory so lobot_tui is importable.
TOOLS_DIR="${LOBOT_TOOLS_DIR:-/opt/Lobot/tools}"
VENV="$TOOLS_DIR/lobot_collector/.venv"

if [[ ! -d "$VENV" ]]; then
  echo "ERROR: Collector venv not found at $VENV" >&2
  echo "Run: python3 -m venv $VENV && $VENV/bin/pip install -r $TOOLS_DIR/lobot_collector/requirements-collector.txt" >&2
  exit 1
fi

exec "$VENV/bin/python3" -m lobot_collector

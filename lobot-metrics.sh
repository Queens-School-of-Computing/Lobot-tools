#!/bin/bash
# Launcher for lobot-metrics service and CLI.
# WorkingDirectory must be the tools directory so lobot_tui is importable.
TOOLS_DIR="${LOBOT_TOOLS_DIR:-/opt/Lobot/tools}"
exec "$TOOLS_DIR/lobot_metrics/.venv/bin/python3" -m lobot_metrics "$@"

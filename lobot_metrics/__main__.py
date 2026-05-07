"""Entry point: python3 -m lobot_metrics [subcommand ...]

Dispatches to cli.main() so all subcommands work:
  python3 -m lobot_metrics              → daemon
  python3 -m lobot_metrics report ...   → report
  python3 -m lobot_metrics sessions ... → sessions
  etc.
"""

from .cli import main

main()

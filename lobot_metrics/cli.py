"""Command-line interface for lobot-metrics.

Usage:
    python3 -m lobot_metrics                        # start daemon (recorder + snapshotter)
    python3 -m lobot_metrics report  --month YYYY-MM [--by user|lab|group]
    python3 -m lobot_metrics export  --month YYYY-MM --format csv --out FILE
    python3 -m lobot_metrics sessions [--active] [--user USER] [--lab LAB]
    python3 -m lobot_metrics send-digest --month YYYY-MM [--to EMAIL]
"""

import argparse
import sys
from pathlib import Path


def _parse_month(value: str) -> tuple[int, int]:
    try:
        year, month = value.split("-")
        y, m = int(year), int(month)
        if not (1 <= m <= 12):
            raise ValueError
        return y, m
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError(f"month must be YYYY-MM, got: {value!r}")


# ── Sub-command handlers ───────────────────────────────────────────────────────

def cmd_report(args: argparse.Namespace) -> None:
    from .billing import load_billing_config
    from .config import BILLING_CONFIG_PATH, DB_PATH
    from .db import open_db
    from .reporter import (
        format_table,
        storage_by_group,
        storage_by_user,
        usage_by_group,
        usage_by_lab,
        usage_by_user,
    )

    year, month = args.month
    month_label = f"{year:04d}-{month:02d}"
    by = args.by  # None means print all sections
    billing = load_billing_config(BILLING_CONFIG_PATH)

    conn = open_db(DB_PATH)
    try:
        rows_user = usage_by_user(conn, year, month) if by in (None, "user") else None
        rows_lab = usage_by_lab(conn, year, month) if by in (None, "lab") else None
        rows_group = usage_by_group(conn, year, month, billing) if by in (None, "group") else None
        storage_user = storage_by_user(conn, year, month) if by in (None, "user") else None
        storage_group = storage_by_group(conn, year, month, billing) if by in (None, "group") else None
    finally:
        conn.close()

    print(f"\nLobot Cluster Usage — {month_label}\n")

    if rows_group is not None:
        print("── By Billing Group ──\n")
        print(format_table(rows_group,
            ["display_name", "user_count", "session_count", "total_hours",
             "cpu_core_hours", "ram_gb_hours", "gpu_hours",
             "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"]))

    if rows_lab is not None:
        print("── By Lab ──\n")
        print(format_table(rows_lab,
            ["lab", "user_count", "session_count", "total_hours",
             "cpu_core_hours", "ram_gb_hours", "gpu_hours",
             "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"]))

    if rows_user is not None:
        print("── By User ──\n")
        print(format_table(rows_user,
            ["username", "lab", "session_count", "total_hours",
             "cpu_core_hours", "ram_gb_hours", "gpu_hours",
             "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"]))

    if storage_group is not None:
        print("── Storage by Billing Group ──\n")
        print(format_table(storage_group,
            ["display_name", "user_count", "total_avg_gb"]))

    if storage_user is not None:
        print("── Storage by User ──\n")
        print(format_table(storage_user,
            ["username", "pvc_name", "pvc_capacity_gb"]))


def cmd_export(args: argparse.Namespace) -> None:
    from .billing import load_billing_config
    from .config import BILLING_CONFIG_PATH, DB_PATH
    from .db import open_db
    from .reporter import (
        export_csv,
        usage_by_group,
        usage_by_lab,
        usage_by_user,
    )

    year, month = args.month
    out = Path(args.out)
    conn = open_db(DB_PATH)
    try:
        if args.by == "user":
            rows = usage_by_user(conn, year, month)
            cols = ["username", "lab", "session_count", "total_hours",
                    "cpu_core_hours", "ram_gb_hours", "gpu_hours",
                    "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"]
        elif args.by == "lab":
            rows = usage_by_lab(conn, year, month)
            cols = ["lab", "user_count", "session_count", "total_hours",
                    "cpu_core_hours", "ram_gb_hours", "gpu_hours",
                    "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"]
        else:  # group
            billing = load_billing_config(BILLING_CONFIG_PATH)
            rows = usage_by_group(conn, year, month, billing)
            cols = ["group", "display_name", "user_count", "session_count",
                    "total_hours", "cpu_core_hours", "ram_gb_hours", "gpu_hours",
                    "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"]
    finally:
        conn.close()

    export_csv(rows, cols, out)
    print(f"Exported {len(rows)} row(s) to {out}")


def cmd_sessions(args: argparse.Namespace) -> None:
    from .config import DB_PATH
    from .db import open_db, query_active_sessions, query_sessions_for_period
    from .reporter import format_table

    conn = open_db(DB_PATH)
    try:
        if args.active:
            rows = query_active_sessions(conn)
            cols = ["session_id", "username", "lab", "node",
                    "start_time", "cpu_requested", "ram_requested_gb", "gpu_requested"]
            print(f"\nActive sessions ({len(rows)} running)\n")
        else:
            from datetime import datetime, timezone
            start = "2000-01-01T00:00:00Z"
            end = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            rows = query_sessions_for_period(
                conn, start, end,
                username=args.user,
                lab=args.lab,
                include_open=True,
            )
            cols = ["session_id", "username", "lab", "start_time",
                    "end_time", "duration_seconds", "cpu_requested",
                    "ram_requested_gb", "gpu_requested", "end_reason"]
            print(f"\nSessions ({len(rows)} total)\n")
    finally:
        conn.close()

    # Session list can be wide; truncate session_id for display
    display_rows = []
    for row in rows:
        r = dict(row)
        if "session_id" in r and r["session_id"]:
            r["session_id"] = r["session_id"][:40] + "…" if len(r["session_id"]) > 40 else r["session_id"]
        display_rows.append(r)

    print(format_table(display_rows, cols))


def cmd_send_digest(args: argparse.Namespace) -> None:
    from datetime import date

    from .emailer import send_monthly_digest

    if args.month:
        year, month = args.month
    else:
        # Default to the previous calendar month
        today = date.today()
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1

    send_monthly_digest(year, month, to=args.to or None)


def cmd_daemon(_args: argparse.Namespace) -> None:
    import asyncio
    import logging
    import signal
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger("lobot_metrics")

    from .config import DB_PATH
    from .db import init_db
    from .recorder import MetricsRecorder
    from .snapshotter import PeriodicSnapshotter

    async def _run() -> None:
        init_db(DB_PATH)
        logger.info("Database ready at %s", DB_PATH)

        recorder = MetricsRecorder(DB_PATH)
        snapshotter = PeriodicSnapshotter(recorder, DB_PATH)
        shutdown_event = asyncio.Event()

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info("Received %s, shutting down…", sig_name)
            asyncio.get_event_loop().call_soon_threadsafe(shutdown_event.set)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        logger.info("Reconciling open sessions with live cluster state…")
        await recorder.reconcile_on_startup()

        logger.info("Starting SSE recorder and periodic snapshotter")
        await recorder.start()
        await snapshotter.start()

        logger.info("lobot-metrics running — Ctrl-C or SIGTERM to stop")
        await shutdown_event.wait()
        logger.info("Shutting down")

    asyncio.run(_run())


# ── Parser ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lobot-metrics",
        description="Lobot cluster resource tracking and billing",
    )
    sub = parser.add_subparsers(dest="command")

    # report
    p_report = sub.add_parser("report", help="Print usage report for a month")
    p_report.add_argument("--month", required=True, type=_parse_month, metavar="YYYY-MM")
    p_report.add_argument(
        "--by", choices=["user", "lab", "group"], default=None,
        help="Group results by user, lab, or group (default: all three)",
    )

    # export
    p_export = sub.add_parser("export", help="Export usage data to a file")
    p_export.add_argument("--month", required=True, type=_parse_month, metavar="YYYY-MM")
    p_export.add_argument(
        "--by", choices=["user", "lab", "group"], default="user",
    )
    p_export.add_argument("--format", choices=["csv"], default="csv")
    p_export.add_argument("--out", required=True, metavar="FILE")

    # sessions
    p_sessions = sub.add_parser("sessions", help="List recorded sessions")
    p_sessions.add_argument("--active", action="store_true", help="Only show running sessions")
    p_sessions.add_argument("--user", metavar="USERNAME")
    p_sessions.add_argument("--lab", metavar="LAB")

    # send-digest
    p_digest = sub.add_parser("send-digest", help="Send monthly email digest")
    p_digest.add_argument(
        "--month", required=False, type=_parse_month, metavar="YYYY-MM",
        help="Month to report (default: previous calendar month)",
    )
    p_digest.add_argument("--to", metavar="EMAIL", help="Override recipient address")

    # daemon (explicit)
    sub.add_parser("daemon", help="Start the recorder daemon (default when no command given)")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None or args.command == "daemon":
        cmd_daemon(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "sessions":
        cmd_sessions(args)
    elif args.command == "send-digest":
        cmd_send_digest(args)
    else:
        parser.print_help()
        sys.exit(1)

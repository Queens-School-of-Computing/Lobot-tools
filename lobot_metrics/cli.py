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

    send_monthly_digest(
        year, month,
        to=args.to or None,
        lab=args.lab or None,
        group=args.group or None,
        all_heatmap=args.all_heatmap,
    )


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


_DOW_ORDER = [1, 2, 3, 4, 5, 6, 0]  # Mon..Sun (strftime %w: 0=Sun)
_DOW_LABELS = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}


def _heatmap_char(util: float) -> str:
    if util == 0:
        return "·"
    if util <= 0.25:
        return "░"
    if util <= 0.50:
        return "▒"
    if util <= 0.75:
        return "▓"
    return "█"


def cmd_heatmap(args: argparse.Namespace) -> None:
    from .config import DB_PATH, HEATMAP_TIMEZONE_NAME, heatmap_tz_offset_minutes
    from .db import open_db, query_heatmap
    from .reporter import _month_bounds

    year, month = args.month
    month_label = f"{year:04d}-{month:02d}"
    metric = args.metric
    lab = args.lab or None
    tz_offset = heatmap_tz_offset_minutes(year, month)

    start, end = _month_bounds(year, month)
    conn = open_db(DB_PATH)
    try:
        rows = query_heatmap(conn, start, end, metric=metric,
                             labs=[lab] if lab else None,
                             tz_offset_minutes=tz_offset)
    finally:
        conn.close()

    if not rows:
        print(f"\n(no snapshot data for {month_label} — the daemon must run for at least one snapshot interval)\n")
        return

    grid = {(r["dow"], r["hour"]): r for r in rows}

    if metric == "pods":
        max_val = max(r["avg_util"] or 0 for r in rows) or 1
        def norm(d, h):
            r = grid.get((d, h))
            return (r["avg_util"] or 0) / max_val if r else 0
    else:
        def norm(d, h):
            r = grid.get((d, h))
            return r["avg_util"] or 0 if r else 0

    peak_row = max(rows, key=lambda r: r["peak_util"] or 0)

    metric_titles = {
        "gpu": "GPU Utilization",
        "cpu": "CPU Utilization",
        "ram": "RAM Utilization",
        "pods": "Active Pods",
    }
    lab_label = f" ({lab})" if lab else " (all labs)"
    print(f"\n{metric_titles[metric]} Heatmap — {month_label}{lab_label}\n")

    col_w = 5
    print("       " + "".join(f"{_DOW_LABELS[d]:^{col_w}}" for d in _DOW_ORDER))
    for hour in range(24):
        cells = "".join(f"{_heatmap_char(norm(d, hour)):^{col_w}}" for d in _DOW_ORDER)
        print(f"{hour:2d}:00  {cells}")

    print()
    print("Legend:  · 0%   ░ 1–25%   ▒ 26–50%   ▓ 51–75%   █ 76–100%")

    dow_name = _DOW_LABELS[peak_row["dow"]]
    hour_str = f"{peak_row['hour']:02d}:00 {HEATMAP_TIMEZONE_NAME}"
    capacity = peak_row.get("capacity")
    if metric == "pods":
        print(f"Peak:    {int(peak_row['peak_util'])} pods — {dow_name} {hour_str}")
    else:
        peak_pct = (peak_row["peak_util"] or 0) * 100
        if capacity:
            peak_abs = (peak_row["peak_util"] or 0) * capacity
            units = {"gpu": "GPUs", "cpu": "cores", "ram": "GB"}[metric]
            print(f"Peak:    {peak_pct:.0f}%  ({peak_abs:.0f}/{capacity:.0f} {units}) — {dow_name} {hour_str}")
        else:
            print(f"Peak:    {peak_pct:.0f}% — {dow_name} {hour_str}")
    print()


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
    p_digest.add_argument(
        "--lab", metavar="LAB",
        help="Show heatmap only for this lab (omits all-labs aggregate)",
    )
    p_digest.add_argument(
        "--group", metavar="GROUP",
        help="Show heatmap only for this billing group key (omits all-labs aggregate)",
    )
    p_digest.add_argument(
        "--all-heatmap", action="store_true",
        help="Include the all-labs aggregate heatmap alongside --lab or --group",
    )

    # heatmap
    p_heatmap = sub.add_parser("heatmap", help="Show resource utilization heatmap (hour × day-of-week)")
    p_heatmap.add_argument("--month", required=True, type=_parse_month, metavar="YYYY-MM")
    p_heatmap.add_argument(
        "--metric", choices=["gpu", "cpu", "ram", "pods"], default="gpu",
        help="Metric to visualize (default: gpu)",
    )
    p_heatmap.add_argument("--lab", metavar="LAB", help="Filter to a specific lab (default: all labs combined)")

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
    elif args.command == "heatmap":
        cmd_heatmap(args)
    else:
        parser.print_help()
        sys.exit(1)

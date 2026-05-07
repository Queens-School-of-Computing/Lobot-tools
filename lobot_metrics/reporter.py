"""Aggregation queries and formatted output for lobot-metrics reports."""

import calendar
import csv
import sqlite3
from pathlib import Path
from typing import Optional

from .billing import BillingConfig
from .db import query_storage_by_user, query_usage_by_lab, query_usage_by_user


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return (start_iso, end_iso) for the given calendar month."""
    _, last_day = calendar.monthrange(year, month)
    start = f"{year:04d}-{month:02d}-01T00:00:00Z"
    if month == 12:
        end = f"{year + 1:04d}-01-01T00:00:00Z"
    else:
        end = f"{year:04d}-{month + 1:02d}-01T00:00:00Z"
    return start, end


def usage_by_user(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    username: Optional[str] = None,
    lab: Optional[str] = None,
) -> list[dict]:
    start, end = _month_bounds(year, month)
    return query_usage_by_user(conn, start, end, username=username, lab=lab)


def usage_by_lab(
    conn: sqlite3.Connection,
    year: int,
    month: int,
) -> list[dict]:
    start, end = _month_bounds(year, month)
    return query_usage_by_lab(conn, start, end)


def usage_by_group(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    billing: BillingConfig,
) -> list[dict]:
    """Aggregate per-user rows into billing groups."""
    rows = usage_by_user(conn, year, month)
    groups: dict[str, dict] = {}

    for row in rows:
        key = billing.resolve_group(row["username"], row["lab"]) or "unassigned"
        if key not in groups:
            groups[key] = {
                "group": key,
                "display_name": billing.get_display_name(key),
                "user_count": 0,
                "session_count": 0,
                "total_hours": 0.0,
                "gpu_hours": 0.0,
                "cpu_core_hours": 0.0,
                "ram_gb_hours": 0.0,
                "peak_cpu": 0.0,
                "peak_ram_gb": 0.0,
                "peak_gpu": 0,
                "pvc_capacity_gb": 0.0,
            }
        g = groups[key]
        g["user_count"] += 1
        g["session_count"] += row["session_count"]
        g["total_hours"] = round(g["total_hours"] + (row["total_hours"] or 0.0), 2)
        g["gpu_hours"] = round(g["gpu_hours"] + (row["gpu_hours"] or 0.0), 2)
        g["cpu_core_hours"] = round(g["cpu_core_hours"] + (row["cpu_core_hours"] or 0.0), 2)
        g["ram_gb_hours"] = round(g["ram_gb_hours"] + (row["ram_gb_hours"] or 0.0), 2)
        g["peak_cpu"] = round(g["peak_cpu"] + (row["peak_cpu"] or 0.0), 1)
        g["peak_ram_gb"] = round(g["peak_ram_gb"] + (row["peak_ram_gb"] or 0.0), 1)
        g["peak_gpu"] += row["peak_gpu"] or 0
        g["pvc_capacity_gb"] = round(g["pvc_capacity_gb"] + (row["pvc_capacity_gb"] or 0.0), 2)

    return sorted(groups.values(), key=lambda x: x["total_hours"], reverse=True)


# ── Formatting ─────────────────────────────────────────────────────────────────

_NUMERIC = {int, float}

_COLUMN_HEADERS = {
    "username": "User",
    "lab": "Lab",
    "group": "Group",
    "display_name": "Billing Group",
    "user_count": "Users",
    "session_count": "Sessions",
    "total_hours": "Hours",
    "gpu_hours": "GPU-hrs",
    "cpu_core_hours": "CPU-core-hrs",
    "ram_gb_hours": "RAM-GB-hrs",
    "avg_cpu": "Avg CPU",
    "avg_ram_gb": "Avg RAM GB",
    "avg_gpu": "Avg GPU",
    "peak_cpu": "Peak CPU",
    "peak_ram_gb": "Peak RAM GB",
    "peak_gpu": "Peak GPU",
    "pvc_capacity_gb": "Avg PVC GB",
    "snapshot_count": "Samples",
    "total_avg_gb": "Total PVC GB",
    "pvc_name": "PVC",
}


def _fmt(val) -> str:
    if val is None:
        return "-"
    if isinstance(val, float):
        return f"{val:.2f}"
    return str(val)


def format_table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(no data)\n"

    headers = {col: _COLUMN_HEADERS.get(col, col.replace("_", " ").title()) for col in columns}

    # Compute column widths
    widths = {col: len(headers[col]) for col in columns}
    for row in rows:
        for col in columns:
            widths[col] = max(widths[col], len(_fmt(row.get(col))))

    def _is_num(col: str) -> bool:
        for row in rows:
            v = row.get(col)
            if v is not None:
                return type(v) in _NUMERIC
        return False

    def _cell(val, col: str) -> str:
        s = _fmt(val)
        return s.rjust(widths[col]) if _is_num(col) else s.ljust(widths[col])

    def _header_cell(col: str) -> str:
        h = headers[col]
        return h.rjust(widths[col]) if _is_num(col) else h.ljust(widths[col])

    sep = "  ".join("-" * widths[col] for col in columns)
    header_line = "  ".join(_header_cell(col) for col in columns)
    data_lines = [
        "  ".join(_cell(row.get(col), col) for col in columns)
        for row in rows
    ]
    return "\n".join([header_line, sep] + data_lines) + "\n"


def storage_by_user(
    conn: sqlite3.Connection,
    year: int,
    month: int,
) -> list[dict]:
    """Per-user average PVC allocation for the month."""
    start, end = _month_bounds(year, month)
    return query_storage_by_user(conn, start, end)


def storage_by_group(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    billing: BillingConfig,
) -> list[dict]:
    """Per-billing-group total PVC allocation (GB)."""
    rows = storage_by_user(conn, year, month)
    groups: dict[str, dict] = {}
    for row in rows:
        key = billing.resolve_group(row["username"], row.get("lab", "")) or "unassigned"
        if key not in groups:
            groups[key] = {
                "group": key,
                "display_name": billing.get_display_name(key),
                "user_count": 0,
                "total_avg_gb": 0.0,
            }
        g = groups[key]
        g["user_count"] += 1
        g["total_avg_gb"] = round(g["total_avg_gb"] + (row["pvc_capacity_gb"] or 0.0), 2)
    return sorted(groups.values(), key=lambda x: x["total_avg_gb"], reverse=True)


def export_csv(rows: list[dict], columns: list[str], out_path: Path) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

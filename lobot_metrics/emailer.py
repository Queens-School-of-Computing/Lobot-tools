"""Monthly HTML digest email for lobot-metrics."""

import asyncio
import logging
import os
import smtplib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from .billing import BillingConfig, load_billing_config
from .config import (
    BILLING_CONFIG_PATH,
    DB_PATH,
    EMAIL_ENABLED,
    FROM_EMAIL,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_SERVER,
    SMTP_USE_TLS,
    SMTP_USERNAME,
    TO_EMAIL,
)
from .db import open_db
from .reporter import (
    _COLUMN_HEADERS,
    _month_bounds,
    storage_by_group,
    storage_by_user,
    usage_by_group,
    usage_by_lab,
    usage_by_user,
)

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="emailer")


# ── SMTP send ──────────────────────────────────────────────────────────────────

def _smtp_send(subject: str, body_html: str, to: Optional[str] = None) -> None:
    if not EMAIL_ENABLED:
        logger.info("Email disabled — would have sent: %s", subject)
        return
    recipient = to or TO_EMAIL
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = FROM_EMAIL
        msg["To"] = recipient
        msg["Subject"] = f"Lobot Metrics: {subject}"
        msg.attach(MIMEText(body_html, "html"))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info("Digest sent to %s: %s", recipient, subject)
    except Exception as exc:
        logger.error("Failed to send digest email (%s): %s", subject, exc)


# ── HTML builder ───────────────────────────────────────────────────────────────

_TABLE_STYLE = (
    "border-collapse:collapse;font-family:monospace;font-size:13px;"
    "width:100%;margin-bottom:24px"
)
_TH_STYLE = (
    "background:#2c3e50;color:#fff;padding:6px 12px;"
    "text-align:left;border:1px solid #aaa"
)
_TD_STYLE = "padding:5px 12px;border:1px solid #ddd"
_TD_NUM_STYLE = "padding:5px 12px;border:1px solid #ddd;text-align:right"


def _th(text: str) -> str:
    return f'<th style="{_TH_STYLE}">{text}</th>'


def _td(val, is_num: bool = False) -> str:
    style = _TD_NUM_STYLE if is_num else _TD_STYLE
    if val is None:
        return f'<td style="{style}">-</td>'
    if isinstance(val, float):
        return f'<td style="{style}">{val:.2f}</td>'
    return f'<td style="{style}">{val}</td>'


def _build_table(rows: list[dict], columns: list[str], title: str) -> str:
    if not rows:
        return f"<h3>{title}</h3><p><em>No data for this period.</em></p>"

    def _is_num(col: str) -> bool:
        for r in rows:
            v = r.get(col)
            if v is not None:
                return isinstance(v, (int, float))
        return False

    headers_html = "".join(
        _th(_COLUMN_HEADERS.get(col, col.replace("_", " ").title()))
        for col in columns
    )
    rows_html = ""
    for row in rows:
        cells = "".join(_td(row.get(col), _is_num(col)) for col in columns)
        rows_html += f"<tr>{cells}</tr>"

    return (
        f"<h3>{title}</h3>"
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{headers_html}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        f"</table>"
    )


def build_monthly_html(
    year: int,
    month: int,
    by_lab: list[dict],
    by_group: list[dict],
    by_user: list[dict],
    storage_group: list[dict],
    storage_user: list[dict],
    month_label: str,
) -> str:
    group_table = _build_table(
        by_group,
        ["display_name", "user_count", "session_count", "total_hours",
         "cpu_core_hours", "ram_gb_hours", "gpu_hours",
         "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"],
        "Compute — By Billing Group",
    )
    lab_table = _build_table(
        by_lab,
        ["lab", "user_count", "session_count", "total_hours",
         "cpu_core_hours", "ram_gb_hours", "gpu_hours",
         "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"],
        "Compute — By Lab",
    )
    user_table = _build_table(
        by_user,
        ["username", "lab", "session_count", "total_hours",
         "cpu_core_hours", "ram_gb_hours", "gpu_hours",
         "peak_cpu", "peak_ram_gb", "peak_gpu", "pvc_capacity_gb"],
        "Compute — By User",
    )
    storage_group_table = _build_table(
        storage_group,
        ["display_name", "user_count", "total_avg_gb"],
        "Storage Allocation — By Billing Group",
    )
    storage_user_table = _build_table(
        storage_user,
        ["username", "pvc_name", "pvc_capacity_gb"],
        "Storage Allocation — By User",
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Lobot Metrics: {month_label}</title></head>
<body style="font-family:sans-serif;max-width:1200px;margin:0 auto;padding:20px">
  <h2>Lobot Cluster Usage Report — {month_label}</h2>
  <p style="color:#555">Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} on {os.uname().nodename}</p>
  <p>
    <strong>Compute metrics:</strong>
    <em>CPU-core-hrs</em> = requested cores × session hours &nbsp;|&nbsp;
    <em>RAM-GB-hrs</em> = requested GB × session hours &nbsp;|&nbsp;
    <em>GPU-hrs</em> = requested GPUs × session hours
  </p>
  <p>
    <strong>Storage metrics:</strong>
    <em>Avg PVC GB</em> = average home directory allocation over the month
    (sampled every 15 minutes)
  </p>
  <hr>
  {group_table}
  {storage_group_table}
  <hr>
  {lab_table}
  <hr>
  {user_table}
  {storage_user_table}
  <p style="color:#999;font-size:11px">
    Compute: only sessions that started and ended within {month_label} are counted.
    Sessions still running at report time are excluded.
    Storage: average PVC allocation from snapshots taken during {month_label}.
  </p>
</body>
</html>"""


# ── Public entry point ─────────────────────────────────────────────────────────

def send_monthly_digest(
    year: int,
    month: int,
    to: Optional[str] = None,
    db_path: Path = DB_PATH,
    billing_path: Path = BILLING_CONFIG_PATH,
) -> None:
    """Build and send the monthly digest email (synchronous; call from CLI or cron)."""
    month_label = f"{year:04d}-{month:02d}"
    logger.info("Building monthly digest for %s", month_label)

    billing = load_billing_config(billing_path)
    conn = open_db(db_path)
    try:
        rows_user = usage_by_user(conn, year, month)
        rows_lab = usage_by_lab(conn, year, month)
        rows_group = usage_by_group(conn, year, month, billing)
        rows_storage_user = storage_by_user(conn, year, month)
        rows_storage_group = storage_by_group(conn, year, month, billing)
    finally:
        conn.close()

    html = build_monthly_html(
        year, month,
        rows_lab, rows_group, rows_user,
        rows_storage_group, rows_storage_user,
        month_label,
    )
    subject = f"Monthly Usage Report — {month_label}"
    _smtp_send(subject, html, to=to)


async def send_monthly_digest_async(
    year: int,
    month: int,
    to: Optional[str] = None,
    db_path: Path = DB_PATH,
    billing_path: Path = BILLING_CONFIG_PATH,
) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor,
        send_monthly_digest,
        year,
        month,
        to,
        db_path,
        billing_path,
    )

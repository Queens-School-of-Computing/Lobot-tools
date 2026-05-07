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
    HEATMAP_TIMEZONE_NAME,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_SERVER,
    SMTP_USE_TLS,
    SMTP_USERNAME,
    TO_EMAIL,
    heatmap_tz_offset_minutes,
)
from .db import open_db, query_heatmap
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


# ── Heatmap ────────────────────────────────────────────────────────────────────

_HM_DOW_ORDER = [1, 2, 3, 4, 5, 6, 0]
_HM_DOW_NAMES = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}

# Light mode: empty=light gray, heavy=dark green (GitHub light)
_HM_ANCHORS_LIGHT = [
    (0.00, (235, 237, 240)),  # #ebedf0
    (0.25, (155, 233, 168)),  # #9be9a8
    (0.50, ( 64, 196,  99)),  # #40c463
    (0.75, ( 48, 161,  78)),  # #30a14e
    (1.00, ( 33, 110,  57)),  # #216e39
]
# Dark mode: empty=near-black, heavy=bright green (GitHub dark)
_HM_ANCHORS_DARK = [
    (0.00, ( 22,  27,  34)),  # #161b22
    (0.25, ( 14,  68,  41)),  # #0e4429
    (0.50, (  0, 109,  50)),  # #006d32
    (0.75, ( 38, 166,  65)),  # #26a641
    (1.00, ( 57, 211,  83)),  # #39d353
]


def _hm_interp(util: float, anchors: list) -> str:
    util = max(0.0, min(1.0, util))
    for i in range(len(anchors) - 1):
        t0, c0 = anchors[i]
        t1, c1 = anchors[i + 1]
        if util <= t1:
            t = (util - t0) / (t1 - t0)
            r = round(c0[0] + t * (c1[0] - c0[0]))
            g = round(c0[1] + t * (c1[1] - c0[1]))
            b = round(c0[2] + t * (c1[2] - c0[2]))
            return f"#{r:02x}{g:02x}{b:02x}"
    r, g, b = anchors[-1][1]
    return f"#{r:02x}{g:02x}{b:02x}"


def _hm_color(util: float) -> str:
    return _hm_interp(util, _HM_ANCHORS_LIGHT)


def _hm_color_dark(util: float) -> str:
    return _hm_interp(util, _HM_ANCHORS_DARK)


def _heatmap_css() -> str:
    """CSS class definitions for heatmap cells, with dark-mode overrides."""
    lines = []
    for i in range(10):
        lines.append(f".hmc{i}{{background:{_hm_color(i/9)}!important}}")
    lines.append("@media(prefers-color-scheme:dark){")
    lines.append(".hm-wrap{background:#0d1117!important;border-radius:8px;padding:12px!important}")
    lines.append(".hm-title{color:#c9d1d9!important}")
    lines.append(".hm-lbl{color:#8b949e!important}")
    for i in range(10):
        lines.append(f".hmc{i}{{background:{_hm_color_dark(i/9)}!important}}")
    lines.append("}")
    return "".join(lines)


def _build_heatmap_html(rows: list[dict], metric: str, title: str) -> str:
    if not rows:
        return f"<h3>{title}</h3><p><em>No snapshot data for this period.</em></p>"

    grid = {(r["dow"], r["hour"]): r for r in rows}

    if metric == "pods":
        max_val = max(r["avg_util"] or 0 for r in rows) or 1
        def util_frac(d, h):
            r = grid.get((d, h))
            return (r["avg_util"] or 0) / max_val if r else 0
    else:
        def util_frac(d, h):
            r = grid.get((d, h))
            return r["avg_util"] or 0 if r else 0

    peak_row = max(rows, key=lambda r: r["peak_util"] or 0)
    dow_name = _HM_DOW_NAMES[peak_row["dow"]]
    capacity = peak_row.get("capacity")
    if metric == "pods":
        peak_note = f"Peak: {int(peak_row['peak_util'])} pods — {dow_name} {peak_row['hour']:02d}:00 {HEATMAP_TIMEZONE_NAME}"
    else:
        peak_pct = (peak_row["peak_util"] or 0) * 100
        if capacity:
            peak_abs = (peak_row["peak_util"] or 0) * capacity
            units = {"gpu": "GPUs", "cpu": "cores", "ram": "GB"}[metric]
            peak_note = f"Peak: {peak_pct:.0f}% ({peak_abs:.0f}/{capacity:.0f} {units}) — {dow_name} {peak_row['hour']:02d}:00 {HEATMAP_TIMEZONE_NAME}"
        else:
            peak_note = f"Peak: {peak_pct:.0f}% — {dow_name} {peak_row['hour']:02d}:00 {HEATMAP_TIMEZONE_NAME}"

    cell_style = "width:16px;height:16px;border-radius:2px"
    lbl_style  = "font-size:10px;color:#666;padding-right:6px;text-align:right;white-space:nowrap;vertical-align:middle"

    header = '<td style="width:38px"></td>' + "".join(
        f'<td class="hm-lbl" style="font-size:10px;color:#666;text-align:center;padding-bottom:3px">{_HM_DOW_NAMES[d]}</td>'
        for d in _HM_DOW_ORDER
    )
    data_rows = ""
    for hour in range(24):
        cells = f'<td class="hm-lbl" style="{lbl_style}">{hour:02d}:00</td>'
        for d in _HM_DOW_ORDER:
            u = util_frac(d, hour)
            level = round(u * 9)
            cells += f'<td class="hmc{level}" style="{cell_style};background:{_hm_color(u)}"></td>'
        data_rows += f"<tr>{cells}</tr>"

    legend = "".join(
        f'<span class="hmc{i}" style="display:inline-block;width:12px;height:12px;background:{_hm_color(i/9)};border-radius:2px;margin:0 1px;vertical-align:middle"></span>'
        for i in range(10)
    )

    return (
        f'<div class="hm-wrap" style="display:inline-block;margin-bottom:4px">'
        f'<h3 class="hm-title" style="margin-bottom:6px">{title}</h3>'
        f'<table style="border-collapse:separate;border-spacing:2px;font-family:monospace">'
        f"<tr>{header}</tr>{data_rows}"
        f"</table>"
        f'<p class="hm-lbl" style="font-size:11px;color:#555;margin-top:4px">'
        f"Less {legend} More &nbsp;|&nbsp; {peak_note}"
        f"</p>"
        f"</div>"
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
    heatmap_sections: Optional[list[tuple[str, dict]]] = None,
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

    heatmap_section = ""
    if heatmap_sections:
        section_parts = []
        for section_label, hm in heatmap_sections:
            gpu_hm  = _build_heatmap_html(hm.get("gpu",  []), "gpu",  "GPU Utilization")
            pods_hm = _build_heatmap_html(hm.get("pods", []), "pods", "Active Pods")
            cpu_hm  = _build_heatmap_html(hm.get("cpu",  []), "cpu",  "CPU Utilization")
            ram_hm  = _build_heatmap_html(hm.get("ram",  []), "ram",  "RAM Utilization")
            section_parts.append(
                f'<h3 style="margin-top:16px">{section_label}</h3>'
                f'<table style="width:100%;border-collapse:collapse">'
                f'<tr>'
                f'<td style="width:50%;vertical-align:top;padding-right:24px">{gpu_hm}</td>'
                f'<td style="width:50%;vertical-align:top">{pods_hm}</td>'
                f'</tr><tr>'
                f'<td style="width:50%;vertical-align:top;padding-right:24px">{cpu_hm}</td>'
                f'<td style="width:50%;vertical-align:top">{ram_hm}</td>'
                f'</tr></table>'
            )
        heatmap_section = (
            f'<hr>'
            f'<h2>Utilization Patterns</h2>'
            f'<p style="color:#555;font-size:12px">Average utilization by hour of day and day of week. All times {HEATMAP_TIMEZONE_NAME}.</p>'
            + "".join(section_parts)
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Lobot Metrics: {month_label}</title><style>{_heatmap_css()}</style></head>
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
  {heatmap_section}
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
    lab: Optional[str] = None,
    group: Optional[str] = None,
    all_heatmap: bool = False,
    db_path: Path = DB_PATH,
    billing_path: Path = BILLING_CONFIG_PATH,
) -> None:
    """Build and send the monthly digest email (synchronous; call from CLI or cron).

    lab/group: filter heatmaps to a specific lab or billing group.
    all_heatmap: when combined with lab/group, also include the all-labs aggregate section.
    Default (no filter): shows the all-labs aggregate.
    """
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
        from .reporter import _month_bounds
        start, end = _month_bounds(year, month)
        tz_offset = heatmap_tz_offset_minutes(year, month)

        def _query_metrics(labs_filter):
            return {
                m: query_heatmap(conn, start, end, metric=m,
                                 labs=labs_filter, tz_offset_minutes=tz_offset)
                for m in ("gpu", "cpu", "ram", "pods")
            }

        heatmap_sections: list[tuple[str, dict]] = []

        if lab:
            heatmap_sections.append((f"Lab: {lab}", _query_metrics([lab])))
        elif group:
            gobj = billing.groups.get(group)
            if gobj and gobj.labs:
                section_label = gobj.display_name
                heatmap_sections.append((section_label, _query_metrics(gobj.labs)))
            else:
                label = gobj.display_name if gobj else group
                logger.warning("Group %r has no labs — falling back to all-labs heatmap", group)
                heatmap_sections.append((f"{label} (all labs)", _query_metrics(None)))

        if not (lab or group) or all_heatmap:
            heatmap_sections.append(("All Labs", _query_metrics(None)))

    finally:
        conn.close()

    html = build_monthly_html(
        year, month,
        rows_lab, rows_group, rows_user,
        rows_storage_group, rows_storage_user,
        month_label,
        heatmap_sections=heatmap_sections,
    )
    subject = f"Monthly Usage Report — {month_label}"
    _smtp_send(subject, html, to=to)


async def send_monthly_digest_async(
    year: int,
    month: int,
    to: Optional[str] = None,
    lab: Optional[str] = None,
    group: Optional[str] = None,
    all_heatmap: bool = False,
    db_path: Path = DB_PATH,
    billing_path: Path = BILLING_CONFIG_PATH,
) -> None:
    import functools
    loop = asyncio.get_event_loop()
    fn = functools.partial(
        send_monthly_digest,
        year, month,
        to=to, lab=lab, group=group, all_heatmap=all_heatmap,
        db_path=db_path, billing_path=billing_path,
    )
    await loop.run_in_executor(_executor, fn)

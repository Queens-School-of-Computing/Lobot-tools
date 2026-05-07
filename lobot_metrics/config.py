"""Configuration for lobot-metrics service."""

import os
import socket
from pathlib import Path

try:
    from zoneinfo import ZoneInfo as _ZI
    _tz_name = os.environ.get("LOBOT_TIMEZONE", "America/Toronto")
    HEATMAP_TIMEZONE = _ZI(_tz_name)
    HEATMAP_TIMEZONE_NAME = _tz_name
except Exception:
    from datetime import timezone as _fallback
    HEATMAP_TIMEZONE = _fallback.utc  # type: ignore[assignment]
    HEATMAP_TIMEZONE_NAME = "UTC"


def heatmap_tz_offset_minutes(year: int, month: int) -> int:
    """UTC offset in minutes for the heatmap timezone, representative for the given month."""
    from datetime import datetime, timezone as _tz
    mid = datetime(year, month, 15, tzinfo=_tz.utc)
    return int(mid.astimezone(HEATMAP_TIMEZONE).utcoffset().total_seconds() // 60)

# ── Collector endpoints ────────────────────────────────────────────────────────
COLLECTOR_SSE_URL = "http://127.0.0.1:9095/api/events"
COLLECTOR_STATE_URL = "http://127.0.0.1:9095/api/state"

# ── Data directory ─────────────────────────────────────────────────────────────
_CLUSTER_DIR = Path(os.environ.get("LOBOT_CLUSTER_DIR", "/opt/Lobot"))
METRICS_DATA_DIR = _CLUSTER_DIR / "metrics_data"
DB_PATH = METRICS_DATA_DIR / "lobot_metrics.db"
BILLING_CONFIG_PATH = METRICS_DATA_DIR / "billing_config.yaml"

# ── Intervals (seconds) ────────────────────────────────────────────────────────
SNAPSHOT_INTERVAL = 900  # 15 minutes

# ── SSE reconnect backoff ──────────────────────────────────────────────────────
SSE_RECONNECT_DELAY_INITIAL = 5.0
SSE_RECONNECT_DELAY_MAX = 120.0

# ── Email (mirrors lobot_collector/config.py) ──────────────────────────────────
EMAIL_ENABLED = True
SMTP_SERVER = "innovate.cs.queensu.ca"
SMTP_PORT = 25
SMTP_USE_TLS = False
SMTP_USERNAME = None
SMTP_PASSWORD = None
FROM_EMAIL = f"{socket.getfqdn().split('.')[0]}@cs.queensu.ca"
TO_EMAIL = "aaron.visser+lobot@queensu.ca"

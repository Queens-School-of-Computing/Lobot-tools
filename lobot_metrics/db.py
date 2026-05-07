"""SQLite schema creation and all query functions for lobot-metrics."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT    NOT NULL UNIQUE,
    username         TEXT    NOT NULL,
    lab              TEXT    NOT NULL DEFAULT '',
    node             TEXT    NOT NULL DEFAULT '',
    start_time       TEXT    NOT NULL,
    end_time         TEXT,
    duration_seconds REAL,
    cpu_requested    REAL    NOT NULL DEFAULT 0,
    ram_requested_gb REAL    NOT NULL DEFAULT 0,
    gpu_requested    INTEGER NOT NULL DEFAULT 0,
    end_reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_username
    ON sessions (username);
CREATE INDEX IF NOT EXISTS idx_sessions_lab
    ON sessions (lab);
CREATE INDEX IF NOT EXISTS idx_sessions_start_time
    ON sessions (start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_open
    ON sessions (end_time) WHERE end_time IS NULL;

CREATE TABLE IF NOT EXISTS resource_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    lab              TEXT NOT NULL,
    cpu_total        INTEGER NOT NULL,
    cpu_requested    INTEGER NOT NULL,
    ram_total_gb     REAL NOT NULL,
    ram_requested_gb REAL NOT NULL,
    gpu_total        INTEGER NOT NULL,
    gpu_requested    INTEGER NOT NULL,
    pod_count        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_resource_snapshots_ts
    ON resource_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_resource_snapshots_lab
    ON resource_snapshots (lab);

CREATE TABLE IF NOT EXISTS node_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          TEXT NOT NULL,
    node_name          TEXT NOT NULL,
    lab                TEXT NOT NULL DEFAULT '',
    cpu_allocatable    INTEGER NOT NULL,
    cpu_requested      INTEGER NOT NULL,
    ram_allocatable_gb REAL NOT NULL,
    ram_requested_gb   REAL NOT NULL,
    gpu_allocatable    INTEGER NOT NULL,
    gpu_requested      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_node_snapshots_ts
    ON node_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_node_snapshots_node
    ON node_snapshots (node_name);

CREATE TABLE IF NOT EXISTS storage_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    node_name    TEXT NOT NULL,
    disk_name    TEXT NOT NULL,
    total_gb     REAL NOT NULL,
    available_gb REAL NOT NULL,
    scheduled_gb REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_storage_snapshots_ts
    ON storage_snapshots (timestamp);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    conn.executescript(_SCHEMA)
    conn.close()


# ── Session writes ─────────────────────────────────────────────────────────────

def upsert_session(conn: sqlite3.Connection, session_id: str, pod) -> None:
    """INSERT OR IGNORE a session row. Safe to call repeatedly (idempotent)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO sessions
            (session_id, username, lab, node, start_time,
             cpu_requested, ram_requested_gb, gpu_requested)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            pod.username,
            pod.resource,  # k8s node label lab=<value>
            pod.node or "",
            pod.start_time or _now_iso(),
            pod.cpu_requested,
            pod.ram_requested_gb,
            pod.gpu_requested,
        ),
    )


def close_session(
    conn: sqlite3.Connection,
    session_id: str,
    end_time: str,
    duration_seconds: Optional[float],
    end_reason: str,
) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET end_time = ?, duration_seconds = ?, end_reason = ?
        WHERE session_id = ? AND end_time IS NULL
        """,
        (end_time, duration_seconds, end_reason, session_id),
    )


def get_open_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sessions WHERE end_time IS NULL").fetchall()
    return [dict(row) for row in rows]


# ── Snapshot writes ────────────────────────────────────────────────────────────

def insert_resource_snapshot(
    conn: sqlite3.Connection,
    timestamp: str,
    lab: str,
    cpu_total: int,
    cpu_requested: int,
    ram_total_gb: float,
    ram_requested_gb: float,
    gpu_total: int,
    gpu_requested: int,
    pod_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO resource_snapshots
            (timestamp, lab, cpu_total, cpu_requested,
             ram_total_gb, ram_requested_gb,
             gpu_total, gpu_requested, pod_count)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (timestamp, lab, cpu_total, cpu_requested,
         ram_total_gb, ram_requested_gb,
         gpu_total, gpu_requested, pod_count),
    )


def insert_node_snapshot(
    conn: sqlite3.Connection,
    timestamp: str,
    node_name: str,
    lab: str,
    cpu_allocatable: int,
    cpu_requested: int,
    ram_allocatable_gb: float,
    ram_requested_gb: float,
    gpu_allocatable: int,
    gpu_requested: int,
) -> None:
    conn.execute(
        """
        INSERT INTO node_snapshots
            (timestamp, node_name, lab,
             cpu_allocatable, cpu_requested,
             ram_allocatable_gb, ram_requested_gb,
             gpu_allocatable, gpu_requested)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (timestamp, node_name, lab,
         cpu_allocatable, cpu_requested,
         ram_allocatable_gb, ram_requested_gb,
         gpu_allocatable, gpu_requested),
    )


def insert_storage_snapshot(
    conn: sqlite3.Connection,
    timestamp: str,
    node_name: str,
    disk_name: str,
    total_gb: float,
    available_gb: float,
    scheduled_gb: float,
) -> None:
    conn.execute(
        """
        INSERT INTO storage_snapshots
            (timestamp, node_name, disk_name,
             total_gb, available_gb, scheduled_gb)
        VALUES (?,?,?,?,?,?)
        """,
        (timestamp, node_name, disk_name,
         total_gb, available_gb, scheduled_gb),
    )


# ── Queries ────────────────────────────────────────────────────────────────────

def query_sessions_for_period(
    conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
    username: Optional[str] = None,
    lab: Optional[str] = None,
    include_open: bool = False,
) -> list[dict]:
    """Sessions that started within [start_iso, end_iso)."""
    filters = ["start_time >= ?", "start_time < ?"]
    params: list = [start_iso, end_iso]
    if not include_open:
        filters.append("end_time IS NOT NULL")
    if username:
        filters.append("username = ?")
        params.append(username)
    if lab:
        filters.append("lab = ?")
        params.append(lab)
    where = " AND ".join(filters)
    rows = conn.execute(
        f"SELECT * FROM sessions WHERE {where} ORDER BY start_time",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def query_active_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE end_time IS NULL ORDER BY start_time"
    ).fetchall()
    return [dict(row) for row in rows]


def query_usage_by_user(
    conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
    username: Optional[str] = None,
    lab: Optional[str] = None,
) -> list[dict]:
    filters = ["start_time >= ?", "start_time < ?", "end_time IS NOT NULL"]
    params: list = [start_iso, end_iso]
    if username:
        filters.append("username = ?")
        params.append(username)
    if lab:
        filters.append("lab = ?")
        params.append(lab)
    where = " AND ".join(filters)
    rows = conn.execute(
        f"""
        SELECT
            username,
            lab,
            COUNT(*) AS session_count,
            ROUND(SUM(duration_seconds) / 3600.0, 2) AS total_hours,
            ROUND(SUM(duration_seconds * gpu_requested) / 3600.0, 2) AS gpu_hours,
            ROUND(SUM(duration_seconds * cpu_requested) / 3600.0, 2) AS cpu_core_hours,
            ROUND(SUM(duration_seconds * ram_requested_gb) / 3600.0, 2) AS ram_gb_hours,
            ROUND(AVG(cpu_requested), 1) AS avg_cpu,
            ROUND(AVG(ram_requested_gb), 1) AS avg_ram_gb,
            ROUND(AVG(gpu_requested), 2) AS avg_gpu
        FROM sessions
        WHERE {where}
        GROUP BY username, lab
        ORDER BY total_hours DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def query_usage_by_lab(
    conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            lab,
            COUNT(DISTINCT username) AS user_count,
            COUNT(*) AS session_count,
            ROUND(SUM(duration_seconds) / 3600.0, 2) AS total_hours,
            ROUND(SUM(duration_seconds * gpu_requested) / 3600.0, 2) AS gpu_hours,
            ROUND(SUM(duration_seconds * cpu_requested) / 3600.0, 2) AS cpu_core_hours,
            ROUND(SUM(duration_seconds * ram_requested_gb) / 3600.0, 2) AS ram_gb_hours
        FROM sessions
        WHERE start_time >= ? AND start_time < ? AND end_time IS NOT NULL
        GROUP BY lab
        ORDER BY total_hours DESC
        """,
        (start_iso, end_iso),
    ).fetchall()
    return [dict(row) for row in rows]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

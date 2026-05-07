"""SSE subscriber: tracks pod lifecycle events as sessions in SQLite."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lobot_tui.data.models import ClusterState, PodInfo
from lobot_tui.data.parsers import _run_kubectl

from .config import (
    COLLECTOR_SSE_URL,
    COLLECTOR_STATE_URL,
    SSE_RECONNECT_DELAY_INITIAL,
    SSE_RECONNECT_DELAY_MAX,
)
from .db import close_session, get_open_sessions, open_db, upsert_session

logger = logging.getLogger(__name__)

_JUPYTER_PREFIX = "jupyter-"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(ts: str) -> datetime:
    """Parse ISO 8601 timestamp (with or without trailing Z) to UTC-aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _session_id(pod: PodInfo) -> str:
    return f"{pod.name}::{pod.start_time}"


def _is_jupyter(pod: PodInfo) -> bool:
    return pod.name.startswith(_JUPYTER_PREFIX) and bool(pod.start_time)


class MetricsRecorder:
    """
    Subscribes to the lobot-collector SSE stream and writes session rows
    to SQLite whenever a jupyter-* pod appears or disappears.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._live_pods: dict[str, PodInfo] = {}  # pod.name → PodInfo
        self._current_state: Optional[ClusterState] = None
        self._lock = asyncio.Lock()

    def current_state(self) -> Optional[ClusterState]:
        return self._current_state

    # ── Startup reconcile ──────────────────────────────────────────────────────

    async def reconcile_on_startup(self) -> None:
        """
        Fetch /api/state once, then:
        - Close open DB sessions whose pod no longer exists.
        - Insert sessions for live pods with no DB row.
        - Populate _live_pods so the SSE diff starts correctly.
        """
        import aiohttp

        state: Optional[ClusterState] = None
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(COLLECTOR_STATE_URL) as resp:
                    data = await resp.json(content_type=None)
                    state = ClusterState.from_dict(data)
            logger.info("reconcile: fetched state (%d pods)", len(state.pods))
        except Exception as exc:
            logger.warning("reconcile: could not fetch collector state: %s — skipping", exc)
            return

        live_jupyter = {pod.name: pod for pod in state.pods if _is_jupyter(pod)}
        live_sids = {_session_id(pod) for pod in live_jupyter.values()}

        loop = asyncio.get_event_loop()

        def _reconcile_db() -> tuple[int, int]:
            conn = open_db(self._db_path)
            try:
                open_rows = get_open_sessions(conn)
                closed = 0
                open_sids = set()
                for row in open_rows:
                    sid = row["session_id"]
                    open_sids.add(sid)
                    pod_name = sid.split("::")[0]
                    if pod_name not in live_jupyter:
                        # Pod stopped while recorder was down; estimate duration
                        start_ts = row["start_time"]
                        try:
                            duration = (_parse_ts(_now_iso()) - _parse_ts(start_ts)).total_seconds()
                        except Exception:
                            duration = None
                        close_session(conn, sid, _now_iso(), duration, "reconciled_missing")
                        closed += 1

                opened = 0
                for pod in live_jupyter.values():
                    sid = _session_id(pod)
                    if sid not in open_sids:
                        upsert_session(conn, sid, pod)
                        opened += 1

                conn.commit()
                return closed, opened
            finally:
                conn.close()

        closed, opened = await loop.run_in_executor(None, _reconcile_db)
        logger.info(
            "reconcile_on_startup: closed %d orphaned session(s), opened %d new session(s)",
            closed,
            opened,
        )

        async with self._lock:
            self._current_state = state
            self._live_pods = dict(live_jupyter)

    # ── SSE loop ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the SSE subscription loop as a background task."""
        asyncio.ensure_future(self._sse_loop())

    async def _sse_loop(self) -> None:
        import aiohttp

        delay = SSE_RECONNECT_DELAY_INITIAL
        while True:
            try:
                logger.info("SSE connecting to %s", COLLECTOR_SSE_URL)
                timeout = aiohttp.ClientTimeout(total=None, connect=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(COLLECTOR_SSE_URL) as response:
                        logger.info("SSE connected")
                        delay = SSE_RECONNECT_DELAY_INITIAL  # reset on success
                        while True:
                            raw = await response.content.readline()
                            if not raw:
                                logger.info("SSE connection closed by server")
                                break
                            line = raw.decode("utf-8").rstrip("\r\n")
                            if line.startswith("data:"):
                                payload = line[5:].strip()
                                if payload:
                                    try:
                                        state = ClusterState.from_dict(json.loads(payload))
                                        await self._handle_state_update(state)
                                    except Exception as exc:
                                        logger.warning("SSE parse error: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "SSE error (%s), reconnecting in %.0fs", exc, delay
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, SSE_RECONNECT_DELAY_MAX)

    # ── State diffing ──────────────────────────────────────────────────────────

    async def _handle_state_update(self, state: ClusterState) -> None:
        async with self._lock:
            self._current_state = state
            new_pods = {pod.name: pod for pod in state.pods if _is_jupyter(pod)}

            appeared = set(new_pods) - set(self._live_pods)
            disappeared = set(self._live_pods) - set(new_pods)

            for name in appeared:
                await self._on_pod_appeared(new_pods[name])
            for name in disappeared:
                await self._on_pod_disappeared(name, self._live_pods[name])

            self._live_pods = new_pods

    async def _on_pod_appeared(self, pod: PodInfo) -> None:
        sid = _session_id(pod)
        pvc_gb = await self._fetch_pvc_size(pod)
        logger.info("pod appeared: %s (PVC %.0f GB)", pod.name, pvc_gb or 0)

        loop = asyncio.get_event_loop()

        def _insert():
            conn = open_db(self._db_path)
            try:
                upsert_session(conn, sid, pod, pvc_capacity_gb=pvc_gb)
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _insert)

    async def _fetch_pvc_size(self, pod: PodInfo) -> Optional[float]:
        """Look up the user's home PVC size from Kubernetes."""
        raw_name = pod.name.removeprefix("jupyter-")
        pvc_name = f"claim-{raw_name}"
        stdout, _stderr, rc = await _run_kubectl(
            "get", "pvc", pvc_name, "-n", "jhub", "-o", "json"
        )
        if rc != 0:
            return None
        try:
            data = json.loads(stdout)
            storage_str = data["spec"]["resources"]["requests"]["storage"]
            return _parse_storage(storage_str)
        except (json.JSONDecodeError, KeyError):
            return None


    async def _on_pod_disappeared(self, pod_name: str, pod: PodInfo) -> None:
        sid = _session_id(pod)
        end_iso = _now_iso()
        try:
            duration = (_parse_ts(end_iso) - _parse_ts(pod.start_time)).total_seconds()
        except Exception:
            duration = None

        logger.info("pod disappeared: %s (duration %.0fs)", pod_name, duration or 0)

        loop = asyncio.get_event_loop()

        def _close():
            conn = open_db(self._db_path)
            try:
                close_session(conn, sid, end_iso, duration, "normal")
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _close)


def _parse_storage(value: str) -> float:
    """Convert a Kubernetes storage string to GB. e.g. '50Gi' → 50.0"""
    value = value.strip()
    units = {"Ti": 1024.0, "Gi": 1.0, "Mi": 1.0 / 1024, "Ki": 1.0 / (1024 ** 2),
             "T": 1000.0, "G": 1.0, "M": 1.0 / 1000, "K": 1.0 / (1000 ** 2)}
    for suffix, factor in units.items():
        if value.endswith(suffix):
            try:
                return float(value[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(value) / (1024 ** 3)
    except ValueError:
        return 0.0

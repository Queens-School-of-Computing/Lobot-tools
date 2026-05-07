"""Periodic cluster-state snapshots written to SQLite every SNAPSHOT_INTERVAL seconds."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lobot_tui.data.parsers import _run_kubectl

from .config import SNAPSHOT_INTERVAL
from .db import (
    insert_node_snapshot,
    insert_pvc_snapshot,
    insert_resource_snapshot,
    insert_storage_snapshot,
    open_db,
)
from .recorder import MetricsRecorder

_JUPYTERHUB_NAMESPACE = "jhub"

logger = logging.getLogger(__name__)


class PeriodicSnapshotter:
    """
    Every SNAPSHOT_INTERVAL seconds, reads the current ClusterState from the
    recorder and writes one row per lab, node, and disk to the database.
    """

    def __init__(self, recorder: MetricsRecorder, db_path: Path) -> None:
        self._recorder = recorder
        self._db_path = db_path

    async def start(self) -> None:
        asyncio.ensure_future(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            state = self._recorder.current_state()
            if state is None:
                logger.debug("snapshotter: no state yet, skipping")
                continue
            await self._take_snapshot(state)
            await self._snapshot_pvcs()

    async def _take_snapshot(self, state) -> None:
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        def _write():
            conn = open_db(self._db_path)
            try:
                with conn:
                    for lab, rs in state.resources.items():
                        insert_resource_snapshot(
                            conn,
                            timestamp,
                            lab,
                            rs.cpu_total,
                            rs.cpu_total - rs.cpu_free,  # cpu_requested
                            rs.ram_total_gb,
                            rs.ram_total_gb - rs.ram_free_gb,  # ram_requested_gb
                            rs.gpu_total,
                            rs.gpu_total - rs.gpu_free,  # gpu_requested
                            rs.pod_count,
                        )

                    for node in state.nodes:
                        if node.is_control_plane:
                            continue
                        insert_node_snapshot(
                            conn,
                            timestamp,
                            node.name,
                            node.resource,
                            node.cpu_allocatable,
                            node.cpu_requested,
                            node.ram_allocatable_gb,
                            node.ram_requested_gb,
                            node.gpu_allocatable,
                            node.gpu_requested,
                        )

                    for node_name, disks in state.longhorn_disks.items():
                        for disk in disks:
                            insert_storage_snapshot(
                                conn,
                                timestamp,
                                node_name,
                                disk.name,
                                disk.total_gb,
                                disk.available_gb,
                                disk.scheduled_gb,
                            )
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)
        logger.info(
            "snapshot at %s: %d labs, %d nodes",
            timestamp,
            len(state.resources),
            len([n for n in state.nodes if not n.is_control_plane]),
        )

    async def _snapshot_pvcs(self) -> None:
        """Query kubectl for user PVCs in the jhub namespace and record them."""
        stdout, stderr, rc = await _run_kubectl(
            "get", "pvc", "-n", _JUPYTERHUB_NAMESPACE, "-o", "json"
        )
        if rc != 0:
            logger.warning("kubectl get pvc failed: %s", stderr.strip())
            return

        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        pvcs = _parse_user_pvcs(stdout)

        def _write():
            conn = open_db(self._db_path)
            try:
                with conn:
                    for pvc in pvcs:
                        insert_pvc_snapshot(
                            conn,
                            timestamp,
                            pvc["username"],
                            pvc["pvc_name"],
                            _JUPYTERHUB_NAMESPACE,
                            pvc["capacity_gb"],
                            pvc.get("storage_class"),
                        )
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)
        logger.info("pvc snapshot at %s: %d user PVCs", timestamp, len(pvcs))


def _parse_user_pvcs(stdout: str) -> list[dict]:
    """Parse kubectl PVC JSON into a list of user PVC records."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    results = []
    for item in data.get("items", []):
        name: str = item.get("metadata", {}).get("name", "")
        annotations: dict = item.get("metadata", {}).get("annotations") or {}
        labels: dict = item.get("metadata", {}).get("labels") or {}

        # Only track user PVCs (claim-* prefix)
        if not name.startswith("claim-"):
            continue

        # Prefer the JupyterHub annotation for the canonical username
        username: Optional[str] = (
            annotations.get("hub.jupyter.org/username")
            or labels.get("hub.jupyter.org/username")
        )
        if not username:
            # Fall back to stripping claim- prefix
            username = name.removeprefix("claim-")

        # Parse storage capacity: "50Gi" → 50.0
        requests: dict = item.get("spec", {}).get("resources", {}).get("requests", {})
        capacity_gb = _parse_storage(requests.get("storage", "0"))
        if capacity_gb == 0:
            continue

        storage_class: Optional[str] = item.get("spec", {}).get("storageClassName")

        results.append({
            "username": username,
            "pvc_name": name,
            "capacity_gb": capacity_gb,
            "storage_class": storage_class,
        })

    return results


def _parse_storage(value: str) -> float:
    """Convert Kubernetes storage string to GB. e.g. '50Gi' → 50.0, '512Mi' → 0.5."""
    value = value.strip()
    if not value or value == "0":
        return 0.0
    units = {
        "Ti": 1024.0,
        "Gi": 1.0,
        "Mi": 1.0 / 1024,
        "Ki": 1.0 / (1024 * 1024),
        "T": 1000.0,
        "G": 1.0,
        "M": 1.0 / 1000,
        "K": 1.0 / (1000 * 1000),
    }
    for suffix, factor in units.items():
        if value.endswith(suffix):
            try:
                return float(value[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(value) / (1024 ** 3)  # bare bytes → GB
    except ValueError:
        return 0.0

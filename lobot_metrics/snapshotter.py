"""Periodic cluster-state snapshots written to SQLite every SNAPSHOT_INTERVAL seconds."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import SNAPSHOT_INTERVAL
from .db import (
    insert_node_snapshot,
    insert_resource_snapshot,
    insert_storage_snapshot,
    open_db,
)
from .recorder import MetricsRecorder

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

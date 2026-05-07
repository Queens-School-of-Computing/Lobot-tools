"""Entry point: python3 -m lobot_metrics  →  starts the recorder daemon."""

import asyncio
import logging
import os
import signal
import sys

from .config import DB_PATH
from .db import init_db
from .recorder import MetricsRecorder
from .snapshotter import PeriodicSnapshotter


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


async def main() -> None:
    logger = logging.getLogger("lobot_metrics")

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


if __name__ == "__main__":
    _setup_logging()
    asyncio.run(main())

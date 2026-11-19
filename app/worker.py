"""Background worker: drains the outbox and runs CSV imports. Run: python -m app.worker

Scale by starting more processes; claims use FOR UPDATE SKIP LOCKED so workers never collide.
"""

import asyncio
import logging
import signal

from app import outbox
from app.db import SessionLocal
from app.imports import processor
from app.models import Outbox  # noqa: F401  (ensures mappers are loaded)

log = logging.getLogger("worker")
BATCH = 500
THROTTLE = 0.05  # pause between full batches so a big backlog can't starve the API's DB connections


async def run(stop: asyncio.Event) -> None:
    while not stop.is_set():
        async with SessionLocal() as session:
            handled = await outbox.process_batch(session, BATCH)
        imported = await processor.run_once(SessionLocal)
        if handled == BATCH:
            await asyncio.sleep(THROTTLE)
        elif not handled and not imported:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)  # finish the current batch, then exit (safe for rolling deploys)
    await run(stop)


if __name__ == "__main__":
    asyncio.run(main())

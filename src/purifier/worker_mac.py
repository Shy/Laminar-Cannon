"""MacBook worker: runs both workflows and pose detection.

Both the parent loop and the per-frame child are hosted here rather than on
the Pi: this machine has the headroom, and the control law lives in the
workflow. The Pi serves only camera and servo activities.

Weights are loaded before the worker starts polling so the first frame does
not pay the one-to-two-second model load.
"""

import asyncio
import contextlib
import logging

from temporalio.worker import Worker

from purifier.activities_mac import MODEL_NAME, detect_person, load_model
from purifier.client import connect, load_config
from purifier.shared import MAC_TASK_QUEUE
from purifier.workflows import TrackFrame, TrackPerson

#: Detection is CPU/GPU-bound and there is one model instance, so serialise.
MAX_CONCURRENT_ACTIVITIES = 1


async def main() -> None:
    """Start the MacBook worker and poll until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    logger = logging.getLogger("purifier.worker_mac")

    config = load_config()
    logger.info("connecting to %s", config.describe())
    client = await connect(config)

    logger.info("loading %s (downloads weights on first run)", MODEL_NAME)
    load_model()
    logger.info("model ready")

    worker = Worker(
        client,
        task_queue=MAC_TASK_QUEUE,
        workflows=[TrackPerson, TrackFrame],
        activities=[detect_person],
        max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
    )
    logger.info("polling task queue %s", MAC_TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())

"""Pi worker: camera and servo activities only.

No workflows are registered here. The Pi has one camera and one I2C bus, so
activities are strictly serialised: two concurrent captures would fight over
the sensor, and two concurrent moves would fight over the servos.
"""

import asyncio
import contextlib
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from purifier.activities_pi import capture_frame, move_servos, read_servo_angles
from purifier.camera import close_camera, get_camera
from purifier.client import connect, load_config
from purifier.shared import PI_TASK_QUEUE

#: One camera, one I2C bus. Never raise this above 1: two concurrent captures
#: would fight over the sensor and two concurrent moves over the servos.
MAX_CONCURRENT_ACTIVITIES = 1


async def main() -> None:
    """Start the Pi worker and poll until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    logger = logging.getLogger("purifier.worker_pi")

    config = load_config()
    logger.info("connecting to %s", config.describe())
    client = await connect(config)

    # Open the camera before polling starts. The sensor takes about four
    # seconds to come up, and paying that here means the first tracking frame
    # is as quick as every other one.
    logger.info("opening camera")
    get_camera()

    # The activities are synchronous and block on subprocess and sleep, so
    # they need a thread executor. Running them in the event loop instead
    # stalled the SDK's task handling and cost about a second per frame.
    # One thread, matching the one-camera, one-bus constraint.
    with ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_ACTIVITIES, thread_name_prefix="pi-activity"
    ) as executor:
        worker = Worker(
            client,
            task_queue=PI_TASK_QUEUE,
            activities=[capture_frame, move_servos, read_servo_angles],
            activity_executor=executor,
            max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
        )
        logger.info("polling task queue %s", PI_TASK_QUEUE)
        try:
            await worker.run()
        finally:
            close_camera()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())

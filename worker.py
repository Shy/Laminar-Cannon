import asyncio
import logging
import sys
from temporalio.client import Client
from temporalio.worker import Worker
from workflows import personDetection
from activities import detect_person_in_image
from model_manager import yolo_model_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    """
    Main function to start the Temporal worker.
    """
    logger.info("Starting worker initialization...")

    # === Model Initialization Phase ===
    # Load YOLO model before accepting any work to ensure fast responses
    try:
        await yolo_model_manager.initialize_model()
        logger.info("YOLO model initialized successfully")
    except Exception as e:
        # Exit if model can't be loaded - worker would be useless without it
        logger.error(f"Failed to initialize YOLO model: {e}")
        sys.exit(1)

    # === Temporal Connection Phase ===
    # Connect to Temporal server
    client = await Client.connect("localhost:7233")

    # === Worker Configuration Phase ===
    # Create a worker that can handle our person detection workflows and activities
    worker = Worker(
        client,
        task_queue="person-detection-task-queue",
        workflows=[personDetection],
        activities=[detect_person_in_image],
    )

    # === Execution Phase ===
    # Start the worker - this blocks until manually stopped (Ctrl+C)
    # The worker will continuously poll for tasks and execute them
    logger.info("Worker started and polling for tasks...")
    logger.info("Task queue: person-detection-task-queue")

    # Start the worker (this will run indefinitely)
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested by user")
    except Exception as e:
        logger.error(f"Worker failed with error: {e}")
        sys.exit(1)

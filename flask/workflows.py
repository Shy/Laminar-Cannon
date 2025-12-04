from temporalio import workflow
from temporalio.common import RetryPolicy
from datetime import timedelta
import asyncio

# Import activities using Temporal's safety mechanism for ML libraries
with workflow.unsafe.imports_passed_through():
    from activities import detect_person_in_image, save_debug_image


@workflow.defn
class personDetection:
    """
    Temporal workflow for person detection in images.
    """

    @workflow.run
    async def run(self, image_b64: str) -> dict:
        """
        Main workflow execution method.

        Args:
            image_b64: Base64 encoded image data

        Returns:
            dict: Detection result with person info and servo control data, or None if no person
        """

        # Step 1: Execute person detection activity with retry policy
        detection_result = await workflow.execute_activity(
            detect_person_in_image,
            image_b64,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
                maximum_attempts=3,
            ),
        )

        # Step 2: Save debug image with detection results
        # This must run sequentially since it needs detection_result
        await workflow.execute_activity(
            save_debug_image,
            args=[image_b64, detection_result, 640, 480],
            start_to_close_timeout=timedelta(seconds=10),
        )

        return detection_result

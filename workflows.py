from temporalio import workflow
from temporalio.common import RetryPolicy
from datetime import timedelta

# Import activities using Temporal's safety mechanism for ML libraries
with workflow.unsafe.imports_passed_through():
    from activities import detect_person_in_image


@workflow.defn
class personDetection:
    """
    Temporal workflow for person detection in images.
    """

    @workflow.run
    async def run(self, image_b64: str) -> bool:
        """
        Main workflow execution method.

        Args:
            image_b64: Base64 encoded image data

        Returns:
            bool: True if person detected, False otherwise
        """

        # Execute person detection activity with retry policy
        return await workflow.execute_activity(
            detect_person_in_image,
            image_b64,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
                maximum_attempts=3,
            ),
        )

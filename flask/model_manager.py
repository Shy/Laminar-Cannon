"""
YOLO Model Manager for Face Detection

This module handles initialization and management of the YOLOv11n face detection model
from Hugging Face. It follows the same pattern as the Hugging Face
example with pre-loading and caching capabilities.
"""

import logging
from typing import Optional
from ultralytics import YOLO

# Create a logger specific to this module for debugging model operations
logger = logging.getLogger(__name__)


class YOLOModelManager:
    """
    Centralized manager for YOLO face detection model with pre-loading and caching capabilities.

    This class handles the initialization and management of the YOLO model:
    - Pre-loads model at startup to avoid loading delays during inference
    - Caches model in memory for fast repeated access
    - Provides proper error handling and logging
    """

    def __init__(self):
        """
        Initialize the model manager with empty cache.
        """
        # Cache for the loaded YOLO model
        self.model: Optional[YOLO] = None

    async def initialize_model(self):
        """
        Initialize and pre-load the YOLOv11n face detection model from Hugging Face.

        This is called once when the Temporal worker starts up, ensuring
        the model is ready before any workflow activities need it.
        Pre-loading prevents cold-start delays during actual inference.
        """
        logger.info("Initializing YOLOv11n face detection model...")

        try:
            # Load YOLOv11n face detection model from Hugging Face
            # Model: https://huggingface.co/AdamCodd/YOLOv11n-face-detection
            logger.info("Loading YOLOv11n-face-detection model from Hugging Face...")

            # Download the model from Hugging Face hub
            from huggingface_hub import hf_hub_download
            model_path = hf_hub_download(
                repo_id="AdamCodd/YOLOv11n-face-detection",
                filename="model.pt"
            )
            logger.info(f"Model downloaded to: {model_path}")

            self.model = YOLO(model_path)

            # Warm up the model with a dummy prediction to ensure it's fully loaded
            logger.info("Warming up model...")
            import numpy as np
            from PIL import Image

            # Create a small dummy image for warmup
            dummy_image = Image.fromarray(np.zeros((320, 320, 3), dtype=np.uint8))
            _ = self.model(dummy_image, verbose=False)

            logger.info("YOLOv11n face detection model initialization complete")

        except Exception as e:
            # Re-raise the exception after logging - this will prevent the worker
            # from starting if the model fails to load
            logger.error(f"Failed to load YOLOv11n face detection model: {e}")
            raise

    def get_model(self) -> YOLO:
        """
        Get the pre-loaded YOLO model.

        Returns:
            YOLO: The loaded YOLO model instance

        Raises:
            RuntimeError: If model hasn't been initialized yet
        """
        if self.model is None:
            raise RuntimeError(
                "YOLO model not initialized. Call initialize_model() first."
            )
        return self.model


# Global instance of the model manager
# This allows activities to access the same model instance
yolo_model_manager = YOLOModelManager()

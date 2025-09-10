from temporalio import activity
import base64
import io
import numpy as np
from PIL import Image
import logging
from typing import Dict, List, Optional, Any

# Import our model manager for accessing the pre-loaded YOLO model
from model_manager import yolo_model_manager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@activity.defn
async def detect_person_in_image(image_b64: str) -> Optional[Dict[str, Any]]:
    """
    Activity to detect if a person is present in the given image.

    Args:
        image_b64: Base64 encoded image data

    Returns:
        Optional[Dict[str, Any]]: Dictionary with person detection info if found, None otherwise.
        Contains 'location' (xywh coordinates), 'confidence', and 'box_info'.
    """
    try:
        # Decode base64 image
        image_data = base64.b64decode(image_b64)
        image = Image.open(io.BytesIO(image_data))

        logger.info(f"Processing image of size: {image.size}")

        # Get the pre-loaded YOLO model and run inference
        model = yolo_model_manager.get_model()
        results = model(image, verbose=False)

        # Check if any person (class 0 in COCO dataset) is detected
        for result in results:
            if result.boxes is not None:
                classes = result.boxes.cls.cpu().numpy()
                # Class 0 is 'person' in COCO dataset
                person_detected = 0 in classes

                if person_detected:
                    confidence_scores = result.boxes.conf.cpu().numpy()
                    location = result.boxes.xywh.cpu().numpy()
                    person_indices = np.where(classes == 0)[0]
                    max_confidence = confidence_scores[person_indices].max()
                    
                    # Get the box info for the person with highest confidence
                    best_person_idx = person_indices[np.argmax(confidence_scores[person_indices])]
                    person_location = location[best_person_idx]

                    detection_info = {
                        'location': person_location.tolist(),  # Convert to list for JSON serialization
                        'confidence': float(max_confidence),
                        'box_info': {
                            'x_center': float(person_location[0]),
                            'y_center': float(person_location[1]),
                            'width': float(person_location[2]),
                            'height': float(person_location[3])
                        }
                    }

                    logger.info(
                        f"Person detected with confidence: {max_confidence:.2f} at {person_location}"
                    )
                    return detection_info

        logger.info("No person detected in image")
        return None

    except Exception as e:
        logger.error(f"Error in person detection: {str(e)}")
        # In case of error, we return None rather than raising
        # This prevents the workflow from failing on processing errors
        return None

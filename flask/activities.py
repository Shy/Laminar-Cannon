from temporalio import activity
import base64
import io
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import logging
from typing import Dict, List, Optional, Any
import os
from datetime import datetime
import glob

# Import our model manager for accessing the pre-loaded YOLO model
from model_manager import yolo_model_manager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def calculate_servo_steps(
    person_x: float, person_y: float, image_width: int, image_height: int
) -> Dict[str, Any]:
    """
    Calculate servo steps needed to center the person in the frame.

    Args:
        person_x: X coordinate of person center in pixels
        person_y: Y coordinate of person center in pixels
        image_width: Total width of image in pixels
        image_height: Total height of image in pixels

    Returns:
        Dict with 'x_steps' and 'y_steps' needed to center the person
    """
    # Calculate center of image
    center_x = image_width / 2
    center_y = image_height / 2

    # Calculate offset from center
    offset_x = person_x - center_x
    offset_y = person_y - center_y

    # Convert pixel offset to servo steps
    # Assuming typical servo parameters (adjust these based on your setup):
    # - Servo range: 180 degrees (0.5ms to 2.5ms pulse width)
    # - Field of view: assume ~60 degrees horizontal, ~45 degrees vertical
    # - Steps per degree: assume 1 step per degree for simplicity

    horizontal_fov_degrees = 60
    vertical_fov_degrees = 45

    # Calculate degrees per pixel
    degrees_per_pixel_x = horizontal_fov_degrees / image_width
    degrees_per_pixel_y = vertical_fov_degrees / image_height

    # Convert to degrees
    offset_degrees_x = offset_x * degrees_per_pixel_x
    offset_degrees_y = offset_y * degrees_per_pixel_y

    # Convert to servo steps (1 step per degree, negative because servo movement is opposite to image coordinates)
    x_steps = -int(round(offset_degrees_x))
    y_steps = -int(round(offset_degrees_y))

    return {
        "x_steps": x_steps,
        "y_steps": y_steps,
        "offset_pixels": {"x": float(offset_x), "y": float(offset_y)},
        "offset_degrees": {"x": float(offset_degrees_x), "y": float(offset_degrees_y)},
    }


@activity.defn
async def detect_person_in_image(
    image_b64: str, image_width: int = 640, image_height: int = 480
) -> Optional[Dict[str, Any]]:
    """
    Activity to detect if a face is present in the given image and calculate servo positioning.

    Args:
        image_b64: Base64 encoded image data
        image_width: Width of the image in pixels (default: 640)
        image_height: Height of the image in pixels (default: 480)

    Returns:
        Optional[Dict[str, Any]]: Dictionary with face detection info if found, None otherwise.
        Contains 'location' (xywh coordinates), 'confidence', 'box_info', and 'servo_control'.
    """
    try:
        # Decode base64 image
        image_data = base64.b64decode(image_b64)
        image = Image.open(io.BytesIO(image_data))

        # Use actual image dimensions if not provided
        actual_width, actual_height = image.size
        if image_width == 640 and image_height == 480:
            image_width, image_height = actual_width, actual_height

        logger.info(f"Processing image of size: {image.size}")

        # Get the pre-loaded YOLOv11n face detection model and run inference
        model = yolo_model_manager.get_model()
        results = model(image, verbose=False)

        # Check if any face (class 0 in face detection model) is detected
        for result in results:
            if result.boxes is not None and len(result.boxes) > 0:
                classes = result.boxes.cls.cpu().numpy()
                confidence_scores = result.boxes.conf.cpu().numpy()
                location = result.boxes.xywh.cpu().numpy()

                # Class 0 is 'face' in the YOLOv11n-face-detection model
                face_indices = np.where(classes == 0)[0]

                if len(face_indices) > 0:
                    max_confidence = confidence_scores[face_indices].max()

                    # Only process if confidence is above threshold
                    if max_confidence < 0.8:
                        logger.info(f"Face detected but confidence too low: {max_confidence:.2f}")
                        return None

                    # Get the box info for the face with highest confidence
                    best_face_idx = face_indices[
                        np.argmax(confidence_scores[face_indices])
                    ]
                    face_location = location[best_face_idx]

                    # Calculate servo steps needed to center the face
                    # Face box is [x_center, y_center, width, height] - use directly
                    servo_control = calculate_servo_steps(
                        face_location[0],
                        face_location[1],
                        image_width,
                        image_height,
                    )

                    detection_info = {
                        "location": face_location.tolist(),  # Convert to list for JSON serialization
                        "confidence": float(max_confidence),
                        "box_info": {
                            "x_center": float(face_location[0]),
                            "y_center": float(face_location[1]),
                            "width": float(face_location[2]),
                            "height": float(face_location[3]),
                        },
                        "servo_control": servo_control,
                    }

                    logger.info(
                        f"Face detected with confidence: {max_confidence:.2f} at {face_location}"
                    )
                    logger.info(
                        f"Servo control: X={servo_control['x_steps']}° Y={servo_control['y_steps']}°"
                    )
                    return detection_info

        logger.info("No face detected in image")
        return None

    except Exception as e:
        logger.error(f"Error in face detection: {str(e)}")
        # In case of error, we return None rather than raising
        # This prevents the workflow from failing on processing errors
        return None


@activity.defn
async def save_debug_image(
    image_b64: str,
    detection_info: Optional[Dict[str, Any]],
    image_width: int = 640,
    image_height: int = 480,
) -> Dict[str, str]:
    """
    Save annotated debug image showing face detection and servo movement direction.

    Args:
        image_b64: Base64 encoded image data
        detection_info: Detection result from detect_person_in_image
        image_width: Width of the image
        image_height: Height of the image

    Returns:
        Dict with status and filename
    """
    try:
        # Create debug directory if it doesn't exist
        debug_dir = "debug_images"
        os.makedirs(debug_dir, exist_ok=True)

        # Decode image
        image_data = base64.b64decode(image_b64)
        image = Image.open(io.BytesIO(image_data))

        # Convert to RGB if needed
        if image.mode != 'RGB':
            image = image.convert('RGB')

        # Create drawing context
        draw = ImageDraw.Draw(image)

        # Try to load a font, fall back to default if not available
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
            small_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        except:
            font = ImageFont.load_default()
            small_font = ImageFont.load_default()

        if detection_info:
            # Draw bounding box
            box_info = detection_info['box_info']
            x_center = box_info['x_center']
            y_center = box_info['y_center']
            width = box_info['width']
            height = box_info['height']

            # Calculate box corners
            x1 = x_center - width / 2
            y1 = y_center - height / 2
            x2 = x_center + width / 2
            y2 = y_center + height / 2

            # Draw bounding box as a square in green
            # Calculate the larger dimension to make a square
            box_width = x2 - x1
            box_height = y2 - y1
            max_dim = max(box_width, box_height)

            # Center the square around the face center
            square_x1 = x_center - max_dim / 2
            square_y1 = y_center - max_dim / 2
            square_x2 = x_center + max_dim / 2
            square_y2 = y_center + max_dim / 2

            draw.rectangle([square_x1, square_y1, square_x2, square_y2], outline="green", width=3)

            # Draw center point
            draw.ellipse([x_center-5, y_center-5, x_center+5, y_center+5], fill="green")

            # Get servo control values
            servo_control = detection_info['servo_control']
            x_steps = servo_control['x_steps']
            y_steps = servo_control['y_steps']

            # Draw center crosshair
            img_center_x = image.width / 2
            img_center_y = image.height / 2
            draw.line([img_center_x-20, img_center_y, img_center_x+20, img_center_y], fill="red", width=2)
            draw.line([img_center_x, img_center_y-20, img_center_x, img_center_y+20], fill="red", width=2)

            # Draw arrows showing movement direction
            arrow_start_x = img_center_x
            arrow_start_y = img_center_y
            arrow_length = 60

            # X axis arrow (horizontal)
            if x_steps != 0:
                x_direction = 1 if x_steps > 0 else -1
                arrow_end_x = arrow_start_x + (arrow_length * x_direction)
                draw.line([arrow_start_x, arrow_start_y - 30, arrow_end_x, arrow_start_y - 30],
                         fill="blue", width=4)
                # Arrowhead
                draw.polygon([
                    (arrow_end_x, arrow_start_y - 30),
                    (arrow_end_x - (10 * x_direction), arrow_start_y - 35),
                    (arrow_end_x - (10 * x_direction), arrow_start_y - 25)
                ], fill="blue")

            # Y axis arrow (vertical)
            if y_steps != 0:
                y_direction = 1 if y_steps > 0 else -1
                arrow_end_y = arrow_start_y + (arrow_length * y_direction)
                draw.line([arrow_start_x + 30, arrow_start_y, arrow_start_x + 30, arrow_end_y],
                         fill="orange", width=4)
                # Arrowhead
                draw.polygon([
                    (arrow_start_x + 30, arrow_end_y),
                    (arrow_start_x + 25, arrow_end_y - (10 * y_direction)),
                    (arrow_start_x + 35, arrow_end_y - (10 * y_direction))
                ], fill="orange")

            # Add text overlay with movement info
            confidence = detection_info['confidence']
            offset_px = detection_info['servo_control']['offset_pixels']
            offset_deg = detection_info['servo_control']['offset_degrees']

            text1 = f"MOVING: X={x_steps}° Y={y_steps}°"
            text2 = f"Confidence: {confidence:.2f}"
            text3 = f"Offset: X={offset_px['x']:.1f}px Y={offset_px['y']:.1f}px"
            text4 = f"Degrees: X={offset_deg['x']:.1f}° Y={offset_deg['y']:.1f}°"

            # Draw text with background
            draw.rectangle([10, 10, 350, 100], fill="black")
            draw.text((15, 15), text1, fill="yellow", font=font)
            draw.text((15, 40), text2, fill="white", font=small_font)
            draw.text((15, 60), text3, fill="white", font=small_font)
            draw.text((15, 80), text4, fill="white", font=small_font)

            # Add legend
            draw.rectangle([10, image.height - 80, 200, image.height - 10], fill="black")
            draw.text((15, image.height - 75), "Blue arrow: X axis", fill="blue", font=small_font)
            draw.text((15, image.height - 55), "Orange arrow: Y axis", fill="orange", font=small_font)
            draw.text((15, image.height - 35), "Red cross: Image center", fill="red", font=small_font)
            draw.text((15, image.height - 15), "Green box: Face", fill="green", font=small_font)
        else:
            # No face detected
            draw.rectangle([10, 10, 250, 40], fill="black")
            draw.text((15, 15), "No face detected", fill="red", font=font)

        # Generate timestamped filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{debug_dir}/debug_{timestamp}.jpg"

        # Save image
        image.save(filename, "JPEG", quality=95)
        logger.info(f"Saved debug image: {filename}")

        # Clean up old images (keep only last 50)
        cleanup_old_images(debug_dir, max_images=50)

        return {"status": "saved", "filename": filename}

    except Exception as e:
        logger.error(f"Error saving debug image: {str(e)}")
        return {"status": "error", "error": str(e)}


def cleanup_old_images(directory: str, max_images: int = 50):
    """
    Keep only the most recent max_images in the directory.
    """
    try:
        # Get all jpg files sorted by modification time
        files = glob.glob(f"{directory}/debug_*.jpg")
        files.sort(key=os.path.getmtime, reverse=True)

        # Delete oldest files if we have more than max_images
        if len(files) > max_images:
            for old_file in files[max_images:]:
                os.remove(old_file)
                logger.info(f"Removed old debug image: {old_file}")
    except Exception as e:
        logger.error(f"Error cleaning up old images: {str(e)}")

"""
Laminar Cannon - Face Tracking Air Purifier
Raspberry Pi Zero 2W Camera and Servo Control

This is the main Pi application that:
1. Captures images from Pi camera using rpicam-still (Pi OS Bookworm)
2. Sends images to remote Flask server for face detection
3. Receives servo movement commands
4. Moves servos to track detected face
5. Controls fans via GPIO 18 hardware PWM
"""

import time
import subprocess
import requests
import logging
import os
from dotenv import load_dotenv
from adafruit_servokit import ServoKit
from PIL import Image
import io
import RPi.GPIO as GPIO

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration from environment
DETECTION_SERVER_URL = os.getenv("DETECTION_SERVER_URL", "http://localhost:5000/detect-person")

# Servo configuration
SERVO_X_CHANNEL = 0  # Horizontal pan (left/right)
SERVO_Y_CHANNEL = 1  # Vertical tilt (up/down)
SERVO_X_RANGE = 270  # Horizontal has 270° rotation
SERVO_Y_RANGE = 180  # Vertical has 180° rotation

# Servo center positions and inversion from .env
SERVO_X_CENTER = int(os.getenv("SERVO_X_CENTER", "135"))  # Horizontal servo center
SERVO_Y_CENTER = int(os.getenv("SERVO_Y_CENTER", "100"))  # Vertical servo center
SERVO_X_INVERT = os.getenv("SERVO_X_INVERT", "false").lower() == "true"
SERVO_Y_INVERT = os.getenv("SERVO_Y_INVERT", "false").lower() == "true"

# Safety limits - Vertical axis ±15° range to prevent tipping
SERVO_Y_MIN = SERVO_Y_CENTER - 15  # Vertical servo minimum
SERVO_Y_MAX = SERVO_Y_CENTER + 15  # Vertical servo maximum

# Servo speed and movement limits
SERVO_SPEED_DEG_PER_SEC = 180  # 270°/sec with 33% buffer for safety (slower)
MIN_MOVE_TIME = 0.15  # Minimum time for any movement (increased for smoother start)
MAX_STEP_SIZE = 30  # Maximum degrees to move per update (prevents large jumps)
RAMP_STEPS = 5  # Number of steps for acceleration/deceleration
INTER_AXIS_DELAY = 0.5  # Delay between X and Y axis movements (increased for safety)

# Fan configuration - GPIO 18 hardware PWM for 25kHz fan control
FAN_PIN = 18  # GPIO 18 (physical pin 12) - hardware PWM capable
FAN_SPEED = int(os.getenv("FAN_SPEED", "80"))  # Fan speed percentage (0-100)
FACE_DETECTION_TIMEOUT = int(os.getenv("FACE_DETECTION_TIMEOUT", "5"))  # Seconds to keep fan running after last detection

# Initialize hardware
logger.info("Initializing servos...")
kit = ServoKit(channels=16)

# Configure servo ranges BEFORE moving them
kit.servo[SERVO_X_CHANNEL].set_pulse_width_range(500, 2500)
kit.servo[SERVO_X_CHANNEL].actuation_range = SERVO_X_RANGE  # 270° for horizontal

kit.servo[SERVO_Y_CHANNEL].set_pulse_width_range(500, 2500)
kit.servo[SERVO_Y_CHANNEL].actuation_range = SERVO_Y_RANGE  # 180° for vertical

# CRITICAL: Center servos immediately to prevent damage
logger.info("CENTERING SERVOS - DO NOT INTERRUPT!")
kit.servo[SERVO_X_CHANNEL].angle = SERVO_X_CENTER  # Horizontal center
kit.servo[SERVO_Y_CHANNEL].angle = SERVO_Y_CENTER  # Vertical center (tilted back 10°)

# Track current positions
current_x = SERVO_X_CENTER  # Horizontal center position
current_y = SERVO_Y_CENTER  # Vertical center position (tilted back)

# Wait for servos to reach center position
time.sleep(1.0)
logger.info(f"Servos centered at X:{current_x}° Y:{current_y}°")

# Initialize fan control via GPIO 18 hardware PWM
logger.info("Initializing fan control...")
GPIO.setmode(GPIO.BCM)  # Use BCM pin numbering
GPIO.setup(FAN_PIN, GPIO.OUT)
fan_pwm = GPIO.PWM(FAN_PIN, 25000)  # 25kHz PWM frequency (Intel spec for 4-pin fans)
fan_pwm.start(0)  # Start with fan off (0% duty cycle)
fan_is_running = False
last_detection_time = 0
logger.info("Fan control initialized (25kHz PWM on GPIO 18)")

logger.info("Camera ready (using rpicam-still)")


def capture_image():
    """
    Capture image using rpicam-still command (Pi OS Bookworm).

    Captures from Pi Camera v2, rotates 90° clockwise to correct orientation,
    and returns JPEG bytes ready to send to detection server.

    Returns:
        bytes: JPEG image data, or None if capture failed
    """
    try:
        # Capture to stdout and read bytes
        result = subprocess.run(
            ['rpicam-still', '-o', '-', '-t', '1', '--width', '640', '--height', '480', '-n'],
            capture_output=True,
            check=True,
            timeout=5  # Prevent hanging if camera fails
        )

        # Rotate image 90 degrees clockwise
        image = Image.open(io.BytesIO(result.stdout))
        rotated = image.rotate(-90, expand=True)  # -90 = clockwise

        # Convert back to bytes
        output = io.BytesIO()
        rotated.save(output, format='JPEG')
        output.seek(0)

        return output.getvalue()
    except subprocess.TimeoutExpired:
        logger.error("Camera capture timed out")
        return None
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to capture image: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to rotate image: {e}")
        return None


def capture_and_detect():
    """
    Main tracking loop function.

    1. Captures image from Pi camera
    2. Sends to Flask detection server
    3. If face detected: moves servos and turns on fan
    4. If no face for FACE_DETECTION_TIMEOUT seconds: turns off fan
    """
    global current_x, current_y, fan_is_running, last_detection_time

    # Capture image
    image_data = capture_image()
    if image_data is None:
        logger.error("Failed to capture image")
        return

    logger.info("Sending image to detection server...")

    try:
        # Send image to server
        files = {'image': ('camera.jpg', image_data, 'image/jpeg')}
        response = requests.post(DETECTION_SERVER_URL, files=files, timeout=10)
        response.raise_for_status()

        result = response.json()

        if result.get("person_detected"):
            # Get servo commands from server
            servo_control = result.get("servo_control", {})
            x_steps = servo_control.get("x_steps", 0)
            y_steps = servo_control.get("y_steps", 0)

            # Limit maximum step size to prevent sudden large movements
            x_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, x_steps))
            y_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, y_steps))

            logger.info(f"Person detected! Moving X:{x_steps}° Y:{y_steps}°")

            # Update detection time and turn on fan if not already running
            last_detection_time = time.time()
            if not fan_is_running:
                logger.info("Face detected - turning on fan")
                fan_pwm.ChangeDutyCycle(FAN_SPEED)  # Set PWM duty cycle (0-100%)
                fan_is_running = True

            # Calculate new positions with safety limits
            # Apply inversion if configured in .env
            x_movement = -x_steps if SERVO_X_INVERT else x_steps
            y_movement = -y_steps if SERVO_Y_INVERT else y_steps

            new_x = max(0, min(SERVO_X_RANGE, current_x + x_movement))  # Horizontal
            new_y = max(SERVO_Y_MIN, min(SERVO_Y_MAX, current_y + y_movement))  # Vertical

            # CRITICAL: Move X first, then Y (NEVER simultaneously to prevent tipping)

            # Move X axis (horizontal servo) with acceleration/deceleration
            if abs(new_x - current_x) > 0.5:
                distance = abs(new_x - current_x)
                step_size = distance / (RAMP_STEPS * 2)

                # Accelerate gradually
                for i in range(1, RAMP_STEPS + 1):
                    intermediate = current_x + (new_x - current_x) * (i * step_size / distance)
                    kit.servo[SERVO_X_CHANNEL].angle = intermediate
                    time.sleep(0.05 * i)  # Gradually increase delay

                # Move to target
                kit.servo[SERVO_X_CHANNEL].angle = new_x
                move_time = max(MIN_MOVE_TIME, distance / SERVO_SPEED_DEG_PER_SEC)
                time.sleep(move_time)

                # Settling time
                time.sleep(0.1)
                time.sleep(INTER_AXIS_DELAY)  # Extended pause between axes
                current_x = new_x

            # Move Y axis (vertical servo) with acceleration/deceleration
            if abs(new_y - current_y) > 0.5:
                distance = abs(new_y - current_y)
                step_size = distance / (RAMP_STEPS * 2)

                # Accelerate gradually
                for i in range(1, RAMP_STEPS + 1):
                    intermediate = current_y + (new_y - current_y) * (i * step_size / distance)
                    kit.servo[SERVO_Y_CHANNEL].angle = intermediate
                    time.sleep(0.05 * i)  # Gradually increase delay

                # Move to target
                kit.servo[SERVO_Y_CHANNEL].angle = new_y
                move_time = max(MIN_MOVE_TIME, distance / SERVO_SPEED_DEG_PER_SEC)
                time.sleep(move_time)

                # Settling time
                time.sleep(0.1)
                current_y = new_y

            logger.info(f"Servos at X:{current_x:.1f}° Y:{current_y:.1f}°")
        else:
            logger.info("No person detected")

    except Exception as e:
        logger.error(f"Error: {e}")

    # Turn off fan if no face detected for FACE_DETECTION_TIMEOUT seconds
    if fan_is_running and (time.time() - last_detection_time > FACE_DETECTION_TIMEOUT):
        logger.info(f"No face for {FACE_DETECTION_TIMEOUT}s - turning off fan")
        fan_pwm.ChangeDutyCycle(0)  # Set duty cycle to 0% (fan off)
        fan_is_running = False


def safe_shutdown():
    """
    Safely center servos and stop fan before shutdown.
    """
    logger.info("SHUTTING DOWN - Stopping fan and centering servos...")
    try:
        # Stop fan
        fan_pwm.stop()
        GPIO.cleanup()
        logger.info("Fan stopped")

        # Center servos
        kit.servo[SERVO_X_CHANNEL].angle = SERVO_X_CENTER  # Horizontal center
        kit.servo[SERVO_Y_CHANNEL].angle = SERVO_Y_CENTER  # Vertical center
        time.sleep(1.0)  # Wait for servos to center
        logger.info("Servos safely centered. Shutdown complete.")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


if __name__ == "__main__":
    logger.info("Starting person tracking...")
    logger.info(f"Server: {DETECTION_SERVER_URL}")

    # Continuous tracking loop
    try:
        while True:
            capture_and_detect()
            time.sleep(0.5)  # Track at ~2 FPS to avoid overloading Pi Zero

    except KeyboardInterrupt:
        safe_shutdown()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        safe_shutdown()

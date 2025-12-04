#!/usr/bin/env python3
"""
Servo Calibration Script for Laminar Cannon

This script helps calibrate servo movements by:
1. Capturing images for 10 seconds while you stay still
2. Recording what movements the detection model calculates
3. Recording what movements the servos actually make
4. Comparing the two to identify calibration issues
"""

import time
import subprocess
import requests
import os
from dotenv import load_dotenv
from adafruit_servokit import ServoKit
import json
from datetime import datetime
from PIL import Image
import io

load_dotenv()

# Configuration - must match app.py
SERVER_URL = os.getenv("DETECTION_SERVER_URL", "http://localhost:5000/detect-person")
SERVO_X_CHANNEL = 0  # Horizontal pan (left/right)
SERVO_Y_CHANNEL = 1  # Vertical tilt (up/down)
SERVO_X_RANGE = 270  # Horizontal has 270° rotation
SERVO_Y_RANGE = 180  # Vertical has 180° rotation

# Load servo configuration from .env
SERVO_X_CENTER = int(os.getenv("SERVO_X_CENTER", "135"))
SERVO_Y_CENTER = int(os.getenv("SERVO_Y_CENTER", "100"))
SERVO_X_INVERT = os.getenv("SERVO_X_INVERT", "false").lower() == "true"
SERVO_Y_INVERT = os.getenv("SERVO_Y_INVERT", "false").lower() == "true"
SERVO_Y_MIN = SERVO_Y_CENTER - 15
SERVO_Y_MAX = SERVO_Y_CENTER + 15

SERVO_SPEED_DEG_PER_SEC = 180
MIN_MOVE_TIME = 0.15
MAX_STEP_SIZE = 30
INTER_AXIS_DELAY = 0.5

print("=" * 70)
print("Laminar Cannon Servo Calibration")
print("=" * 70)
print()
print("This script will help calibrate your servo movements.")
print("When you press Enter, stay as STILL as possible for 10 seconds.")
print()
print("The script will:")
print("  1. Capture images and send them to the detection server")
print("  2. Record what movements the model CALCULATES")
print("  3. Record what movements the servos ACTUALLY make")
print("  4. Generate a report comparing calculated vs actual")
print()
print("=" * 70)
print()

# Initialize servos
print("Initializing servos...")
kit = ServoKit(channels=16)
kit.servo[SERVO_X_CHANNEL].set_pulse_width_range(500, 2500)
kit.servo[SERVO_X_CHANNEL].actuation_range = SERVO_X_RANGE
kit.servo[SERVO_Y_CHANNEL].set_pulse_width_range(500, 2500)
kit.servo[SERVO_Y_CHANNEL].actuation_range = SERVO_Y_RANGE

# Center servos
print(f"Centering servos at X={SERVO_X_CENTER}° Y={SERVO_Y_CENTER}°...")
kit.servo[SERVO_X_CHANNEL].angle = SERVO_X_CENTER
kit.servo[SERVO_Y_CHANNEL].angle = SERVO_Y_CENTER
time.sleep(1)

current_x = SERVO_X_CENTER
current_y = SERVO_Y_CENTER

print("✓ Servos centered and ready")
print()

input("Press Enter when you're ready to start the 10-second calibration test...")
print()
print("=" * 70)
print("STARTING CALIBRATION - STAY STILL!")
print("=" * 70)

# Data collection
calibration_data = []
start_time = time.time()
frame_count = 0

while time.time() - start_time < 10.0:
    frame_count += 1
    frame_start = time.time()

    # Capture image
    try:
        result = subprocess.run(
            ['rpicam-still', '-o', '-', '-t', '1', '--width', '640', '--height', '480', '-n'],
            capture_output=True,
            timeout=5,
            check=True
        )
        raw_image_data = result.stdout

        # Rotate image 90 degrees clockwise (same as app.py)
        image = Image.open(io.BytesIO(raw_image_data))
        rotated = image.rotate(-90, expand=True)  # -90 = clockwise
        output = io.BytesIO()
        rotated.save(output, format='JPEG')
        image_data = output.getvalue()
    except Exception as e:
        print(f"✗ Frame {frame_count}: Camera error: {e}")
        continue

    # Send to detection server
    try:
        files = {'image': ('test.jpg', image_data, 'image/jpeg')}
        response = requests.post(SERVER_URL, files=files, timeout=10)
        response.raise_for_status()
        detection_result = response.json()
    except Exception as e:
        print(f"✗ Frame {frame_count}: Server error: {e}")
        continue

    # Process detection result
    if detection_result.get('person_detected'):
        servo_control = detection_result.get('servo_control', {})
        x_steps = servo_control.get('x_steps', 0)
        y_steps = servo_control.get('y_steps', 0)
        confidence = detection_result.get('detection_info', {}).get('confidence', 0)

        # Limit step size
        x_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, x_steps))
        y_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, y_steps))

        # Calculate new positions (using same logic as app.py)
        # Apply inversion if configured in .env
        x_movement = -x_steps if SERVO_X_INVERT else x_steps
        y_movement = -y_steps if SERVO_Y_INVERT else y_steps

        calculated_x = max(0, min(SERVO_X_RANGE, current_x + x_movement))
        calculated_y = max(SERVO_Y_MIN, min(SERVO_Y_MAX, current_y + y_movement))

        # Store current position before moving
        before_x = current_x
        before_y = current_y

        # Move X axis
        if abs(calculated_x - current_x) > 0.5:
            kit.servo[SERVO_X_CHANNEL].angle = calculated_x
            move_time = max(MIN_MOVE_TIME, abs(calculated_x - current_x) / SERVO_SPEED_DEG_PER_SEC)
            time.sleep(move_time)
            time.sleep(INTER_AXIS_DELAY)
            current_x = calculated_x

        # Move Y axis
        if abs(calculated_y - current_y) > 0.5:
            kit.servo[SERVO_Y_CHANNEL].angle = calculated_y
            move_time = max(MIN_MOVE_TIME, abs(calculated_y - current_y) / SERVO_SPEED_DEG_PER_SEC)
            time.sleep(move_time)
            current_y = calculated_y

        # Record data
        data_point = {
            'frame': frame_count,
            'timestamp': time.time() - start_time,
            'confidence': confidence,
            'calculated_steps': {'x': x_steps, 'y': y_steps},
            'position_before': {'x': before_x, 'y': before_y},
            'position_after': {'x': current_x, 'y': current_y},
            'actual_movement': {'x': current_x - before_x, 'y': current_y - before_y}
        }
        calibration_data.append(data_point)

        print(f"Frame {frame_count:2d}: Calc[X={x_steps:+3d}° Y={y_steps:+3d}°] "
              f"Actual[X={current_x - before_x:+.1f}° Y={current_y - before_y:+.1f}°] "
              f"Pos[X={current_x:.1f}° Y={current_y:.1f}°] Conf={confidence:.2f}")
    else:
        print(f"Frame {frame_count:2d}: No face detected")

    # Small delay between frames
    elapsed = time.time() - frame_start
    if elapsed < 0.5:  # Aim for ~2 fps
        time.sleep(0.5 - elapsed)

print()
print("=" * 70)
print("CALIBRATION COMPLETE")
print("=" * 70)
print()

# Generate report
if calibration_data:
    print(f"Captured {len(calibration_data)} valid detections")
    print()

    # Calculate statistics
    total_calc_x = sum(d['calculated_steps']['x'] for d in calibration_data)
    total_calc_y = sum(d['calculated_steps']['y'] for d in calibration_data)
    total_actual_x = sum(d['actual_movement']['x'] for d in calibration_data)
    total_actual_y = sum(d['actual_movement']['y'] for d in calibration_data)

    avg_calc_x = total_calc_x / len(calibration_data)
    avg_calc_y = total_calc_y / len(calibration_data)
    avg_actual_x = total_actual_x / len(calibration_data)
    avg_actual_y = total_actual_y / len(calibration_data)

    print("SUMMARY:")
    print("-" * 70)
    print(f"Total Calculated Movement: X={total_calc_x:+.1f}° Y={total_calc_y:+.1f}°")
    print(f"Total Actual Movement:     X={total_actual_x:+.1f}° Y={total_actual_y:+.1f}°")
    print()
    print(f"Average per Frame (Calculated): X={avg_calc_x:+.1f}° Y={avg_calc_y:+.1f}°")
    print(f"Average per Frame (Actual):     X={avg_actual_x:+.1f}° Y={avg_actual_y:+.1f}°")
    print()

    # Analysis
    print("ANALYSIS:")
    print("-" * 70)

    if abs(total_calc_x) < 5 and abs(total_calc_y) < 5:
        print("✓ GOOD: Model sees you as mostly centered (minimal drift)")
    else:
        print(f"⚠ WARNING: Model sees drift - Total calculated movement:")
        print(f"           X={total_calc_x:+.1f}° Y={total_calc_y:+.1f}°")

    if abs(total_actual_x) < 5 and abs(total_actual_y) < 5:
        print("✓ GOOD: Servos stayed mostly centered")
    else:
        print(f"⚠ WARNING: Servos drifted from center:")
        print(f"           X={total_actual_x:+.1f}° Y={total_actual_y:+.1f}°")

    # Check if calculated and actual match
    x_diff = abs(total_calc_x - total_actual_x)
    y_diff = abs(total_calc_y - total_actual_y)

    if x_diff < 2:
        print("✓ GOOD: X axis - calculated and actual match")
    else:
        print(f"✗ ISSUE: X axis - mismatch of {x_diff:.1f}° (calculated vs actual)")
        print(f"         This suggests X axis calibration is correct")

    if y_diff < 2:
        print("✓ GOOD: Y axis - calculated and actual match")
    else:
        print(f"✗ ISSUE: Y axis - mismatch of {y_diff:.1f}° (calculated vs actual)")
        if total_calc_y != 0:
            ratio = total_actual_y / total_calc_y
            print(f"         Ratio: {ratio:.2f}x (actual/calculated)")
            if abs(ratio + 1) < 0.2:
                print(f"         → Y axis appears to be INVERTED")
            elif abs(ratio) < 0.5:
                print(f"         → Y axis may need direction adjustment")

    print()

    # Final position
    print(f"Final servo position: X={current_x:.1f}° Y={current_y:.1f}°")
    print(f"Started at:           X={SERVO_X_CENTER:.1f}° Y={SERVO_Y_CENTER:.1f}°")
    print(f"Net drift:            X={current_x - SERVO_X_CENTER:+.1f}° Y={current_y - SERVO_Y_CENTER:+.1f}°")
    print()

    # Save detailed data
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"calibration_{timestamp}.json"
    with open(filename, 'w') as f:
        json.dump({
            'summary': {
                'total_frames': len(calibration_data),
                'total_calculated': {'x': total_calc_x, 'y': total_calc_y},
                'total_actual': {'x': total_actual_x, 'y': total_actual_y},
                'final_position': {'x': current_x, 'y': current_y},
                'starting_position': {'x': SERVO_X_CENTER, 'y': SERVO_Y_CENTER}
            },
            'frames': calibration_data
        }, f, indent=2)
    print(f"Detailed data saved to: {filename}")
else:
    print("No valid detections captured during calibration")

print()
print("=" * 70)

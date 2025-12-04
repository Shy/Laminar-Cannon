#!/usr/bin/env python3
"""
Interactive Calibration Tool for Laminar Cannon

Provides multiple calibration options:
1. Servo direction test - Test and verify X/Y axis directions
2. Servo position calibration - Find optimal center positions
3. Movement accuracy test - Verify calculated vs actual movements (stay still)
4. Field of view test - Test servo range and limits
"""

import time
import subprocess
import requests
import os
import sys
from dotenv import load_dotenv, set_key
from adafruit_servokit import ServoKit
import json
from datetime import datetime
from PIL import Image
import io

load_dotenv()

# Configuration
ENV_FILE = ".env"
SERVER_URL = os.getenv("DETECTION_SERVER_URL", "http://localhost:5000/detect-person")
SERVO_X_CHANNEL = 0
SERVO_Y_CHANNEL = 1
SERVO_X_RANGE = 270
SERVO_Y_RANGE = 180
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

# Initialize servos
kit = ServoKit(channels=16)
kit.servo[SERVO_X_CHANNEL].set_pulse_width_range(500, 2500)
kit.servo[SERVO_X_CHANNEL].actuation_range = SERVO_X_RANGE
kit.servo[SERVO_Y_CHANNEL].set_pulse_width_range(500, 2500)
kit.servo[SERVO_Y_CHANNEL].actuation_range = SERVO_Y_RANGE


def print_header(title):
    """Print a formatted header"""
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)
    print()


def print_current_config():
    """Display current servo configuration"""
    print("Current Configuration:")
    print("-" * 70)
    print(f"  SERVO_X_CENTER = {SERVO_X_CENTER}°")
    print(f"  SERVO_Y_CENTER = {SERVO_Y_CENTER}°")
    print(f"  SERVO_X_INVERT = {SERVO_X_INVERT}")
    print(f"  SERVO_Y_INVERT = {SERVO_Y_INVERT}")
    print(f"  SERVO_Y_MIN = {SERVO_Y_MIN}° (center - 15°)")
    print(f"  SERVO_Y_MAX = {SERVO_Y_MAX}° (center + 15°)")
    print()


def update_env_var(key, value):
    """Update a variable in the .env file"""
    env_path = os.path.join(os.path.dirname(__file__), ENV_FILE)
    set_key(env_path, key, str(value))
    print(f"✓ Updated {key} = {value} in .env")


def center_servos():
    """Move servos to center position"""
    kit.servo[SERVO_X_CHANNEL].angle = SERVO_X_CENTER
    kit.servo[SERVO_Y_CHANNEL].angle = SERVO_Y_CENTER
    time.sleep(1)


def capture_and_rotate_image():
    """Capture image from camera and rotate it"""
    result = subprocess.run(
        ['rpicam-still', '-o', '-', '-t', '1', '--width', '640', '--height', '480', '-n'],
        capture_output=True,
        timeout=5,
        check=True
    )
    raw_image_data = result.stdout

    # Rotate image 90 degrees clockwise
    image = Image.open(io.BytesIO(raw_image_data))
    rotated = image.rotate(-90, expand=True)
    output = io.BytesIO()
    rotated.save(output, format='JPEG')
    return output.getvalue()


def test_servo_directions():
    """Test 1: Servo Direction Test"""
    print_header("Test 1: Servo Direction Test")
    print("This test will help determine if your servo axes are inverted.")
    print()
    print("Instructions:")
    print("  1. Position yourself in front of the camera")
    print("  2. Stay centered in the frame")
    print("  3. The servos will move to test each axis")
    print("  4. Observe if the movement is correct")
    print()

    input("Press Enter to start the test...")

    print("\nCentering servos...")
    center_servos()
    current_x = SERVO_X_CENTER
    current_y = SERVO_Y_CENTER

    # Test X axis
    print("\n" + "-" * 70)
    print("Testing X AXIS (Horizontal)")
    print("-" * 70)
    print("The servo will move LEFT, then return to center.")
    print("Watch: Does the camera/cannon move LEFT as expected?")
    input("Press Enter to test X axis LEFT movement...")

    test_x = max(0, current_x - 30)
    kit.servo[SERVO_X_CHANNEL].angle = test_x
    time.sleep(1)

    moved_left = input("Did the camera move LEFT? (y/n): ").strip().lower()

    print("\nReturning to center...")
    kit.servo[SERVO_X_CHANNEL].angle = current_x
    time.sleep(1)

    # Test Y axis
    print("\n" + "-" * 70)
    print("Testing Y AXIS (Vertical)")
    print("-" * 70)
    print("The servo will move UP, then return to center.")
    print("Watch: Does the camera/cannon tilt UP as expected?")
    input("Press Enter to test Y axis UP movement...")

    test_y = min(SERVO_Y_MAX, current_y + 10)
    kit.servo[SERVO_Y_CHANNEL].angle = test_y
    time.sleep(1)

    moved_up = input("Did the camera tilt UP? (y/n): ").strip().lower()

    print("\nReturning to center...")
    kit.servo[SERVO_Y_CHANNEL].angle = current_y
    time.sleep(1)

    # Analysis
    print("\n" + "=" * 70)
    print("ANALYSIS")
    print("=" * 70)

    if moved_left == 'y' and moved_up == 'y':
        print("✓ Both axes moving correctly!")
        print("  No changes needed to SERVO_X_INVERT or SERVO_Y_INVERT")
    else:
        print("⚠ Issues detected:")
        if moved_left != 'y':
            print(f"  X axis is inverted - Current: SERVO_X_INVERT={SERVO_X_INVERT}")
            new_x_invert = not SERVO_X_INVERT
            update = input(f"  Update SERVO_X_INVERT to {new_x_invert}? (y/n): ").strip().lower()
            if update == 'y':
                update_env_var("SERVO_X_INVERT", str(new_x_invert).lower())
                print("  ✓ Updated! Restart the app for changes to take effect.")

        if moved_up != 'y':
            print(f"  Y axis is inverted - Current: SERVO_Y_INVERT={SERVO_Y_INVERT}")
            new_y_invert = not SERVO_Y_INVERT
            update = input(f"  Update SERVO_Y_INVERT to {new_y_invert}? (y/n): ").strip().lower()
            if update == 'y':
                update_env_var("SERVO_Y_INVERT", str(new_y_invert).lower())
                print("  ✓ Updated! Restart the app for changes to take effect.")


def test_servo_centers():
    """Test 2: Servo Position Calibration"""
    print_header("Test 2: Servo Center Position Calibration")
    print("This test helps you find the optimal center positions for your servos.")
    print()
    print("Instructions:")
    print("  1. We'll move servos to various positions")
    print("  2. You tell us which position looks most centered")
    print("  3. We'll update your .env file with the best values")
    print()

    input("Press Enter to start...")

    # X axis calibration
    print("\n" + "-" * 70)
    print("Calibrating X AXIS (Horizontal)")
    print("-" * 70)

    x_positions = [
        ("Current center", SERVO_X_CENTER),
        ("10° left", SERVO_X_CENTER - 10),
        ("10° right", SERVO_X_CENTER + 10),
        ("20° left", SERVO_X_CENTER - 20),
        ("20° right", SERVO_X_CENTER + 20),
    ]

    print("\nTesting positions:")
    for i, (name, pos) in enumerate(x_positions):
        print(f"  {i+1}. {name} ({pos}°)")

    print()
    for i, (name, pos) in enumerate(x_positions):
        kit.servo[SERVO_X_CHANNEL].angle = pos
        time.sleep(1)
        print(f"Position {i+1}: {name} ({pos}°)")
        time.sleep(1)

    choice = input("\nWhich position looked most centered? (1-5): ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(x_positions):
            new_x_center = x_positions[idx][1]
            print(f"Selected: {x_positions[idx][0]} ({new_x_center}°)")
            update = input(f"Update SERVO_X_CENTER to {new_x_center}? (y/n): ").strip().lower()
            if update == 'y':
                update_env_var("SERVO_X_CENTER", new_x_center)
    except ValueError:
        print("Invalid choice, skipping X axis update")

    # Y axis calibration
    print("\n" + "-" * 70)
    print("Calibrating Y AXIS (Vertical)")
    print("-" * 70)

    y_positions = [
        ("Current center", SERVO_Y_CENTER),
        ("5° down", SERVO_Y_CENTER - 5),
        ("5° up", SERVO_Y_CENTER + 5),
        ("10° down", SERVO_Y_CENTER - 10),
        ("10° up", SERVO_Y_CENTER + 10),
    ]

    print("\nTesting positions:")
    for i, (name, pos) in enumerate(y_positions):
        print(f"  {i+1}. {name} ({pos}°)")

    print()
    for i, (name, pos) in enumerate(y_positions):
        kit.servo[SERVO_Y_CHANNEL].angle = pos
        time.sleep(1)
        print(f"Position {i+1}: {name} ({pos}°)")
        time.sleep(1)

    choice = input("\nWhich position looked most centered? (1-5): ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(y_positions):
            new_y_center = y_positions[idx][1]
            print(f"Selected: {y_positions[idx][0]} ({new_y_center}°)")
            update = input(f"Update SERVO_Y_CENTER to {new_y_center}? (y/n): ").strip().lower()
            if update == 'y':
                update_env_var("SERVO_Y_CENTER", new_y_center)
    except ValueError:
        print("Invalid choice, skipping Y axis update")

    print("\n✓ Center position calibration complete!")


def test_movement_accuracy():
    """Test 3: Movement Accuracy Test (Stay Still)"""
    print_header("Test 3: Movement Accuracy Test")
    print("This test verifies calculated movements match actual servo movements.")
    print()
    print("Instructions:")
    print("  1. Position yourself in front of the camera")
    print("  2. When you press Enter, STAY AS STILL AS POSSIBLE for 10 seconds")
    print("  3. The system will capture images and move servos")
    print("  4. Results will show if calculations match actual movements")
    print()

    input("Press Enter when ready to start the 10-second test...")

    print("\nCentering servos...")
    center_servos()
    current_x = SERVO_X_CENTER
    current_y = SERVO_Y_CENTER

    print("\n" + "=" * 70)
    print("STARTING TEST - STAY STILL!")
    print("=" * 70)

    calibration_data = []
    start_time = time.time()
    frame_count = 0

    while time.time() - start_time < 10.0:
        frame_count += 1
        frame_start = time.time()

        try:
            image_data = capture_and_rotate_image()
        except Exception as e:
            print(f"✗ Frame {frame_count}: Camera error: {e}")
            continue

        try:
            files = {'image': ('test.jpg', image_data, 'image/jpeg')}
            response = requests.post(SERVER_URL, files=files, timeout=10)
            response.raise_for_status()
            detection_result = response.json()
        except Exception as e:
            print(f"✗ Frame {frame_count}: Server error: {e}")
            continue

        if detection_result.get('person_detected'):
            servo_control = detection_result.get('servo_control', {})
            x_steps = servo_control.get('x_steps', 0)
            y_steps = servo_control.get('y_steps', 0)
            confidence = detection_result.get('detection_info', {}).get('confidence', 0)

            x_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, x_steps))
            y_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, y_steps))

            # Apply inversion
            x_movement = -x_steps if SERVO_X_INVERT else x_steps
            y_movement = -y_steps if SERVO_Y_INVERT else y_steps

            calculated_x = max(0, min(SERVO_X_RANGE, current_x + x_movement))
            calculated_y = max(SERVO_Y_MIN, min(SERVO_Y_MAX, current_y + y_movement))

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

        elapsed = time.time() - frame_start
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)

    # Generate report
    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)
    print()

    if calibration_data:
        print(f"Captured {len(calibration_data)} valid detections")
        print()

        total_calc_x = sum(d['calculated_steps']['x'] for d in calibration_data)
        total_calc_y = sum(d['calculated_steps']['y'] for d in calibration_data)
        total_actual_x = sum(d['actual_movement']['x'] for d in calibration_data)
        total_actual_y = sum(d['actual_movement']['y'] for d in calibration_data)

        print("SUMMARY:")
        print("-" * 70)
        print(f"Total Calculated Movement: X={total_calc_x:+.1f}° Y={total_calc_y:+.1f}°")
        print(f"Total Actual Movement:     X={total_actual_x:+.1f}° Y={total_actual_y:+.1f}°")
        print()

        print("ANALYSIS:")
        print("-" * 70)

        x_diff = abs(total_calc_x - total_actual_x)
        y_diff = abs(total_calc_y - total_actual_y)

        if x_diff < 2:
            print("✓ GOOD: X axis - calculated and actual match")
        else:
            print(f"✗ ISSUE: X axis - mismatch of {x_diff:.1f}°")
            if total_calc_x != 0:
                ratio = total_actual_x / total_calc_x
                print(f"         Ratio: {ratio:.2f}x (actual/calculated)")
                if abs(ratio + 1) < 0.2:
                    print(f"         → X axis appears to be INVERTED")
                    suggest = not SERVO_X_INVERT
                    update = input(f"\nUpdate SERVO_X_INVERT to {suggest}? (y/n): ").strip().lower()
                    if update == 'y':
                        update_env_var("SERVO_X_INVERT", str(suggest).lower())

        if y_diff < 2:
            print("✓ GOOD: Y axis - calculated and actual match")
        else:
            print(f"✗ ISSUE: Y axis - mismatch of {y_diff:.1f}°")
            if total_calc_y != 0:
                ratio = total_actual_y / total_calc_y
                print(f"         Ratio: {ratio:.2f}x (actual/calculated)")
                if abs(ratio + 1) < 0.2:
                    print(f"         → Y axis appears to be INVERTED")
                    suggest = not SERVO_Y_INVERT
                    update = input(f"\nUpdate SERVO_Y_INVERT to {suggest}? (y/n): ").strip().lower()
                    if update == 'y':
                        update_env_var("SERVO_Y_INVERT", str(suggest).lower())

        # Save data
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
        print(f"\nDetailed data saved to: {filename}")
    else:
        print("No valid detections captured during test")


def test_field_of_view():
    """Test 4: Field of View Test"""
    print_header("Test 4: Field of View Test")
    print("This test moves servos through their full range.")
    print()
    print("Instructions:")
    print("  Watch the servos move through various positions")
    print("  Verify the safety limits are appropriate")
    print()

    input("Press Enter to start...")

    print("\nCentering servos...")
    center_servos()
    time.sleep(1)

    positions = [
        ("Center", SERVO_X_CENTER, SERVO_Y_CENTER),
        ("Left-Up", SERVO_X_CENTER - 30, SERVO_Y_MAX),
        ("Right-Up", SERVO_X_CENTER + 30, SERVO_Y_MAX),
        ("Right-Down", SERVO_X_CENTER + 30, SERVO_Y_MIN),
        ("Left-Down", SERVO_X_CENTER - 30, SERVO_Y_MIN),
        ("Center", SERVO_X_CENTER, SERVO_Y_CENTER),
    ]

    for name, x_pos, y_pos in positions:
        print(f"\nMoving to {name}: X={x_pos}° Y={y_pos}°")
        kit.servo[SERVO_X_CHANNEL].angle = x_pos
        time.sleep(0.5)
        kit.servo[SERVO_Y_CHANNEL].angle = y_pos
        time.sleep(1.5)

    print("\n✓ Field of view test complete!")


def main():
    """Main interactive menu"""
    while True:
        print_header("Laminar Cannon - Interactive Calibration Tool")
        print_current_config()

        print("Available Tests:")
        print("-" * 70)
        print("  1. Servo Direction Test")
        print("     - Test if X/Y axes move in correct directions")
        print("     - Diagnose and fix axis inversions")
        print()
        print("  2. Servo Center Position Calibration")
        print("     - Find optimal center positions for servos")
        print("     - Adjust SERVO_X_CENTER and SERVO_Y_CENTER")
        print()
        print("  3. Movement Accuracy Test")
        print("     - Verify calculated vs actual movements (10 second test)")
        print("     - Requires staying still in front of camera")
        print("     - Saves detailed JSON log")
        print()
        print("  4. Field of View Test")
        print("     - Test servo range through various positions")
        print("     - Verify safety limits")
        print()
        print("  5. Exit")
        print("-" * 70)

        choice = input("\nSelect a test (1-5): ").strip()

        if choice == '1':
            test_servo_directions()
        elif choice == '2':
            test_servo_centers()
        elif choice == '3':
            test_movement_accuracy()
        elif choice == '4':
            test_field_of_view()
        elif choice == '5':
            print("\nCentering servos before exit...")
            center_servos()
            print("Goodbye!")
            sys.exit(0)
        else:
            print("\n✗ Invalid choice. Please select 1-5.")

        print()
        input("Press Enter to return to main menu...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nCentering servos before exit...")
        center_servos()
        print("Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        print("Centering servos...")
        center_servos()
        sys.exit(1)

#!/usr/bin/env python3
"""
Hardware Test Suite for Laminar Cannon

Tests all hardware components before running the main application:
1. Pi Camera capture
2. Fan control via GPIO 18 hardware PWM
3. Servo movement (pan/tilt)
4. Network connectivity to Flask detection server
5. Full integration test (camera → server → servos)
"""

import time
import subprocess
import requests
import os
from dotenv import load_dotenv

load_dotenv()

# Load servo configuration from .env
SERVO_X_CENTER = int(os.getenv("SERVO_X_CENTER", "135"))
SERVO_Y_CENTER = int(os.getenv("SERVO_Y_CENTER", "100"))
SERVO_X_INVERT = os.getenv("SERVO_X_INVERT", "false").lower() == "true"
SERVO_Y_INVERT = os.getenv("SERVO_Y_INVERT", "false").lower() == "true"
SERVO_Y_MIN = SERVO_Y_CENTER - 15
SERVO_Y_MAX = SERVO_Y_CENTER + 15

print("=" * 50)
print("Laminar Cannon Hardware Test Suite")
print("=" * 50)
print()

# Test 1: Camera
print("Test 1: Camera Capture")
print("-" * 50)
try:
    print("Capturing test image...")
    result = subprocess.run(
        ['rpicam-still', '-o', 'test-camera.jpg', '-t', '1', '--width', '640', '--height', '480', '-n'],
        capture_output=True,
        timeout=5,
        check=True
    )

    # Check file was created and has reasonable size
    import os
    if os.path.exists('test-camera.jpg'):
        size = os.path.getsize('test-camera.jpg')
        print(f"✓ Camera working! Image saved: test-camera.jpg ({size} bytes)")
        if size < 1000:
            print("⚠ Warning: Image file is very small, check camera focus/lighting")
    else:
        print("✗ Image file not created")
except subprocess.TimeoutExpired:
    print("✗ Camera timeout - check camera connection")
except subprocess.CalledProcessError as e:
    print(f"✗ Camera error: {e}")
except FileNotFoundError:
    print("✗ rpicam-still command not found")

print()
input("Press Enter to continue to fan test...")
print()

# Test 2: Fan Control
print("Test 2: Fan Control (Hardware PWM via GPIO 18)")
print("-" * 50)
try:
    import RPi.GPIO as GPIO

    print("Initializing GPIO 18 for hardware PWM...")

    # GPIO 18 (physical pin 12) supports hardware PWM
    FAN_PIN = 18
    PWM_FREQUENCY = 25000  # 25kHz - Intel spec for 4-pin PWM fans

    GPIO.setmode(GPIO.BCM)  # Use BCM pin numbering
    GPIO.setup(FAN_PIN, GPIO.OUT)
    fan_pwm = GPIO.PWM(FAN_PIN, PWM_FREQUENCY)
    fan_pwm.start(0)  # Start with 0% duty cycle (fan off)

    print("✓ GPIO 18 initialized (PWM freq: 25 kHz)")
    print()
    print("Testing fan speeds (listen for fan speed changes):")
    print()

    # Test sequence - different fan speeds
    # Duty cycle: 0 (off) to 100 (full speed)
    test_speeds = [
        ("Off", 0),
        ("25%", 25),
        ("50%", 50),
        ("75%", 75),
        ("100%", 100),
        ("Off", 0)
    ]

    for name, duty_cycle in test_speeds:
        print(f"  Setting fan to {name} speed (duty cycle: {duty_cycle}%)...")
        fan_pwm.ChangeDutyCycle(duty_cycle)
        print(f"     Listen to the fan... Press Enter when ready for next speed")
        input()

    # Clean up
    fan_pwm.stop()
    GPIO.cleanup(FAN_PIN)

    print()
    print("✓ Fan control test complete")
    print("Did you hear the fan speed change? (y/n): ", end='')
    fan_ok = input().strip().lower()
    if fan_ok != 'y':
        print("⚠ Check fan wiring:")
        print("  - Fan PWM wire → GPIO 18 (physical pin 12)")
        print("  - Fan 12V wire → 12V power supply (+)")
        print("  - Fan GND wire → Common GND")
        print("  - Common GND: Power supply GND + Pi GND connected together")

except ImportError:
    print("✗ RPi.GPIO not installed")
    print("  Run: sudo apt-get install python3-rpi.gpio")
except Exception as e:
    print(f"✗ Fan control error: {e}")
    import RPi.GPIO as GPIO
    GPIO.cleanup()

print()
input("Press Enter to continue to servo test...")
print()

# Test 3: Servos
print("Test 3: Servo Movement")
print("-" * 50)
try:
    from adafruit_servokit import ServoKit

    print("Initializing servos...")
    kit = ServoKit(channels=16)

    # Configure servos
    kit.servo[0].set_pulse_width_range(500, 2500)
    kit.servo[0].actuation_range = 270  # Horizontal has 270° rotation
    kit.servo[1].set_pulse_width_range(500, 2500)
    kit.servo[1].actuation_range = 180  # Vertical has 180° rotation

    # Servo speed constants (adjust based on your servo specs)
    # Typical hobby servo: 0.17 sec/60° at 4.8V = 353°/sec
    # Using 270°/sec with 33% safety buffer = 180°/sec effective (slower for stability)
    SERVO_SPEED_DEG_PER_SEC = 180  # 270°/sec with 33% buffer
    MIN_MOVE_TIME = 0.15  # Minimum time for any movement (increased for smoother start)
    MAX_STEP_SIZE = 30  # Maximum degrees to move per update (prevents large jumps)
    RAMP_STEPS = 5  # Number of steps for acceleration/deceleration
    INTER_AXIS_DELAY = 0.5  # Delay between X and Y axis movements (increased for safety)

    print("✓ Servos initialized")
    print()
    print("Watch the servos move through test sequence:")
    print()

    # Test sequence - move slowly to prevent jerky movements
    # Servo 0 = horizontal (left/right), servo 1 = vertical (up/down)
    # Servo 1 (vertical) limited to ±15° from center
    positions = [
        ("Center", SERVO_X_CENTER, SERVO_Y_CENTER),
        ("Left-Up", SERVO_X_CENTER - 30, SERVO_Y_MAX),
        ("Right-Down", SERVO_X_CENTER + 30, SERVO_Y_MIN),
        ("Center", SERVO_X_CENTER, SERVO_Y_CENTER)
    ]

    # Start at center
    kit.servo[0].angle = SERVO_X_CENTER  # Horizontal center
    kit.servo[1].angle = SERVO_Y_CENTER  # Vertical center
    time.sleep(1)

    for name, x_pos, y_pos in positions:
        # Clamp vertical servo (servo 1) to ±15° from center
        y_pos = max(SERVO_Y_MIN, min(SERVO_Y_MAX, y_pos))

        print(f"  Moving to {name}: X={x_pos}° Y={y_pos}°")

        # Get current positions
        current_x = kit.servo[0].angle
        current_y = kit.servo[1].angle

        # Move X first, then Y (NEVER simultaneously to prevent tipping)

        # Move X axis only with acceleration/deceleration
        if abs(x_pos - current_x) > 0.5:
            distance = abs(x_pos - current_x)
            step_size = distance / (RAMP_STEPS * 2)

            # Accelerate
            for i in range(1, RAMP_STEPS + 1):
                intermediate = current_x + (x_pos - current_x) * (i * step_size / distance)
                kit.servo[0].angle = intermediate
                time.sleep(0.05 * i)  # Gradually increase delay

            # Move to target
            kit.servo[0].angle = x_pos
            move_time = max(MIN_MOVE_TIME, distance / SERVO_SPEED_DEG_PER_SEC)
            time.sleep(move_time)

            # Decelerate (brief settling)
            time.sleep(0.1)
            time.sleep(INTER_AXIS_DELAY)  # Pause between axes

        # Move Y axis only with acceleration/deceleration
        if abs(y_pos - current_y) > 0.5:
            distance = abs(y_pos - current_y)
            step_size = distance / (RAMP_STEPS * 2)

            # Accelerate
            for i in range(1, RAMP_STEPS + 1):
                intermediate = current_y + (y_pos - current_y) * (i * step_size / distance)
                kit.servo[1].angle = intermediate
                time.sleep(0.05 * i)  # Gradually increase delay

            # Move to target
            kit.servo[1].angle = y_pos
            move_time = max(MIN_MOVE_TIME, distance / SERVO_SPEED_DEG_PER_SEC)
            time.sleep(move_time)

            # Decelerate (brief settling)
            time.sleep(0.1)

        time.sleep(1.0)  # Pause at each position

    print()
    print("✓ Servo test complete")
    print("Did both servos move smoothly? (y/n): ", end='')
    servo_ok = input().strip().lower()
    if servo_ok != 'y':
        print("⚠ Check servo wiring and power supply")

except ImportError:
    print("✗ adafruit_servokit not installed")
    print("  Run: pip3 install --break-system-packages adafruit-circuitpython-servokit")
except Exception as e:
    print(f"✗ Servo error: {e}")

print()
input("Press Enter to continue to network test...")
print()

# Test 3: Network
print("Test 3: Network Connectivity")
print("-" * 50)

SERVER_URL = os.getenv("DETECTION_SERVER_URL", "http://localhost:5000/detect-person")
health_url = SERVER_URL.replace("/detect-person", "/health")

print(f"Server URL: {SERVER_URL}")
print(f"Testing connection to: {health_url}")
print()

try:
    response = requests.get(health_url, timeout=5)
    if response.status_code == 200:
        print(f"✓ Server is reachable! Response: {response.json()}")
    else:
        print(f"⚠ Server responded with status {response.status_code}")
except requests.exceptions.Timeout:
    print("✗ Connection timeout - check server IP address in .env")
except requests.exceptions.ConnectionError:
    print("✗ Cannot connect to server - is Flask server running?")
    print(f"  Check: {SERVER_URL}")
except Exception as e:
    print(f"✗ Network error: {e}")

print()
input("Press Enter to continue to integration test...")
print()

# Test 4: Full Integration
print("Test 4: Integration Test (Camera → Server → Servos)")
print("-" * 50)
print("This will capture an image and send it to the server...")
print()

try:
    # Capture image
    print("1. Capturing image...")
    result = subprocess.run(
        ['rpicam-still', '-o', '-', '-t', '1', '--width', '640', '--height', '480', '-n'],
        capture_output=True,
        timeout=5,
        check=True
    )
    image_data = result.stdout
    print(f"   ✓ Captured {len(image_data)} bytes")

    # Send to server
    print("2. Sending to detection server...")
    files = {'image': ('test.jpg', image_data, 'image/jpeg')}
    response = requests.post(SERVER_URL, files=files, timeout=10)
    response.raise_for_status()

    result = response.json()
    print(f"   ✓ Server response received")
    print()
    print(f"   Person detected: {result.get('person_detected')}")

    if result.get('person_detected'):
        servo_control = result.get('servo_control', {})
        print(f"   Servo commands: X={servo_control.get('x_steps')}° Y={servo_control.get('y_steps')}°")
        print(f"   Confidence: {result.get('detection_info', {}).get('confidence', 0):.2f}")

        # Test servo movement
        print()
        print("3. Testing servo movement with detection data...")
        from adafruit_servokit import ServoKit
        kit = ServoKit(channels=16)
        kit.servo[0].set_pulse_width_range(500, 2500)
        kit.servo[0].actuation_range = 270  # Horizontal has 270° rotation
        kit.servo[1].set_pulse_width_range(500, 2500)
        kit.servo[1].actuation_range = 180  # Vertical has 180° rotation

        current_x = SERVO_X_CENTER  # Horizontal center position
        current_y = SERVO_Y_CENTER  # Vertical center position

        # Servo speed constants (should match test configuration)
        SERVO_SPEED_DEG_PER_SEC = 180  # 270°/sec with 33% buffer
        MIN_MOVE_TIME = 0.15
        MAX_STEP_SIZE = 30  # Maximum degrees to move per update
        INTER_AXIS_DELAY = 0.5  # Delay between axes

        # Limit maximum step size to prevent sudden large movements
        x_steps = servo_control.get('x_steps', 0)
        y_steps = servo_control.get('y_steps', 0)
        x_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, x_steps))
        y_steps = max(-MAX_STEP_SIZE, min(MAX_STEP_SIZE, y_steps))

        # Calculate with limited steps
        # Horizontal (servo 0) - full 270° range
        # Vertical (servo 1) - clamped to ±15° from center
        # Apply inversion if configured in .env
        x_movement = -x_steps if SERVO_X_INVERT else x_steps
        y_movement = -y_steps if SERVO_Y_INVERT else y_steps

        new_x = max(0, min(270, current_x + x_movement))  # Horizontal
        new_y = max(SERVO_Y_MIN, min(SERVO_Y_MAX, current_y + y_movement))  # Vertical

        print(f"   Moving servos to X={new_x:.1f}° Y={new_y:.1f}°")

        print(f"   Limited steps: X={x_steps}° Y={y_steps}°")

        # Move X first, then Y (NEVER simultaneously to prevent tipping)
        if abs(new_x - current_x) > 0.5:
            kit.servo[0].angle = new_x
            move_time = max(MIN_MOVE_TIME, abs(new_x - current_x) / SERVO_SPEED_DEG_PER_SEC)
            time.sleep(move_time)
            time.sleep(INTER_AXIS_DELAY)  # Extended pause between axes

        if abs(new_y - current_y) > 0.5:
            kit.servo[1].angle = new_y
            move_time = max(MIN_MOVE_TIME, abs(new_y - current_y) / SERVO_SPEED_DEG_PER_SEC)
            time.sleep(move_time)
            time.sleep(0.2)  # Settling time

        print("   ✓ Servos moved to track person")
        print()
        print("✓✓✓ FULL INTEGRATION TEST PASSED! ✓✓✓")
    else:
        print("   No person detected in frame")
        print("   Stand in front of camera and try: python3 app.py")

except Exception as e:
    print(f"✗ Integration test failed: {e}")

print()
print("=" * 50)
print("Hardware Test Complete!")
print("=" * 50)
print()
print("If all tests passed, you're ready to run:")
print("  python3 app.py")
print()

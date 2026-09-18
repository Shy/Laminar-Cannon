"""Camera and servo activities, served by the Pi worker.

Two design points carry most of the weight here:

* Servo position is read back from the PCA9685's duty-cycle registers rather
  than tracked in software. That is what lets every scheduled workflow be a
  fresh, stateless execution, and it means a move that fails halfway leaves an
  accurate position for the next frame instead of a lie.
* The camera is never opened while the servos are moving. That is satisfied by
  construction: the per-frame child workflow runs these in sequence, and the
  parent awaits each child before starting the next.

These activities are deliberately synchronous, not ``async def``. They block
on ``subprocess.run`` and ``time.sleep``, and doing that inside the worker's
event loop stalls the SDK's own task handling - measured at roughly a second
of added latency per frame. Registered with a single-threaded executor, they
block a worker thread instead, which is harmless, and the thread count of one
preserves the serialisation the hardware needs.
"""

import base64
import time
from datetime import UTC, datetime

from temporalio import activity
from temporalio.exceptions import ApplicationError

from purifier.camera import CameraError, get_camera
from purifier.hardware import (
    BONNET_I2C_ADDRESS,
    CAPTURE_HEIGHT,
    CAPTURE_WIDTH,
    PAN_SERVO,
    REFERENCE_CLOCK_HZ,
    SERVO_FREQUENCY_HZ,
    TILT_SERVO,
    ServoSpec,
    duty_cycle_to_pulse_us,
    pulse_us_to_duty_cycle,
)
from purifier.pca9685 import PCA9685
from purifier.shared import CapturedFrame, MoveRequest, ServoAngles

#: Ramping is done in whole PCA9685 duty-cycle counts, not in degrees.
#:
#: The chip is 12-bit, so one count is 4.88 us: 0.659 degrees on the 270 deg
#: pan servo and 0.44 degrees on the 180 deg tilt servo. A step expressed in
#: degrees does not divide evenly into counts, so a 1 degree step became an
#: alternating 1-count, 2-count jump - visibly uneven during motion. Whole
#: counts keep every step identical, which is what makes it look smooth.
#:
#: Two counts per step gives 66 deg/s on pan and 44 deg/s on tilt: two thirds
#: of the original 100 deg/s, with steps of 1.32 and 0.88 degrees. The step
#: timing cannot go below one PWM period without targets arriving mid-servo
#: frame, so speed comes from step size rather than step rate. Drop back to 1
#: if the motion looks coarse.
RAMP_COUNTS_PER_STEP = 2

#: Seconds between ramp steps: exactly one PWM period at 50 Hz. Every new
#: target lands on a fresh servo frame, which is the best alignment available,
#: and it doubles the previous rate to about 33 deg/s on pan and 22 deg/s on
#: tilt. Safe to run this fast now that the base is clamped, travel is capped
#: at +/-45 degrees and the servos are no longer being over-volted.
RAMP_STEP_DELAY_SECONDS = 0.02

#: Seconds to wait after a move before returning, so the mechanism stops
#: oscillating before the next frame is captured. A frame taken mid-sway
#: yields a wrong offset and therefore a wrong correction, so this cannot go
#: to zero - but a clamped base settles far quicker than a free-standing one.
SETTLE_AFTER_MOVE_SECONDS = 0.2

#: Cached PCA9685 driver, opened once per worker process. Reopening the I2C
#: bus on every activity is slow and can leave the bus in a bad state.
_pca: PCA9685 | None = None


def _open_pca() -> PCA9685:
    """Open, configure and cache the PCA9685 driver.

    The chip powers up asleep with a prescale of 30 (about 197 Hz), so the
    frequency must be set before any servo will respond. Setting it does not
    disturb channel registers, so servos already holding a position keep it.

    :returns: The configured driver.
    :raises ApplicationError: Non-retryable, if the I2C bus or the bonnet is
        absent. No number of retries will conjure up hardware.
    """
    global _pca
    if _pca is not None:
        return _pca

    try:
        driver = PCA9685(
            bus_number=1,
            address=BONNET_I2C_ADDRESS,
            reference_clock_hz=REFERENCE_CLOCK_HZ,
        )
        driver.configure()
        if abs(driver.frequency - SERVO_FREQUENCY_HZ) > 1.0:
            driver.frequency = SERVO_FREQUENCY_HZ
    except (OSError, ValueError) as bus_error:
        raise ApplicationError(
            f"cannot reach the Servo Bonnet at 0x{BONNET_I2C_ADDRESS:02x}: "
            f"{bus_error}. Check I2C is enabled and `i2cdetect -y 1` shows 40.",
            non_retryable=True,
        ) from bus_error

    _pca = driver
    return _pca


def _read_angle(pca: PCA9685, spec: ServoSpec) -> float | None:
    """Read one servo's current angle back from the chip.

    :param pca: The PCA9685 instance.
    :param spec: Which servo to read.
    :returns: The angle in degrees, or ``None`` if the channel is not being
        driven, which means the position is genuinely unknown.
    """
    duty_cycle = pca.get_duty_cycle(spec.channel)
    if duty_cycle == 0:
        return None
    return spec.pulse_us_to_angle(duty_cycle_to_pulse_us(duty_cycle))


def _write_angle(pca: PCA9685, spec: ServoSpec, angle: float) -> None:
    """Drive one servo to an angle.

    :param pca: The PCA9685 instance.
    :param spec: Which servo to drive.
    :param angle: Target angle, clamped to the axis's permitted travel.
    """
    clamped = spec.clamp(angle)
    pulse_us = spec.angle_to_pulse_us(clamped)
    pca.set_duty_cycle(spec.channel, pulse_us_to_duty_cycle(pulse_us))


def _ramp_to(pca: PCA9685, spec: ServoSpec, start: float, target: float) -> float:
    """Move a servo from ``start`` to ``target`` one duty-cycle count at a time.

    Working in counts rather than degrees matters: a step sized in degrees does
    not divide evenly into the chip's 12-bit counts, so it lands as an
    alternating one-then-two count jump that the servo renders as shaking.

    :param pca: The PCA9685 instance.
    :param spec: Which servo to move.
    :param start: Angle the servo is currently at.
    :param target: Angle to reach, clamped to permitted travel.
    :returns: The clamped target angle.
    """
    clamped_target = spec.clamp(target)
    start_duty = pulse_us_to_duty_cycle(spec.angle_to_pulse_us(spec.clamp(start)))
    target_duty = pulse_us_to_duty_cycle(spec.angle_to_pulse_us(clamped_target))

    step = RAMP_COUNTS_PER_STEP if target_duty >= start_duty else -RAMP_COUNTS_PER_STEP
    duty = start_duty
    while duty != target_duty:
        remaining = target_duty - duty
        duty += step if abs(remaining) >= abs(step) else remaining
        pca.set_duty_cycle(spec.channel, duty)
        time.sleep(RAMP_STEP_DELAY_SECONDS)

    return clamped_target


@activity.defn
def capture_frame() -> CapturedFrame:
    """Capture one JPEG frame and return it base64-encoded.

    Uses a persistent camera process signalled per frame. Starting
    ``rpicam-still`` fresh each time cost about 960 ms, nearly all of it
    process startup and opening the sensor; signalling a running process takes
    about 310 ms. See :mod:`purifier.camera`.

    :returns: The captured frame, ready to cross the wire.
    :raises ApplicationError: Non-retryable if the capture helper is missing,
        retryable if a capture fails after the camera has been restarted.
    """
    try:
        jpeg_bytes = get_camera().capture()
    except CameraError as camera_error:
        message = str(camera_error)
        if "not found" in message:
            raise ApplicationError(message, non_retryable=True) from camera_error
        raise ApplicationError(f"capture failed: {message}") from camera_error

    activity.logger.info(
        "captured %dx%d, %d bytes", CAPTURE_WIDTH, CAPTURE_HEIGHT, len(jpeg_bytes)
    )
    return CapturedFrame(
        jpeg_base64=base64.b64encode(jpeg_bytes).decode("ascii"),
        width=CAPTURE_WIDTH,
        height=CAPTURE_HEIGHT,
        # Exposure is not reported per capture in signal mode. Auto-exposure
        # runs continuously with the camera open, which is why frames are
        # better exposed than the old fixed warm-up managed.
        exposure_us=0.0,
        captured_at=datetime.now(UTC).isoformat(),
    )


@activity.defn
def move_servos(request: MoveRequest) -> ServoAngles:
    """Apply a relative move and report the resulting angles.

    Current position is read back from the chip, so the delta is applied to
    where the servos actually are. If a channel is not being driven the
    position is unknown, and the servo is homed to the centre of its travel
    first and the requested delta applied from there.

    :param request: The relative move to apply, in degrees.
    :returns: Angles after the move, read back from the chip.
    """
    pca = _open_pca()

    homed = False
    current_pan = _read_angle(pca, PAN_SERVO)
    current_tilt = _read_angle(pca, TILT_SERVO)

    if current_pan is None or current_tilt is None:
        homed = True
        current_pan = PAN_SERVO.centre_angle if current_pan is None else current_pan
        current_tilt = TILT_SERVO.centre_angle if current_tilt is None else current_tilt
        activity.logger.info(
            "servo registers read zero; homing to pan %.1f tilt %.1f",
            current_pan,
            current_tilt,
        )
        _write_angle(pca, PAN_SERVO, current_pan)
        _write_angle(pca, TILT_SERVO, current_tilt)
        time.sleep(0.5)

    final_pan = _ramp_to(
        pca, PAN_SERVO, current_pan, current_pan + request.delta_pan_degrees
    )
    final_tilt = _ramp_to(
        pca, TILT_SERVO, current_tilt, current_tilt + request.delta_tilt_degrees
    )
    time.sleep(SETTLE_AFTER_MOVE_SECONDS)

    # Read back rather than trusting the commanded value, so the result
    # reflects what the hardware actually holds.
    read_pan = _read_angle(pca, PAN_SERVO)
    read_tilt = _read_angle(pca, TILT_SERVO)

    angles = ServoAngles(
        pan_degrees=round(read_pan if read_pan is not None else final_pan, 2),
        tilt_degrees=round(read_tilt if read_tilt is not None else final_tilt, 2),
        homed=homed,
    )
    activity.logger.info(
        "moved pan %+.1f to %.1f, tilt %+.1f to %.1f",
        request.delta_pan_degrees,
        angles.pan_degrees,
        request.delta_tilt_degrees,
        angles.tilt_degrees,
    )
    return angles


@activity.defn
def read_servo_angles() -> ServoAngles:
    """Report current servo angles without moving anything.

    Useful for the diagnostic path and for confirming the read-back maths
    against a physical protractor during calibration.

    :returns: Current angles; unknown axes report their centre with
        ``homed`` false, since nothing was driven.
    """
    pca = _open_pca()
    pan = _read_angle(pca, PAN_SERVO)
    tilt = _read_angle(pca, TILT_SERVO)
    return ServoAngles(
        pan_degrees=round(pan, 2) if pan is not None else PAN_SERVO.centre_angle,
        tilt_degrees=round(tilt, 2) if tilt is not None else TILT_SERVO.centre_angle,
        homed=False,
    )

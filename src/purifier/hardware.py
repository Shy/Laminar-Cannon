"""Servo and camera hardware definitions for the Pi, and the angle maths.

This module is the single home for the servo calibration constants. The
diagnostic CLI in :mod:`purifier.servo_tool` imports them from here, so there
is exactly one place to retune pulse widths.

The PCA9685's duty-cycle registers are the source of truth for servo position.
Reading them back is what lets every scheduled workflow be stateless, so the
conversions here are the load-bearing part of that design.
"""

from dataclasses import dataclass

#: I2C address of the Servo Bonnet with no address jumpers soldered.
BONNET_I2C_ADDRESS = 0x40

#: Standard hobby servo update rate, in Hz.
SERVO_FREQUENCY_HZ = 50

#: PCA9685 internal oscillator, in Hz.
REFERENCE_CLOCK_HZ = 25_000_000

#: PWM period at :data:`SERVO_FREQUENCY_HZ`, in microseconds.
PWM_PERIOD_US = 1_000_000 // SERVO_FREQUENCY_HZ

#: Full scale of the PCA9685's 16-bit duty cycle representation.
DUTY_CYCLE_FULL_SCALE = 0xFFFF


@dataclass(frozen=True)
class ServoSpec:
    """Calibration for one servo channel.

    :param channel: PCA9685 output channel index.
    :param name: Label used in logs and activity results.
    :param actuation_range: Total mechanical travel in degrees.
    :param min_pulse_us: Pulse width in microseconds at 0 degrees.
    :param max_pulse_us: Pulse width in microseconds at ``actuation_range``.
    :param min_angle: Lowest angle this axis is allowed to reach.
    :param max_angle: Highest angle this axis is allowed to reach.
    """

    channel: int
    name: str
    actuation_range: int
    min_pulse_us: int
    max_pulse_us: int
    min_angle: float
    max_angle: float

    @property
    def centre_angle(self) -> float:
        """Midpoint of the permitted travel, used as the home position.

        :returns: Angle halfway between ``min_angle`` and ``max_angle``.
        """
        return (self.min_angle + self.max_angle) / 2

    def clamp(self, angle: float) -> float:
        """Limit an angle to this axis's permitted travel.

        :param angle: Requested angle in degrees.
        :returns: Angle clamped to ``min_angle..max_angle``.
        """
        return max(self.min_angle, min(self.max_angle, angle))

    def angle_to_pulse_us(self, angle: float) -> float:
        """Convert an angle to its pulse width.

        :param angle: Angle in degrees, within the actuation range.
        :returns: Pulse width in microseconds.
        """
        span = self.max_pulse_us - self.min_pulse_us
        return self.min_pulse_us + span * (angle / self.actuation_range)

    def pulse_us_to_angle(self, pulse_us: float) -> float:
        """Convert a pulse width back to an angle.

        :param pulse_us: Pulse width in microseconds.
        :returns: Angle in degrees, not clamped, so an out-of-range register
            value is visible to the caller rather than silently corrected.
        """
        span = self.max_pulse_us - self.min_pulse_us
        return (pulse_us - self.min_pulse_us) / span * self.actuation_range


#: Tilt axis: channel 0, the 25 kg-cm 180 degree servo.
#:
#: This is NOT the axis the design originally assigned to channel 0. Measured
#: on the assembled rig: commanding channel 0 pitches the camera up and down.
#: The build decided the mapping, so the code follows the build.
#:
#: Travel is restricted to +/-45 degrees around centre. The servo can do far
#: more, but a wide fast swing puts enough reaction torque into the mount to
#: topple the stand - which is not hypothetical, it happened.
TILT_SERVO = ServoSpec(
    channel=0,
    name="tilt",
    actuation_range=180,
    min_pulse_us=500,
    max_pulse_us=2500,
    min_angle=45.0,
    max_angle=135.0,
)

#: Pan axis: channel 1, the 20 kg-cm 270 degree servo.
#:
#: Measured: commanding channel 1 rotates the camera left and right. Given the
#: same +/-45 degree window around centre as tilt, so both axes behave alike
#: and neither can swing far enough to unbalance the mount.
PAN_SERVO = ServoSpec(
    channel=1,
    name="pan",
    actuation_range=270,
    min_pulse_us=500,
    max_pulse_us=2500,
    min_angle=90.0,
    max_angle=180.0,
)

# --- Camera ------------------------------------------------------------------

#: Capture helper shipped with Raspberry Pi OS.
RPICAM_STILL = "rpicam-still"

#: Forced sensor mode. Without this, libcamera picks the cropped 1536x864 mode
#: for small outputs and silently throws away up to half the horizontal field
#: of view, which makes the tracker lose people far sooner.
SENSOR_MODE = "2304:1296"

#: Capture size. Full field of view at ~800 ms. JPEG size tracks scene detail,
#: not dimensions: ~33 KB of blank wall, ~110 KB of a real cluttered room
#: (~147 KB once base64-encoded). Wider output costs payload, not time.
CAPTURE_WIDTH = 768
CAPTURE_HEIGHT = 432

#: Fixed focus distance in dioptres (~0.63 m). Autofocus needs ~2.5 s to lock,
#: which does not fit the loop, and pose detection tolerates a soft frame.
LENS_POSITION_DIOPTRES = 1.6

#: Milliseconds of sensor warm-up before the frame is taken. Capture is about
#: 70 percent of the whole pipeline, so this is the most valuable millisecond
#: to trim. 150 ms still lets auto exposure converge; going to --immediate
#: saves another 100 ms but risks a badly exposed frame, and a dark frame
#: costs a whole detection.
CAPTURE_SETTLE_MS = 150

#: In-pipeline rotation, in degrees, undoing the camera's mounting on the
#: purifier frame. Measured: the module is mounted inverted, so the raw sensor
#: frame comes out upside down.
#:
#: libcamera only supports 0 and 180, which is exactly enough here. Rotating
#: real pixels is better than an EXIF tag: nothing downstream has to honour a
#: tag, and 180 degrees preserves width and height, so no coordinate maths
#: changes. Measured cost: none (810 ms with, 832 ms without).
CAPTURE_ROTATION_DEGREES = 180


def duty_cycle_to_pulse_us(duty_cycle: int) -> float:
    """Convert a PCA9685 duty-cycle register value to a pulse width.

    :param duty_cycle: 16-bit duty cycle as read from a channel.
    :returns: Pulse width in microseconds.
    """
    return duty_cycle / DUTY_CYCLE_FULL_SCALE * PWM_PERIOD_US


def pulse_us_to_duty_cycle(pulse_us: float) -> int:
    """Convert a pulse width to a PCA9685 duty-cycle register value.

    :param pulse_us: Pulse width in microseconds.
    :returns: 16-bit duty cycle, clamped to the representable range.
    """
    raw = round(pulse_us / PWM_PERIOD_US * DUTY_CYCLE_FULL_SCALE)
    return max(0, min(DUTY_CYCLE_FULL_SCALE, raw))

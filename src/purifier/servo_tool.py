"""Servo diagnostics and calibration CLI, run on the Pi.

This is the single servo tool. It replaces the earlier standalone
``servo_test.py``, which imported Adafruit Blinka: Blinka's ``board`` module
pulls in a GPIO backend even for I2C-only use, and that backend cannot be
built on this Pi without ``swig`` and root. Consolidating also removes a real
hazard, which is that the calibration constants existed in two places and
could silently drift apart.

Everything here goes through the same code the tracking workflow uses, so
what it reports is what the tracker sees. Moves are relative by default;
nothing jumps to an absolute angle unless asked, because the travel limits
are still estimates and an assembled mount can reach a mechanical stop.
"""

import argparse
import contextlib
import time

from temporalio.testing import ActivityEnvironment

from purifier.activities_pi import (
    _open_pca,
    _ramp_to,
    _read_angle,
    _write_angle,
    move_servos,
    read_servo_angles,
)
from purifier.hardware import PAN_SERVO, TILT_SERVO, ServoSpec, duty_cycle_to_pulse_us
from purifier.shared import MoveRequest

#: Axes in a stable order for reporting and sweeping.
AXES: tuple[ServoSpec, ...] = (PAN_SERVO, TILT_SERVO)

#: Seconds to pause at each end of a sweep.
SWEEP_PAUSE_SECONDS = 0.4


def report_registers() -> None:
    """Print raw register values alongside the derived angles.

    Showing duty cycle and pulse width next to the angle makes a wrong
    calibration constant obvious, instead of hiding it behind a plausible
    looking angle.
    """
    pca = _open_pca()
    print(f"PCA9685 frequency: {pca.frequency:.2f} Hz")
    for spec in AXES:
        duty = pca.get_duty_cycle(spec.channel)
        pulse_us = duty_cycle_to_pulse_us(duty)
        angle = _read_angle(pca, spec)
        rendered = (
            "not driven (position unknown)" if angle is None else f"{angle:.2f} deg"
        )
        print(f"{spec.name:5s} ch{spec.channel}  duty={duty:5d}  {pulse_us:7.1f} us")
        print(f"        angle = {rendered}")
        print(
            f"        limits {spec.min_angle:.0f}-{spec.max_angle:.0f}, "
            f"centre {spec.centre_angle:.0f}, full range {spec.actuation_range}"
        )


def centre_all() -> None:
    """Drive both axes to the middle of their permitted travel."""
    pca = _open_pca()
    for spec in AXES:
        current = _read_angle(pca, spec)
        if current is None:
            print(f"{spec.name}: position unknown, commanding {spec.centre_angle:.1f}")
            _write_angle(pca, spec, spec.centre_angle)
            time.sleep(0.5)
            continue
        print(f"{spec.name}: {current:.1f} -> {spec.centre_angle:.1f}")
        _ramp_to(pca, spec, current, spec.centre_angle)


def sweep(spec: ServoSpec) -> None:
    """Sweep one axis across its permitted travel and return to centre.

    :param spec: Which axis to sweep.
    """
    pca = _open_pca()
    current = _read_angle(pca, spec)
    if current is None:
        _write_angle(pca, spec, spec.centre_angle)
        time.sleep(0.5)
        current = spec.centre_angle

    for target in (spec.min_angle, spec.max_angle, spec.centre_angle):
        print(f"{spec.name}: {current:.1f} -> {target:.1f}")
        current = _ramp_to(pca, spec, current, target)
        time.sleep(SWEEP_PAUSE_SECONDS)


def release_all() -> None:
    """Stop driving both channels so the servos go limp."""
    pca = _open_pca()
    for spec in AXES:
        pca.set_duty_cycle(spec.channel, 0)
    print("both channels released; position is now unknown until next move")


def nudge(delta_pan: float, delta_tilt: float) -> None:
    """Apply a relative move through the real activity and report the result.

    This is the direction calibration path: nudge one axis, see which way the
    camera actually turns, and set ``PAN_DIRECTION`` or ``TILT_DIRECTION``
    from evidence.

    :param delta_pan: Pan change in degrees.
    :param delta_tilt: Tilt change in degrees.
    """
    # The activities are synchronous, so ActivityEnvironment.run returns the
    # value directly rather than a coroutine.
    environment = ActivityEnvironment()
    before = environment.run(read_servo_angles)
    print(f"before:    pan={before.pan_degrees:.2f} tilt={before.tilt_degrees:.2f}")

    after = environment.run(
        move_servos,
        MoveRequest(delta_pan_degrees=delta_pan, delta_tilt_degrees=delta_tilt),
    )
    print(f"requested: pan {delta_pan:+.2f} tilt {delta_tilt:+.2f}")
    print(f"after:     pan={after.pan_degrees:.2f} tilt={after.tilt_degrees:.2f}")
    if after.homed:
        print("note: servos were homed first, position had been unknown")

    actual_pan = after.pan_degrees - before.pan_degrees
    actual_tilt = after.tilt_degrees - before.tilt_degrees
    print(f"actual:    pan {actual_pan:+.2f} tilt {actual_tilt:+.2f}")
    if abs(actual_pan - delta_pan) > 1.0 or abs(actual_tilt - delta_tilt) > 1.0:
        print("warning: actual differs from requested, a travel limit was hit")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.
    :returns: Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--read", action="store_true", help="report angles and registers (default)"
    )
    mode.add_argument("--centre", action="store_true", help="centre both axes")
    mode.add_argument(
        "--sweep",
        choices=("pan", "tilt", "both"),
        help="sweep an axis across its permitted travel",
    )
    mode.add_argument(
        "--release", action="store_true", help="stop driving both channels"
    )
    parser.add_argument(
        "--pan", type=float, default=0.0, metavar="DEG", help="relative pan move"
    )
    parser.add_argument(
        "--tilt", type=float, default=0.0, metavar="DEG", help="relative tilt move"
    )
    return parser.parse_args(argv)


def main() -> None:
    """Entry point."""
    args = parse_args()

    if args.centre:
        centre_all()
    elif args.release:
        release_all()
    elif args.sweep:
        targets = (
            AXES
            if args.sweep == "both"
            else (PAN_SERVO if args.sweep == "pan" else TILT_SERVO,)
        )
        for spec in targets:
            sweep(spec)
    elif args.pan != 0.0 or args.tilt != 0.0:
        nudge(args.pan, args.tilt)
    else:
        report_registers()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()

"""Tests for servo angle maths and PCA9685 register conversions.

These conversions are what make the stateless-workflow design work: position
is recovered by reading registers back, so a rounding error here would show
up as the camera slowly drifting off target.
"""

import pytest

from purifier.hardware import (
    PAN_SERVO,
    TILT_SERVO,
    ServoSpec,
    duty_cycle_to_pulse_us,
    pulse_us_to_duty_cycle,
)

ALL_SERVOS = [PAN_SERVO, TILT_SERVO]


@pytest.mark.parametrize("spec", ALL_SERVOS, ids=lambda spec: spec.name)
def test_angle_pulse_round_trip(spec: ServoSpec) -> None:
    for angle in (0.0, 45.0, 90.0, float(spec.actuation_range)):
        pulse_us = spec.angle_to_pulse_us(angle)
        assert spec.pulse_us_to_angle(pulse_us) == pytest.approx(angle, abs=1e-6)


@pytest.mark.parametrize("spec", ALL_SERVOS, ids=lambda spec: spec.name)
def test_pulse_endpoints_match_calibration(spec: ServoSpec) -> None:
    assert spec.angle_to_pulse_us(0.0) == pytest.approx(spec.min_pulse_us)
    assert spec.angle_to_pulse_us(spec.actuation_range) == pytest.approx(
        spec.max_pulse_us
    )


@pytest.mark.parametrize("spec", ALL_SERVOS, ids=lambda spec: spec.name)
def test_clamp_respects_travel_limits(spec: ServoSpec) -> None:
    assert spec.clamp(-999.0) == spec.min_angle
    assert spec.clamp(999.0) == spec.max_angle
    assert spec.clamp(spec.centre_angle) == spec.centre_angle


@pytest.mark.parametrize("spec", ALL_SERVOS, ids=lambda spec: spec.name)
def test_centre_is_within_limits(spec: ServoSpec) -> None:
    assert spec.min_angle <= spec.centre_angle <= spec.max_angle


def test_duty_cycle_round_trip_is_accurate_enough() -> None:
    # The 16-bit register quantises to ~0.3 us, which is far finer than any
    # servo's deadband, so a round trip must not drift by a visible angle.
    for pulse_us in (500.0, 1000.0, 1500.0, 2000.0, 2500.0):
        duty = pulse_us_to_duty_cycle(pulse_us)
        assert duty_cycle_to_pulse_us(duty) == pytest.approx(pulse_us, abs=0.5)


def test_duty_cycle_is_clamped_to_register_width() -> None:
    assert pulse_us_to_duty_cycle(-100.0) == 0
    assert pulse_us_to_duty_cycle(1e9) == 0xFFFF


def test_zero_duty_cycle_means_unknown_position() -> None:
    # capture/move rely on 0 meaning "not driven", which is how an unhomed
    # servo is detected after a power cycle.
    assert duty_cycle_to_pulse_us(0) == 0.0
    assert PAN_SERVO.pulse_us_to_angle(0.0) < PAN_SERVO.min_angle

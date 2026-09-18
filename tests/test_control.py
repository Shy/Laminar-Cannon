"""Tests for the control law that turns pixel offsets into servo moves."""

from purifier.shared import (
    CENTRE_DEADBAND,
    MAX_STEP_DEGREES,
    PAN_DEGREES_PER_OFFSET,
    PAN_DIRECTION,
    PROPORTIONAL_GAIN,
    TILT_DEGREES_PER_OFFSET,
    TILT_DIRECTION,
    Detection,
)
from purifier.workflows import compute_move


def _detection(offset_x: float, offset_y: float) -> Detection:
    """Build a Detection with only the offsets that matter here.

    :param offset_x: Normalised horizontal offset.
    :param offset_y: Normalised vertical offset.
    :returns: A Detection suitable for control-law tests.
    """
    return Detection(
        offset_x=offset_x,
        offset_y=offset_y,
        confidence=0.9,
        people_detected=1,
        aim_point="eyes",
        box_area_fraction=0.16,
    )


def test_dead_centre_produces_no_move() -> None:
    assert compute_move(_detection(0.0, 0.0)) is None


def test_inside_deadband_produces_no_move() -> None:
    just_inside = CENTRE_DEADBAND * 0.9
    assert compute_move(_detection(just_inside, -just_inside)) is None


def test_outside_deadband_on_one_axis_still_moves() -> None:
    move = compute_move(_detection(0.5, 0.0))
    assert move is not None
    assert move.delta_pan_degrees != 0.0
    # The axis inside the deadband must not be nudged.
    assert move.delta_tilt_degrees == 0.0


def test_gain_undershoots_deliberately() -> None:
    # A full half-frame error must command only a fraction of the travel that
    # would fully correct it: the loop is slow, and a gain at or above 1.0
    # would hunt instead of settling.
    move = compute_move(_detection(1.0, 0.0))
    assert move is not None
    expected = PROPORTIONAL_GAIN * PAN_DEGREES_PER_OFFSET * PAN_DIRECTION
    assert move.delta_pan_degrees == round(expected, 2)
    assert abs(move.delta_pan_degrees) < PAN_DEGREES_PER_OFFSET


def test_correction_drives_camera_toward_target() -> None:
    # Measured on the rig: a positive pan command turns the camera LEFT, and a
    # positive tilt command pitches it DOWN. So a target right of centre needs
    # a negative pan command, and a target below centre a positive tilt one.
    # Getting either sign wrong makes the camera flee the person.
    move = compute_move(_detection(0.8, 0.8))
    assert move is not None
    assert move.delta_pan_degrees < 0, "target right of centre needs negative pan"
    assert move.delta_tilt_degrees > 0, "target below centre needs positive tilt"


def test_axes_use_their_own_measured_scale() -> None:
    # The axes have different optics per degree, so one shared scale would
    # leave one axis sluggish and the other prone to oscillation.
    assert PAN_DEGREES_PER_OFFSET != TILT_DEGREES_PER_OFFSET
    move = compute_move(_detection(1.0, 1.0))
    assert move is not None
    assert move.delta_pan_degrees == round(
        PROPORTIONAL_GAIN * PAN_DEGREES_PER_OFFSET * PAN_DIRECTION, 2
    )
    assert move.delta_tilt_degrees == round(
        PROPORTIONAL_GAIN * TILT_DEGREES_PER_OFFSET * TILT_DIRECTION, 2
    )


def test_sign_follows_offset() -> None:
    right = compute_move(_detection(0.8, 0.0))
    left = compute_move(_detection(-0.8, 0.0))
    assert right is not None and left is not None
    assert right.delta_pan_degrees == -left.delta_pan_degrees


def test_step_is_clamped() -> None:
    # An absurd offset must still be bounded by the per-frame clamp.
    move = compute_move(_detection(50.0, -50.0))
    assert move is not None
    assert abs(move.delta_pan_degrees) <= MAX_STEP_DEGREES
    assert abs(move.delta_tilt_degrees) <= MAX_STEP_DEGREES


def test_formula_matches_measured_frame() -> None:
    # Offsets from a real captured frame: a person left of and slightly above
    # centre. Expected values are derived from the constants rather than
    # hardcoded, so retuning the gain does not break this - but changing the
    # shape of the formula still will, which is what is worth guarding.
    offset_x, offset_y = -0.711, -0.172
    move = compute_move(_detection(offset_x, offset_y))
    assert move is not None
    assert move.delta_pan_degrees == round(
        offset_x * PROPORTIONAL_GAIN * PAN_DEGREES_PER_OFFSET * PAN_DIRECTION, 2
    )
    assert move.delta_tilt_degrees == round(
        offset_y * PROPORTIONAL_GAIN * TILT_DEGREES_PER_OFFSET * TILT_DIRECTION, 2
    )
    # Pan positive: a positive command turns the camera left, toward them.
    assert move.delta_pan_degrees > 0
    # Tilt negative: a negative command pitches up, toward them.
    assert move.delta_tilt_degrees < 0


def test_gain_stays_in_a_stable_range() -> None:
    # At or above 1.0 the loop hunts instead of settling. Guards a future
    # "just make it snappier" edit from quietly making it oscillate.
    assert 0.0 < PROPORTIONAL_GAIN < 1.0


def test_max_step_still_clamps() -> None:
    # MAX_STEP_DEGREES survives as a safety clamp even though it no longer
    # sets the working scale.
    move = compute_move(_detection(10.0, 10.0))
    assert move is not None
    assert abs(move.delta_pan_degrees) == MAX_STEP_DEGREES
    assert abs(move.delta_tilt_degrees) == MAX_STEP_DEGREES

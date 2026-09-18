"""End-to-end tests for the parent loop and the per-frame child workflow.

These exist because of a bug the unit tests structurally could not catch.
Activities are dispatched by string name - importing the Pi activity module
into the workflow would drag smbus2 onto the MacBook - and a string carries no
type information, so without an explicit ``result_type`` the SDK hands back a
raw dict. Every attribute access then fails at runtime while the type
annotations look correct and mypy stays silent.

Testing ``compute_move`` in isolation passes real dataclasses in, so it can
never see this. Only running the workflows through a server can.
"""

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from purifier.shared import (
    MAC_TASK_QUEUE,
    PI_TASK_QUEUE,
    CapturedFrame,
    Detection,
    DetectionResult,
    MoveRequest,
    ServoAngles,
    TrackerConfig,
    TrackerState,
)
from purifier.workflows import TrackFrame, TrackPerson

#: Moves the control law asked for, so tests can assert on them.
_requested_moves: list[MoveRequest] = []

#: Detection the stub returns; a test sets this before running.
_next_detection: list[DetectionResult] = []

#: Frame indices on which capture should fail, to exercise resilience.
_fail_captures_before: list[int] = [0]

#: How many times capture has been called this test.
_capture_calls: list[int] = [0]


def _reset() -> None:
    _requested_moves.clear()
    _next_detection.clear()
    _fail_captures_before[0] = 0
    _capture_calls[0] = 0


@activity.defn(name="capture_frame")
async def stub_capture_frame() -> CapturedFrame:
    """Return a frame without touching a camera, failing if asked to.

    :returns: A minimal frame; the stub detector ignores its contents.
    :raises ApplicationError: While the configured failure budget lasts.
    """
    _capture_calls[0] += 1
    if _capture_calls[0] <= _fail_captures_before[0]:
        raise ApplicationError("simulated camera failure", non_retryable=True)
    return CapturedFrame(
        jpeg_base64="",
        width=768,
        height=432,
        exposure_us=2900.0,
        captured_at="2026-09-18T17:43:12+00:00",
    )


@activity.defn(name="detect_person")
async def stub_detect_person(frame: CapturedFrame) -> DetectionResult:
    """Return the detection the test queued.

    :param frame: Must arrive as a real CapturedFrame, which is itself part
        of what these tests verify.
    :returns: The queued detection result.
    """
    assert isinstance(frame, CapturedFrame), (
        "activity received a dict, not CapturedFrame - the workflow is "
        "missing an argument type"
    )
    return _next_detection[-1]


@activity.defn(name="move_servos")
async def stub_move_servos(request: MoveRequest) -> ServoAngles:
    """Record the requested move and report plausible angles.

    :param request: The move the workflow computed.
    :returns: Angles as if the move had been applied.
    """
    assert isinstance(request, MoveRequest), (
        "activity received a dict, not MoveRequest - the workflow is "
        "missing an argument type"
    )
    _requested_moves.append(request)
    return ServoAngles(pan_degrees=138.83, tilt_degrees=85.96, homed=False)


@pytest.fixture
async def environment():
    """Start a time-skipping Temporal test server.

    :returns: The running environment.
    """
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yield env
    except Exception as exc:
        pytest.skip(f"test server unavailable: {exc}")


def _workers(client: Client) -> tuple[Worker, Worker]:
    """Build the two workers the workflows need.

    Two are required because the workflows pin camera and servo activities to
    the Pi task queue, exactly as in production.

    :param client: Client for the test environment.
    :returns: The Mac-side and Pi-side workers.
    """
    return (
        Worker(
            client,
            task_queue=MAC_TASK_QUEUE,
            workflows=[TrackPerson, TrackFrame],
            activities=[stub_detect_person],
        ),
        Worker(
            client,
            task_queue=PI_TASK_QUEUE,
            activities=[stub_capture_frame, stub_move_servos],
        ),
    )


async def _run_loop(client: Client, config: TrackerConfig) -> TrackerState:
    """Run the parent loop to completion.

    :param client: Client for the test environment.
    :param config: Loop parameters; must bound the run somehow.
    :returns: Final counters.
    """
    mac_worker, pi_worker = _workers(client)
    async with mac_worker, pi_worker:
        return await client.execute_workflow(
            TrackPerson.run,
            args=[config, None],
            id="test-track-person",
            task_queue=MAC_TASK_QUEUE,
        )


def _detection(offset_x: float, offset_y: float) -> DetectionResult:
    return DetectionResult(
        detection=Detection(
            offset_x=offset_x,
            offset_y=offset_y,
            confidence=0.93,
            people_detected=1,
            aim_point="eyes",
            box_area_fraction=0.16,
        ),
        inference_ms=31.0,
    )


@pytest.mark.asyncio
async def test_offcentre_person_produces_a_move(environment) -> None:
    _reset()
    _next_detection.append(_detection(-0.48, -0.45))

    state = await _run_loop(environment.client, TrackerConfig(stop_after_frames=1))

    assert state.frames_processed == 1
    assert state.moves_made == 1
    assert len(_requested_moves) == 1
    # Person left of and above centre: pan positive turns the camera left,
    # tilt negative pitches it up. Both must point at the person.
    assert _requested_moves[0].delta_pan_degrees > 0
    assert _requested_moves[0].delta_tilt_degrees < 0


@pytest.mark.asyncio
async def test_centred_person_skips_the_move_activity(environment) -> None:
    _reset()
    _next_detection.append(_detection(0.01, -0.02))

    state = await _run_loop(environment.client, TrackerConfig(stop_after_frames=1))

    assert state.moves_made == 0
    assert _requested_moves == [], "inside the deadband nothing should move"


@pytest.mark.asyncio
async def test_no_person_holds_position(environment) -> None:
    _reset()
    _next_detection.append(DetectionResult(detection=None, inference_ms=28.0))

    state = await _run_loop(environment.client, TrackerConfig(stop_after_frames=1))

    assert state.frames_without_person == 1
    assert _requested_moves == []


@pytest.mark.asyncio
async def test_loop_survives_a_failed_frame(environment) -> None:
    # The whole point of the child-per-frame split: one bad capture must cost
    # a single frame, not the tracker.
    _reset()
    _next_detection.append(_detection(-0.48, -0.45))
    _fail_captures_before[0] = 1

    state = await _run_loop(environment.client, TrackerConfig(stop_after_frames=3))

    assert state.frames_processed == 3
    assert state.frames_failed == 1
    assert state.moves_made == 2, "frames after the failure must still work"
    assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_persistent_failure_eventually_fails_the_loop(environment) -> None:
    # A rig that never recovers should surface as a failed workflow, not a
    # loop quietly spinning on nothing.
    _reset()
    _next_detection.append(_detection(-0.48, -0.45))
    _fail_captures_before[0] = 99

    with pytest.raises(WorkflowFailureError) as caught:
        await _run_loop(
            environment.client,
            TrackerConfig(stop_after_frames=20, max_consecutive_failures=3),
        )

    # WorkflowFailureError's own message is generic ("Workflow execution
    # failed"); the reason lives on the cause.
    messages = []
    cause: BaseException | None = caught.value
    while cause is not None:
        messages.append(str(cause))
        cause = cause.__cause__
    assert any("consecutive frames failed" in message for message in messages), (
        f"gave up for the wrong reason: {messages}"
    )


@pytest.mark.asyncio
async def test_continue_as_new_bounds_history(environment) -> None:
    # Without this the parent's history grows for as long as the loop runs.
    _reset()
    _next_detection.append(_detection(0.01, -0.02))

    state = await _run_loop(
        environment.client,
        TrackerConfig(frames_per_run=2, stop_after_frames=5),
    )

    assert state.frames_processed == 5
    assert state.generation >= 2, "loop should have continued as new"


@pytest.mark.asyncio
async def test_stop_signal_exits_cleanly(environment) -> None:
    _reset()
    _next_detection.append(_detection(0.01, -0.02))

    mac_worker, pi_worker = _workers(environment.client)
    async with mac_worker, pi_worker:
        handle = await environment.client.start_workflow(
            TrackPerson.run,
            args=[TrackerConfig(), None],
            id="test-track-person-stop",
            task_queue=MAC_TASK_QUEUE,
        )
        await handle.signal(TrackPerson.stop)
        state = await handle.result()

    # Exits cleanly rather than being terminated, so no frame is abandoned
    # mid-ramp with the servos still moving.
    assert isinstance(state, TrackerState)

"""The tracking loop, as a parent workflow driving one child per frame.

``TrackPerson`` is the long-running parent: it loops, runs one ``TrackFrame``
child per frame, and calls continue-as-new periodically to keep its own
history bounded. ``TrackFrame`` does the actual capture -> detect -> move for
a single frame.

The split is for legibility. Running the activities inline in the parent left
a history of hundreds of interleaved activity events that was unreadable in
the UI. One child per frame gives the parent a single line per frame and each
child a short, self-contained history of exactly three activities. The cost is
a workflow task per frame, roughly 200-400 ms, which is a deliberate trade of
latency for a history someone can actually read.

This still replaces the earlier Schedule-per-frame design, which paid that
same startup cost *and* waited for a tick boundary on top.

Both workflows are hosted on the MacBook worker. The Pi does nothing but
serve camera and servo activities, which keeps the workflow sandbox off a
415 MB board.

Two things are deliberate:

* **The control law lives here, not in an activity.** It is pure
  deterministic arithmetic, so it stays replay-safe, and keeping it in the
  workflow means the decision - and the reason a frame did or did not move -
  is visible in the workflow source and in history.
* **A failed frame does not kill the loop.** A child that fails takes only
  its own frame down; the parent counts it and carries on, giving up only if
  failures never stop. Without that, one bad capture would end the tracker.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from purifier.shared import (
        CENTRE_DEADBAND,
        MAX_STEP_DEGREES,
        PAN_DEGREES_PER_OFFSET,
        PAN_DIRECTION,
        PI_TASK_QUEUE,
        PROPORTIONAL_GAIN,
        TILT_DEGREES_PER_OFFSET,
        TILT_DIRECTION,
        CapturedFrame,
        Detection,
        DetectionResult,
        MoveRequest,
        ServoAngles,
        TrackerConfig,
        TrackerState,
        TrackerStatus,
        TrackingOutcome,
    )

#: Retry policy for hardware and model activities. Kept short: in a loop, a
#: frame that retries for long is worse than a frame skipped, because the
#: subject has moved on by the time it succeeds.
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=1),
    maximum_attempts=2,
)

#: Capture measures ~1.2 s including process startup.
CAPTURE_TIMEOUT = timedelta(seconds=20)

#: Inference is tens of milliseconds warm, but the first frame after a worker
#: restart may still be loading or downloading weights.
DETECT_TIMEOUT = timedelta(seconds=60)

#: A full-range ramped move plus settle, with headroom.
MOVE_TIMEOUT = timedelta(seconds=30)


def compute_move(detection: Detection) -> MoveRequest | None:
    """Convert a normalised pixel offset into a relative servo move.

    Proportional control with a deliberate undershoot: a gain at or above 1.0
    in a loop this slow hunts around the target instead of settling.

    The per-axis degrees-per-offset scales are measured on the rig, not
    derived from the lens spec, so they absorb the real optics, the servo
    calibration and any mechanical linkage in one number each.

    :param detection: Where the tracked person is, normalised to -1..1.
    :returns: The move to apply, or ``None`` if the target is already inside
        the deadband and no move is warranted.
    """
    if (
        abs(detection.offset_x) <= CENTRE_DEADBAND
        and abs(detection.offset_y) <= CENTRE_DEADBAND
    ):
        return None

    def axis_delta(offset: float, degrees_per_offset: float, direction: int) -> float:
        if abs(offset) <= CENTRE_DEADBAND:
            return 0.0
        raw = offset * PROPORTIONAL_GAIN * degrees_per_offset * direction
        return max(-MAX_STEP_DEGREES, min(MAX_STEP_DEGREES, raw))

    return MoveRequest(
        delta_pan_degrees=round(
            axis_delta(detection.offset_x, PAN_DEGREES_PER_OFFSET, PAN_DIRECTION), 2
        ),
        delta_tilt_degrees=round(
            axis_delta(detection.offset_y, TILT_DEGREES_PER_OFFSET, TILT_DIRECTION), 2
        ),
    )


#: Child workflow ID prefix. Numbered per frame so the parent's history reads
#: as an ordered list and any single frame is easy to open in the UI.
FRAME_WORKFLOW_PREFIX = "track-frame"

#: How long a single frame's child workflow may take before it is abandoned.
#: Generous enough for a slow capture plus a full-range servo move.
FRAME_TIMEOUT = timedelta(seconds=60)


@workflow.defn
class TrackFrame:
    """One frame: capture, find the nearest person, re-aim the camera.

    Deliberately small. Its whole history is three activities, so opening one
    frame in the UI shows exactly what happened and nothing else.
    """

    @workflow.run
    async def run(self) -> TrackingOutcome:
        """Process a single frame.

        :returns: What this frame did, summarised for the Temporal UI.
        """
        frame: CapturedFrame = await workflow.execute_activity(
            "capture_frame",
            task_queue=PI_TASK_QUEUE,
            result_type=CapturedFrame,
            start_to_close_timeout=CAPTURE_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )

        # result_type is required, not decorative. These activities are
        # dispatched by string name because importing the Pi activity module
        # here would drag smbus2 onto the MacBook, and a string carries no
        # type information - without result_type the SDK hands back a raw dict
        # and every attribute access fails while mypy stays silent.
        result: DetectionResult = await workflow.execute_activity(
            "detect_person",
            frame,
            result_type=DetectionResult,
            start_to_close_timeout=DETECT_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )

        if result.detection is None:
            # Nobody there: hold position rather than sweeping. A search
            # pattern would need cross-frame state, which belongs to the
            # parent, not here.
            return TrackingOutcome(
                people_detected=0,
                action="no_person",
                inference_ms=result.inference_ms,
                notes=["nobody detected; holding position"],
            )

        detection = result.detection
        move = compute_move(detection)

        if move is None:
            # Inside the deadband: skip the move activity entirely. Saves a
            # round trip and keeps near-centred frames out of the servo path.
            return TrackingOutcome(
                people_detected=detection.people_detected,
                action="centred",
                inference_ms=result.inference_ms,
                notes=[
                    f"within {CENTRE_DEADBAND:.0%} of centre at "
                    f"({detection.offset_x:+.3f}, {detection.offset_y:+.3f})"
                ],
            )

        angles: ServoAngles = await workflow.execute_activity(
            "move_servos",
            move,
            task_queue=PI_TASK_QUEUE,
            result_type=ServoAngles,
            start_to_close_timeout=MOVE_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )

        notes = [
            f"aimed at {detection.aim_point} "
            f"(confidence {detection.confidence:.2f}, "
            f"{detection.box_area_fraction:.1%} of frame)"
        ]
        if angles.homed:
            notes.append("servos were homed first: position was unknown")

        return TrackingOutcome(
            people_detected=detection.people_detected,
            action="moved",
            requested_move=move,
            angles_after=angles,
            inference_ms=result.inference_ms,
            notes=notes,
        )


@workflow.defn
class TrackPerson:
    """Drive the tracking loop, one child workflow per frame."""

    def __init__(self) -> None:
        self._state = TrackerState()
        self._stop_requested = False
        self._last_action = "starting"
        self._last_note = ""

    @workflow.signal
    def stop(self) -> None:
        """Ask the loop to finish the current frame and exit cleanly.

        Preferred over terminating the workflow: the frame in flight
        completes, so the servos are never left mid-ramp.
        """
        self._stop_requested = True

    @workflow.query
    def status(self) -> TrackerStatus:
        """Report progress without disturbing the loop.

        :returns: Counters, the most recent action, and whether a stop is
            pending.
        """
        return TrackerStatus(
            state=self._state,
            last_action=self._last_action,
            last_note=self._last_note,
            stopping=self._stop_requested,
        )

    @workflow.run
    async def run(
        self, config: TrackerConfig | None = None, state: TrackerState | None = None
    ) -> TrackerState:
        """Run the tracking loop until stopped.

        :param config: Loop parameters; defaults are used when omitted.
        :param state: Counters carried in from a previous generation.
        :returns: Final counters, once stopped or the frame budget is spent.
        :raises ApplicationError: If frames keep failing, so a broken rig
            surfaces as a failed workflow rather than a silent no-op loop.
        """
        settings = config or TrackerConfig()
        self._state = state or TrackerState()

        frames_this_run = 0
        while True:
            if self._stop_requested:
                self._last_action = "stopped"
                return self._state
            if (
                settings.stop_after_frames is not None
                and self._state.frames_processed >= settings.stop_after_frames
            ):
                self._last_action = "frame budget reached"
                return self._state
            if frames_this_run >= settings.frames_per_run:
                # Bound history: the parent accumulates a few events per
                # child, so it still grows without this. Counters carry over;
                # servo position does not need to, because it is read back
                # from the hardware.
                self._state.generation += 1
                workflow.continue_as_new(args=[settings, self._state])

            await self._run_one_frame(settings)
            frames_this_run += 1

            if settings.frame_pause_seconds > 0:
                await workflow.sleep(settings.frame_pause_seconds)

    async def _run_one_frame(self, settings: TrackerConfig) -> None:
        """Run one child workflow, absorbing its failure if it fails.

        :param settings: Loop parameters, for the failure budget.
        :raises ApplicationError: If consecutive failures exceed the budget.
        """
        frame_number = self._state.frames_processed
        try:
            outcome: TrackingOutcome = await workflow.execute_child_workflow(
                TrackFrame.run,
                id=f"{FRAME_WORKFLOW_PREFIX}-{frame_number:06d}",
                task_queue=workflow.info().task_queue,
                execution_timeout=FRAME_TIMEOUT,
            )
        except ChildWorkflowError as child_error:
            self._state.frames_failed += 1
            self._state.consecutive_failures += 1
            self._last_action = "failed"
            self._last_note = str(child_error.cause or child_error)[:200]
            workflow.logger.warning(
                "frame %d failed (%d consecutive): %s",
                frame_number,
                self._state.consecutive_failures,
                self._last_note,
            )
            if self._state.consecutive_failures >= settings.max_consecutive_failures:
                raise ApplicationError(
                    f"{self._state.consecutive_failures} consecutive frames failed; "
                    f"last error: {self._last_note}"
                ) from child_error
        else:
            self._state.consecutive_failures = 0
            self._last_action = outcome.action
            self._last_note = "; ".join(outcome.notes)
            if outcome.action == "moved":
                self._state.moves_made += 1
            elif outcome.action == "no_person":
                self._state.frames_without_person += 1
        finally:
            self._state.frames_processed += 1

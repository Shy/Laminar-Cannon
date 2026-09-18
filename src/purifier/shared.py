"""Data contracts crossing the Pi/MacBook boundary, plus tracking constants.

Every activity argument and return value is defined here so both workers agree
on the wire format. Payloads travel through Temporal, so all fields must be
JSON-serialisable: JPEG bytes are carried base64-encoded, because the default
payload converter cannot serialise raw ``bytes``.
"""

from dataclasses import dataclass, field

#: Task queue served by the Pi worker: camera and servo activities.
PI_TASK_QUEUE = "purifier-pi"

#: Task queue served by the MacBook worker: the workflow and pose detection.
MAC_TASK_QUEUE = "purifier-mac"

#: Workflow ID prefix; the Schedule appends its own run suffix.
WORKFLOW_ID_PREFIX = "track-person"

#: Identifier of the Schedule that drives the tracking loop.
SCHEDULE_ID = "track-person-loop"

# --- Control law -------------------------------------------------------------
# Tuned against real hardware. Deliberately conservative: the loop takes
# ~1.5-2 s end to end, and a high gain in a slow loop oscillates.

#: Proportional gain. Below 1.0 the camera undershoots and converges smoothly;
#: at or above 1.0 it will hunt around the target.
#:
#: Raised from 0.5 after watching real convergence: corrections were stepping
#: down cleanly but over three or four frames, which reads as sluggish. At
#: 0.65 each frame closes about two thirds of the error, so it settles in two
#: frames instead of four. Back off toward 0.5 if the camera starts to
#: overshoot and hunt.
PROPORTIONAL_GAIN = 0.65

#: Fraction of a half-frame within which no move is issued. Stops the servos
#: buzzing and keeps near-centred frames out of the servo path entirely.
CENTRE_DEADBAND = 0.05

#: Largest angle change allowed in a single frame, in degrees, per axis. This
#: is a safety clamp, not the working scale - see the per-axis scales below.
MAX_STEP_DEGREES = 20.0

#: Degrees of servo travel needed to move the image by a full half-frame,
#: measured on the assembled rig with 8 degree nudges and cross-correlation:
#: pan 14.75 px/deg over 768 px, tilt 14.0 px/deg over 432 px. That works out
#: to roughly a 52 degree horizontal and 31 degree vertical field of view,
#: noticeably narrower than the module's 66 degree spec figure.
#:
#: These exist so PROPORTIONAL_GAIN means what it says. Scaling both axes by
#: MAX_STEP_DEGREES instead made the effective gain 0.38 on pan and 0.65 on
#: tilt - the first too slow to converge, the second hot enough to oscillate.
PAN_DEGREES_PER_OFFSET = 26.0
TILT_DEGREES_PER_OFFSET = 15.4

#: Sign applied to the pan correction. Measured: a positive command on
#: channel 1 turns the camera LEFT, so correcting a target that is right of
#: centre needs a negative command.
PAN_DIRECTION = -1

#: Sign applied to the tilt correction. Measured: a positive command on
#: channel 0 pitches the camera DOWN, which is the same direction as a
#: positive vertical offset (y grows downward), so no inversion is needed.
TILT_DIRECTION = 1


@dataclass
class CapturedFrame:
    """A JPEG frame captured on the Pi, ready to cross the wire.

    :param jpeg_base64: Base64-encoded JPEG bytes.
    :param width: Frame width in pixels.
    :param height: Frame height in pixels.
    :param exposure_us: Exposure time the sensor chose, in microseconds.
    :param captured_at: ISO 8601 timestamp of the capture, for the history.
    """

    jpeg_base64: str
    width: int
    height: int
    exposure_us: float
    captured_at: str


@dataclass
class Detection:
    """Where the tracked person is, as offsets from the frame centre.

    Offsets are normalised to ``-1.0..1.0`` so the workflow's control law is
    independent of capture resolution. Positive ``offset_x`` means the target
    is right of centre; positive ``offset_y`` means below centre.

    :param offset_x: Horizontal offset from centre, normalised.
    :param offset_y: Vertical offset from centre, normalised.
    :param confidence: Model confidence for the chosen person.
    :param people_detected: How many people were found in the frame.
    :param aim_point: Which keypoint the offsets describe, for the history.
    :param box_area_fraction: Chosen person's box area as a fraction of the
        frame, which is how "nearest" was decided.
    """

    offset_x: float
    offset_y: float
    confidence: float
    people_detected: int
    aim_point: str
    box_area_fraction: float


@dataclass
class DetectionResult:
    """Outcome of pose detection: a target, or an explained absence.

    :param detection: The chosen person, or ``None`` if nobody was found.
    :param inference_ms: Model inference time, excluding decode.
    """

    detection: Detection | None
    inference_ms: float


@dataclass
class MoveRequest:
    """A relative servo move, in degrees, computed by the workflow.

    :param delta_pan_degrees: Pan change to apply; positive is one direction,
        determined by :data:`PAN_DIRECTION`.
    :param delta_tilt_degrees: Tilt change to apply.
    """

    delta_pan_degrees: float
    delta_tilt_degrees: float


@dataclass
class ServoAngles:
    """Servo angles read back from the PCA9685 after a move.

    The chip's registers are the source of truth for position, so a move that
    fails partway through still leaves a readable, accurate state for the next
    frame to work from.

    :param pan_degrees: Pan angle after the move.
    :param tilt_degrees: Tilt angle after the move.
    :param homed: ``True`` if the servos had to be driven to centre first
        because the registers read zero, meaning position was unknown.
    """

    pan_degrees: float
    tilt_degrees: float
    homed: bool = False


@dataclass
class TrackingOutcome:
    """What one frame of the tracking loop did, returned by the workflow.

    This is the per-frame summary that shows up in the Temporal UI, so it
    carries enough detail to explain the frame without opening the activities.

    :param people_detected: How many people the model found.
    :param action: One of ``moved``, ``centred``, ``no_person``.
    :param requested_move: The move the control law asked for, if any.
    :param angles_after: Servo angles after the move, if one was made.
    :param inference_ms: Model inference time for the frame.
    :param notes: Human-readable detail, shown in the workflow result.
    """

    people_detected: int
    action: str
    requested_move: MoveRequest | None = None
    angles_after: ServoAngles | None = None
    inference_ms: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class TrackerConfig:
    """Parameters for one run of the tracking loop.

    :param frames_per_run: Frames to process before calling continue-as-new.
        Bounds history size; the loop itself is unaffected.
    :param frame_pause_seconds: Optional pause between frames. Zero runs as
        fast as the pipeline sustains.
    :param stop_after_frames: Total frames across all generations, then exit
        cleanly. ``None`` runs until signalled. Useful for demos and tests.
    :param max_consecutive_failures: Consecutive failed frames tolerated
        before the workflow gives up and fails loudly.
    """

    frames_per_run: int = 50
    frame_pause_seconds: float = 0.0
    stop_after_frames: int | None = None
    max_consecutive_failures: int = 5


@dataclass
class TrackerState:
    """Counters carried across continue-as-new boundaries.

    Servo position is deliberately absent: it is read back from the PCA9685
    inside the move activity, so the hardware stays the single source of
    truth even across generations.

    :param generation: How many times the loop has continued as new.
    :param frames_processed: Frames handled across all generations.
    :param moves_made: Frames that resulted in a servo move.
    :param frames_without_person: Frames where nobody was detected.
    :param frames_failed: Frames abandoned because an activity failed.
    :param consecutive_failures: Current run of failures, reset by any
        successful frame.
    """

    generation: int = 0
    frames_processed: int = 0
    moves_made: int = 0
    frames_without_person: int = 0
    frames_failed: int = 0
    consecutive_failures: int = 0


@dataclass
class TrackerStatus:
    """Snapshot returned by the workflow's status query.

    :param state: Counters so far.
    :param last_action: Action taken on the most recent frame.
    :param last_note: Human-readable detail from the most recent frame.
    :param stopping: Whether a stop has been requested.
    """

    state: TrackerState
    last_action: str
    last_note: str
    stopping: bool

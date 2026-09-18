"""Pose detection activity, served by the MacBook worker.

The model is loaded once when the worker starts, not per activity: a cold
load costs one to two seconds, which would dominate a frame that otherwise
takes tens of milliseconds.

Pose is used rather than plain detection so the camera can aim at the head.
A standing person's bounding-box centre sits around the waist, which frames
them with their head near the top edge of the shot.
"""

import base64
import binascii
import io
import time
from typing import Final

from temporalio import activity
from temporalio.exceptions import ApplicationError

from purifier.shared import CapturedFrame, Detection, DetectionResult

#: Ultralytics pose model. Swap to yolo11m-pose.pt or yolo11l-pose.pt to lean
#: harder on the MacBook; the keypoint indices are identical.
MODEL_NAME: Final = "yolo11s-pose.pt"

#: COCO person class index, the only class we care about.
PERSON_CLASS_INDEX: Final = 0

#: Minimum confidence for a person to be considered a candidate.
MIN_CONFIDENCE: Final = 0.4

#: COCO pose keypoint indices used to locate the head.
NOSE_INDEX: Final = 0
LEFT_EYE_INDEX: Final = 1
RIGHT_EYE_INDEX: Final = 2
LEFT_SHOULDER_INDEX: Final = 5
RIGHT_SHOULDER_INDEX: Final = 6

#: Minimum keypoint confidence before a keypoint is trusted for aiming.
MIN_KEYPOINT_CONFIDENCE: Final = 0.3

#: Module-level cache so the model survives across activity invocations.
_model: object | None = None


def load_model(model_name: str = MODEL_NAME) -> object:
    """Load and cache the pose model.

    Call this from the worker's startup path so the first frame does not pay
    the load cost. Ultralytics downloads the weights on first use.

    :param model_name: Ultralytics model filename.
    :returns: The loaded model.
    """
    global _model
    if _model is None:
        from ultralytics import YOLO

        _model = YOLO(model_name)
    return _model


def _decode_jpeg(frame: CapturedFrame) -> object:
    """Decode a base64 JPEG payload into an upright PIL image.

    :param frame: The captured frame from the Pi.
    :returns: A decoded ``PIL.Image``, rotated per its EXIF orientation tag.
    :raises ApplicationError: Non-retryable, if the payload is not a valid
        JPEG. Retrying a corrupt payload cannot help.
    """
    from PIL import Image, ImageOps

    try:
        jpeg_bytes = base64.b64decode(frame.jpeg_base64, validate=True)
    except (binascii.Error, ValueError) as decode_error:
        raise ApplicationError(
            f"frame payload is not valid base64: {decode_error}",
            non_retryable=True,
        ) from decode_error

    try:
        decoded = Image.open(io.BytesIO(jpeg_bytes))
        # Apply the EXIF orientation tag before inference. This is not
        # cosmetic: the pose model is trained on upright people, and the same
        # frame scores 0.92 upright versus no detection at all sideways.
        # Image.open() ignores the tag, so without this the camera's physical
        # mounting silently breaks detection.
        return ImageOps.exif_transpose(decoded).convert("RGB")
    except Exception as image_error:
        # Deliberately broad. ultralytics monkeypatches PIL.Image.open, and
        # its version raises ModuleNotFoundError for pi_heif rather than
        # UnidentifiedImageError when a decode fails. Catching only PIL's
        # exception lets that escape as an unexpected error, which Temporal
        # retries - three attempts at a payload that can never decode.
        raise ApplicationError(
            f"frame payload is not a decodable JPEG: "
            f"{type(image_error).__name__}: {image_error}",
            non_retryable=True,
        ) from image_error


def _aim_point(
    keypoints_xy: list[list[float]],
    keypoint_confidence: list[float],
    box_centre: tuple[float, float],
) -> tuple[float, float, str]:
    """Choose the pixel to aim at for one person.

    Prefers the midpoint of the eyes, then the nose, then the midpoint of the
    shoulders, and falls back to the bounding-box centre when the upper body
    is not visible.

    :param keypoints_xy: Per-keypoint ``[x, y]`` pixel coordinates.
    :param keypoint_confidence: Per-keypoint confidence, same ordering.
    :param box_centre: Fallback ``(x, y)`` if no keypoint is trustworthy.
    :returns: ``(x, y, label)`` where label names the keypoint used.
    """

    def trusted(index: int) -> bool:
        return (
            index < len(keypoint_confidence)
            and keypoint_confidence[index] >= MIN_KEYPOINT_CONFIDENCE
        )

    if trusted(LEFT_EYE_INDEX) and trusted(RIGHT_EYE_INDEX):
        left = keypoints_xy[LEFT_EYE_INDEX]
        right = keypoints_xy[RIGHT_EYE_INDEX]
        return (left[0] + right[0]) / 2, (left[1] + right[1]) / 2, "eyes"

    if trusted(NOSE_INDEX):
        nose = keypoints_xy[NOSE_INDEX]
        return nose[0], nose[1], "nose"

    if trusted(LEFT_SHOULDER_INDEX) and trusted(RIGHT_SHOULDER_INDEX):
        left = keypoints_xy[LEFT_SHOULDER_INDEX]
        right = keypoints_xy[RIGHT_SHOULDER_INDEX]
        return (left[0] + right[0]) / 2, (left[1] + right[1]) / 2, "shoulders"

    return box_centre[0], box_centre[1], "box_centre"


@activity.defn
async def detect_person(frame: CapturedFrame) -> DetectionResult:
    """Find the nearest person in a frame and report where to aim.

    The largest bounding box wins, which is effectively the closest person.
    That is deterministic per frame and needs no cross-frame state, so it
    survives this workflow being a fresh execution every second.

    :param frame: Captured frame from the Pi.
    :returns: The chosen target, or an explained absence.
    """
    image = _decode_jpeg(frame)
    model = load_model()

    started = time.perf_counter()
    predictions = model.predict(  # type: ignore[attr-defined]
        image,
        classes=[PERSON_CLASS_INDEX],
        conf=MIN_CONFIDENCE,
        verbose=False,
    )
    inference_ms = (time.perf_counter() - started) * 1000

    # EXIF rotation may have swapped the axes, so the frame dimensions the Pi
    # reported are not the ones the model saw. Normalise against what was
    # actually inferred on, or a 90-degree mounting scales both axes wrongly.
    image_width: int = image.width  # type: ignore[attr-defined]
    image_height: int = image.height  # type: ignore[attr-defined]

    result = predictions[0]
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        activity.logger.info(
            "no person detected in %dx%d frame", image_width, image_height
        )
        return DetectionResult(detection=None, inference_ms=inference_ms)

    box_coords = boxes.xyxy.tolist()
    confidences = boxes.conf.tolist()
    areas = [(coords[2] - coords[0]) * (coords[3] - coords[1]) for coords in box_coords]
    chosen = max(range(len(areas)), key=lambda index: areas[index])

    left, top, right, bottom = box_coords[chosen]
    box_centre = ((left + right) / 2, (top + bottom) / 2)

    keypoints_xy: list[list[float]] = []
    keypoint_confidence: list[float] = []
    if result.keypoints is not None and result.keypoints.xy is not None:
        keypoints_xy = result.keypoints.xy[chosen].tolist()
        if result.keypoints.conf is not None:
            keypoint_confidence = result.keypoints.conf[chosen].tolist()

    aim_x, aim_y, aim_label = _aim_point(keypoints_xy, keypoint_confidence, box_centre)

    # Normalise to -1..1 about the frame centre so the control law does not
    # depend on capture resolution.
    offset_x = (aim_x - image_width / 2) / (image_width / 2)
    offset_y = (aim_y - image_height / 2) / (image_height / 2)
    frame_area = float(image_width * image_height)

    detection = Detection(
        offset_x=round(offset_x, 4),
        offset_y=round(offset_y, 4),
        confidence=round(float(confidences[chosen]), 4),
        people_detected=len(box_coords),
        aim_point=aim_label,
        box_area_fraction=round(areas[chosen] / frame_area, 4),
    )
    activity.logger.info(
        "tracking %d/%d person at offset (%.3f, %.3f) via %s in %.0f ms",
        chosen + 1,
        len(box_coords),
        detection.offset_x,
        detection.offset_y,
        aim_label,
        inference_ms,
    )
    return DetectionResult(detection=detection, inference_ms=inference_ms)

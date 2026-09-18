"""Tests for pose detection, including the EXIF orientation regression.

The orientation case is the important one. The camera is mounted rotated, and
the pose model is trained on upright people: the same frame scores 0.92
upright and detects nothing at all sideways. ``Image.open`` ignores the EXIF
orientation tag, so forgetting to apply it silently breaks detection while
every other part of the system looks healthy.

These tests need the Mac extra (ultralytics) and the fixture images, so they
skip cleanly on the Pi.
"""

import base64
from pathlib import Path

import pytest

from purifier.shared import CapturedFrame

ultralytics = pytest.importorskip("ultralytics", reason="needs the mac extra")

FIXTURES = Path(__file__).parent / "fixtures"
TAGGED = FIXTURES / "person_sideways_orient8.jpg"
UNTAGGED = FIXTURES / "person_sideways_notag.jpg"

pytestmark = pytest.mark.skipif(
    not TAGGED.is_file() or not UNTAGGED.is_file(),
    reason="fixture images not present",
)


def _frame(path: Path) -> CapturedFrame:
    """Build a CapturedFrame from a fixture image.

    Dimensions are reported as stored, pre-rotation, exactly as the Pi sends
    them - which is the condition that exposed the normalisation bug.

    :param path: Fixture image path.
    :returns: A frame ready to pass to the activity.
    """
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.width, image.height
    return CapturedFrame(
        jpeg_base64=base64.b64encode(path.read_bytes()).decode("ascii"),
        width=width,
        height=height,
        exposure_us=15124.0,
        captured_at="2026-09-18T12:00:00+00:00",
    )


@pytest.fixture(scope="module")
def loaded_model() -> object:
    """Load the pose model once for the module.

    :returns: The loaded model.
    """
    from purifier.activities_mac import load_model

    return load_model()


@pytest.mark.asyncio
async def test_detects_person_when_orientation_tag_present(
    loaded_model: object,
) -> None:
    from temporalio.testing import ActivityEnvironment

    from purifier.activities_mac import detect_person

    result = await ActivityEnvironment().run(detect_person, _frame(TAGGED))

    assert result.detection is not None, (
        "orientation tag was present but no person was found; "
        "check that _decode_jpeg still applies ImageOps.exif_transpose"
    )
    assert result.detection.confidence > 0.7
    assert result.detection.people_detected >= 1


@pytest.mark.asyncio
async def test_offsets_are_normalised_within_range(loaded_model: object) -> None:
    from temporalio.testing import ActivityEnvironment

    from purifier.activities_mac import detect_person

    result = await ActivityEnvironment().run(detect_person, _frame(TAGGED))
    assert result.detection is not None

    # Normalisation must use the post-rotation dimensions. If it used the
    # pre-rotation ones, a 90-degree mounting pushes an axis outside -1..1.
    assert -1.0 <= result.detection.offset_x <= 1.0
    assert -1.0 <= result.detection.offset_y <= 1.0


@pytest.mark.asyncio
async def test_aims_at_head_not_box_centre(loaded_model: object) -> None:
    from temporalio.testing import ActivityEnvironment

    from purifier.activities_mac import detect_person

    result = await ActivityEnvironment().run(detect_person, _frame(TAGGED))
    assert result.detection is not None
    assert result.detection.aim_point in {"eyes", "nose", "shoulders"}


@pytest.mark.asyncio
async def test_sideways_frame_without_tag_is_the_known_failure(
    loaded_model: object,
) -> None:
    # Documents why the tag matters: identical pixels, no tag, no detection.
    # If this ever starts finding a person, the model got better at rotated
    # people and the orientation handling could be revisited.
    from temporalio.testing import ActivityEnvironment

    from purifier.activities_mac import detect_person

    result = await ActivityEnvironment().run(detect_person, _frame(UNTAGGED))
    assert result.detection is None


@pytest.mark.asyncio
async def test_corrupt_payload_is_non_retryable() -> None:
    from temporalio.exceptions import ApplicationError
    from temporalio.testing import ActivityEnvironment

    from purifier.activities_mac import detect_person

    bad = CapturedFrame(
        jpeg_base64=base64.b64encode(b"not a jpeg at all").decode("ascii"),
        width=768,
        height=432,
        exposure_us=0.0,
        captured_at="2026-09-18T12:00:00+00:00",
    )
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(detect_person, bad)
    assert caught.value.non_retryable

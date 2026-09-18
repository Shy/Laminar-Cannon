#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Capture a test still from the Pi Camera Module 3 (IMX708) on the purifier.

Wraps ``rpicam-still`` rather than importing picamera2: picamera2 depends on
system libcamera bindings that cannot be installed into an isolated
virtualenv, while rpicam-still ships with Raspberry Pi OS. Nothing outside the
standard library is needed, so this runs with plain ``python3``.

Two corrections are applied that raw rpicam-still does not do for this build:

* Autofocus runs in continuous mode. In the default mode the lens stays parked
  (``AfState: 0``) and every frame comes back soft.
* The frame is rotated 180 degrees in-pipeline, undoing the module's
  inverted mounting. This rotates real pixels rather than setting an EXIF
  tag, so every downstream consumer sees an upright image - image viewers
  honour EXIF orientation, but most computer-vision libraries ignore it.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

#: Capture helper shipped with Raspberry Pi OS.
RPICAM_STILL = "rpicam-still"

#: Half-resolution sensor mode: 56 fps readout, quick on a Pi Zero 2 W.
DEFAULT_WIDTH = 2304
DEFAULT_HEIGHT = 1296

#: Full 12 MP sensor mode, used by --full.
FULL_WIDTH = 4608
FULL_HEIGHT = 2592

#: Milliseconds to let autofocus and auto-exposure settle before capturing.
DEFAULT_SETTLE_MS = 5000

#: In-pipeline rotation, in degrees, that makes this module's output upright.
#: The module is mounted inverted on the purifier frame. libcamera supports
#: only 0 and 180, which is all that is needed here.
MOUNTED_ROTATION_DEGREES = 180

#: Metadata fields worth printing after a capture.
REPORTED_METADATA_KEYS = (
    "ExposureTime",
    "AnalogueGain",
    "DigitalGain",
    "LensPosition",
    "AfState",
    "ColourTemperature",
)

#: Human-readable names for libcamera's AfState enum.
AF_STATE_NAMES = {
    0: "idle (lens parked, expect a soft image)",
    1: "scanning",
    2: "focused",
    3: "failed",
}

#: AfState value meaning the lens reached focus.
AF_STATE_FOCUSED = 2


def require_rpicam() -> None:
    """Abort with a clear message if ``rpicam-still`` is missing.

    :raises SystemExit: If the capture helper is not on ``PATH``.
    """
    if shutil.which(RPICAM_STILL) is None:
        raise SystemExit(
            f"{RPICAM_STILL} not found. It ships with Raspberry Pi OS; "
            "install it with `sudo apt install rpicam-apps`."
        )


def list_cameras() -> None:
    """Print the cameras libcamera can see."""
    completed = subprocess.run(
        [RPICAM_STILL, "--list-cameras"],
        capture_output=True,
        text=True,
        check=False,
    )
    print(completed.stdout.strip() or completed.stderr.strip())


def capture_still(
    destination: Path,
    metadata_path: Path,
    width: int,
    height: int,
    settle_ms: int,
    rotation: int,
) -> None:
    """Capture one still to ``destination`` and its metadata to JSON.

    :param destination: Where to write the captured JPEG.
    :param metadata_path: Where to write the capture metadata as JSON.
    :param width: Requested capture width in pixels.
    :param height: Requested capture height in pixels.
    :param settle_ms: Milliseconds to run the camera before capturing, so
        autofocus and auto-exposure converge.
    :param rotation: In-pipeline rotation in degrees; 0 or 180.
    :raises SystemExit: If the capture helper exits non-zero.
    """
    command = [
        RPICAM_STILL,
        "--nopreview",
        "--timeout",
        str(settle_ms),
        "--autofocus-mode",
        "continuous",
        "--width",
        str(width),
        "--height",
        str(height),
        "--rotation",
        str(rotation),
        "--metadata",
        str(metadata_path),
        "--metadata-format",
        "json",
        "--output",
        str(destination),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        print(completed.stderr.strip())
        raise SystemExit(f"{RPICAM_STILL} failed with code {completed.returncode}")


def read_metadata(metadata_path: Path) -> dict[str, float]:
    """Load the capture metadata written alongside the image.

    :param metadata_path: Path to the JSON metadata file.
    :returns: Parsed metadata, or an empty mapping if missing or invalid.
    """
    try:
        parsed = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def report_metadata(metadata: dict[str, float]) -> None:
    """Print the interesting capture metadata, including focus state.

    :param metadata: Parsed capture metadata.
    """
    for key in REPORTED_METADATA_KEYS:
        if key not in metadata:
            continue
        value = float(metadata[key])
        if key == "ExposureTime":
            print(f"  {key}: {value:.0f} us ({value / 1000:.1f} ms)")
        elif key == "AfState":
            label = AF_STATE_NAMES.get(int(value), "unknown")
            print(f"  {key}: {int(value)} - {label}")
        elif key == "LensPosition" and value > 0:
            print(f"  {key}: {value:.3f} dioptres (~{1 / value:.2f} m)")
        else:
            print(f"  {key}: {value:.3f}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.
    :returns: Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the cameras libcamera can see, then exit",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="where to write the JPEG (default ~/captures/cam-<timestamp>.jpg)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=f"capture at full {FULL_WIDTH}x{FULL_HEIGHT} instead of half res",
    )
    parser.add_argument(
        "--settle",
        type=int,
        default=DEFAULT_SETTLE_MS,
        help=(
            "milliseconds to let autofocus settle "
            f"(default {DEFAULT_SETTLE_MS}; below ~2000 the lens may not lock)"
        ),
    )
    parser.add_argument(
        "--rotation",
        type=int,
        default=MOUNTED_ROTATION_DEGREES,
        choices=(0, 180),
        help=(
            "in-pipeline rotation in degrees "
            f"(default {MOUNTED_ROTATION_DEGREES} for this mounting; "
            "0 leaves the frame as the sensor read it)"
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    """Entry point."""
    args = parse_args()
    require_rpicam()

    if args.list:
        list_cameras()
        return

    if args.output is not None:
        destination = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = Path.home() / "captures" / f"cam-{timestamp}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)

    width = FULL_WIDTH if args.full else DEFAULT_WIDTH
    height = FULL_HEIGHT if args.full else DEFAULT_HEIGHT
    metadata_path = destination.with_suffix(".json")

    print(f"capturing {width}x{height}, {args.settle} ms settle")
    capture_still(
        destination=destination,
        metadata_path=metadata_path,
        width=width,
        height=height,
        settle_ms=args.settle,
        rotation=args.rotation,
    )

    metadata = read_metadata(metadata_path)
    if metadata:
        print("metadata:")
        report_metadata(metadata)
        if int(metadata.get("AfState", 0)) != AF_STATE_FOCUSED:
            print("  warning: lens did not reach focus; raise --settle")

    size_kib = destination.stat().st_size / 1024
    print(f"wrote {destination} ({size_kib:.0f} KiB)")


if __name__ == "__main__":
    sys.exit(main())

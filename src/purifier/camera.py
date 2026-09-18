"""A persistent camera process, captured from on demand.

Starting ``rpicam-still`` fresh for every frame cost about 960 ms, almost all
of it process startup and opening the camera - two thirds of a whole tracking
frame. In signal mode the process stays running with the camera open and
captures on ``SIGUSR1``, measured at roughly 310 ms.

Keeping the camera open has a second benefit: auto-exposure runs continuously
instead of being given a fixed warm-up per capture, so frames are better
exposed than the previous 150 ms settle allowed.

The process is treated as disposable. Any capture failure tears it down and
the next call starts a fresh one, because a wedged camera process is far more
likely than a transient hiccup, and a restart costs one slow frame.
"""

import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from types import TracebackType
from typing import IO

from purifier.hardware import (
    CAPTURE_HEIGHT,
    CAPTURE_ROTATION_DEGREES,
    CAPTURE_WIDTH,
    LENS_POSITION_DIOPTRES,
    RPICAM_STILL,
    SENSOR_MODE,
)

logger = logging.getLogger(__name__)

#: Where the camera process writes frames. Each is deleted once read.
FRAME_DIR = Path("/tmp/purifier-frames")

#: The camera process logs here. A file, not a pipe: an undrained pipe fills
#: and blocks the process, which would wedge capture entirely.
LOG_PATH = FRAME_DIR / "camera.log"

#: Substrings meaning libcamera has the sensor configured. Until one appears,
#: SIGUSR1 must not be sent: its default disposition is to terminate, so a
#: signal arriving before rpicam-still installs its handler kills the process.
READY_MARKERS = ("configuring streams", "Registered camera", "Mode selection")

#: Grace period after a readiness marker, for the handler to be installed.
READY_GRACE_SECONDS = 0.75

#: Fallback wait if no marker ever appears but the process is still alive.
READY_FALLBACK_SECONDS = 6.0

#: Seconds to allow for the camera to open on startup. Measured at ~4 s; the
#: worker pays this once so the first tracking frame does not.
STARTUP_TIMEOUT_SECONDS = 15.0

#: Seconds to allow for one capture. Measured at ~310 ms, so this is generous
#: and only trips when the process has genuinely wedged.
CAPTURE_TIMEOUT_SECONDS = 6.0

#: A frame is complete once its size stops changing between two polls.
POLL_INTERVAL_SECONDS = 0.005

#: Frame filenames wrap at this many captures, matching the %04d format.
FRAME_INDEX_MODULUS = 10000


class CameraError(RuntimeError):
    """Raised when the camera process cannot produce a frame."""


def _kill_stale_camera_processes() -> None:
    """Kill any orphaned camera process left by a previous worker.

    libcamera allows exactly one process to hold the sensor, refusing others
    with "Pipeline handler in use by another process". A worker killed with
    SIGKILL never runs its cleanup, so its camera process survives and blocks
    every subsequent start until someone notices.

    Only processes launched by this project are touched, identified by both
    the helper name and this module's frame directory appearing in their
    command line.
    """
    marker = str(FRAME_DIR)
    own_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode()
        except OSError:
            # The process exited between listing and reading; nothing to do.
            continue
        if RPICAM_STILL not in cmdline or marker not in cmdline:
            continue
        logger.warning("killing orphaned camera process %d", pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                logger.warning("cannot signal process %d", pid)
                break
            time.sleep(0.4)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break


class SignalCamera:
    """A long-lived ``rpicam-still`` process that captures on demand.

    Not thread-safe by design: the Pi worker runs activities through a
    single-threaded executor, so captures are already serialised, and the
    hardware could not support concurrent use anyway.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None
        self._next_index = 0

    def __enter__(self) -> "SignalCamera":
        """Start the camera on entry.

        :returns: This camera.
        """
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the camera on exit."""
        self.close()

    @property
    def running(self) -> bool:
        """Whether the camera process is alive.

        :returns: ``True`` if a process exists and has not exited.
        """
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        """Start the camera process and wait for it to open the sensor.

        :raises CameraError: If the helper is missing or the camera never
            becomes ready.
        """
        if self.running:
            return

        if shutil.which(RPICAM_STILL) is None:
            raise CameraError(
                f"{RPICAM_STILL} not found; install it with "
                "`sudo apt install rpicam-apps`."
            )

        _kill_stale_camera_processes()

        FRAME_DIR.mkdir(parents=True, exist_ok=True)
        for stale in FRAME_DIR.glob("*.jpg"):
            stale.unlink(missing_ok=True)
        self._next_index = 0

        command = [
            RPICAM_STILL,
            "--nopreview",
            "--signal",
            # Run until told to stop rather than for a fixed duration.
            "--timeout",
            "0",
            # Force the full-sensor mode: left to choose, libcamera picks a
            # cropped mode for small outputs and quietly discards up to half
            # the horizontal field of view.
            "--mode",
            SENSOR_MODE,
            "--width",
            str(CAPTURE_WIDTH),
            "--height",
            str(CAPTURE_HEIGHT),
            # Fixed focus: autofocus needs ~2.5 s to lock, and pose detection
            # tolerates a soft frame.
            "--autofocus-mode",
            "manual",
            "--lens-position",
            str(LENS_POSITION_DIOPTRES),
            # The module is mounted inverted; rotate real pixels so nothing
            # downstream has to honour an EXIF tag.
            "--rotation",
            str(CAPTURE_ROTATION_DEGREES),
            "-o",
            str(FRAME_DIR / "f%04d.jpg"),
        ]

        # Logged to a file rather than a pipe: an undrained pipe fills and
        # blocks the camera process, which would wedge capture entirely. The
        # log is also how readiness is detected.
        LOG_PATH.unlink(missing_ok=True)
        self._log_handle = LOG_PATH.open("wb")
        self._process = subprocess.Popen(
            command,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        logger.info("camera process %d starting", self._process.pid)
        self._wait_until_ready()
        logger.info("camera ready")

    def _wait_for_signal_handler(self) -> None:
        """Block until it is safe to send SIGUSR1.

        SIGUSR1's default disposition is to terminate, so signalling before
        rpicam-still installs its handler kills the process outright. This was
        not a theory: the first version signalled immediately and the camera
        died every time. Readiness is read from the process log, falling back
        to a fixed delay because the marker text is not contractual.

        :raises CameraError: If the process exits while starting up.
        """
        started = time.monotonic()
        deadline = started + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not self.running:
                raise CameraError(f"{RPICAM_STILL} exited while starting up")
            try:
                log = LOG_PATH.read_text(errors="replace")
            except OSError:
                log = ""
            if any(marker in log for marker in READY_MARKERS):
                time.sleep(READY_GRACE_SECONDS)
                return
            if time.monotonic() - started > READY_FALLBACK_SECONDS:
                logger.warning(
                    "no camera readiness marker seen; proceeding after %.1fs",
                    READY_FALLBACK_SECONDS,
                )
                return
            time.sleep(0.05)
        raise CameraError(f"camera not ready within {STARTUP_TIMEOUT_SECONDS:.0f}s")

    def _wait_until_ready(self) -> None:
        """Wait for the sensor, then take one throwaway frame.

        Opening the camera takes about four seconds. Paying it here means the
        first real tracking frame is as fast as every other one.

        :raises CameraError: If no frame arrives before the startup timeout.
        """
        self._wait_for_signal_handler()
        self._capture_once(timeout_seconds=STARTUP_TIMEOUT_SECONDS)

    def _capture_once(self, timeout_seconds: float) -> bytes:
        """Signal one capture and return its JPEG bytes.

        :param timeout_seconds: How long to wait for a complete frame.
        :returns: The JPEG bytes.
        :raises CameraError: If the process is not running, or no complete
            frame appears in time.
        """
        process = self._process
        if process is None or process.poll() is not None:
            raise CameraError("camera process is not running")

        target = FRAME_DIR / f"f{self._next_index:04d}.jpg"
        target.unlink(missing_ok=True)
        process.send_signal(signal.SIGUSR1)

        deadline = time.monotonic() + timeout_seconds
        previous_size = -1
        while time.monotonic() < deadline:
            if target.exists():
                size = target.stat().st_size
                # A frame is done once its size stops changing; the file
                # appears before it is fully written.
                if size > 0 and size == previous_size:
                    data = target.read_bytes()
                    target.unlink(missing_ok=True)
                    self._next_index = (self._next_index + 1) % FRAME_INDEX_MODULUS
                    return data
                previous_size = size
            time.sleep(POLL_INTERVAL_SECONDS)

        raise CameraError(f"no frame within {timeout_seconds:.1f}s")

    def capture(self) -> bytes:
        """Capture one frame, restarting the camera once if needed.

        :returns: JPEG bytes of the captured frame.
        :raises CameraError: If capture fails even after a restart.
        """
        if not self.running:
            self.start()

        try:
            return self._capture_once(CAPTURE_TIMEOUT_SECONDS)
        except CameraError as first_error:
            logger.warning("capture failed (%s); restarting camera", first_error)
            self.close()
            self.start()
            return self._capture_once(CAPTURE_TIMEOUT_SECONDS)

    def close(self) -> None:
        """Stop the camera process, escalating if it ignores SIGUSR2."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return

        # SIGUSR2 is rpicam-still's own "finish and exit" signal.
        process.send_signal(signal.SIGUSR2)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        logger.info("camera process stopped")


#: Process-wide camera, shared by every capture activity in this worker.
_camera = SignalCamera()


def get_camera() -> SignalCamera:
    """Return the process-wide camera, starting it if necessary.

    :returns: The shared camera.
    """
    if not _camera.running:
        _camera.start()
    return _camera


def close_camera() -> None:
    """Stop the process-wide camera, if it is running."""
    _camera.close()

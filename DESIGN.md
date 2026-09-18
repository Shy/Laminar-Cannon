# Person-Tracking Camera on Temporal

A teaching artifact: a Raspberry Pi Zero 2 W aims a camera at whoever is
standing in front of it, with a MacBook doing the person detection and
Temporal Cloud coordinating the two.

The point is to show durable execution across heterogeneous workers - a
constrained ARM device and a laptop with a GPU - not to build a fast tracker.
Temporal adds tens to hundreds of milliseconds per activity, so a tight
control loop is the wrong use of it. At roughly one frame per second, it fits.

## Architecture

```
Temporal Cloud (API key auth)
  |
  +-- workflow TrackPerson  (parent loop)         queue: purifier-mac
        |  continue-as-new every 50 frames
        |
        +-- workflow TrackFrame  (one per frame)  queue: purifier-mac
              |
              +-- activity capture_frame   queue: purifier-pi   (Pi)
              +-- activity detect_person   queue: purifier-mac  (Mac, YOLO11-pose)
              +-- activity move_servos     queue: purifier-pi   (Pi)
```

A long-running parent loop drives one child workflow per frame. Both
workflows are hosted on the MacBook worker; the Pi serves only camera and
servo activities, which keeps the workflow sandbox off a 415 MB board.

The child-per-frame split is for legibility. Activities inline in the parent
produced a history of hundreds of interleaved events that was unreadable in
the UI; now the parent shows one line per frame and each child holds a short,
self-contained history of exactly three activities. Measured cost of the
split: 0.07-0.14 s per frame, far less than feared.

Servo position is not carried across frames or generations. It is read back
from the PCA9685 registers inside the move activity, so the hardware stays
the single source of truth.

### Superseded: a Schedule per frame

The loop began as a Temporal Schedule firing one `TrackPerson` execution per
tick. It worked, but per-frame workflow startup plus waiting for the next
tick boundary cost about a second a frame and bought nothing. A parent loop
removes both. The Schedule tooling is kept in `purifier.schedule` so the
leftover paused Schedule in Cloud can be inspected or deleted.

## Hardware

| Channel | Device | Axis | Range | Notes |
| ------- | ------ | ---- | ----- | ----- |
| 0 | 25 kg-cm servo | pan | 180 deg | Stronger servo carries the tilt assembly |
| 1 | 20 kg-cm servo | tilt | 270 deg | Only ~90 deg of travel is used |
| - | Camera Module 3 (IMX708) | - | - | CSI, manual fixed focus |
| 15 | unused | - | - | 12 V fan is wired direct, not switched |

Both servos are wildly overspecified for a ~5 g camera. Torque is irrelevant
here; backlash and deadband are the real problems, so expect jitter and
overshoot at small angles.

## Decisions

| Decision | Choice | Why |
| -------- | ------ | --- |
| Loop shape | One workflow per frame, via a Schedule | Each history is small and readable end to end; teaches Schedules |
| Overlap policy | `SKIP` | Self-tuning, can never back up. `ALLOW_ALL` would be a bug: two runs would read the same start angles and both apply a delta |
| Servo state | Read back from PCA9685 registers | Keeps each run genuinely stateless. Needs a homing step when registers read zero |
| Capture | `--mode 2304:1296 --width 768 --height 432` | Full sensor field of view at ~691 ms and 33 KB. The naive 640x480 silently crops to half the sensor width |
| Focus | Manual fixed (`--lens-position`) | Faster and deterministic; YOLO does not need a sharp frame. Autofocus needed 2.5 s to lock |
| Transport | JPEG bytes in the Temporal payload | ~33 KB, ~44 KB base64. Small enough that history stays readable |
| Detection | YOLO11-pose on the Mac | Keypoints let it aim at the head, not the bbox center, which frames a standing person badly |
| Target choice | Largest bounding box; no-op when none | Deterministic per frame, needs no state. "Faces whoever is nearest" |
| Control law | Proportional, `Kp ~= 0.5`, 5% deadband, max-step clamp | Deliberately undershoots so a ~1.5 s loop converges without oscillating. Needs no lens FOV data |
| Coordination | Temporal Cloud, API key auth | Immune to conference WiFi client isolation - neither machine needs to reach the other |
| Retries | 3 attempts per activity, 30 s `workflow_execution_timeout` | `SKIP` plus infinite retries deadlocks silently: one stuck run makes every later tick skip |
| Pi deploy | rsync + systemd unit | Worker survives reboots; a dead worker otherwise looks like a Temporal failure |

## Measured, not guessed

On the Pi, two passes, `rpicam-still` with a fresh process each time:

| Capture | Sensor crop | Time | Size |
| ------- | ----------- | ---- | ---- |
| 640x480, naive | 2304x1728 (half width) | ~750 ms | 34 KB |
| 640x360, naive | 3072x1728 (67%) | ~691 ms | 22 KB |
| 640x360 `--mode 2304:1296` | full 4608x2592 | ~697 ms | 23 KB |
| 768x432 `--mode 2304:1296` | full 4608x2592 | ~691 ms | 33 KB |
| 640x360 `--mode 4608:2592` | full | ~1739 ms | 21 KB |
| 640x480, continuous AF, 2 s settle | - | ~2450 ms | 34 KB |

Roughly 600 ms is the floor - that is process startup plus camera open, paid
once per capture.

Per-frame budget: ~700 ms capture, 20-50 ms detection warm, 200-500 ms servo
move, plus Cloud round trips. Expect ~1.5-2 s per frame, so `SKIP` settles on
an effective ~2 s cadence.

## Constraints

- **Never capture while the servos are moving.** Satisfied by construction:
  the activities are sequential within one run, and `SKIP` means runs never
  overlap.
- **Pi RAM is 415 MB total.** Nothing else runs on it. `temporalio` has
  aarch64 wheels, so no Rust compile.
- **Python pinned to 3.13.5 on both sides.** Matches the Pi's system Python
  and sidesteps whether PyTorch has 3.14 wheels.
- **torch must never land on the Pi.** Hence two extras rather than one
  dependency list.
- **The API key lives on the Pi's SD card.** Re-provisioning is part of the
  cost of losing a card.

## Layout

```
pyproject.toml              # requires-python 3.13.5; extras: pi, mac
.env.example                # TEMPORAL_ADDRESS / _NAMESPACE / _API_KEY
src/purifier/
  shared.py                 # dataclasses for activity arguments and returns
  activities_pi.py          # capture_frame, move_servos
  activities_mac.py         # detect_person
  workflows.py              # TrackPerson
  worker_pi.py
  worker_mac.py
  schedule.py               # create or update the Schedule
servo_test.py               # standalone hardware check
camera_test.py              # standalone camera check
```

## Idempotency note worth keeping

If `move_servos` fails partway through a ramp, the servos are left at an
intermediate angle - and that is fine. The next frame reads the actual angles
back off the PCA9685 and computes a fresh delta from wherever they really are.
The hardware is the source of truth, so a partial failure self-corrects on the
next tick rather than needing compensation.

## Found during implementation

Two bugs that the design did not anticipate, both worth keeping in mind:

**EXIF orientation is load-bearing for detection, not cosmetic.** The pose
model is trained on upright people. The same frame scores 0.92 confidence
upright and detects *nothing at all* sideways. Worse, `PIL.Image.open`
ignores the EXIF orientation tag, so embedding the tag at capture is not
enough - `ImageOps.exif_transpose` has to be applied before inference. There
is a regression test for this, because every other part of the system looks
perfectly healthy when it breaks.

A second-order trap: `exif_transpose` swaps width and height for a 90 degree
rotation, so offsets must be normalised against the decoded image's
dimensions, not the ones the Pi reported. Using the Pi's values scales both
axes by the wrong divisor.

**ultralytics monkeypatches `PIL.Image.open`.** Its replacement raises
`ModuleNotFoundError` for the optional `pi_heif` package when a decode fails,
rather than PIL's `UnidentifiedImageError`. Catching only PIL's exception let
that escape as an unexpected error, which Temporal treats as retryable - so a
corrupt payload would burn all three attempts instead of failing fast. The
decode handler is deliberately broad for this reason.

## Hardware lessons, learned the hard way

**Servos were being fed ~11 V against a 4.8-6.8 V rating.** The symptom was
the pan axis shaking while holding position. Two software theories were
plausible and both were wrong: ramp-step quantization (real, and worth fixing
anyway) and control-loop gain. Neither was the cause. Dropping the supply to
5 V stopped it immediately.

Worth remembering as a diagnostic order: servo jitter *while holding* points
at power or the servo, not at the code. Jitter *during motion* is the one that
points at the ramp.

**Reaction torque tipped the stand over.** These servos produce vastly more
torque than a 5 g camera needs, and a fast wide swing puts the surplus into
the mount. Fixed by clamping the base, restricting travel to +/-45 degrees per
axis, and cutting the slew rate.

**The axes were not what the design assumed.** Channel 0 turned out to be
tilt and channel 1 pan, and only one of the two directions was positive. All
three facts came from commanding an axis and looking at the resulting frame -
none of them were knowable in advance.

## Latency, measured

Per-frame breakdown on the parent/child architecture, after the fixes below:

| Stage | Time |
| ----- | ---- |
| `capture_frame` | 0.60 s |
| `detect_person` (warm) | 0.10 s |
| `move_servos` (when needed) | 0.56-0.70 s |
| Child workflow overhead | 0.07-0.15 s |
| **Frame, no move** | **0.94-1.27 s** |
| **Frame with a move** | **1.82-2.18 s** |

The first few frames run 3-9 s while the pose weights load and the camera
opens for the first time. That is warmup, not steady state.

**Activities must be synchronous, not `async def`.** The Pi activities block
on `subprocess.run` and `time.sleep`. Declared `async`, that blocking runs
inside the worker's event loop and stalls the SDK's own task handling:
`capture_frame` measured 2.19 s against a bare `rpicam-still` time of 0.74 s.
Making them ordinary `def` and giving the worker a single-threaded executor
brought it to 0.96 s - about 1.2 s a frame recovered. One thread, not more,
because there is one camera and one I2C bus.

**The camera process is kept open, not started per frame.** Starting
`rpicam-still` fresh cost 0.96 s, nearly all of it process startup and opening
the sensor. In signal mode the process stays running and captures on SIGUSR1:
measured 0.31 s from signal to a complete file, 0.60 s as the whole activity
including the base64 encode and the completion round trip. Auto-exposure also
runs continuously now, so frames are better exposed than a fixed per-capture
warm-up allowed.

One trap worth recording: **SIGUSR1's default disposition is to terminate.**
Signalling before rpicam-still installs its handler kills the process every
time, so startup waits for a readiness marker in the process log before the
first signal. The log goes to a file, not a pipe - an undrained pipe fills and
blocks the camera process outright.

### Servo ramp rate, and why it moved around

| Setting | Pan | Tilt | Reason |
| ------- | --- | ---- | ------ |
| 2.0 deg / 20 ms | 100 deg/s | 100 deg/s | Original |
| 1.0 deg / 50 ms | 20 deg/s | 20 deg/s | Stand tipped over from reaction torque |
| 1 count / 40 ms | 16.5 deg/s | 11 deg/s | Degree steps landed as uneven 1-then-2 count jumps |
| 1 count / 20 ms | 33 deg/s | 22 deg/s | First latency pass |
| **2 counts / 20 ms** | **66 deg/s** | **44 deg/s** | Current; `move_servos` 0.9 s -> 0.62 s |

Step timing cannot go below one PWM period (20 ms) without targets arriving
mid-servo-frame, so speed comes from step size, not step rate. The axes differ
because one count is 0.659 deg on the 270 deg pan servo but 0.440 deg on the
180 deg tilt servo.

The caution in the slower numbers is partly historical: 100 deg/s was only
ever shown to tip a *free-standing* mount. The base is clamped now, travel is
capped at +/-45 deg per axis, and the servos are no longer over-volted.

## Open items

- Is the pan/tilt mount built, with the camera physically on the servos?
  Until it is, the loop can be tested end to end against a stationary camera -
  servo commands will execute and read back correctly, they just will not
  change the view.
- The camera is being remounted so the frame is natively upright. After that,
  set `CAPTURE_ORIENTATION` in `src/purifier/hardware.py` and
  `MOUNTED_ORIENTATION` in `camera_test.py` to `1`. Pixel-x then maps to pan
  and pixel-y to tilt with no rotation in the path at all. Mounted sideways
  the axes swap and every sign error gets twice as confusing.
- `PAN_DIRECTION` and `TILT_DIRECTION` in `shared.py` are guesses. If the
  camera turns away from the person instead of toward them, flip the sign.
- Test fixtures are untracked: they are photos of a real person. Either commit
  them deliberately or swap in a public-domain image, then drop the
  `tests/fixtures/*.jpg` line from `.gitignore`.
- A paused Schedule named `track-person-loop` still exists in Cloud from the
  superseded design. It is harmless while paused, but unpausing it would now
  start a fresh long-running loop every couple of seconds. Remove it with
  `just schedule-delete` once nothing else needs it.
- The systemd unit in `deploy/` is written but not installed: that needs
  sudo on the Pi. The worker currently runs detached by hand, so it will not
  survive a reboot.
- `Kp`, the deadband and the max-step clamp all need tuning against real
  hardware. They are constants in one place for that reason.
- Servo move duration is an estimate; it was never measured, because the
  servos were unplugged throughout.

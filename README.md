# Laminar Cannon

A person-tracking air purifier that automatically aims filtered air at people using computer vision and pan/tilt servos.

## Highlights

- 🎯 Real-time person tracking with YOLO11 pose detection, aimed at the head
- 🌀 Automatically directs HEPA-filtered air toward detected people
- 🤖 Distributed workflow orchestration with Temporal
- 🎮 Raspberry Pi Zero 2 W drives the servos over I2C
- 📹 Works with a standard Pi Camera Module

## Overview

The Laminar Cannon detects people in real time and mechanically aims fans through a HEPA filter to direct clean air at them. A worker on a laptop runs pose detection while a Raspberry Pi captures frames and drives pan/tilt servos, with Temporal coordinating the two.

Inspired by PC fan powered airpurifiers, this project started with an excellent base by forking from the [Nukit Laminar Cannon](https://github.com/opennukit/Nukit-Laminar-Cannon/). This is a from-scratch build using off-the-shelf components, that I mostly had in my apartment, using python person detection rather than expensive camera eqipment. Nukit has a lot of great information about why this style of airpurifier is effective and I'll defer to them, rather than repeating it.

**Cost:** ~$180-230 in parts

## How It Works

1. A long-running `TrackPerson` workflow loops, starting one `TrackFrame` child workflow per frame
2. The Pi captures a 768x432 frame and returns it as JPEG bytes
3. The laptop runs YOLO11-pose and reports where the person's head is, as an offset from frame centre
4. The workflow converts that offset into a relative servo move
5. The Pi applies the move and reads the resulting angles back off the servo driver

Roughly 1 second per frame when the camera is already centred, 2 seconds when it has to move.

Pose detection is used rather than plain face or body detection so the camera can aim at the **head** specifically. A standing person's bounding-box centre sits around the waist, which frames them with their head near the top edge of the shot.

### Why Temporal?

The Pi Zero 2 W can't run YOLO detection locally - it would take 10+ seconds per frame. By offloading detection to a Temporal workflow on a more powerful machine:

- **Pi stays lightweight** - it only serves camera and servo activities
- **Fast detection** - YOLO runs on laptop hardware in about 100 ms
- **Scalable** - add more Pi units tracking different areas, all using the same detection worker
- **Reliable** - a failed frame costs one frame, not the tracker; the loop counts it and carries on

One detection worker can support dozens of tracking units.

Worth being honest about the fit: Temporal adds tens to hundreds of milliseconds per activity, so this is the wrong tool for a tight, high-rate control loop. At roughly one frame per second it fits comfortably, and durable execution across two very different machines is the interesting part.

## Hardware

- Raspberry Pi Zero 2 W + Camera Module 3 (IMX708)
- Adafruit 16-Channel PWM Servo Bonnet (PCA9685, I2C `0x40`)
- 1x 25 kg-cm servo (tilt) and 1x 20 kg-cm 270° servo (pan)
- 2x high static pressure 120 mm 12 V fans (>2.2 mm H₂O static pressure)
- 12 V to 5-6 V buck converter, **rated 8 A or more**
- 12 V / 5 A power supply
- Levoit Core 200S HEPA Filter

### Things that will bite you

**Size the buck converter properly.** Each servo can pull 2-3 A stalled. A common LM2596 module is 2-3 A total and will brown out mid-sweep - the Pi reboots or the I2C bus drops out. Use an XL4016-class module instead, and put a 1000 µF capacitor across `V+`/`GND` at the bonnet.

**Never feed 12 V to the bonnet's `V+`.** These servos are rated 4.8-6.8 V. Over-volting them causes them to hunt and shake while holding position, which looks exactly like a software problem and is not.

**Clamp the base.** These servos produce far more torque than a light camera needs, and the surplus goes into the mount as reaction torque. A wide fast swing will tip a free-standing rig over. Travel is capped at ±45° per axis in software and the slew rate is deliberately limited, but neither substitutes for clamping it down. Don't ask me how I know. Twice.

### Wiring

Servo power comes from the buck converter; the fans run straight off 12 V. One supply feeds everything.

```
12V 5A supply ──┬── 12V ─────────────────── Fan 1 + / Fan 2 +
                │                           (always on, not switched)
                ├── buck 12V→5-6V (8A+) ─── Bonnet V+ / GND
                │                           + 1000µF cap at the bonnet
                └── GND ────────────────┬── common ground
                                        └── Fan 1 - / Fan 2 -

Pi USB 5V ────── Raspberry Pi ── I2C (GPIO 2/3) ── Servo Bonnet
                      │                                  ├── Ch 0 ──► Tilt servo
                      │                                  └── Ch 1 ──► Pan servo
                      └── CSI ── Camera Module 3
```

Servo connectors are the usual three wires: **brown → GND, red → V+, yellow → PWM**. The bonnet's header rows are silkscreened `GND` / `V+` / `PWM`; line the brown wire up with `GND`. Getting red and brown swapped kills a servo instantly.

**Channel 0 is tilt and channel 1 is pan**, which is the opposite of what you might expect. That mapping came from commanding each axis on the assembled rig and seeing which way the picture moved, so check yours rather than trusting this.

## Software

Two workers. The laptop hosts both workflows plus pose detection; the Pi serves only camera and servo activities. They find each other through Temporal, so they never need to reach each other directly - which also means conference wifi client isolation can't break it.

### Prerequisites

- Python 3.13 on both machines (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) on both machines
- A Temporal Cloud namespace and API key, or adapt `src/purifier/client.py` for a local dev server
- I2C enabled on the Pi: `sudo raspi-config` → Interface Options → I2C, then reboot
- `rpicam-apps` on the Pi (ships with Raspberry Pi OS). Confirm the camera with `rpicam-still --list-cameras`

### Configuration

```bash
cp .env.example .env
```

```bash
TEMPORAL_ADDRESS=your-namespace.tmprl.cloud:7233
TEMPORAL_NAMESPACE=your-namespace.your-account
TEMPORAL_API_KEY=your-api-key
```

The same `.env` is needed on the Pi; `just pi-deploy` copies it across. It is gitignored - keep it that way.

Set the Pi's hostname at the top of the `justfile` (`pi_host`), or add a `Host` entry to your `~/.ssh/config`.

### Running

On the laptop:

```bash
just sync          # install deps, including torch and ultralytics
just worker-mac    # hosts both workflows and pose detection
```

On the Pi, from the laptop:

```bash
just pi-bootstrap          # one time: installs uv
just pi-deploy             # rsync the code and sync dependencies
just pi-worker-fg          # run the worker in the foreground
```

For something that survives a reboot, install the systemd unit instead (needs sudo on the Pi):

```bash
just pi-install-service
just pi-logs
```

Then drive the loop from the laptop:

```bash
just frames 1      # a single frame, the quickest end-to-end check
just start         # run continuously
just status        # frames processed, last action, current state
just stop          # finish the current frame and exit cleanly
```

`just stop` signals the workflow rather than terminating it, so the frame in flight completes and the servos are never abandoned mid-ramp.

## Calibrating the hardware

Check the camera on its own:

```bash
python3 camera_test.py --list          # confirm the sensor is detected
python3 camera_test.py                 # capture to ~/captures/
python3 camera_test.py --rotation 0    # raw sensor orientation
```

The camera module is mounted inverted here, so captures are rotated 180° in the libcamera pipeline. If yours is the right way up, set `CAPTURE_ROTATION_DEGREES` to `0` in `src/purifier/hardware.py`.

Servos, from the laptop:

```bash
just pi-servo-read           # angles read back off the PCA9685 registers
just pi-servo-centre         # centre both axes
just pi-servo-nudge 10 0     # relative move: pan +10°, tilt 0
just pi-servo-release        # stop driving, servos go limp
```

To establish your own direction signs: capture a frame, nudge one axis by a known amount, capture again, and see which way the picture moved. If the camera turns away from the person instead of toward them, flip `PAN_DIRECTION` or `TILT_DIRECTION` in `src/purifier/shared.py`.

The tracking gain lives in the same file. `PROPORTIONAL_GAIN` is deliberately below 1.0 so the camera undershoots and settles; at or above 1.0 it hunts around the target. `PAN_DEGREES_PER_OFFSET` and `TILT_DEGREES_PER_OFFSET` are measured on this rig and will differ on yours.

## Tests

```bash
just check    # ruff and mypy
uv run pytest
```

The workflow tests run against a real Temporal test server with stubbed hardware, so they exercise the actual serialisation path. Detection tests need a fixture image with a person in it under `tests/fixtures/`; they skip cleanly when it is absent.

## Project layout

```
3D/                          CAD for the printed parts (STEP and 3MF)
src/purifier/
  workflows.py               TrackPerson parent loop, TrackFrame child, control law
  activities_pi.py           capture_frame, move_servos, read_servo_angles
  activities_mac.py          detect_person (YOLO11-pose)
  camera.py                  persistent rpicam-still process
  pca9685.py                 servo driver over smbus2
  hardware.py                servo calibration and camera settings
  shared.py                  data contracts and tuning constants
  tracker.py                 start / stop / status CLI
  servo_tool.py              servo diagnostics and calibration CLI
camera_test.py               standalone camera check
deploy/                      systemd unit for the Pi worker
DESIGN.md                    why things are built the way they are
```

`DESIGN.md` is worth a read before changing anything: it records the measurements behind the tuning constants and several traps that cost real debugging time.

## Known gaps

- **Fan control was dropped.** An earlier version drove the fans from the Pi's hardware PWM and switched them off five seconds after the last detection. The current code doesn't touch the fans at all - they're wired straight to 12 V and run constantly. Re-adding this needs a MOSFET on a Pi PWM pin, since the PCA9685 shares one prescaler across all channels and can't produce the 25 kHz a 4-wire fan expects.
- The camera's focus is fixed, set by `LENS_POSITION_DIOPTRES`. Autofocus needs about 2.5 seconds to lock, which doesn't fit the loop.
- Nobody detected means hold position. There's no search pattern or return-to-centre.
- With several people in frame it tracks the largest bounding box, which is effectively the nearest person. Targets can switch between frames.

## License

Matching the original [Nukit Laminar Cannon](https://github.com/opennukit/Nukit-Laminar-Cannon/). This is also a [GPL-3 project](https://www.gnu.org/licenses/gpl-3.0.en.html).
Feel free to fork and adapt!

# Laminar Cannon

A face-tracking air purifier that automatically aims filtered air at people using computer vision and pan/tilt servos.

## Highlights

- 🎯 Real-time face tracking with YOLO detection
- 🌀 Automatically directs HEPA-filtered air toward detected faces
- 🤖 Distributed workflow orchestration with Temporal
- 🎮 Raspberry Pi Zero 2W controls servos and fans via GPIO
- 📹 Works with standard Pi Camera Module

## Overview

The Laminar Cannon detects faces in real-time and mechanically aims fans through a HEPA filter to direct clean air at people. A Flask server running YOLO detection communicates with a Raspberry Pi that controls pan/tilt servos and PWM fans.

Inspired by PC fan powered airpurifiers, this project started with an excellent base by forking from the [Nukit Laminar Cannon](https://github.com/opennukit/Nukit-Laminar-Cannon/). This is a from-scratch build using off-the-shelf components, that I mostly had in my apartment, using python face detection rather than expensive camera eqipment. Nukit has a lot of great information about why this style of airpurifier is effective and I'll defer to them, rather than repeating it. 

**Cost:** ~$180-230 in parts

## How It Works

1. Pi captures images at ~2 FPS
2. Flask server detects faces using YOLO via Temporal workflow
3. Server calculates servo movements needed to center the face
4. Pi moves servos and turns on fans
5. Fans turn off 5 seconds after last detection

### Why Temporal?

The Pi Zero 2W can't run YOLO detection locally - it would take 10+ seconds per frame. By offloading detection to a Temporal workflow on a more powerful machine:

- **Pi stays lightweight** - Only handles camera capture and servo control
- **Fast detection** - YOLO runs on server hardware in <100ms
- **Scalable** - Add more Pi units tracking different areas, all using the same detection server
- **Reliable** - Temporal handles retries and failure recovery automatically

One detection server can support dozens of tracking units.

## Hardware

- Raspberry Pi Zero 2 W + Camera Module
- Adafruit 16-Channel PWM Servo Bonnet
- 2x MG90S Pan/Tilt Servos
- 2x High Static pressure 120mm 12v fans (>2.2mm H₂O static pressure)
- LM2596 Buck Converter (12V→5V)
- 12V/5A Power Supply
- Levoit Core 200S HEPA Filter

### Wiring

```
Power Supplies:
┌──────────────────┐          ┌──────────────────┐
│  USB 5V/3A       │          │  12V/5A Power    │
│  (Raspberry Pi)  │          │  Supply          │
└────────┬─────────┘          └────────┬─────────┘
         │                             │ 12V
         │ 5V                     ┌────┴────┐
         │                        │         │
         ▼                        ▼         ▼
┌────────────────────┐                      │
│  Raspberry Pi      │    ┌──────────────┐  │
│                    │    │   LM2596     │  │
│                    │    │ Buck Convert │  │
│                    │    │ 12V → 5V     │  │
│                    │    └──────┬───────┘  │
│                    │           │ 5V       │ 12V
│  I2C (GPIO 2/3)    │           ▼          │
│         │          │    ┌──────────────┐  │
│         └──────────┼───►│Servo Bonnet  │  │
│                    │    │  (stacked)   │  │
│                    │    │              │  │
│                    │    │ V+ ◄─────────┘  │ (5V for servos)
│                    │    │ Ch 0 ───────────┼──► Pan Servo
│                    │    │ Ch 1 ───────────┼──► Tilt Servo
│  GPIO 18 (Pin 12)  │    │              │  │
│  Hardware PWM ─────┼────┼──────────────┼──┼──► Fan 1 PWM (Pin 4)
│                    │    │              │  │         │
│  ┌──────────┐      │    │              │  │         │ Daisy-chain
│  │ Pi Camera│      │    └──────────────┘  │         └─► Fan 2 PWM
│  │ (CSI)    │      │                      │
│  └──────────┘      │                      │
│                    │                      │
│      GND───────────┼──────────────────────┼─┐ Common GND
└────────────────────┘                      │ │
                                            │ │
                                   ┌────────┴─┴──┐
                                   │             │
                                   ▼             ▼
                              ┌─────────┐   ┌─────────┐
                              │ Fan 1   │   │ Fan 2   │
                              │ 120mm   │   │ 120mm   │
                              │ PWM     │   │ PWM     │
                              │         │   │         │
                              │ Pin 1: GND──┼─Pin 1: GND
                              │ Pin 2: 12V──┼─Pin 2: 12V
                              │ Pin 4: PWM──┼►Pin 4: PWM (chained)
                              └────┬────┘   └────┬────┘
                                   │             │
                                   └─────────────┘

```

## Configuration

Edit `pi/.env`:

```bash
DETECTION_SERVER_URL=http://192.168.1.100:5001/detect-person
FAN_SPEED=80                    # 0-100%
FACE_DETECTION_TIMEOUT=5        # Seconds
```

## Quick Start

### 1. Detection Server

Run on your Mac/PC:

```bash
cd flask
pip install -r requirements.txt

# Start these in separate terminals:
temporal server start-dev
python worker.py
python app.py
```

### 2. Raspberry Pi

```bash
cd pi
pip install -r requirements.txt
cp .env.example .env
nano .env  # Set your server IP

python test-hardware.py # Test all your hardware step by step. 
#This is encouraged because it is very top heavy and can the thing can flip over if you arne't careful with how you mount things or how fast you let the servors move. Don't ask me how I know. . . 
python calibrate.py  # Calibrate servos
python app.py        # Start tracking
```
## License

Matching the original [Nukit Laminar Cannon](https://github.com/opennukit/Nukit-Laminar-Cannon/). This is also a [GPL-3 project](https://www.gnu.org/licenses/gpl-3.0.en.html). 
Feel free to fork and adapt!

# SO-101 VR Control

Open-source browser-based VR teleoperation for the SO-101 robot arm using Meta Quest. One command, no app install.
<img width="1799" height="949" alt="Screenshot from 2026-05-28 19-49-15" src="https://github.com/user-attachments/assets/c88ac88d-317c-44c9-9eea-3eb3625b529f" />


## Features

- **Browser-based WebXR** — open a URL on your Quest, start controlling instantly
- **Single-arm optimized** — both VR controllers map to one arm
- **Calibration-safe kinematics** — joint limits tightened to actual motor range
- **Self-collision protection** — ground collision prevention, velocity limiting
- **Dark UI dashboard** — real-time status, settings, keyboard fallback
- **PyBullet IK** — multi-reference pose inverse kinematics solver

## Hardware

| Component | Details |
|-----------|---------|
| Robot | SO-101 (6x STS3215 FEEtech servos) |
| VR Headset | Meta Quest 2 / Pro / 3 / 3S |
| Connection | USB serial (`/dev/ttyACM0`) |

## Installation

```bash
conda activate pi0
cd ~/so101-vr
pip install -e .
```

## Quick Start

```bash
so101-vr --port /dev/ttyACM0 --log-level info
```

Output:

```
  SO-101 VR Control v2.0.0
  ========================
  Dashboard:  https://<your-ip>:8443
  Quest URL:  https://<your-ip>:8443
  WebSocket:  wss://<your-ip>:8442
```

Open the URL on your Quest browser, click **Connect Robot**, then **Start VR Control**.

## VR Controls

| Action | Controller |
|--------|-----------|
| **Move arm** | Hold **GRIP** — arm tracks your hand |
| **Release arm** | Release **GRIP** |
| **Close gripper** | Hold **TRIGGER** |
| **Open gripper** | Release **TRIGGER** |
| **Wrist rotation** | Controller roll & pitch |

Either hand works — both are remapped to the single arm.

## Keyboard Controls

Enable via the dashboard toggle.

| Key | Action |
|-----|--------|
| W / S | Forward / Back |
| A / D | Left / Right |
| Q / E | Down / Up |
| Z / X | Wrist Roll |
| F | Toggle Gripper |
| ESC | Emergency Stop |

## CLI Options

```
so101-vr [OPTIONS]

--port PORT         SO-101 serial port (e.g. /dev/ttyACM0)
--no-robot          Simulation only, no hardware
--no-vr             Disable VR WebSocket server
--no-keyboard       Disable keyboard input
--autoconnect       Auto-engage motors on startup
--log-level LEVEL   debug | info | warning | error
--https-port PORT   HTTPS port (default: 8443)
--ws-port PORT      WebSocket port (default: 8442)
```

## Safety

- Joint limits tightened from motor calibration (5° safety margin)
- Velocity limited to 200°/s (10°/step at 20 Hz)
- Ground collision prevention (Z-floor enforcement + post-IK FK check)
- LeRobot `max_relative_target = 10°` per command
- VR wrist clamped to ±170° roll, ±90° flex

## Architecture

```
Quest Browser (WebXR / A-Frame)
      │
      │ WSS :8442
      ▼
VRWebSocketServer
      │
      │ asyncio.Queue
      ▼
ControlLoop → _resolve_arm() → routes both hands to single arm
      │
      ▼
RobotInterface
  ├── IK solver (PyBullet, calibration-safe limits)
  ├── FK collision check (Z-floor)
  ├── Velocity limiter (10°/step)
  └── send_command()
        │
        │ USB serial
        ▼
LeRobot SOFollower (max_relative_target=10)
        │
        ▼
SO-101 (6x STS3215)
```

## Credits

Based on [Telegrip](https://github.com/DipFlip/telegrip) by Emil Rofors. Modified for single-arm SO-101 operation with safety enhancements by [GetSet Robotics](mailto:getsetrobotics@gmail.com).

## License

MIT

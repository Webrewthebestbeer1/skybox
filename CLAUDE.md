# Skybox — Cabin Camera Pan Control

## What this is

A balena-deployed system for a remote cabin Raspberry Pi 3. Two containers:

1. **balena-cam** — video stream on port 80 (pre-existing, not included in this repo)
2. **motor-control** — Flask API (port 5000) driving a TMC5130A stepper motor over SPI to pan the camera

User opens `http://<pi-ip>:5000` to see the video stream and control pan left/right.

## File structure

```
skybox/
├── docker-compose.yml              # Multi-container balena config
├── balena.yml                      # Fleet metadata
├── CLAUDE.md                       # This file
└── motor-control/
    ├── Dockerfile.template         # balenalib python base, spidev
    └── app/
        ├── requirements.txt        # flask, spidev
        ├── tmc5130.py              # TMC5130A SPI driver (Mode 3, 1 MHz)
        ├── server.py               # Flask API + position persistence
        └── templates/
            └── index.html          # Web UI (zero external deps)
```

## Key design decisions

- **SPI Mode 3, 1 MHz** — TMC5130A requirement. 5-byte datagrams, pipelined reads (read twice to get value).
- **StealthChop + S-curve ramp** — quiet operation, smooth acceleration.
- **Atomic position persistence** — `write-to-tmp + os.replace` to `/data/motor_position.json`. Survives power cuts.
- **No movement on boot** — loads saved position into XACTUAL and XTARGET.
- **Soft limits** — enforced server-side, configurable via env vars.
- **TPOWERDOWN** — reduces hold current after standstill to prevent heat buildup.
- **SPI error recovery** — catches errors, retries with full reinit (3 attempts).
- **Zero external web dependencies** — no CDN, no jQuery. Everything inline. Works offline.
- **Touch-hold support** — press and hold step buttons for continuous movement on mobile.

## API routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Web UI |
| `/api/status` | GET | Position, target, moving state, limits |
| `/api/step` | POST | Relative move `{"steps": 1000}` |
| `/api/stop` | POST | Emergency stop (XTARGET = XACTUAL) |
| `/api/home` | POST | Move to position 0 |
| `/api/set-home` | POST | Define current position as 0 |

## Environment variables (balena dashboard)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SOFT_LIMIT_LEFT` | -51200 | Left limit (microsteps) |
| `SOFT_LIMIT_RIGHT` | 51200 | Right limit |
| `MOTOR_VMAX` | 100000 | Max velocity |
| `MOTOR_AMAX` | 500 | Max acceleration |
| `MOTOR_CURRENT_RUN` | 16 | Run current (0-31) |
| `MOTOR_CURRENT_HOLD` | 8 | Hold current (0-31) |
| `SPI_BUS` | 0 | SPI bus |
| `SPI_DEVICE` | 0 | SPI chip select |
| `FLASK_PORT` | 5000 | Web server port |
| `CAM_PORT` | 80 | balena-cam port for iframe |

## Hardware

- Raspberry Pi 3 running balenaOS 3.0.8
- TMC5130A on SPI0: MOSI=GPIO10, MISO=GPIO9, SCLK=GPIO11, CS=GPIO8
- Stepper motor connected to TMC5130A outputs

## Deploy

```bash
cd ~/Code/heisenbrewcrew/skybox
balena push <fleet-name>
```

## Not yet implemented

- No simulation/mock mode for local testing without hardware
- balena-cam source not included (assumed pre-deployed)
- No HTTPS or authentication (local network only)

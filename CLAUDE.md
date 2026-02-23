# Skybox — Cabin Camera Pan Control

## What this is

A balena-deployed system for a remote cabin Raspberry Pi 3. Single container:

**motor-control** — Flask API (port 80) with integrated MJPEG video stream from a USB webcam and TMC5130A stepper motor control over SPI to pan the camera.

User opens `http://<pi-ip>` to see the video stream and control pan left/right.

## File structure

```
skybox/
├── docker-compose.yml              # Balena config
├── balena.yml                      # Fleet metadata
├── wiring_img.jpg                  # Hardware photo
├── CLAUDE.md                       # This file
└── motor-control/
    ├── Dockerfile.template         # balenalib python base, ffmpeg, spidev
    └── app/
        ├── requirements.txt        # flask, spidev, Pillow, numpy
        ├── db.py                   # SQLite persistence layer (/data/skybox.db)
        ├── camera.py               # MJPEG streaming via ffmpeg subprocess
        ├── car_detector.py         # Motion-based vehicle detection + counting + overlay
        ├── device_stats.py         # System stats + downtime tracking
        ├── tmc5130.py              # TMC5130A SPI driver (Mode 3, 1 MHz)
        ├── server.py               # Flask API + camera + position persistence
        ├── static/
        │   └── wiring_img.jpg      # Hardware photo (served by Flask)
        └── templates/
            └── index.html          # Web UI (zero external deps)
```

## Key design decisions

- **SQLite WAL mode** — single database at `/data/skybox.db`. Replaced atomic JSON files. WAL mode handles concurrent reads from Flask threads and the heartbeat thread. Auto-migrates legacy JSON files on first run.
- **MJPEG via ffmpeg subprocess** — captures from `/dev/video0`, pipes MJPEG frames to Flask streaming response. Light on memory (~15-25MB). No OpenCV dependency.
- **Single shared frame buffer** — one ffmpeg process feeds multiple browser clients via `threading.Event`.
- **Auto-restart** — ffmpeg process is restarted automatically if it dies.
- **SPI Mode 3, 1 MHz** — TMC5130A requirement. 5-byte datagrams, pipelined reads (read twice to get value).
- **StealthChop + S-curve ramp** — quiet operation, smooth acceleration.
- **No movement on boot** — loads saved position into XACTUAL and XTARGET.
- **User-configurable soft limits** — settable from the web UI. User limits override env var defaults per-side. Stored in SQLite.
- **TPOWERDOWN** — reduces hold current after standstill to prevent heat buildup.
- **SPI error recovery** — catches errors, retries with full reinit (3 attempts).
- **Zero external web dependencies** — no CDN, no jQuery. Everything inline. Works offline.
- **Touch-hold support** — press and hold step buttons for continuous movement on mobile.
- **Inverted UI direction** — the camera video is flipped via ffmpeg (`hflip,vflip` in `camera.py`), so the UI negates step values and position display to match visual left/right. Motor-internal coordinates are unchanged; the inversion is purely in the UI layer (`index.html` `data-steps` attributes and `updateBar`).
- **Visitor logging** — records IP and timestamp of each page visit. Capped at 100 entries.
- **Downtime tracking** — heartbeat every 60s; detects boot gaps and records downtime events.
- **Car detection** — motion-based detection using frame differencing against a slowly-adapting background model. Runs in a background thread at ~3 FPS. Crops to a configurable ROI (the road area) before analysis. Counts vehicles using a debounced state machine. Optional red bounding box overlay on the video stream. Controlled by developer settings (hidden panel, triple-click Stop to reveal). Uses only numpy + PIL — no ML model needed. TFLite was tried but MobileNet SSD can't detect cars this small and distant.

## API routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Web UI (logs visitor IP) |
| `/api/stream` | GET | MJPEG video stream |
| `/api/snapshot` | GET | Single JPEG frame — `http://<pi-ip>/api/snapshot` |
| `/api/status` | GET | Position, target, moving state, limits (effective + user + defaults) |
| `/api/step` | POST | Relative move `{"steps": 1000}` |
| `/api/stop` | POST | Emergency stop (XTARGET = XACTUAL) |
| `/api/home` | POST | Move to position 0 |
| `/api/set-home` | POST | Define current position as 0 |
| `/api/set-limit-left` | POST | Set left soft limit to current position |
| `/api/set-limit-right` | POST | Set right soft limit to current position |
| `/api/clear-limits` | POST | Clear user limits, revert to env var defaults |
| `/api/device-stats` | GET | System stats + downtime summary |
| `/api/visits` | GET | Recent visitor log |
| `/api/dev-settings` | GET/POST | Developer settings (count_cars, highlight_cars, car_count, reset_car_count) |

## Database schema (`/data/skybox.db`)

```sql
-- Key-value store for settings
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Keys: motor_position, soft_limit_left, soft_limit_right,
--        tracking_since, last_heartbeat, count_cars,
--        highlight_cars, car_count

-- Downtime events (capped at 50)
CREATE TABLE downtime_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL, end_ts REAL, duration_s REAL);

-- Visitor log (capped at 100)
CREATE TABLE visits (id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL, ip TEXT);
```

## Environment variables (balena dashboard)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SOFT_LIMIT_LEFT` | -51200 | Default left limit (microsteps), overridden by user-set limits |
| `SOFT_LIMIT_RIGHT` | 51200 | Default right limit, overridden by user-set limits |
| `MOTOR_VMAX` | 100000 | Max velocity |
| `MOTOR_AMAX` | 500 | Max acceleration |
| `MOTOR_CURRENT_RUN` | 16 | Run current (0-31) |
| `MOTOR_CURRENT_HOLD` | 8 | Hold current (0-31) |
| `SPI_BUS` | 0 | SPI bus |
| `SPI_DEVICE` | 0 | SPI chip select |
| `FLASK_PORT` | 5000 | Web server port (set to 80 in docker-compose) |
| `CAM_DEVICE` | /dev/video0 | V4L2 camera device |
| `CAM_WIDTH` | 640 | Capture width |
| `CAM_HEIGHT` | 480 | Capture height |
| `CAM_FPS` | 10 | Capture frame rate |
| `CAM_QUALITY` | 80 | JPEG quality (1-100) |
| `DETECT_ROI_X1` | 0.05 | Detection ROI left edge (fraction of frame) |
| `DETECT_ROI_Y1` | 0.35 | Detection ROI top edge (fraction of frame) |
| `DETECT_ROI_X2` | 0.85 | Detection ROI right edge (fraction of frame) |
| `DETECT_ROI_Y2` | 0.75 | Detection ROI bottom edge (fraction of frame) |

## Hardware

- Raspberry Pi 3 running balenaOS 3.0.8
- USB webcam on /dev/video0
- TMC5130A on SPI0: MOSI=GPIO10, MISO=GPIO9, SCLK=GPIO11, CS=GPIO8
- Stepper motor connected to TMC5130A outputs
- See `wiring_img.jpg` or the "Hardware" link in the web UI

## Balena free plan limits

- **Cloud builds:** 10 per day (`balena push <fleet>`)
- **Devices:** up to 10 per fleet
- **Log retention:** limited (a few days)
- **Local push** (`balena push <device-ip>`) does not count toward cloud build limits, but requires local mode enabled on the device from the dashboard

## Deploy

Cloud build (counts toward daily limit):
```bash
cd ~/Code/heisenbrewcrew/skybox
balena push KarlsonPaaTaket
```

Local push (no daily limit, requires local mode):
```bash
balena push 192.168.1.102 --nolive
```

Local mode is toggled in the balena dashboard under device settings. When local mode is off, `balena push <ip>` will fail with `ECONNREFUSED` on port 48484.

## Hot deploy (no rebuild)

Syncs local `motor-control/app/` code to the running container (~100KB, excludes static assets). Requires a tunnel in a separate terminal.

Terminal 1 (keep running):
```bash
balena device tunnel 0197707 -p 22222:22222
```

Terminal 2:
```bash
cd ~/Code/heisenbrewcrew/skybox
./deploy-hot.sh
```

Then restart the motor-control service from the balena cloud dashboard to apply changes.

## Device SSH

Device UUID: `0197707` (partial, sufficient for balena CLI).

SSH into the motor-control container (remote, via balena cloud):
```bash
balena device ssh 0197707 motor-control
```

SSH into the motor-control container (local network):
```bash
balena device ssh 192.168.1.102 motor-control
```

Edit files then kill the server — balena auto-restarts with the new code:
```bash
vi /usr/src/app/server.py
pkill -f "python3 server.py"
```

SSH into the host OS:
```bash
balena device ssh 0197707
```

Requires an SSH key registered with balena cloud (`balena key list` to check, `balena key add "name" ~/.ssh/id_ed25519.pub` to add).

Note: `balena ssh` is deprecated — use `balena device ssh` instead.

## Useful URLs (local network)

- Web UI: `http://192.168.1.102`
- Snapshot: `http://192.168.1.102/api/snapshot`
- Video stream: `http://192.168.1.102/api/stream`

## Not yet implemented

- No simulation/mock mode for local testing without hardware
- No HTTPS or authentication (local network only)

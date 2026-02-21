"""
Flask API server for TMC5130A stepper motor pan control.

Provides web UI with embedded video stream and motor pan controls.
Persists motor position atomically to survive restarts.
"""

import json
import logging
import os
import tempfile
import threading
import time

from flask import Flask, jsonify, render_template, request

from tmc5130 import TMC5130, TMC5130Error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# --- Configuration from environment ---
SOFT_LIMIT_LEFT = int(os.environ.get("SOFT_LIMIT_LEFT", -51200))
SOFT_LIMIT_RIGHT = int(os.environ.get("SOFT_LIMIT_RIGHT", 51200))
MOTOR_VMAX = int(os.environ.get("MOTOR_VMAX", 100000))
MOTOR_AMAX = int(os.environ.get("MOTOR_AMAX", 500))
MOTOR_CURRENT_RUN = int(os.environ.get("MOTOR_CURRENT_RUN", 16))
MOTOR_CURRENT_HOLD = int(os.environ.get("MOTOR_CURRENT_HOLD", 8))
SPI_BUS = int(os.environ.get("SPI_BUS", 0))
SPI_DEVICE = int(os.environ.get("SPI_DEVICE", 0))
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))
CAM_PORT = int(os.environ.get("CAM_PORT", 80))

POSITION_FILE = "/data/motor_position.json"

app = Flask(__name__)
motor = TMC5130(bus=SPI_BUS, device=SPI_DEVICE)
motor_lock = threading.Lock()


def save_position(position: int) -> None:
    """Atomically persist the motor position to disk."""
    data = {"position": position}
    dir_name = os.path.dirname(POSITION_FILE)
    os.makedirs(dir_name, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, POSITION_FILE)
        log.debug("Position saved: %d", position)
    except OSError:
        log.exception("Failed to save position")


def load_position() -> int:
    """Load persisted motor position, defaulting to 0."""
    try:
        with open(POSITION_FILE, "r") as f:
            data = json.load(f)
            pos = int(data.get("position", 0))
            log.info("Loaded saved position: %d", pos)
            return pos
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        log.info("No saved position found, defaulting to 0")
        return 0


def init_motor() -> None:
    """Initialize the TMC5130 and restore saved position."""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            motor.close()
            motor.open()
            motor.init(
                current_run=MOTOR_CURRENT_RUN,
                current_hold=MOTOR_CURRENT_HOLD,
                vmax=MOTOR_VMAX,
                amax=MOTOR_AMAX,
            )
            saved_pos = load_position()
            motor.set_position(saved_pos)
            log.info("Motor initialized successfully (attempt %d)", attempt)
            return
        except Exception:
            log.exception("Motor init failed (attempt %d/%d)", attempt, max_retries)
            time.sleep(1)
    log.error("Motor initialization failed after %d attempts", max_retries)


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


@app.route("/")
def index():
    return render_template("index.html", cam_port=CAM_PORT)


@app.route("/api/status")
def api_status():
    with motor_lock:
        try:
            position = motor.get_position()
            target = motor.get_target()
            moving = motor.is_moving()
            save_position(position)
        except Exception:
            log.exception("SPI error reading status")
            return jsonify({"error": "SPI communication error"}), 500

    return jsonify(
        {
            "position": position,
            "target": target,
            "moving": moving,
            "soft_limit_left": SOFT_LIMIT_LEFT,
            "soft_limit_right": SOFT_LIMIT_RIGHT,
        }
    )


@app.route("/api/step", methods=["POST"])
def api_step():
    data = request.get_json(force=True, silent=True) or {}
    steps = int(data.get("steps", 0))
    if steps == 0:
        return jsonify({"error": "steps must be non-zero"}), 400

    with motor_lock:
        try:
            current_target = motor.get_target()
            new_target = clamp(
                current_target + steps, SOFT_LIMIT_LEFT, SOFT_LIMIT_RIGHT
            )
            motor.move_to(new_target)
            log.info("Step: %+d -> target %d", steps, new_target)
        except Exception:
            log.exception("SPI error during step")
            init_motor()
            return jsonify({"error": "SPI error, motor reinitialized"}), 500

    return jsonify({"target": new_target})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with motor_lock:
        try:
            motor.stop()
            position = motor.get_position()
            save_position(position)
            log.info("Stop at position %d", position)
        except Exception:
            log.exception("SPI error during stop")
            init_motor()
            return jsonify({"error": "SPI error, motor reinitialized"}), 500

    return jsonify({"position": position})


@app.route("/api/home", methods=["POST"])
def api_home():
    with motor_lock:
        try:
            motor.move_to(0)
            log.info("Homing to position 0")
        except Exception:
            log.exception("SPI error during home")
            init_motor()
            return jsonify({"error": "SPI error, motor reinitialized"}), 500

    return jsonify({"target": 0})


@app.route("/api/set-home", methods=["POST"])
def api_set_home():
    with motor_lock:
        try:
            motor.set_position(0)
            save_position(0)
            log.info("Current position defined as home (0)")
        except Exception:
            log.exception("SPI error during set-home")
            init_motor()
            return jsonify({"error": "SPI error, motor reinitialized"}), 500

    return jsonify({"position": 0})


if __name__ == "__main__":
    log.info("Starting motor control server on port %d", FLASK_PORT)
    log.info(
        "Soft limits: left=%d right=%d", SOFT_LIMIT_LEFT, SOFT_LIMIT_RIGHT
    )
    init_motor()
    app.run(host="0.0.0.0", port=FLASK_PORT, threaded=True)

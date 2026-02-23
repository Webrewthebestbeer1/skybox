"""
Flask API server for TMC5130A stepper motor pan control.

Provides web UI with embedded video stream and motor pan controls.
Persists motor position and settings to SQLite.
"""

import logging
import os
import threading
import time

from flask import Flask, jsonify, render_template, request, Response

import db
from camera import CameraStream
from car_detector import CarDetector
from device_stats import DowntimeTracker, get_all_stats
from tmc5130 import TMC5130, TMC5130Error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# --- Configuration from environment ---
SOFT_LIMIT_LEFT = -51200
SOFT_LIMIT_RIGHT = 51200
MOTOR_VMAX = 15000
MOTOR_AMAX = 150
MOTOR_CURRENT_RUN = int(os.environ.get("MOTOR_CURRENT_RUN", 16))
MOTOR_CURRENT_HOLD = int(os.environ.get("MOTOR_CURRENT_HOLD", 8))
SPI_BUS = int(os.environ.get("SPI_BUS", 0))
SPI_DEVICE = int(os.environ.get("SPI_DEVICE", 0))
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))
CAM_DEVICE = os.environ.get("CAM_DEVICE", "/dev/video0")
CAM_WIDTH = int(os.environ.get("CAM_WIDTH", 640))
CAM_HEIGHT = int(os.environ.get("CAM_HEIGHT", 480))
CAM_FPS = int(os.environ.get("CAM_FPS", 10))
CAM_QUALITY = int(os.environ.get("CAM_QUALITY", 80))

# Detection ROI as fractions of frame — crop to road area before inference
DETECT_ROI_X1 = float(os.environ.get("DETECT_ROI_X1", 0.05))
DETECT_ROI_Y1 = float(os.environ.get("DETECT_ROI_Y1", 0.35))
DETECT_ROI_X2 = float(os.environ.get("DETECT_ROI_X2", 0.85))
DETECT_ROI_Y2 = float(os.environ.get("DETECT_ROI_Y2", 0.75))

app = Flask(__name__)
motor = TMC5130(bus=SPI_BUS, device=SPI_DEVICE)
motor_lock = threading.Lock()
camera = CameraStream(
    device=CAM_DEVICE,
    width=CAM_WIDTH,
    height=CAM_HEIGHT,
    fps=CAM_FPS,
    quality=CAM_QUALITY,
)
downtime_tracker = None  # initialized in __main__ after db.init_db()
car_detector = None      # initialized in __main__ after camera.start()
current_vmax = MOTOR_VMAX
current_amax = MOTOR_AMAX


def get_effective_limits():
    """Return (left, right) effective soft limits.

    User-set limits from DB override env var defaults per-side.
    """
    user_left, user_right = db.get_user_limits()
    left = user_left if user_left is not None else SOFT_LIMIT_LEFT
    right = user_right if user_right is not None else SOFT_LIMIT_RIGHT
    return (left, right)


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
            saved_pos = db.load_position()
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
    ip = request.remote_addr or "unknown"
    db.log_visit(ip)
    return render_template("index.html")


@app.route("/hardware")
def hardware():
    return render_template("hardware.html")


@app.route("/api/hw-info")
def api_hw_info():
    return jsonify({
        "vmax": MOTOR_VMAX,
        "amax": MOTOR_AMAX,
        "current_run": MOTOR_CURRENT_RUN,
        "current_hold": MOTOR_CURRENT_HOLD,
        "spi_bus": SPI_BUS,
        "spi_device": SPI_DEVICE,
        "cam_device": CAM_DEVICE,
        "cam_width": CAM_WIDTH,
        "cam_height": CAM_HEIGHT,
        "cam_fps": CAM_FPS,
    })


@app.route("/api/stream")
def video_stream():
    if car_detector and db.get_highlight_cars():
        gen = car_detector.generate_mjpeg()
    else:
        gen = camera.generate_mjpeg()
    return Response(gen, mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/snapshot")
def snapshot():
    frame = camera.get_frame()
    if frame is None:
        return jsonify({"error": "No frame available"}), 503
    return Response(frame, mimetype="image/jpeg")


@app.route("/api/status")
def api_status():
    with motor_lock:
        try:
            position = motor.get_position()
            target = motor.get_target()
            moving = motor.is_moving()
            db.save_position(position)
        except Exception:
            log.exception("SPI error reading status")
            return jsonify({"error": "SPI communication error"}), 500

    left, right = get_effective_limits()
    user_left, user_right = db.get_user_limits()

    return jsonify(
        {
            "position": position,
            "target": target,
            "moving": moving,
            "soft_limit_left": left,
            "soft_limit_right": right,
            "user_limit_left": user_left,
            "user_limit_right": user_right,
            "default_limit_left": SOFT_LIMIT_LEFT,
            "default_limit_right": SOFT_LIMIT_RIGHT,
        }
    )


@app.route("/api/step", methods=["POST"])
def api_step():
    data = request.get_json(force=True, silent=True) or {}
    steps = int(data.get("steps", 0))
    if steps == 0:
        return jsonify({"error": "steps must be non-zero"}), 400

    left, right = get_effective_limits()

    with motor_lock:
        try:
            current_target = motor.get_target()
            new_target = clamp(current_target + steps, left, right)
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
            db.save_position(position)
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
            db.save_position(0)
            log.info("Current position defined as home (0)")
        except Exception:
            log.exception("SPI error during set-home")
            init_motor()
            return jsonify({"error": "SPI error, motor reinitialized"}), 500

    return jsonify({"position": 0})


@app.route("/api/set-limit-left", methods=["POST"])
def api_set_limit_left():
    with motor_lock:
        try:
            position = motor.get_position()
        except Exception:
            log.exception("SPI error reading position")
            return jsonify({"error": "SPI communication error"}), 500

    db.set_user_limit_left(position)
    left, right = get_effective_limits()
    log.info("User left limit set to %d", position)
    return jsonify({
        "soft_limit_left": left,
        "soft_limit_right": right,
        "user_limit_left": position,
    })


@app.route("/api/set-limit-right", methods=["POST"])
def api_set_limit_right():
    with motor_lock:
        try:
            position = motor.get_position()
        except Exception:
            log.exception("SPI error reading position")
            return jsonify({"error": "SPI communication error"}), 500

    db.set_user_limit_right(position)
    left, right = get_effective_limits()
    log.info("User right limit set to %d", position)
    return jsonify({
        "soft_limit_left": left,
        "soft_limit_right": right,
        "user_limit_right": position,
    })


@app.route("/api/clear-limits", methods=["POST"])
def api_clear_limits():
    db.clear_user_limits()
    log.info("User limits cleared, using defaults: left=%d right=%d",
             SOFT_LIMIT_LEFT, SOFT_LIMIT_RIGHT)
    return jsonify({
        "soft_limit_left": SOFT_LIMIT_LEFT,
        "soft_limit_right": SOFT_LIMIT_RIGHT,
        "user_limit_left": None,
        "user_limit_right": None,
    })


@app.route("/api/dev-settings", methods=["GET"])
def api_dev_settings_get():
    count = car_detector.get_car_count() if car_detector else db.get_car_count()
    return jsonify({
        "count_cars": db.get_count_cars(),
        "highlight_cars": db.get_highlight_cars(),
        "car_count": count,
    })


@app.route("/api/dev-settings", methods=["POST"])
def api_dev_settings_post():
    data = request.get_json(force=True, silent=True) or {}

    # Handle count_cars toggle — starts/stops the detector
    if "count_cars" in data:
        enabled = bool(data["count_cars"])
        db.set_count_cars(enabled)
        if car_detector:
            if enabled:
                car_detector.set_counting(True)
                car_detector.set_car_count(db.get_car_count())
                if not car_detector._running:
                    car_detector.start()
            else:
                car_detector.set_counting(False)
                # If highlight is also off, stop the detector entirely
                if not db.get_highlight_cars():
                    car_detector.stop()
        log.info("Count cars set to %s", enabled)

    # Handle highlight_cars toggle
    if "highlight_cars" in data:
        enabled = bool(data["highlight_cars"])
        db.set_highlight_cars(enabled)
        if car_detector:
            car_detector.set_highlight(enabled)
            if enabled and not car_detector._running:
                car_detector.start()
            elif not enabled and not db.get_count_cars():
                car_detector.stop()
        log.info("Highlight cars set to %s", enabled)

    # Handle car count reset
    if data.get("reset_car_count"):
        db.reset_car_count()
        if car_detector:
            car_detector.reset_car_count()
        log.info("Car count reset")

    count = car_detector.get_car_count() if car_detector else db.get_car_count()
    return jsonify({
        "count_cars": db.get_count_cars(),
        "highlight_cars": db.get_highlight_cars(),
        "car_count": count,
    })


@app.route("/api/speed", methods=["GET"])
def api_speed_get():
    return jsonify({"vmax": current_vmax, "amax": current_amax})


@app.route("/api/speed", methods=["POST"])
def api_speed_post():
    global current_vmax, current_amax
    data = request.get_json(force=True, silent=True) or {}
    vmax = int(data.get("vmax", current_vmax))
    amax = int(data.get("amax", current_amax))
    vmax = max(1000, min(200000, vmax))
    amax = max(50, min(5000, amax))
    with motor_lock:
        try:
            motor.set_speed(vmax, amax)
            current_vmax = vmax
            current_amax = amax
        except Exception:
            log.exception("SPI error setting speed")
            return jsonify({"error": "SPI error"}), 500
    log.info("Speed changed: vmax=%d amax=%d", vmax, amax)
    return jsonify({"vmax": current_vmax, "amax": current_amax})


@app.route("/api/detection-log")
def api_detection_log():
    if car_detector:
        return jsonify({"events": car_detector.get_detection_log()})
    return jsonify({"events": []})


@app.route("/api/visits")
def api_visits():
    return jsonify({"visits": db.get_visits()})


@app.route("/api/device-stats")
def api_device_stats():
    stats = get_all_stats()
    stats["downtime"] = downtime_tracker.get_summary()
    return jsonify(stats)


if __name__ == "__main__":
    log.info("Starting motor control server on port %d", FLASK_PORT)
    log.info(
        "Default soft limits: left=%d right=%d", SOFT_LIMIT_LEFT, SOFT_LIMIT_RIGHT
    )
    db.init_db()
    init_motor()
    camera.start()
    downtime_tracker = DowntimeTracker()
    downtime_tracker.start()

    # Initialize car detector (starts only when toggled on)
    detect_roi = (DETECT_ROI_X1, DETECT_ROI_Y1, DETECT_ROI_X2, DETECT_ROI_Y2)
    car_detector = CarDetector(camera, roi=detect_roi)
    car_detector.set_on_car_counted(lambda count: db.set_car_count(count))

    # Always start with detection off — it's too CPU-heavy to auto-resume on a Pi 3
    db.set_count_cars(False)
    db.set_highlight_cars(False)
    log.info("Car detection disabled on boot")

    app.run(host="0.0.0.0", port=FLASK_PORT, threaded=True)

"""
SQLite persistence layer for Skybox.

Single database at /data/skybox.db. WAL mode for concurrent reads
from Flask's threaded request handlers.
"""

import json
import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

DB_PATH = "/data/skybox.db"
MAX_DOWNTIME_EVENTS = 50

_conn = None


def init_db():
    """Open the database, enable WAL mode, create tables, migrate JSON."""
    global _conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _create_tables()
    _migrate_json()
    log.info("Database initialized at %s", DB_PATH)


def _create_tables():
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS downtime_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts   REAL NOT NULL,
            end_ts     REAL NOT NULL,
            duration_s REAL NOT NULL
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS visits (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            ip        TEXT NOT NULL
        )
    """)
    _conn.commit()


def _migrate_json():
    _migrate_position_json()
    _migrate_uptime_json()


def _migrate_position_json():
    json_path = "/data/motor_position.json"
    if not os.path.exists(json_path):
        return
    if get_setting("motor_position") is not None:
        return
    try:
        with open(json_path, "r") as f:
            data = json.load(f)
        pos = int(data.get("position", 0))
        set_setting("motor_position", str(pos))
        os.rename(json_path, json_path + ".bak")
        log.info("Migrated motor_position.json (position=%d)", pos)
    except Exception:
        log.exception("Failed to migrate motor_position.json")


def _migrate_uptime_json():
    json_path = "/data/uptime_log.json"
    if not os.path.exists(json_path):
        return
    if get_setting("tracking_since") is not None:
        return
    try:
        with open(json_path, "r") as f:
            data = json.load(f)
        tracking_since = data.get("tracking_since")
        last_heartbeat = data.get("last_heartbeat")
        events = data.get("events", [])

        if tracking_since is not None:
            set_setting("tracking_since", str(tracking_since))
        if last_heartbeat is not None:
            set_setting("last_heartbeat", str(last_heartbeat))

        for ev in events:
            add_downtime_event(ev["start"], ev["end"], ev["duration_s"])

        os.rename(json_path, json_path + ".bak")
        log.info("Migrated uptime_log.json (%d events)", len(events))
    except Exception:
        log.exception("Failed to migrate uptime_log.json")


# --- Settings CRUD ---

def get_setting(key):
    """Return value for key, or None if not present."""
    row = _conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def set_setting(key, value):
    """Insert or update a settings key."""
    _conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    _conn.commit()


def delete_setting(key):
    """Delete a settings key."""
    _conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    _conn.commit()


# --- Motor position ---

def save_position(position):
    set_setting("motor_position", str(position))


def load_position():
    val = get_setting("motor_position")
    if val is None:
        return 0
    try:
        return int(val)
    except ValueError:
        return 0


# --- Soft limits ---

def get_user_limits():
    """Return (left, right) tuple. Each is int or None."""
    left = get_setting("soft_limit_left")
    right = get_setting("soft_limit_right")
    return (
        int(left) if left is not None else None,
        int(right) if right is not None else None,
    )


def set_user_limit_left(value):
    set_setting("soft_limit_left", str(int(value)))


def set_user_limit_right(value):
    set_setting("soft_limit_right", str(int(value)))


def clear_user_limits():
    delete_setting("soft_limit_left")
    delete_setting("soft_limit_right")


# --- Downtime tracking ---

def get_tracking_since():
    val = get_setting("tracking_since")
    return float(val) if val is not None else None


def set_tracking_since(ts):
    set_setting("tracking_since", str(ts))


def get_last_heartbeat():
    val = get_setting("last_heartbeat")
    return float(val) if val is not None else None


def set_last_heartbeat(ts):
    set_setting("last_heartbeat", str(ts))


def add_downtime_event(start_ts, end_ts, duration_s):
    _conn.execute(
        "INSERT INTO downtime_events (start_ts, end_ts, duration_s) VALUES (?, ?, ?)",
        (start_ts, end_ts, round(duration_s, 1)),
    )
    _conn.execute("""
        DELETE FROM downtime_events WHERE id NOT IN (
            SELECT id FROM downtime_events ORDER BY id DESC LIMIT ?
        )
    """, (MAX_DOWNTIME_EVENTS,))
    _conn.commit()


def get_downtime_events(limit=10):
    """Return recent events as list of dicts, chronological order."""
    rows = _conn.execute(
        "SELECT start_ts, end_ts, duration_s FROM downtime_events "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"start": r[0], "end": r[1], "duration_s": r[2]}
        for r in reversed(rows)
    ]


def get_total_downtime():
    row = _conn.execute(
        "SELECT COALESCE(SUM(duration_s), 0) FROM downtime_events"
    ).fetchone()
    return row[0]


# --- Visitor log ---

# --- Developer settings ---

def get_count_cars():
    """Return whether car counting is enabled."""
    val = get_setting("count_cars")
    return val == "1"


def set_count_cars(enabled):
    """Enable or disable car counting."""
    set_setting("count_cars", "1" if enabled else "0")


MAX_VISITS = 100


def log_visit(ip):
    """Record a page visit."""
    _conn.execute(
        "INSERT INTO visits (timestamp, ip) VALUES (?, ?)",
        (time.time(), ip),
    )
    _conn.execute("""
        DELETE FROM visits WHERE id NOT IN (
            SELECT id FROM visits ORDER BY id DESC LIMIT ?
        )
    """, (MAX_VISITS,))
    _conn.commit()


def get_visits(limit=50):
    """Return recent visits as list of dicts, newest first."""
    rows = _conn.execute(
        "SELECT timestamp, ip FROM visits ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"timestamp": r[0], "ip": r[1]} for r in rows]

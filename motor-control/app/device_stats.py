"""
Device statistics and downtime tracking for Skybox.

Reads system stats from /proc and /sys, tracks downtime events
with persistence via the db module (SQLite).
"""

import logging
import os
import threading
import time

import db

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 60  # seconds


def get_uptime():
    """Return system uptime in seconds."""
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except (IOError, ValueError, IndexError):
        return None


def get_cpu_temp():
    """Return CPU temperature in degrees Celsius."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return int(f.read().strip()) / 1000.0
    except (IOError, ValueError):
        return None


def get_memory():
    """Return dict with total, used, percent from /proc/meminfo."""
    try:
        info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    val = int(parts[1]) * 1024  # kB to bytes
                    info[key] = val
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        used = total - available
        pct = (used / total * 100) if total > 0 else 0
        return {"total": total, "used": used, "percent": round(pct, 1)}
    except (IOError, ValueError, KeyError):
        return None


def get_cpu_load():
    """Return dict with 1, 5, 15 minute load averages."""
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().split()
            return {
                "load_1": float(parts[0]),
                "load_5": float(parts[1]),
                "load_15": float(parts[2]),
            }
    except (IOError, ValueError, IndexError):
        return None


def get_wifi_signal():
    """Return dict with link quality and signal dBm from /proc/net/wireless."""
    try:
        with open("/proc/net/wireless", "r") as f:
            lines = f.readlines()
            # First two lines are headers
            if len(lines) < 3:
                return None
            parts = lines[2].split()
            return {
                "interface": parts[0].rstrip(":"),
                "link_quality": float(parts[2].rstrip(".")),
                "signal_dbm": float(parts[3].rstrip(".")),
            }
    except (IOError, ValueError, IndexError):
        return None


def get_disk_usage():
    """Return dict with total, used, percent for /data partition."""
    try:
        st = os.statvfs("/data")
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used = total - free
        pct = (used / total * 100) if total > 0 else 0
        return {"total": total, "used": used, "percent": round(pct, 1)}
    except OSError:
        return None


def get_all_stats():
    """Collect all system stats into a single dict."""
    return {
        "uptime": get_uptime(),
        "cpu_temp": get_cpu_temp(),
        "memory": get_memory(),
        "cpu_load": get_cpu_load(),
        "wifi": get_wifi_signal(),
        "disk": get_disk_usage(),
    }


class DowntimeTracker(object):
    """Tracks device downtime by writing periodic heartbeats.

    On startup, checks the gap since the last heartbeat. If the gap
    exceeds 2x the heartbeat interval, records a downtime event.
    Requires db.init_db() to have been called first.
    """

    def __init__(self):
        self._lock = threading.Lock()
        if db.get_tracking_since() is None:
            db.set_tracking_since(time.time())
        self._check_for_downtime()
        self._write_heartbeat()

    def _check_for_downtime(self):
        """Detect downtime gap since last heartbeat."""
        last_hb = db.get_last_heartbeat()
        if last_hb is None:
            return

        now = time.time()
        gap = now - last_hb
        threshold = HEARTBEAT_INTERVAL * 2

        if gap > threshold:
            uptime_s = get_uptime()
            boot_time = (now - uptime_s) if uptime_s is not None else now
            duration = round(boot_time - last_hb, 1)

            db.add_downtime_event(last_hb, boot_time, duration)
            log.info(
                "Recorded downtime event: %.0fs (from %s)",
                duration,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_hb)),
            )

    def _write_heartbeat(self):
        """Write current timestamp as heartbeat."""
        with self._lock:
            db.set_last_heartbeat(time.time())

    def start(self):
        """Start the background heartbeat thread."""
        t = threading.Thread(target=self._heartbeat_loop, name="downtime-heartbeat")
        t.daemon = True
        t.start()
        log.info("Downtime tracker heartbeat started (interval=%ds)", HEARTBEAT_INTERVAL)

    def _heartbeat_loop(self):
        """Periodically write heartbeats."""
        while True:
            time.sleep(HEARTBEAT_INTERVAL)
            self._write_heartbeat()

    def get_summary(self):
        """Return downtime summary dict."""
        with self._lock:
            tracking_since = db.get_tracking_since() or time.time()
            events = db.get_downtime_events(limit=10)
            total_downtime = db.get_total_downtime()

        now = time.time()
        total_tracked = now - tracking_since

        if total_tracked > 0:
            uptime_pct = max(0, min(100, (1 - total_downtime / total_tracked) * 100))
        else:
            uptime_pct = 100.0

        return {
            "uptime_pct": round(uptime_pct, 2),
            "tracking_since": tracking_since,
            "total_tracked_s": round(total_tracked, 0),
            "total_downtime_s": round(total_downtime, 0),
            "recent_events": events,
        }

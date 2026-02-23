"""
Camera streaming via ffmpeg subprocess.

Captures from a V4L2 device (USB webcam) and serves MJPEG frames.
Single ffmpeg process feeds multiple Flask clients via a shared frame buffer.
"""

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger(__name__)

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


class CameraStream:
    def __init__(self, device="/dev/video0", width=640, height=480, fps=10, quality=80):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality

        self._frame = None
        self._frame_id = 0
        self._lock = threading.Lock()
        self._process = None
        self._capture_thread = None
        self._running = False

    def _build_ffmpeg_cmd(self, try_mjpeg=True):
        cmd = [
            "ffmpeg",
            "-f", "v4l2",
        ]
        if try_mjpeg:
            cmd += ["-input_format", "mjpeg"]
        cmd += [
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", self.device,
            "-vf", "hflip,vflip",
            "-f", "mjpeg",
            "-q:v", str(max(1, min(31, 31 - int(self.quality * 31 / 100)))),
            "-an",
            "pipe:1",
        ]
        return cmd

    def _start_ffmpeg(self):
        self._stop_ffmpeg()

        if not os.path.exists(self.device):
            log.error("Video device %s not found", self.device)
            return False

        for try_mjpeg in [True, False]:
            cmd = self._build_ffmpeg_cmd(try_mjpeg=try_mjpeg)
            log.info("Starting ffmpeg: %s", " ".join(cmd))
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                time.sleep(0.5)
                if self._process.poll() is None:
                    log.info("ffmpeg started (pid=%d)", self._process.pid)
                    threading.Thread(target=self._drain_stderr, daemon=True).start()
                    return True
                else:
                    stderr = self._process.stderr.read().decode(errors="replace")
                    log.warning("ffmpeg exited immediately: %s", stderr[:500])
            except Exception:
                log.exception("Failed to start ffmpeg")

        return False

    def _stop_ffmpeg(self):
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            except Exception:
                pass
            self._process = None

    def _drain_stderr(self):
        try:
            while self._process and self._process.poll() is None:
                line = self._process.stderr.readline()
                if line:
                    log.debug("ffmpeg: %s", line.decode(errors="replace").strip())
        except Exception:
            pass

    def _capture_loop(self):
        buf = bytearray()

        while self._running:
            if self._process is None or self._process.poll() is not None:
                log.warning("ffmpeg not running, restarting in 2s...")
                time.sleep(2)
                if not self._start_ffmpeg():
                    continue

            try:
                chunk = self._process.stdout.read(4096)
                if not chunk:
                    log.warning("ffmpeg stdout empty")
                    self._stop_ffmpeg()
                    continue
            except Exception:
                log.exception("Error reading from ffmpeg")
                self._stop_ffmpeg()
                continue

            buf.extend(chunk)

            while True:
                start = buf.find(JPEG_START)
                if start == -1:
                    buf.clear()
                    break

                end = buf.find(JPEG_END, start + 2)
                if end == -1:
                    if start > 0:
                        del buf[:start]
                    break

                frame = bytes(buf[start : end + 2])
                del buf[: end + 2]

                with self._lock:
                    self._frame = frame
                    self._frame_id += 1

    def start(self):
        if self._running:
            return
        self._running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        log.info("Camera stream started")

    def stop(self):
        self._running = False
        self._stop_ffmpeg()
        if self._capture_thread:
            self._capture_thread.join(timeout=5)
        log.info("Camera stream stopped")

    def get_frame(self):
        with self._lock:
            return self._frame

    def generate_mjpeg(self):
        last_id = -1
        while True:
            with self._lock:
                if self._frame is not None and self._frame_id != last_id:
                    frame = self._frame
                    last_id = self._frame_id
                else:
                    frame = None

            if frame is None:
                time.sleep(0.05)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                b"\r\n" + frame + b"\r\n"
            )

"""
Vehicle detection via motion detection in ROI with jitter compensation.

The camera is mounted high on a cabin looking down at a distant road.
Wind causes the camera to physically shake, so raw frame differencing
sees the entire image as "motion". We solve this with:

1. Phase correlation (FFT-based) to compute the global camera shift
   between frames, then align before differencing. This cancels jitter
   so only independently-moving objects (vehicles) remain as motion.
2. Morphological opening to remove residual noise.
3. Contiguous blob detection with minimum size filters.
"""

import io
import logging
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

log = logging.getLogger(__name__)

# Counting state machine
CONFIRM_FRAMES = 2
CLEAR_FRAMES = 5

# Default ROI as fractions of frame
DEFAULT_ROI = (0.05, 0.35, 0.85, 0.75)

# Motion detection parameters
PIXEL_THRESHOLD = 40
BG_ALPHA = 0.01
BLUR_RADIUS = 4
ERODE_ITERATIONS = 3
DILATE_ITERATIONS = 4
MIN_BLOB_WIDTH = 15
MIN_BLOB_HEIGHT = 8
MIN_BLOB_AREA = 120
MAX_JITTER_PX = 15   # max camera shift to compensate (ignore larger)
EDGE_MARGIN = 18      # pixels to exclude along ROI edges after alignment


class CarDetector:
    """Motion-based vehicle detector with jitter compensation."""

    def __init__(self, camera, roi=None):
        self._camera = camera
        self._roi = roi or DEFAULT_ROI

        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        self._counting_enabled = False
        self._highlight_enabled = False

        self._bg_frame = None

        self._motion_box = None
        self._motion_fraction = 0.0
        self._overlay_frame = None
        self._overlay_frame_id = 0

        self._car_present = False
        self._detect_streak = 0
        self._clear_streak = 0
        self._car_count = 0

        self._on_car_counted = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._bg_frame = None
        self._thread = threading.Thread(target=self._detect_loop, daemon=True)
        self._thread.start()
        log.info("Car detector started (motion + jitter compensation, ROI=%s)", self._roi)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._bg_frame = None
        with self._lock:
            self._overlay_frame = None
            self._motion_box = None
        log.info("Car detector stopped")

    def set_counting(self, enabled):
        self._counting_enabled = enabled

    def set_highlight(self, enabled):
        self._highlight_enabled = enabled
        if not enabled:
            with self._lock:
                self._overlay_frame = None

    def set_on_car_counted(self, callback):
        self._on_car_counted = callback

    def get_car_count(self):
        return self._car_count

    def set_car_count(self, count):
        self._car_count = count

    def reset_car_count(self):
        self._car_count = 0

    def get_overlay_frame(self):
        with self._lock:
            return self._overlay_frame

    # --- Image alignment (jitter compensation) ---

    @staticmethod
    def _phase_correlate(frame, reference):
        """Compute translation offset between two images using phase correlation.

        Returns (dx, dy) in pixels. Fast FFT-based approach.
        """
        f1 = np.fft.fft2(frame)
        f2 = np.fft.fft2(reference)

        # Cross-power spectrum
        cross = f1 * np.conj(f2)
        magnitude = np.abs(cross)
        magnitude[magnitude < 1e-8] = 1e-8
        cross /= magnitude

        corr = np.fft.ifft2(cross).real

        # Find peak location
        peak = np.unravel_index(np.argmax(corr), corr.shape)
        dy, dx = peak[0], peak[1]

        # Handle wrap-around (shifts can be negative)
        h, w = frame.shape
        if dy > h // 2:
            dy -= h
        if dx > w // 2:
            dx -= w

        return int(dx), int(dy)

    @staticmethod
    def _shift_image(arr, dx, dy):
        """Shift a 2D array by (dx, dy) pixels, filling edges with border values."""
        h, w = arr.shape
        result = arr.copy()

        if abs(dx) >= w or abs(dy) >= h:
            return result

        # Shift along y
        if dy > 0:
            result[dy:, :] = arr[:-dy, :]
            result[:dy, :] = arr[0, :]  # fill top with border
        elif dy < 0:
            result[:dy, :] = arr[-dy:, :]
            result[dy:, :] = arr[-1, :]  # fill bottom with border

        # Shift along x (operate on already-shifted result)
        temp = result.copy()
        if dx > 0:
            result[:, dx:] = temp[:, :-dx]
            result[:, :dx] = temp[:, 0:1]
        elif dx < 0:
            result[:, :dx] = temp[:, -dx:]
            result[:, dx:] = temp[:, -1:]

        return result

    # --- Morphological operations ---

    @staticmethod
    def _erode(mask, iterations=1):
        result = mask
        for _ in range(iterations):
            new = result.copy()
            new[1:, :] &= result[:-1, :]
            new[:-1, :] &= result[1:, :]
            new[:, 1:] &= result[:, :-1]
            new[:, :-1] &= result[:, 1:]
            result = new
        return result

    @staticmethod
    def _dilate(mask, iterations=1):
        result = mask
        for _ in range(iterations):
            new = result.copy()
            new[1:, :] |= result[:-1, :]
            new[:-1, :] |= result[1:, :]
            new[:, 1:] |= result[:, :-1]
            new[:, :-1] |= result[:, 1:]
            result = new
        return result

    @staticmethod
    def _find_runs(arr):
        runs = []
        start = None
        for i, v in enumerate(arr):
            if v and start is None:
                start = i
            elif not v and start is not None:
                runs.append((start, i))
                start = None
        if start is not None:
            runs.append((start, len(arr)))
        return runs

    def _get_roi_pixels(self, img_w, img_h):
        x1_f, y1_f, x2_f, y2_f = self._roi
        return (
            int(x1_f * img_w),
            int(y1_f * img_h),
            int(x2_f * img_w),
            int(y2_f * img_h),
        )

    def _detect_loop(self):
        log.info("Motion detection loop started")

        while self._running:
            if not self._counting_enabled and not self._highlight_enabled:
                time.sleep(0.5)
                continue

            frame_jpeg = self._camera.get_frame()
            if frame_jpeg is None:
                time.sleep(0.5)
                continue

            try:
                result = self._detect_motion(frame_jpeg)
                detected, motion_box, motion_frac, jitter = result
            except Exception:
                log.exception("Motion detection error")
                time.sleep(1)
                continue

            with self._lock:
                self._motion_box = motion_box
                self._motion_fraction = motion_frac

            if self._counting_enabled:
                self._update_count(detected)

            if self._highlight_enabled:
                try:
                    overlay = self._draw_overlay(
                        frame_jpeg, motion_box, motion_frac, jitter
                    )
                    with self._lock:
                        self._overlay_frame = overlay
                        self._overlay_frame_id += 1
                except Exception:
                    log.exception("Overlay drawing error")

            time.sleep(0.3)

        log.info("Motion detection loop stopped")

    def _detect_motion(self, frame_jpeg):
        """Detect vehicle motion with jitter compensation.

        Returns (detected, motion_box, motion_fraction, jitter_offset).
        """
        img = Image.open(io.BytesIO(frame_jpeg)).convert("L")
        img = img.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))
        frame_arr = np.array(img, dtype=np.float32)

        orig_w, orig_h = img.size
        roi_x1, roi_y1, roi_x2, roi_y2 = self._get_roi_pixels(orig_w, orig_h)
        roi = frame_arr[roi_y1:roi_y2, roi_x1:roi_x2]

        # Initialize background
        if self._bg_frame is None or self._bg_frame.shape != roi.shape:
            self._bg_frame = roi.copy()
            return False, None, 0.0, (0, 0)

        # Phase correlation: find global camera shift
        dx, dy = self._phase_correlate(roi, self._bg_frame)

        # Clamp to max jitter (large shifts = scene change, not jitter)
        if abs(dx) > MAX_JITTER_PX or abs(dy) > MAX_JITTER_PX:
            dx, dy = 0, 0

        # Align background to current frame
        aligned_bg = self._shift_image(self._bg_frame, dx, dy)

        # Frame differencing on aligned images
        diff = np.abs(roi - aligned_bg)
        raw_mask = diff > PIXEL_THRESHOLD

        # Mask out edges — the shift fills border pixels with repeated
        # values, and sub-pixel residual jitter concentrates at edges
        m = EDGE_MARGIN
        h, w = raw_mask.shape
        if 2 * m < h and 2 * m < w:
            raw_mask[:m, :] = False
            raw_mask[-m:, :] = False
            raw_mask[:, :m] = False
            raw_mask[:, -m:] = False

        # Morphological opening
        cleaned = self._erode(raw_mask, ERODE_ITERATIONS)
        cleaned = self._dilate(cleaned, DILATE_ITERATIONS)

        # Update background toward current frame (only still pixels)
        still = ~raw_mask
        self._bg_frame[still] = (
            BG_ALPHA * roi[still] + (1 - BG_ALPHA) * self._bg_frame[still]
        )

        # Find largest blob
        motion_box, blob_area = self._find_largest_blob(cleaned, roi_x1, roi_y1)

        roi_area = roi.shape[0] * roi.shape[1]
        motion_frac = blob_area / roi_area if roi_area > 0 else 0.0

        return motion_box is not None, motion_box, motion_frac, (dx, dy)

    def _find_largest_blob(self, mask, roi_x1, roi_y1):
        """Find the largest contiguous motion blob meeting size thresholds."""
        col_counts = np.sum(mask, axis=0)
        active_cols = col_counts >= 2

        col_runs = self._find_runs(active_cols)
        if not col_runs:
            return None, 0

        col_runs.sort(key=lambda r: r[1] - r[0], reverse=True)

        for col_start, col_end in col_runs:
            width = col_end - col_start
            if width < MIN_BLOB_WIDTH:
                break

            col_slice = mask[:, col_start:col_end]
            row_has_motion = np.any(col_slice, axis=1)
            row_runs = self._find_runs(row_has_motion)
            if not row_runs:
                continue

            best_row = max(row_runs, key=lambda r: r[1] - r[0])
            row_start, row_end = best_row
            height = row_end - row_start

            if height < MIN_BLOB_HEIGHT:
                continue

            blob_region = mask[row_start:row_end, col_start:col_end]
            area = int(np.sum(blob_region))

            if area < MIN_BLOB_AREA:
                continue

            box = (
                col_start + roi_x1,
                row_start + roi_y1,
                col_end + roi_x1,
                row_end + roi_y1,
            )
            return box, area

        return None, 0

    def _update_count(self, vehicle_detected):
        if vehicle_detected:
            self._detect_streak += 1
            self._clear_streak = 0
            if not self._car_present and self._detect_streak >= CONFIRM_FRAMES:
                self._car_present = True
                self._car_count += 1
                log.info("Vehicle counted! Total: %d", self._car_count)
                if self._on_car_counted:
                    try:
                        self._on_car_counted(self._car_count)
                    except Exception:
                        log.exception("Error in car counted callback")
        else:
            self._clear_streak += 1
            self._detect_streak = 0
            if self._car_present and self._clear_streak >= CLEAR_FRAMES:
                self._car_present = False

    def _draw_overlay(self, frame_jpeg, motion_box, motion_frac, jitter):
        """Draw ROI, motion box, and debug info on frame."""
        img = Image.open(io.BytesIO(frame_jpeg)).convert("RGB")
        draw = ImageDraw.Draw(img)

        roi = self._get_roi_pixels(img.width, img.height)

        # Car count
        count_text = "count: %d" % self._car_count
        draw.text((roi[2] - 70, roi[1] + 2), count_text, fill="#4fc3f7")

        # Motion bounding box in red
        if motion_box:
            x1, y1, x2, y2 = motion_box
            for offset in range(3):
                draw.rectangle(
                    [x1 - offset, y1 - offset, x2 + offset, y2 + offset],
                    outline="red",
                )
            label = "%.2f%%" % (motion_frac * 100)
            draw.text((x1 + 2, y1 - 14), label, fill="red")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()

    def generate_mjpeg(self):
        last_overlay_id = -1

        while True:
            frame = None

            with self._lock:
                if (
                    self._overlay_frame is not None
                    and self._overlay_frame_id != last_overlay_id
                ):
                    frame = self._overlay_frame
                    last_overlay_id = self._overlay_frame_id

            if frame is None:
                frame = self._camera.get_frame()

            if frame is None:
                time.sleep(0.05)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                b"\r\n" + frame + b"\r\n"
            )

            time.sleep(0.05)

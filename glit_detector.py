
glint_detector.py -- lens-glint (retro-reflection) detection for ARGUS RS-28S.

No Kivy / Android imports here on purpose: this is pure numpy + OpenCV so it can be
unit-tested on a desktop with synthetic frames (see test_glint_detector.py).

HOW IT WORKS
A camera lens is a retro-reflector: light from a source next to the phone's camera
(its own flash LED) is bounced straight back as a tiny, very bright, compact point.
Lamps, LEDs, screws and windows also make bright points, but they are visible
WITHOUT the phone's flash. So the scanner alternates the torch OFF / ON and only
reports bright points that (a) are small and compact, (b) persist across frames,
and (c) are present with the flash ON but absent with it OFF.

If the torch cannot be controlled (torch_available=False) it falls back to a weaker
PASSIVE mode that can only report unconfirmed CANDIDATES, never confirmed glints.

LIMITS (be honest with yourself about these)
* Range: a lens glint is only a few pixels wide. Expect ~1-3 m in a dim room.
* The phone must be held reasonably steady during each 1.5 s phase.
* Bright rooms wash the effect out. Dim the lights.
* Shiny small objects that happen to sit in the flash's retro-reflection cone (some
  reflective tape, cat-eye reflectors, glass beads) look identical to a lens.
  A detection means "inspect this spot", not "a camera is here".
"""
import math
import time
from collections import namedtuple

import cv2
import numpy as np

Detection = namedtuple("Detection", "x y confidence kind contrast")
ScanState = namedtuple("ScanState", "status want_torch detections motion phase n_frames")

# ---- tunables (pixel values are at the ~320 px wide working resolution) -------------
WORK_WIDTH = 320
TOPHAT_KERNEL = 9       # bright features smaller than this survive the top-hat
MIN_PEAK = 170          # absolute luminance (0-255) a glint must reach
MIN_TOPHAT = 45         # minimum local contrast above the surroundings
MAX_AREA = 90           # bigger than this = window / lamp / mirror, not a lens
MIN_AREA = 2            # single hot pixels are sensor noise
MAX_ASPECT = 2.4        # streaks / edges are not lenses
MIN_FILL = 0.45         # blob must fill its bounding box (roughly round)
MAX_COMPONENTS = 200    # bail out on absurdly noisy frames

CLUSTER_TOL = 0.035     # "same spot" radius across frames (fraction of frame width)
MIN_PERSIST_ON = 0.5    # must be seen in >=50% of flash-ON frames
MAX_PERSIST_OFF = 0.2   # and in <=20% of flash-OFF frames at the same spot
MOTION_MAX = 8.0        # mean abs gray-level change between frames => "hold steady"


def to_work_gray(frame):
    """RGB/RGBA uint8 frame (H,W,C), top-down -> luminance, max-pooled to ~WORK_WIDTH.

    Max-pooling (not averaging) so a tiny bright point keeps its peak brightness."""
    if frame.ndim == 3 and frame.shape[2] == 4:
        gray = cv2.cvtColor(frame, cv2.COLOR_RGBA2GRAY)
    elif frame.ndim == 3:
        gray = cv2.cvtColor(np.ascontiguousarray(frame[..., :3]), cv2.COLOR_RGB2GRAY)
    else:
        gray = frame
    h, w = gray.shape
    f = max(1, w // WORK_WIDTH)
    if f > 1:
        hh, ww = (h // f) * f, (w // f) * f
        gray = gray[:hh, :ww].reshape(hh // f, f, ww // f, f).max(axis=(1, 3))
    return np.ascontiguousarray(gray)


def find_bright_points(gray):
    """Return small, bright, compact blobs: list of (nx, ny, contrast, peak, area, ar).

    nx, ny are 0..1 (top-left origin); ar = H/W, used for isotropic distances."""
    H, W = gray.shape
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (TOPHAT_KERNEL, TOPHAT_KERNEL))
    th = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)

    # noise-adaptive floor: busy/bright scenes raise the bar automatically
    med = float(np.median(th))
    mad = float(np.median(np.abs(th.astype(np.float32) - med))) * 1.4826 + 1e-6
    thr = max(MIN_TOPHAT, med + 8.0 * mad)

    mask = ((th >= thr) & (gray >= MIN_PEAK)).astype(np.uint8)
    n, _labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)

    out = []
    ar = H / float(W)
    for i in range(1, min(n, MAX_COMPONENTS)):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < MIN_AREA or area > MAX_AREA:
            continue
        if max(w, h) / float(max(1, min(w, h))) > MAX_ASPECT:
            continue
        if area >= 6 and area / float(w * h) < MIN_FILL:
            continue
        contrast = float(th[y:y + h, x:x + w].max())
        peak = float(gray[y:y + h, x:x + w].max())
        cx, cy = cents[i]
        out.append((cx / W, cy / H, contrast, peak, area, ar))
    return out


class _Cluster(object):
    __slots__ = ("x", "y", "ar", "members", "hits", "contrast", "last_frame")

    def __init__(self, x, y, ar, contrast, frame_idx):
        self.x, self.y, self.ar = x, y, ar
        self.members, self.hits = 1, 1
        self.contrast = contrast
        self.last_frame = frame_idx


def _dist(ax, ay, bx, by, ar):
    return math.hypot(ax - bx, (ay - by) * ar)


def _cluster(frames):
    """Group blobs that keep landing on the same spot across frames."""
    clusters = []
    for fi, blobs in enumerate(frames):
        for (nx, ny, contrast, _peak, _area, ar) in blobs:
            best, best_d = None, CLUSTER_TOL
            for c in clusters:
                d = _dist(nx, ny, c.x, c.y, ar)
                if d <= best_d:
                    best, best_d = c, d
            if best is None:
                clusters.append(_Cluster(nx, ny, ar, contrast, fi))
                continue
            best.x = (best.x * best.members + nx) / (best.members + 1)
            best.y = (best.y * best.members + ny) / (best.members + 1)
            best.members += 1
            best.contrast = max(best.contrast, contrast)
            if best.last_frame != fi:          # one hit per frame per spot
                best.hits += 1
                best.last_frame = fi
    return clusters


def confidence_label(conf):
    return "HIGH" if conf >= 0.6 else "MEDIUM" if conf >= 0.35 else "LOW"


class GlintScanner(object):
    """Feed it frames; it tells you whether the torch should be on and what it found."""

    def __init__(self, torch_available=True, settle_s=0.45, phase_s=1.5,
                 min_frames=4, hold_s=7.0):
        self.torch_available = torch_available
        self.settle_s = settle_s
        self.phase_s = phase_s
        self.min_frames = min_frames
        self.hold_s = hold_s
        self.reset()

    def reset(self):
        self._phase = "OFF"
        self._phase_t0 = None
        self._frames = {"ON": [], "OFF": [], "PASSIVE": []}
        self._prev_small = None
        self._results = []
        self._results_t = -1e9
        self.note = ""

    # ------------------------------------------------------------------------------
    def tick(self, frame, now=None):
        now = time.monotonic() if now is None else now
        if self._phase_t0 is None:
            self._phase_t0 = now

        if self.torch_available:
            if now - self._phase_t0 >= self.phase_s:
                if self._phase == "ON":
                    self._evaluate_flash(now)
                    self._frames = {"ON": [], "OFF": [], "PASSIVE": []}
                    self._phase = "OFF"
                else:
                    self._phase = "ON"
                self._phase_t0 = now
                self._prev_small = None        # brightness jumps across a toggle
            phase = self._phase
        else:
            phase = "PASSIVE"
            if now - self._phase_t0 >= 2 * self.phase_s:
                self._evaluate_passive(now)
                self._frames = {"ON": [], "OFF": [], "PASSIVE": []}
                self._phase_t0 = now

        gray = to_work_gray(frame)
        small = cv2.GaussianBlur(cv2.resize(gray, (64, 48), interpolation=cv2.INTER_AREA),
                                 (3, 3), 0)
        motion = 0.0
        if self._prev_small is not None:
            motion = float(np.mean(np.abs(small.astype(np.int16) -
                                          self._prev_small.astype(np.int16))))
        self._prev_small = small

        elapsed = now - self._phase_t0
        settling = self.torch_available and elapsed < self.settle_s
        if settling:
            status = "SETTLING"
        elif motion > MOTION_MAX:
            status = "HOLD STEADY"
        else:
            self._frames[phase].append(find_bright_points(gray))
            if phase == "PASSIVE":
                status = "PASSIVE SCAN (NO TORCH)"
            else:
                status = "SCANNING FLASH-" + phase

        fresh = self._results if (now - self._results_t) <= self.hold_s else []
        return ScanState(status, self.torch_available and self._phase == "ON",
                         list(fresh), motion, phase, len(self._frames[phase]))

    # ------------------------------------------------------------------------------
    def _evaluate_flash(self, now):
        on, off = self._frames["ON"], self._frames["OFF"]
        if len(on) < self.min_frames or len(off) < self.min_frames:
            self.note = "not enough steady frames - hold the phone still"
            return
        on_cl, off_cl = _cluster(on), _cluster(off)
        results = []
        for c in on_cl:
            p_on = c.hits / float(len(on))
            if p_on < MIN_PERSIST_ON:
                continue
            p_off = max((o.hits / float(len(off)) for o in off_cl
                         if _dist(o.x, o.y, c.x, c.y, c.ar) <= 1.6 * CLUSTER_TOL),
                        default=0.0)
            if p_off > MAX_PERSIST_OFF:
                continue                      # visible without flash = ambient source
            conf = (p_on - p_off) * min(1.0, max(0.35, c.contrast / 120.0))
            results.append(Detection(c.x, c.y, conf, "GLINT", c.contrast))
        results.sort(key=lambda d: -d.confidence)
        self._results, self._results_t = results, now
        self.note = ""

    def _evaluate_passive(self, now):
        frames = self._frames["PASSIVE"]
        if len(frames) < self.min_frames:
            return
        results = []
        for c in _cluster(frames):
            p = c.hits / float(len(frames))
            if p >= 0.6:
                results.append(Detection(c.x, c.y, min(0.4, p * 0.5), "CANDIDATE", c.contrast))
        results.sort(key=lambda d: -d.confidence)
        self._results, self._results_t = results, now
        if __name__== "__main__":
            import cv2
            print(f"Testing with WORK_WIDTH={WORK_WIDTH}")
            h, w = 240, 320
            off = np.zeros((h,w), dtype=np.uint8)
            on = np.zeros((h,w), dtype=np.uint8)
            cv2.circle(on, (160,120), 2, 255, -1)
            print("OK - detector loads. Now call your detect function")

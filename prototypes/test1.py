"""
Synthetic-scene tests for glint_detector.py. Run:  python test_glint_detector.py

These prove the LOGIC behaves as designed on physics-inspired fake frames.
They do NOT prove real-world accuracy -- only testing on a real phone in a real
room can do that.
"""
import sys
import time

import cv2
import numpy as np

import glint_detector as gd

H, W = 480, 640


class Scene(object):
    """Renders RGBA frames. `items` = dict(kind, x, y, r, peak, on_only, color)."""

    def __init__(self, items=(), seed=0, texture=False, hot_pixels=True, noise=4.0):
        self.items = list(items)
        self.rng = np.random.RandomState(seed)
        self.noise = noise
        self.bg = 22.0
        self.last_t = 0.0
        self.tex = None
        if texture:
            t = self.rng.rand(H + 200, W + 200).astype(np.float32)
            t = cv2.GaussianBlur(t, (0, 0), 6)
            self.tex = (t - t.min()) / (t.max() - t.min()) * 60.0
        self.hot = [(self.rng.randint(0, H), self.rng.randint(0, W)) for _ in range(12)] \
            if hot_pixels else []

    def render(self, t, torch_on, shift=(0, 0), jitter=0.0):
        dt = max(0.0, t - self.last_t)
        self.last_t = t
        target = 47.0 if torch_on else 22.0           # auto-exposure ramps over ~0.3 s
        self.bg += (target - self.bg) * min(1.0, dt / 0.3)
        img = np.full((H, W), self.bg, np.float32)
        if self.tex is not None:
            dy, dx = 100 + int(shift[0]), 100 + int(shift[1])
            img += self.tex[dy:dy + H, dx:dx + W]
        rgb = np.stack([img, img, img], axis=2)
        for it in self.items:
            if it.get("on_only") and not torch_on:
                continue
            x = it["x"] + (self.rng.randn() * jitter if jitter else 0) + shift[1]
            y = it["y"] + (self.rng.randn() * jitter if jitter else 0) + shift[0]
            layer = np.zeros((H, W), np.float32)
            if it["kind"] == "circle":
                cv2.circle(layer, (int(round(x)), int(round(y))), it["r"], 1.0, -1)
            elif it["kind"] == "rect":
                cv2.rectangle(layer, (int(x), int(y)), (int(x + it["w"]), int(y + it["h"])), 1.0, -1)
            layer = cv2.GaussianBlur(layer, (0, 0), 0.8)
            color = it.get("color", (1.0, 1.0, 1.0))
            for c in range(3):
                rgb[..., c] = np.maximum(rgb[..., c], layer * it["peak"] * color[c])
        rgb += self.rng.randn(H, W, 1).astype(np.float32) * 0 + \
            self.rng.randn(H, W, 3).astype(np.float32) * self.noise
        for (hy, hx) in self.hot:
            rgb[hy, hx, :] = 255
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        return np.dstack([rgb, np.full((H, W, 1), 255, np.uint8)])


def run(scene, duration=13.0, fps=10, torch_available=True, motion=None, jitter=0.0):
    sc = gd.GlintScanner(torch_available=torch_available)
    torch, log, statuses = False, [], []
    rng = np.random.RandomState(1)
    for i in range(int(duration * fps)):
        t = i / float(fps)
        shift = (0, 0)
        if motion:
            shift = (rng.randint(-motion, motion + 1), rng.randint(-motion, motion + 1))
        frame = scene.render(t, torch, shift=shift, jitter=jitter)
        st = sc.tick(frame, now=t)
        torch = st.want_torch
        statuses.append(st.status)
        log.append(st.detections)
    return log, statuses


def final_dets(log):
    return log[-1] if log else []


def near(det, x, y, tol=0.03):
    return abs(det.x - x / float(W)) < tol and abs(det.y - y / float(H)) < tol


results = []


def check(name, cond, detail=""):
    results.append(cond)
    print("%-4s %-52s %s" % ("PASS" if cond else "FAIL", name, detail))


def circle(x, y, r=3, peak=255, on_only=False, color=(1, 1, 1)):
    return dict(kind="circle", x=x, y=y, r=r, peak=peak, on_only=on_only, color=color)


print("--- flash-differential mode ---")

log, _ = run(Scene([circle(200, 150, on_only=True)]))
d = final_dets(log)
check("A  lens glint (flash-only) is detected",
      len(d) == 1 and d[0].kind == "GLINT" and near(d[0], 200, 150),
      "-> %s" % [(round(x.x * W), round(x.y * H), round(x.confidence, 2)) for x in d])

first = next((i for i, dd in enumerate(log) if dd), None)
check("A2 time to first detection <= 6 s", first is not None and first / 10.0 <= 6.0,
      "-> %.1f s" % (first / 10.0 if first is not None else -1))

log, _ = run(Scene([circle(200, 150)]))
check("B  ambient LED (always on) is NOT flagged", not any(log), "")

log, _ = run(Scene([dict(kind="rect", x=400, y=60, w=90, h=70, peak=250)]))
check("C  bright window is NOT flagged", not any(log), "")

log, _ = run(Scene([circle(300, 250, r=14, on_only=True)]))
check("D  large flash-only reflection (mirror) NOT flagged", not any(log), "")

log, _ = run(Scene([circle(200, 150, on_only=True)]), jitter=3.0)
d = final_dets(log)
check("E  glint survives +-3 px hand shake", len(d) == 1 and near(d[0], 200, 150, 0.05),
      "-> %d detections" % len(d))

log, _ = run(Scene([circle(200, 150, peak=110, on_only=True)]))
check("F  weak reflection (peak 110) NOT flagged (sensitivity limit)", not any(log), "")

log, sts = run(Scene([circle(200, 150, on_only=True)], texture=True), motion=45)
check("G  fast panning => HOLD STEADY, no detections",
      not any(log) and "HOLD STEADY" in sts,
      "-> %d hold-steady frames" % sts.count("HOLD STEADY"))

log, _ = run(Scene([circle(200, 150, on_only=True, color=(1, 0, 0))]))
check("I  coloured (red) flash-only spot ignored", not any(log), "")

log, _ = run(Scene([circle(150, 120, on_only=True), circle(480, 330, on_only=True)]))
d = final_dets(log)
check("K  two lenses both detected",
      len(d) == 2 and any(near(x, 150, 120) for x in d) and any(near(x, 480, 330) for x in d),
      "-> %d detections" % len(d))

log, _ = run(Scene([dict(kind="rect", x=250, y=200, w=32, h=2, peak=255, on_only=True)]))
check("L  thin streak (flash-only) NOT flagged", not any(log), "")

log, _ = run(Scene([circle(200, 150, r=1, on_only=True)]))
d = final_dets(log)
check("M  tiny 1 px-radius glint still detected", len(d) == 1 and near(d[0], 200, 150),
      "-> %d detections" % len(d))

log, _ = run(Scene([circle(200, 150, on_only=True), circle(420, 300)]))
d = final_dets(log)
check("N  glint found, ambient LED beside it ignored",
      len(d) == 1 and near(d[0], 200, 150), "-> %d detections" % len(d))

print("--- false-alarm soak test ---")
alarms, steady = 0, 0
for seed in range(6):
    log, _ = run(Scene([], seed=seed), duration=60.0)
    alarms += sum(1 for dd in log if dd)
    steady += len(log)
check("H  noise+hot pixels only: zero alarms in 6x60 s", alarms == 0,
      "-> %d alarm frames of %d" % (alarms, steady))

alarms = 0
for seed in range(3):
    log, _ = run(Scene([circle(300 + 20 * seed, 200)], seed=seed), duration=60.0)
    alarms += sum(1 for dd in log if dd)
check("H2 ambient LED only: zero alarms in 3x60 s", alarms == 0, "-> %d alarm frames" % alarms)

print("--- passive fallback (no torch control) ---")
log, sts = run(Scene([circle(200, 150)]), torch_available=False, duration=14.0)
d = final_dets(log)
check("J  passive mode reports CANDIDATE, never GLINT",
      len(d) >= 1 and all(x.kind == "CANDIDATE" for x in d) and d[0].confidence <= 0.4,
      "-> %s" % [(x.kind, round(x.confidence, 2)) for x in d])

print("--- speed ---")
sc = gd.GlintScanner()
scene = Scene([circle(200, 150, on_only=True)])
frames = [scene.render(i / 10.0, i % 2 == 0) for i in range(30)]
t0 = time.perf_counter()
for i, f in enumerate(frames):
    sc.tick(f, now=i / 10.0)
ms = (time.perf_counter() - t0) / len(frames) * 1000.0
check("S  per-frame cost at 640x480 (desktop CPU)", ms < 25.0, "-> %.1f ms/frame" % ms)

print("\n%d/%d checks passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
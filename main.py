"""
Hidden-Camera Detector - integrated Kivy app (Android)

Home menu with four working modules:
  1. EMF / Magnetic   - magnetometer baseline + anomaly alarm        (physical)
  2. Lens Glint       - camera: tiny, persistent, very bright spots  (visual)
  3. Stealth IR       - camera: purple-white IR LED glow in the dark (visual)
  4. Network Scan     - LAN scan for camera ports, ONVIF, RTSP       (digital)

Every module raises the same alarm (beeps + vibration) when it finds something.
Results are INDICATORS, not proof: always inspect the flagged spot by hand.
Only scan networks you own or are authorised to test.
"""
import math
import os
import random
import re
import socket
import struct
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from kivy.app import App
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.camera import Camera
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.screenmanager import Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.utils import platform

try:
    from plyer import magnetometer, vibrator
except Exception:
    magnetometer = None
    vibrator = None

GREEN, ORANGE, RED, BLUE, YELLOW = (0, 1, 0, 1), (1, .6, 0, 1), (1, 0, 0, 1), (0, .7, 1, 1), (1, 1, 0, 1)


# ==========================================================================
# Shared: alarm (generated tones + vibration) and Android permissions
# ==========================================================================
def make_tone(path, freq, duration, volume=0.9, rate=22050):
    n = int(rate * duration)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / (rate * 0.005), (n - i) / (rate * 0.01))
        frames += struct.pack("<h", int(32767 * volume * env * math.sin(2 * math.pi * freq * i / rate)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


class Alarm:
    def __init__(self):
        self.muted = False
        self.sounds = {}
        self.last_beep = 0.0
        self.last_vibe = 0.0
        app = App.get_running_app()
        folder = app.user_data_dir if app else os.getcwd()
        os.makedirs(folder, exist_ok=True)
        for name, (freq, dur) in {"tick": (900, 0.08), "alarm": (1900, 0.12)}.items():
            path = os.path.join(folder, name + ".wav")
            try:
                if not os.path.exists(path):
                    make_tone(path, freq, dur)
                self.sounds[name] = SoundLoader.load(path)
            except Exception as e:
                print("audio error", name, e)

    def beep(self, name="tick", min_interval=0.3, vibrate=0.0):
        now = time.time()
        if self.muted or now - self.last_beep < min_interval:
            return
        self.last_beep = now
        snd = self.sounds.get(name)
        if snd:
            try:
                snd.stop()
                snd.play()
            except Exception:
                pass
        if vibrate and vibrator and now - self.last_vibe > 0.6:
            self.last_vibe = now
            try:
                vibrator.vibrate(time=vibrate)
            except Exception:
                pass


def request_permissions():
    if platform == "android":
        try:
            from android.permissions import Permission, request_permissions as rp
            rp([Permission.CAMERA])
        except Exception as e:
            print("permission request failed:", e)


# ==========================================================================
# Base class for module screens
# ==========================================================================
class ModuleScreen(Screen):
    title = "Module"

    def __init__(self, alarm, **kw):
        super().__init__(**kw)
        self.alarm = alarm
        self.root_box = BoxLayout(orientation="vertical", padding=12, spacing=8)
        top = BoxLayout(size_hint_y=None, height=48, spacing=8)
        back = Button(text="< Back", size_hint_x=0.3)
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "home"))
        top.add_widget(back)
        top.add_widget(Label(text=self.title, bold=True, font_size="20sp"))
        mute = Button(text="Mute", size_hint_x=0.3)
        mute.bind(on_release=lambda b: self._toggle_mute(b))
        top.add_widget(mute)
        self.root_box.add_widget(top)
        self.add_widget(self.root_box)
        self.build_body(self.root_box)

    def _toggle_mute(self, btn):
        self.alarm.muted = not self.alarm.muted
        btn.text = "Unmute" if self.alarm.muted else "Mute"

    def build_body(self, box):
        pass

    def on_enter(self, *a):
        self.start()

    def on_leave(self, *a):
        self.stop()

    def start(self):
        pass

    def stop(self):
        pass


# ==========================================================================
# 1. EMF / magnetic module
# ==========================================================================
class EMFScreen(ModuleScreen):
    title = "EMF / Magnetic"
    NOTICE, WARNING, DANGER = 4.0, 12.0, 30.0   # uT change from baseline
    SMOOTH, CAL_SECONDS, HZ = 0.35, 4.0, 20.0

    def build_body(self, box):
        self.value = Label(text="-- uT", font_size="36sp", size_hint_y=0.2)
        self.delta = Label(text="Change vs baseline: --", size_hint_y=0.1)
        self.bar = ProgressBar(max=self.DANGER * 1.5, size_hint_y=0.08)
        self.peak_lbl = Label(text="Peak: 0.0 uT", size_hint_y=0.07)
        self.status = Label(text="", font_size="20sp", bold=True, halign="center", size_hint_y=0.25)
        self.status.bind(size=lambda w, s: setattr(w, "text_size", (s[0], None)))
        cal = Button(text="Recalibrate", size_hint_y=0.12)
        cal.bind(on_release=lambda *_: self.calibrate())
        for w in (self.value, self.delta, self.bar, self.peak_lbl, self.status, cal):
            box.add_widget(w)
        self.sim = False
        self.ev = None
        self._t = 0.0

    def start(self):
        self.sim = False
        try:
            magnetometer.enable()
        except Exception as e:
            print("magnetometer unavailable -> simulation:", e)
            self.sim = True
        self.filtered = None
        self.calibrate()
        self.ev = Clock.schedule_interval(self.update, 1.0 / self.HZ)

    def stop(self):
        if self.ev:
            self.ev.cancel()
            self.ev = None
        if not self.sim:
            try:
                magnetometer.disable()
            except Exception:
                pass

    def calibrate(self):
        self.baseline, self.samples, self.cal_t0, self.peak = None, [], time.time(), 0.0
        self.status.text = "Calibrating...\nHold phone still, away from electronics/metal"
        self.status.color = BLUE

    def read(self):
        if self.sim:
            self._t += 0.05
            v = 45 + random.gauss(0, 0.4)
            return v + (random.uniform(15, 45) if int(self._t) % 20 > 15 else 0)
        try:
            x, y, z = magnetometer.field
        except Exception:
            return None
        if None in (x, y, z):
            return None
        return math.sqrt(x * x + y * y + z * z)

    def update(self, dt):
        raw = self.read()
        if raw is None:
            self.status.text = "Waiting for magnetometer..."
            return
        self.filtered = raw if self.filtered is None else self.filtered + self.SMOOTH * (raw - self.filtered)
        self.value.text = f"{self.filtered:.1f} uT"
        if self.baseline is None:
            self.samples.append(raw)
            if time.time() - self.cal_t0 >= self.CAL_SECONDS and len(self.samples) >= 10:
                self.baseline = sorted(self.samples)[len(self.samples) // 2]
                self.status.text, self.status.color = "Scanning - move slowly along walls/floor", GREEN
            return
        d = abs(self.filtered - self.baseline)
        if d < self.NOTICE:
            self.baseline += 0.01 * (self.filtered - self.baseline)
        self.peak = max(d, self.peak * 0.995)
        self.delta.text = f"Change vs baseline: {d:.1f} uT (baseline {self.baseline:.1f})"
        self.peak_lbl.text = f"Peak: {self.peak:.1f} uT"
        self.bar.value = min(d, self.bar.max)
        if d >= self.DANGER:
            self.status.text, self.status.color = "DANGER: strong magnetic source!\nPossible hidden device", RED
            self.alarm.beep("alarm", 0.10, vibrate=0.3)
        elif d >= self.WARNING:
            f = (d - self.WARNING) / (self.DANGER - self.WARNING)
            self.status.text, self.status.color = "WARNING: magnetic anomaly\nInspect this area", ORANGE
            self.alarm.beep("tick", 0.40 - 0.25 * f, vibrate=0.15)
        elif d >= self.NOTICE:
            f = (d - self.NOTICE) / (self.WARNING - self.NOTICE)
            self.status.text, self.status.color = "Slight anomaly - keep scanning", YELLOW
            self.alarm.beep("tick", 1.0 - 0.5 * f)
        else:
            self.status.text, self.status.color = "Scanning - normal background", GREEN


# ==========================================================================
# Camera-based modules (lens glint + stealth IR)
# ==========================================================================
def find_blobs(mask, max_pixels=60):
    """4-connected components on a small boolean grid. Returns list of (cy, cx, size)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    blobs = []
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        stack, pts = [(y, x)], []
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            pts.append((cy, cx))
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(pts) <= max_pixels:
            ys, xs = zip(*pts)
            blobs.append((sum(ys) / len(ys), sum(xs) / len(xs), len(pts)))
    return blobs


class CameraScreen(ModuleScreen):
    STEP = 4            # analyse every 4th pixel
    FPS = 10.0
    NEED_FRAMES = 6     # a candidate must persist this many frames
    MATCH_DIST = 3.0    # grid cells
    idle_text = ""

    def build_body(self, box):
        self.cam_holder = BoxLayout(size_hint_y=0.55)
        self.info = Label(text="", size_hint_y=0.12)
        self.status = Label(text="", font_size="18sp", bold=True, halign="center", size_hint_y=0.2)
        self.status.bind(size=lambda w, s: setattr(w, "text_size", (s[0], None)))
        sw = Button(text="Switch camera", size_hint_y=0.1)
        sw.bind(on_release=lambda *_: self.switch_camera())
        for w in (self.cam_holder, self.info, self.status, sw):
            box.add_widget(w)
        self.cam_index, self.cam, self.ev, self.tracks = 0, None, None, []

    def start(self):
        request_permissions()
        self.tracks = []
        self.open_camera()
        self.ev = Clock.schedule_interval(self.analyse, 1.0 / self.FPS)

    def stop(self):
        if self.ev:
            self.ev.cancel()
            self.ev = None
        self.close_camera()

    def open_camera(self):
        self.close_camera()
        try:
            self.cam = Camera(index=self.cam_index, play=True, resolution=(640, 480))
            self.cam_holder.add_widget(self.cam)
        except Exception as e:
            self.status.text, self.status.color = f"Camera error: {e}", RED

    def close_camera(self):
        if self.cam:
            self.cam.play = False
            self.cam_holder.remove_widget(self.cam)
            self.cam = None

    def switch_camera(self):
        self.cam_index = 1 - self.cam_index
        self.tracks = []
        self.open_camera()

    def small_rgb(self):
        if not self.cam or not self.cam.texture:
            return None
        w, h = self.cam.texture.size
        try:
            arr = np.frombuffer(self.cam.texture.pixels, dtype=np.uint8).reshape(h, w, 4)
        except Exception:
            return None
        return arr[::self.STEP, ::self.STEP, :3].astype(np.int16)

    def candidate_mask(self, rgb):
        raise NotImplementedError

    def verdict(self, where, n):
        raise NotImplementedError

    def analyse(self, dt):
        rgb = self.small_rgb()
        if rgb is None:
            self.status.text = "Waiting for camera..."
            return
        mask = self.candidate_mask(rgb)
        if mask.sum() > 0.02 * mask.size:      # scene blown out / wrong lighting
            self.status.text, self.status.color = "Too bright - dim the room lights", BLUE
            self.tracks = []
            return
        h, w = mask.shape
        new_tracks = []
        for cy, cx, size in find_blobs(mask):
            hit = next((t for t in self.tracks if math.hypot(t["y"] - cy, t["x"] - cx) <= self.MATCH_DIST), None)
            new_tracks.append({"y": cy, "x": cx, "age": hit["age"] + 1 if hit else 1})
        self.tracks = new_tracks
        persistent = [t for t in self.tracks if t["age"] >= self.NEED_FRAMES]
        self.info.text = f"Candidates: {len(self.tracks)}   Persistent: {len(persistent)}"
        if persistent:
            t = max(persistent, key=lambda t: t["age"])
            where = "left" if t["x"] < w / 3 else "right" if t["x"] > 2 * w / 3 else "centre"
            where += " / " + ("top" if t["y"] < h / 3 else "bottom" if t["y"] > 2 * h / 3 else "middle")
            self.verdict(where, len(persistent))
        else:
            self.status.text, self.status.color = self.idle_text, GREEN


class GlintScreen(CameraScreen):
    title = "Lens Glint"
    idle_text = "Scanning...\nDim the room, put a torch next to the phone camera, sweep slowly"

    def candidate_mask(self, rgb):
        bright = rgb.max(axis=2) >= 245
        # keep only spots surrounded by a much darker neighbourhood (lens retro-reflection)
        from numpy.lib.stride_tricks import sliding_window_view
        g = rgb.mean(axis=2)
        local = sliding_window_view(np.pad(g, 3, mode="edge"), (7, 7)).mean(axis=(2, 3))
        return bright & (local < 140)

    def verdict(self, where, n):
        self.status.text = f"POSSIBLE LENS at {where}\n({n} persistent glint) - inspect it"
        self.status.color = RED
        self.alarm.beep("alarm", 0.15, vibrate=0.3)


class IRScreen(CameraScreen):
    title = "Stealth IR"
    idle_text = "Scanning...\nTurn lights off; use the FRONT camera; sweep ceilings and corners"

    def candidate_mask(self, rgb):
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        # IR LEDs look pale violet/white to phone sensors: red and blue high, green lower
        return (np.maximum(r, b) >= 200) & ((r + b) // 2 - g >= 35)

    def verdict(self, where, n):
        self.status.text = f"IR EMITTER suspected at {where}\n({n} persistent source)"
        self.status.color = RED
        self.alarm.beep("alarm", 0.15, vibrate=0.3)


# ==========================================================================
# 4. Network scan module
# ==========================================================================
CAMERA_PORTS = {
    554: ("RTSP video stream", 3), 8554: ("RTSP alt", 3),
    37777: ("Dahua DVR/camera", 3), 34567: ("XMeye/DVR", 3),
    8000: ("Hikvision SDK", 2), 1935: ("RTMP stream", 2),
    8080: ("HTTP alt", 1), 8081: ("HTTP alt", 1), 81: ("HTTP alt", 1),
    80: ("HTTP", 0), 443: ("HTTPS", 0), 8888: ("HTTP alt", 0),
}
CAMERA_WORDS = re.compile(r"hikvision|dahua|ipcam|webcam|camera|dvr|nvr|onvif|netcam|foscam|"
                          r"reolink|amcrest|tapo|wyze", re.I)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def probe_host(ip):
    open_ports = []
    for port in CAMERA_PORTS:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            if s.connect_ex((ip, port)) == 0:
                open_ports.append(port)
        except Exception:
            pass
        finally:
            s.close()
    if not open_ports:
        return None
    score = max(CAMERA_PORTS[p][1] for p in open_ports)
    banner = ""
    for port in (p for p in open_ports if p in (80, 8080, 81, 8081, 8888)):
        try:
            s = socket.create_connection((ip, port), timeout=0.8)
            s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
            banner = s.recv(2048).decode("latin-1", "ignore")
            s.close()
            if CAMERA_WORDS.search(banner):
                score += 3
                break
        except Exception:
            pass
    return {"ip": ip, "ports": open_ports, "score": score, "banner": banner[:60].replace("\r\n", " ")}


def onvif_discover(timeout=3.0):
    """WS-Discovery probe: ONVIF cameras answer this on UDP 3702."""
    probe = ('<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
             'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
             'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
             'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:%s</w:MessageID>'
             '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
             '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
             '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'
             % uuid.uuid4()).encode()
    found = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        s.sendto(probe, ("239.255.255.250", 3702))
        end = time.time() + timeout
        while time.time() < end:
            try:
                _, addr = s.recvfrom(65535)
                found.add(addr[0])
            except socket.timeout:
                pass
    except Exception as e:
        print("onvif probe failed:", e)
    finally:
        s.close()
    return found


class NetworkScreen(ModuleScreen):
    title = "Network Scan"

    def build_body(self, box):
        self.info = Label(text="Tap Scan to search your Wi-Fi network for cameras.", size_hint_y=0.1)
        self.bar = ProgressBar(max=254, size_hint_y=0.05)
        self.result = Label(text="", halign="left", valign="top", size_hint_y=None, markup=True)
        self.result.bind(texture_size=lambda w, s: setattr(w, "height", s[1]),
                         width=lambda w, v: setattr(w, "text_size", (v, None)))
        sv = ScrollView()
        sv.add_widget(self.result)
        self.btn = Button(text="Scan network", size_hint_y=0.12)
        self.btn.bind(on_release=lambda *_: self.scan())
        for w in (self.info, self.bar, sv, self.btn):
            box.add_widget(w)
        self.running = False

    def stop(self):
        self.running = False

    def scan(self):
        if self.running:
            return
        ip = local_ip()
        if not ip:
            self.info.text = "Not connected to a network."
            return
        self.running, self.bar.value, self.result.text = True, 0, ""
        self.btn.text = "Scanning..."
        self.info.text = f"Your IP: {ip}  (scanning {ip.rsplit('.', 1)[0]}.1-254)"
        threading.Thread(target=self._scan_thread, args=(ip,), daemon=True).start()

    def _scan_thread(self, my_ip):
        prefix = my_ip.rsplit(".", 1)[0]
        onvif = onvif_discover()
        hosts, done = [], [0]

        def work(i):
            ip = f"{prefix}.{i}"
            r = probe_host(ip) if ip != my_ip and self.running else None
            done[0] += 1
            Clock.schedule_once(lambda dt: setattr(self.bar, "value", done[0]))
            return r

        with ThreadPoolExecutor(max_workers=64) as ex:
            for r in ex.map(work, range(1, 255)):
                if r:
                    hosts.append(r)
        for ip in onvif:
            h = next((x for x in hosts if x["ip"] == ip), None)
            if h:
                h["score"] += 4
                h["onvif"] = True
            else:
                hosts.append({"ip": ip, "ports": [], "score": 4, "banner": "", "onvif": True})
        hosts.sort(key=lambda h: -h["score"])
        Clock.schedule_once(lambda dt: self._show(hosts))

    def _show(self, hosts):
        lines, flagged = [], 0
        for h in hosts:
            if h["score"] >= 3:
                tag, flagged = "[color=ff3333]SUSPECT CAMERA[/color]", flagged + 1
            elif h["score"] >= 1:
                tag = "[color=ffaa00]Possible[/color]"
            else:
                tag = "[color=33ff33]Normal device[/color]"
            names = ", ".join(f"{p} ({CAMERA_PORTS[p][0]})" for p in h["ports"]) or "none"
            extra = "  ONVIF responder" if h.get("onvif") else ""
            lines.append(f"{tag}  {h['ip']}\n   ports: {names}{extra}\n   {h['banner']}\n")
        self.result.text = "\n".join(lines) or "No devices with camera-related ports found."
        self.info.text = f"Done: {len(hosts)} device(s) of interest, {flagged} suspect."
        self.btn.text, self.running = "Scan network", False
        if flagged:
            self.alarm.last_beep = 0
            self.alarm.beep("alarm", 0, vibrate=0.5)


# ==========================================================================
# Home + app
# ==========================================================================
class HomeScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=20, spacing=14)
        box.add_widget(Label(text="Hidden Camera Detector", font_size="26sp", bold=True, size_hint_y=0.15))
        for text, target in (("EMF / Magnetic  (physical)", "emf"),
                             ("Lens Glint  (optical)", "glint"),
                             ("Stealth IR  (optical)", "ir"),
                             ("Network Scan  (digital)", "net")):
            b = Button(text=text, font_size="18sp")
            b.bind(on_release=lambda _, t=target: setattr(self.manager, "current", t))
            box.add_widget(b)
        box.add_widget(Label(text="Results are indicators - always inspect by hand.",
                             size_hint_y=0.1, font_size="13sp"))
        self.add_widget(box)


class DetectorApp(App):
    def build(self):
        self.title = "Hidden Camera Detector"
        request_permissions()
        alarm = Alarm()
        self.sm = ScreenManager()
        self.sm.add_widget(HomeScreen(name="home"))
        self.sm.add_widget(EMFScreen(alarm, name="emf"))
        self.sm.add_widget(GlintScreen(alarm, name="glint"))
        self.sm.add_widget(IRScreen(alarm, name="ir"))
        self.sm.add_widget(NetworkScreen(alarm, name="net"))
        return self.sm

    def on_pause(self):
        self.sm.current_screen.stop() if hasattr(self.sm.current_screen, "stop") else None
        return True

    def on_resume(self):
        self.sm.current_screen.start() if hasattr(self.sm.current_screen, "start") else None


if __name__ == "__main__":
    DetectorApp().run()

import cv2
import numpy as np
import socket
import threading
import time
import math
import os
import platform
from datetime import datetime

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import Image
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.graphics.texture import Texture
from kivy.utils import platform as kivy_platform

# Safely bind to physical smartphone magnetometer and notifications via Plyer
try:
    from plyer import spatialorientation, notification
except ImportError:
    spatialorientation = None
    notification = None

# Pull Android dynamic runtime permission triggers if executing inside an APK container
if kivy_platform == 'android':
    from android.permissions import request_permissions, Permission

# Global cross-thread data storage containers
net_status = "Initializing Scanner..."
net_count = 0
device_ip = "0.0.0.0"
VAULT_DIR = "evidence_vault"

if not os.path.exists(VAULT_DIR):
    os.makedirs(VAULT_DIR)

# ---------------------------------------------------------------------
# EMBEDDED KIVY DESIGN LAYOUT SCREEN (Cyber-Sentinel Logo Theme)
# ---------------------------------------------------------------------
Builder.load_string('''
<EMFScannerDashboard>:
    orientation: 'vertical'
    padding: 20
    spacing: 15
    canvas.before:
        Color:
            rgba: 0.0, 0.0, 0.0, 1  # Pure Black background matching your logo
        Rectangle:
            pos: self.pos
            size: self.size

    # TOP TRACKING CONSOLE HEADER
    BoxLayout:
        orientation: 'horizontal'
        size_hint_y: 0.10
        padding: 10
        canvas.before:
            Color:
                rgba: 0.0, 0.15, 0.05, 0.3  # Transparent dark neon green bar glow
            Rectangle:
                pos: self.pos
                size: self.size
            Color:
                rgba: 0.12, 0.8, 0.23, 1  # Solid neon green circuit line borders
            Line:
                rectangle: (self.x, self.y, self.width, self.height)
                width: 1.2

        Label:
            text: "[b]ARGUS RS-28S : SENTINEL CORE[/b]"
            markup: True
            font_size: '20sp'
            color: 0.12, 0.8, 0.23, 1  # Electric Circuit Green
            halign: 'left'
            text_size: self.size
            valign: 'middle'

    # CENTER RADAR MONITOR BLOCK (The Circuit Board Card split for live viewfinder)
    BoxLayout:
        orientation: 'vertical'
        size_hint_y: 0.62
        padding: 15
        spacing: 10
        canvas.before:
            Color:
                rgba: 0.02, 0.04, 0.02, 1  # Matrix dark green hue tint
            Rectangle:
                pos: self.pos
                size: self.size
            Color:
                rgba: 0.12, 0.8, 0.23, 0.6  # Glowing green tracking border corners
            Line:
                rectangle: (self.x, self.y, self.width, self.height)
                width: 1.5

        # LIVE OPENCV CAMERA SCANNER VIEWFINDER
        Image:
            id: video_live_feed
            size_hint_y: 0.55
            allow_stretch: True
            keep_ratio: True

        # REAL-TIME TEXT LOG WINDOW
        Label:
            id: telemetry_display
            text: "Requesting core operating permissions..."
            markup: True
            font_size: '14sp'
            line_height: 1.3
            halign: 'center'
            valign: 'middle'
            text_size: self.size
            size_hint_y: 0.45

    # BOTTOM CONTROLLER INTERACTIVE RADARS
    BoxLayout:
        orientation: 'vertical'
        size_hint_y: 0.28
        spacing: 10

        Button:
            id: action_btn
            text: "INITIALIZE MAGNETIC CALIBRATION"
            font_size: '14sp'
            bold: True
            background_normal: ''
            background_color: 0.0, 0.15, 0.05, 1  # Dark green button base matrix
            color: 0.12, 0.8, 0.23, 1  # Text matches neon logo lines
            size_hint_y: 0.45
            on_press: root.start_calibration(self)
            canvas.before:
                Color:
                    rgba: 0.12, 0.8, 0.23, 1
                    # FIX #2: original had "0.12, \\n 8, 0.23, 1" split across a
                    # line break with "8" instead of "0.8" -> invalid KV syntax,
                    # this crashed Builder.load_string() before any Python ran.
                Line:
                    rectangle: (self.x, self.y, self.width, self.height)
                    width: 1.1

        BoxLayout:
            orientation: 'horizontal'
            spacing: 12
            size_hint_y: 0.55

            Button:
                text: "STEALTH IR MODE"
                font_size: '13sp'
                bold: True
                background_normal: ''
                background_color: 0.02, 0.02, 0.02, 1
                color: 0.12, 0.8, 0.23, 0.8
                on_press: root.toggle_scanner_mode(1)
                canvas.before:
                    Color:
                        rgba: 0.12, 0.8, 0.23, 0.4
                    Line:
                        rectangle: (self.x, self.y, self.width, self.height)
                        width: 1

            Button:
                text: "LENS GLINT MODE"
                font_size: '13sp'
                bold: True
                background_normal: ''
                background_color: 0.02, 0.02, 0.02, 1
                color: 0.12, 0.8, 0.23, 0.8
                on_press: root.toggle_scanner_mode(2)
                canvas.before:
                    Color:
                        rgba: 0.12, 0.8, 0.23, 0.4
                    Line:
                        rectangle: (self.x, self.y, self.width, self.height)
                        width: 1
''')


def trigger_android_notification(title, message):
    """Fires native Android system status bar push notifications."""
    if notification:
        try:
            notification.notify(
                title=title,
                message=message,
                app_name="Argus RS-28S",
                timeout=3
            )
        except Exception:
            pass


def background_network_scanner():
    """MODEL 1: DIGITAL APPROACH - Background network scanner thread looking for active RTSP loops."""
    global net_status, net_count, device_ip
    while True:
        try:
            dummy_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            dummy_sock.connect(("8.8.8.8", 80))
            device_ip = dummy_sock.getsockname()[0]
            dummy_sock.close()

            ip_parts = device_ip.split('.')
            subnet_prefix = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}."

            discovered_streams = 0
            for host in range(1, 255):
                target_ip = f"{subnet_prefix}{host}"
                if target_ip == device_ip:
                    continue

                scan_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # FIX #4: 0.02s (20ms) is too tight for a real TCP handshake over
                # Wi-Fi and will produce false negatives (real cameras missed).
                # Raised to 0.25s -- still fast enough for a /24 sweep.
                scan_sock.settimeout(0.25)
                if scan_sock.connect_ex((target_ip, 554)) == 0:
                    discovered_streams += 1
                scan_sock.close()

            net_count = discovered_streams
            net_status = f"CRITICAL: {net_count} IP CAMERAS" if net_count > 0 else "Wi-Fi Secure"
            if net_count > 0:
                trigger_android_notification("NETWORK EMERGENCY", f"Detected {net_count} active video streams!")
        except Exception:
            net_status = "Network Scan Inactive"
        time.sleep(12.0)


class EMFScannerDashboard(BoxLayout):
    def __init__(self, **kwargs):
        super(EMFScannerDashboard, self).__init__(**kwargs)
        self.ambient_baseline = 40.0
        self.calibration_buffer = []
        self.is_calibrating = False
        self.operational_mode = '1'
        self.last_log_time = 0
        self.magnetometer_active = "Checking Sensor..."
        self.permissions_granted = False

        # Async Camera Capture Pipeline Containers
        self.current_frame = None
        self.capture = None

        # Fire up the dynamic platform permissions checking process before hooking hardware registers
        if kivy_platform == 'android':
            request_permissions([Permission.CAMERA, Permission.ACCESS_FINE_LOCATION], self.permissions_callback)
        else:
            self.permissions_granted = True
            self.initialize_hardware_components()

    def permissions_callback(self, permissions, results):
        if all(results):
            self.permissions_granted = True
            self.initialize_hardware_components()
        else:
            self.ids.telemetry_display.text = "[COLOR=ff3333][b]ERROR: ACCESS DENIED[/b][/COLOR]"

    def initialize_hardware_components(self):
        # 1. Start background network socket worker threads
        net_thread = threading.Thread(target=background_network_scanner, daemon=True)
        net_thread.start()

        # 2. Start OpenCV Video Capture via independent thread to prevent blocking
        video_thread = threading.Thread(target=self.background_camera_worker, daemon=True)
        video_thread.start()

        # 3. Enable Plyer hardware orientation tracking layers
        if spatialorientation:
            try:
                spatialorientation.enable_listener()
                self.magnetometer_active = "Sensor Connected"
            except Exception:
                self.magnetometer_active = "Hardware Chip Missing"
        else:
            self.magnetometer_active = "Simulation Mode"

        # Start main ticker update render loop clocks
        Clock.schedule_interval(self.master_app_processing_engine, 0.05)

    def start_calibration(self, button_widget):
        if not self.permissions_granted:
            return
        self.calibration_buffer = []
        self.is_calibrating = True
        button_widget.text = "CALIBRATING ROOM BASE..."

    def toggle_scanner_mode(self, selected_mode):
        self.operational_mode = str(selected_mode)

    def background_camera_worker(self):
        """MODEL 2: OPENCV OPTICAL SCANNER THREAD ARRAY"""
        while not self.permissions_granted:
            time.sleep(0.5)

        try:
            self.capture = cv2.VideoCapture(0)
            self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        except Exception:
            self.capture = None

        while True:
            if self.capture and self.capture.isOpened():
                ret, frame = self.capture.read()
                if ret:
                    self.current_frame = frame
                else:
                    time.sleep(0.03)
            else:
                sim_frame = np.zeros((480, 640, 3), dtype=np.uint8)
                y_line = int((time.time() * 150) % 440) + 20
                cv2.line(sim_frame, (10, y_line), (630, y_line), (12, 180, 23), 2)
                cv2.putText(sim_frame, "ACTIVE SENTINEL SCAN MATRIX OPEN", (130, 220),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (12, 180, 23), 1, cv2.LINE_AA)
                self.current_frame = sim_frame
                time.sleep(0.03)

    # FIX #1 / #3: this method was called by Clock.schedule_interval() in
    # initialize_hardware_components() but was never defined anywhere in the
    # original file -> guaranteed AttributeError as soon as hardware init ran.
    # Implemented here consistent with the existing widget ids and state
    # variables (video_live_feed, telemetry_display, current_frame,
    # calibration_buffer, magnetometer_active, operational_mode).
    def master_app_processing_engine(self, dt):
        """Main UI tick (~20Hz): pushes camera frames to the viewfinder,
        advances calibration, and refreshes the telemetry log."""

        # 1. Push the latest frame (real or simulated) into the Kivy Image widget
        if self.current_frame is not None:
            frame = self.current_frame
            buf = cv2.flip(frame, 0).tobytes()
            texture = Texture.create(size=(frame.shape[1], frame.shape[0]), colorfmt='bgr')
            texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')
            self.ids.video_live_feed.texture = texture

        # 2. MODEL 3: PHYSICAL APPROACH - advance EMF baseline calibration.
        # Replace `self.ambient_baseline` sampling below with a real reading
        # from spatialorientation/magnetometer once that sensor pipeline is wired up.
        if self.is_calibrating:
            self.calibration_buffer.append(self.ambient_baseline)
            if len(self.calibration_buffer) >= 20:
                self.ambient_baseline = sum(self.calibration_buffer) / len(self.calibration_buffer)
                self.is_calibrating = False
                self.ids.action_btn.text = "INITIALIZE MAGNETIC CALIBRATION"

        # 3. Refresh telemetry text with the latest state from all three models
        self.ids.telemetry_display.text = (
            f"[b]NETWORK:[/b] {net_status}\n"
            f"[b]DEVICE IP:[/b] {device_ip}\n"
            f"[b]EMF SENSOR:[/b] {self.magnetometer_active}\n"
            f"[b]MODE:[/b] {self.operational_mode}"
        )

    def shutdown_hardware(self):
        # FIX #5: nothing previously released the camera handle; on some
        # platforms this leaves the camera locked after the app closes.
        if self.capture is not None:
            try:
                self.capture.release()
            except Exception:
                pass


# FIX #6: the original file ended right after background_camera_worker() with
# no App subclass and no run() call. Without this, Kivy has nothing to create
# a window or start its event loop with -- the script would import fine and
# then just exit, doing nothing visible. This is the entry point that actually
# launches EMFScannerDashboard as the app's root widget.
class ArgusApp(App):
    def build(self):
        self.title = "Argus RS-28S : Sentinel Core"
        self.dashboard = EMFScannerDashboard()
        return self.dashboard

    def on_stop(self):
        # Ensures the camera handle from FIX #5 is released when the app closes
        self.dashboard.shutdown_hardware()


if __name__ == '__main__':
    ArgusApp().run()
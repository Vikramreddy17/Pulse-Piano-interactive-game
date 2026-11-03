
import tkinter as tk
import threading
import asyncio
import serial
import json
import math
import time
import os

from websockets.asyncio.server import serve

# =============================================
# LIDAR MAP ONLY: 149 x 30 INCHES
# LOCALHOST GAME REMAINS: 149 x 80 INCHES
# =============================================

FULL_W_IN = 149.0
HEIGHT_IN = 30.0

DEAD_LEFT_IN = 4.0
DEAD_RIGHT_IN = 4.0
SENSOR_Y_DEFAULT = 15.0

ZONE_COUNT = 10
ACTIVE_W_IN = FULL_W_IN - DEAD_LEFT_IN - DEAD_RIGHT_IN
ZONE_WIDTH_IN = ACTIVE_W_IN / ZONE_COUNT

MM = 25.4
FULL_W = FULL_W_IN * MM
HEIGHT = HEIGHT_IN * MM
X0 = DEAD_LEFT_IN * MM
X1 = (FULL_W_IN - DEAD_RIGHT_IN) * MM

WIN_W = 1490
WIN_H = 300

PORT = "COM7"
BAUD = 230400

WS_HOST = "127.0.0.1"
WS_PORT = 8765

LAYOUT = "piano-149x30-v4"
SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "lidar_piano_settings.json"
)

# LD19
BIN_COUNT = 720
MIN_RANGE = 80
MAX_RANGE = 12000
MIN_INTENSITY = 10
STALE = 0.25

# Background subtraction
DIFF = 75
BG_NEIGHBORS = 3
CAPTURE_SECONDS = 1.5
AUTO_CAPTURE_DELAY = 2.0

# Foot detection
GROUP_GAP = 115
MERGE_DISTANCE = 2.0 * MM

TRACK_GATE = 320
TRACK_TTL = 0.34
MOVE_INTERVAL = 0.075

ZONE_STABLE_FRAMES = 2
ZONE_HYSTERESIS = 0.6 * MM

# A new detection must persist before becoming a foot.
NEW_TRACK_FRAMES = 2

ZONES = [
    ("A", "F4", "#F7BA78"),
    ("B", "G4", "#63DBFA"),
    ("C", "A4", "#B4A1FF"),
    ("D", "B4", "#6EE9B7"),
    ("E", "C5", "#FF9EB7"),
    ("F", "D5", "#88DFF7"),
    ("G", "E5", "#EED388"),
    ("H", "F5", "#D5A8FF"),
    ("I", "G5", "#A8EB91"),
    ("J", "A5", "#FFAE92"),
]


# =============================================
# COORDINATE MAPPING
# =============================================

def zone_index(x, y):
    if not (X0 <= x < X1 and 0 <= y < HEIGHT):
        return None

    index = int(
        (x - X0) / (ZONE_WIDTH_IN * MM)
    )

    if 0 <= index < ZONE_COUNT:
        return index

    return None


# =============================================
# CRC8 FOR LD19
# =============================================

def crc8(data):
    crc = 0

    for byte in data:
        crc ^= byte

        for _ in range(8):
            crc = (
                (crc << 1)
                ^ (0x4D if crc & 0x80 else 0)
            ) & 255

    return crc


# =============================================
# SERIAL READER
# =============================================

class Reader:

    def __init__(self):
        self.lock = threading.Lock()
        self.bins = {}

        self.ok = 0
        self.bad = 0
        self.rpm = 0
        self.last = 0

        self.error = ""
        self.stop = threading.Event()
        self.thread = None

    def start(self, port):
        self.thread = threading.Thread(
            target=self.run,
            args=(port,),
            daemon=True
        )
        self.thread.start()

    def run(self, port):
        try:
            with serial.Serial(
                port,
                BAUD,
                timeout=0.02
            ) as device:

                buffer = bytearray()

                while not self.stop.is_set():
                    buffer.extend(device.read(512))

                    while True:
                        k = buffer.find(b"\x54\x2c")

                        if k < 0:
                            if len(buffer) > 1:
                                del buffer[:-1]
                            break

                        if k:
                            del buffer[:k]

                        if len(buffer) < 47:
                            break

                        packet = bytes(buffer[:47])

                        if crc8(packet[:46]) != packet[46]:
                            with self.lock:
                                self.bad += 1
                            del buffer[0]
                            continue

                        del buffer[:47]

                        speed = int.from_bytes(
                            packet[2:4], "little"
                        )

                        start = int.from_bytes(
                            packet[4:6], "little"
                        )

                        end = int.from_bytes(
                            packet[42:44], "little"
                        )

                        step = (
                            (end - start) % 36000
                        ) / 11

                        now = time.monotonic()

                        with self.lock:
                            for i in range(12):
                                off = 6 + i * 3

                                distance = int.from_bytes(
                                    packet[off:off+2],
                                    "little"
                                )

                                intensity = packet[off+2]

                                angle = (
                                    (start+i*step)/100
                                ) % 360

                                j = int(angle*2) % BIN_COUNT

                                self.bins[j] = (
                                    distance,
                                    intensity,
                                    now
                                )

                            self.rpm = speed / 6
                            self.ok += 1
                            self.last = now

        except (OSError, serial.SerialException) as e:
            with self.lock:
                self.error = str(e)

    def snapshot(self):
        now = time.monotonic()

        with self.lock:
            scan = [
                (j, j*.5, d, intensity)
                for j, (d, intensity, ts)
                in self.bins.items()
                if now-ts < STALE
            ]

            stats = (
                self.ok,
                self.bad,
                self.rpm,
                now-self.last if self.last else 999,
                self.error
            )

        return sorted(scan), stats

    def close(self):
        self.stop.set()

        if self.thread:
            self.thread.join(timeout=0.8)


# =============================================
# WEBSOCKET SERVER
# =============================================

class WebSocketHub:

    def __init__(self):
        self.loop = None
        self.clients = set()
        self.ready = threading.Event()

    async def handler(self, websocket):
        self.clients.add(websocket)

        try:
            async for _ in websocket:
                pass
        finally:
            self.clients.discard(websocket)

    async def broadcast(self, payload):
        if not self.clients:
            return

        message = json.dumps(payload)

        await asyncio.gather(
            *[
                client.send(message)
                for client in list(self.clients)
            ],
            return_exceptions=True
        )

    def send(self, payload):
        if not self.ready.is_set() or self.loop is None:
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast(payload),
                self.loop
            )
        except RuntimeError:
            pass

    async def main(self):
        async with serve(
            self.handler,
            WS_HOST,
            WS_PORT
        ):
            self.ready.set()
            print(
                f"WebSocket ready: ws://{WS_HOST}:{WS_PORT}",
                flush=True
            )

            await asyncio.Future()

    def start(self):
        def worker():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            try:
                self.loop.run_until_complete(self.main())
            except Exception as e:
                print("WebSocket error:", e)
            finally:
                self.ready.clear()
                self.loop.close()

        threading.Thread(
            target=worker,
            daemon=True
        ).start()


# =============================================
# LIDAR STUDIO
# =============================================

class Studio:

    def __init__(self, root):
        self.root = root

        root.title("LD19 Piano Map | 149 x 30 inches")
        root.geometry(f"{WIN_W}x{WIN_H}")
        root.configure(bg="#0b1628")

        self.canvas = tk.Canvas(
            root,
            bg="#0b1628",
            highlightthickness=0,
            bd=0
        )
        self.canvas.pack(fill="both", expand=True)

        self.sensor_y = SENSOR_Y_DEFAULT
        self.forward = 0.0
        self.mirror = False

        self.load_settings()

        self.hub = WebSocketHub()
        self.hub.start()

        self.reader = None

        self.demo = False
        self.demo_down = False
        self.was_demo = False

        self.show_grid = False
        self.show_info = True
        self.fullscreen = False

        self.background_enabled = False
        self.baseline = None

        self.cap_end = None
        self.cap_samples = {}
        self.auto_done = False
        self.first_data = None

        self.tracks = {}
        self.next_id = 1

        self.pending = {}

        self.status = "Starting"
        self.stats_text = ""

        self.canvas.bind(
            "<ButtonPress-1>", self.on_down
        )
        self.canvas.bind(
            "<B1-Motion>", self.on_drag
        )
        self.canvas.bind(
            "<ButtonRelease-1>", self.on_up
        )

        root.bind("<Key>", self.on_key)
        root.protocol(
            "WM_DELETE_WINDOW", self.close
        )

        self.connect()
        self.tick()

    # =========================================
    # CALIBRATION SETTINGS
    # =========================================

    def load_settings(self):
        try:
            with open(
                SETTINGS_FILE,
                encoding="utf-8"
            ) as f:
                data = json.load(f)

            if data.get("layout") != LAYOUT:
                return

            self.sensor_y = max(
                0,
                min(
                    HEIGHT_IN,
                    float(data.get(
                        "sensor_y", self.sensor_y
                    ))
                )
            )

            self.forward = float(
                data.get("forward", self.forward)
            )
            self.mirror = bool(
                data.get("mirror", self.mirror)
            )

        except (OSError, ValueError, TypeError):
            pass

    def save_settings(self):
        try:
            with open(
                SETTINGS_FILE,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump({
                    "layout": LAYOUT,
                    "sensor_y": self.sensor_y,
                    "forward": self.forward,
                    "mirror": self.mirror
                }, f, indent=2)
        except OSError:
            pass

    # =========================================
    # KEYS
    # =========================================

    def on_key(self, event):
        key = event.keysym.lower()

        if key == "h":
            self.show_info = not self.show_info

        elif key == "g":
            self.show_grid = not self.show_grid

        elif key == "d":
            self.demo = not self.demo

        elif key == "c":
            self.start_capture()

        elif key == "r":
            self.connect()

        elif key == "f11":
            self.fullscreen = not self.fullscreen
            self.root.attributes(
                "-fullscreen", self.fullscreen
            )

        elif key == "escape":
            self.fullscreen = False
            self.root.attributes(
                "-fullscreen", False
            )

        elif key in (
            "m", "up", "down", "left", "right"
        ):
            if key == "m":
                self.mirror = not self.mirror

            elif key == "left":
                self.forward = (
                    self.forward-1
                ) % 360

            elif key == "right":
                self.forward = (
                    self.forward+1
                ) % 360

            elif key == "up":
                self.sensor_y = max(
                    0, self.sensor_y-.5
                )

            elif key == "down":
                self.sensor_y = min(
                    HEIGHT_IN, self.sensor_y+.5
                )

            self.clear_tracks()
            self.baseline = None
            self.background_enabled = False
            self.auto_done = True

            self.status = (
                "Calibration changed: press C"
            )

            self.save_settings()

    # =========================================
    # COORDINATE TRANSFORMATION
    # =========================================

    def to_xy(self, angle, distance):
        theta = math.radians(
            angle-self.forward
        )

        sign = -1 if self.mirror else 1

        x = distance*math.cos(theta)
        y = (
            self.sensor_y*MM
            + sign*distance*math.sin(theta)
        )

        return x, y

    # =========================================
    # EVENTS
    # =========================================

    def emit(self, kind, tid, x, y, stable_zone=None):
        index = (
            stable_zone
            if stable_zone is not None
            else zone_index(x, y)
        )

        if kind != "touch_up" and index is None:
            return

        event = {
            "type": kind,
            "id": tid,
            "x_inches": round(x/MM, 4),
            "y_inches": round(y/MM, 4),
            "x_mm": round(x, 1),
            "y_mm": round(y, 1),
            "zone": (
                ZONES[index][0]
                if index is not None else None
            ),
            "note": (
                ZONES[index][1]
                if index is not None else None
            ),
            "timestamp": time.time()
        }

        self.hub.send(event)

        if kind != "touch_move":
            print(json.dumps(event), flush=True)

    def clear_tracks(self):
        for tid, tr in list(self.tracks.items()):
            self.emit(
                "touch_up",
                tid,
                tr["x"],
                tr["y"],
                tr["zone"]
            )

        self.tracks.clear()
        self.pending.clear()

    # =========================================
    # CONNECT
    # =========================================

    def connect(self):
        if self.reader:
            self.reader.close()

            if (
                self.reader.thread
                and self.reader.thread.is_alive()
            ):
                self.status = "Serial still closing"
                return

        self.clear_tracks()

        self.baseline = None
        self.background_enabled = False
        self.cap_end = None
        self.cap_samples = {}
        self.auto_done = False
        self.first_data = None

        self.reader = Reader()
        self.reader.start(PORT)

        self.status = f"Opening {PORT}"

    # =========================================
    # BACKGROUND FILTER
    # =========================================

    def is_background(self, j, d):
        if self.baseline is None:
            return False

        for k in range(
            -BG_NEIGHBORS,
            BG_NEIGHBORS+1
        ):
            old = self.baseline.get(
                (j+k) % BIN_COUNT
            )

            if (
                old is not None
                and abs(old-d) < DIFF
            ):
                return True

        return False

    def filtered(self, scan):
        points = []

        for j, a, d, intensity in scan:
            if not (
                MIN_RANGE <= d <= MAX_RANGE
                and intensity >= MIN_INTENSITY
            ):
                continue

            x, y = self.to_xy(a, d)

            if zone_index(x, y) is None:
                continue

            if (
                self.background_enabled
                and self.is_background(j, d)
            ):
                continue

            points.append((j, x, y))

        return sorted(points)

    # =========================================
    # CLUSTER POINTS
    # =========================================

    def clusters(self, points):
        groups = []
        current = []

        for p in points:
            if current:
                distance = math.hypot(
                    p[1]-current[-1][1],
                    p[2]-current[-1][2]
                )

                if (
                    p[0]-current[-1][0] > 3
                    or distance > GROUP_GAP
                ):
                    groups.append(current)
                    current = []

            current.append(p)

        if current:
            groups.append(current)

        return [
            (
                sum(p[1] for p in group)/len(group),
                sum(p[2] for p in group)/len(group)
            )
            for group in groups
            if 2 <= len(group) <= 28
        ]

    # =========================================
    # MERGE TOE + FOOT: LESS THAN 2 INCHES
    # =========================================

    def merge_nearby(self, detections):
        parents = list(range(len(detections)))

        def find(i):
            while parents[i] != i:
                parents[i] = parents[parents[i]]
                i = parents[i]
            return i

        for i in range(len(detections)):
            for j in range(i+1, len(detections)):
                if math.dist(
                    detections[i],
                    detections[j]
                ) < MERGE_DISTANCE:

                    a = find(i)
                    b = find(j)

                    if a != b:
                        parents[b] = a

        groups = {}

        for i, point in enumerate(detections):
            groups.setdefault(
                find(i), []
            ).append(point)

        return [
            (
                sum(p[0] for p in group)/len(group),
                sum(p[1] for p in group)/len(group)
            )
            for group in groups.values()
        ]

    # =========================================
    # TRACKING
    # =========================================

    def new_track(self, x, y):
        now = time.monotonic()

        return {
            "x": x,
            "y": y,
            "seen": now,
            "sent": now,
            "zone": zone_index(x, y),
            "candidate": None,
            "candidate_count": 0
        }

    def update_track(self, tid, x, y, now):
        tr = self.tracks[tid]

        tr["x"] = .8*x+.2*tr["x"]
        tr["y"] = .8*y+.2*tr["y"]
        tr["seen"] = now

        candidate = zone_index(tr["x"], tr["y"])

        # Boundary hysteresis.
        if (
            candidate is not None
            and tr["zone"] is not None
            and candidate != tr["zone"]
        ):
            boundary = (
                X0
                + max(candidate, tr["zone"])
                * ZONE_WIDTH_IN*MM
            )

            if (
                candidate > tr["zone"]
                and tr["x"] < boundary+ZONE_HYSTERESIS
            ):
                candidate = tr["zone"]

            elif (
                candidate < tr["zone"]
                and tr["x"] > boundary-ZONE_HYSTERESIS
            ):
                candidate = tr["zone"]

        # One confirmed event per zone transition.
        if candidate != tr["zone"]:
            if candidate == tr["candidate"]:
                tr["candidate_count"] += 1
            else:
                tr["candidate"] = candidate
                tr["candidate_count"] = 1

            if tr["candidate_count"] >= ZONE_STABLE_FRAMES:
                tr["zone"] = candidate
                tr["candidate"] = None
                tr["candidate_count"] = 0

                self.emit(
                    "touch_move",
                    tid,
                    tr["x"],
                    tr["y"],
                    tr["zone"]
                )

                tr["sent"] = now

        else:
            tr["candidate"] = None
            tr["candidate_count"] = 0

        # Heartbeats keep the on-screen cursor updated.
        # They must NOT trigger new notes in the browser.
        if now-tr["sent"] >= MOVE_INTERVAL:
            self.emit(
                "touch_move",
                tid,
                tr["x"],
                tr["y"],
                tr["zone"]
            )
            tr["sent"] = now

    def track(self, detections):
        now = time.monotonic()

        # Stable nearest-neighbor association.
        pairs = []

        for i, (x, y) in enumerate(detections):
            for tid, tr in self.tracks.items():
                distance = math.hypot(
                    tr["x"]-x,
                    tr["y"]-y
                )

                if distance < TRACK_GATE:
                    pairs.append((distance, i, tid))

        pairs.sort()

        used_detections = set()
        used_tracks = set()

        for _, i, tid in pairs:
            if (
                i in used_detections
                or tid in used_tracks
            ):
                continue

            used_detections.add(i)
            used_tracks.add(tid)

            self.update_track(
                tid,
                *detections[i],
                now
            )

        # New detections must survive two frames.
        next_pending = {}

        for i, (x, y) in enumerate(detections):
            if i in used_detections:
                continue

            previous = None
            closest = TRACK_GATE

            for key, p in self.pending.items():
                gap = math.hypot(
                    p["x"]-x,
                    p["y"]-y
                )

                if (
                    gap < closest
                    and key not in next_pending
                ):
                    previous = key
                    closest = gap

            if previous is not None:
                p = self.pending[previous]
                p["x"], p["y"] = x, y
                p["count"] += 1
                p["seen"] = now
                key = previous
            else:
                key = self.next_id
                self.next_id += 1

                p = {
                    "x": x,
                    "y": y,
                    "count": 1,
                    "seen": now
                }

            if p["count"] >= NEW_TRACK_FRAMES:
                # Do not create a duplicate track close to
                # an existing foot, including temporarily
                # occluded tracks.
                close_existing = any(
                    math.hypot(
                        tr["x"]-x,
                        tr["y"]-y
                    ) < MERGE_DISTANCE
                    for tr in self.tracks.values()
                )

                if not close_existing:
                    self.tracks[key] = self.new_track(x, y)
                    tr = self.tracks[key]

                    self.emit(
                        "touch_down",
                        key,
                        x, y,
                        tr["zone"]
                    )

            else:
                next_pending[key] = p

        self.pending = next_pending

        # Touch ends only after continuous disappearance.
        for tid, tr in list(self.tracks.items()):
            if (
                tid not in used_tracks
                and now-tr["seen"] >= TRACK_TTL
            ):
                self.emit(
                    "touch_up",
                    tid,
                    tr["x"],
                    tr["y"],
                    tr["zone"]
                )
                del self.tracks[tid]

    # =========================================
    # BACKGROUND CAPTURE
    # =========================================

    def start_capture(self):
        if self.demo:
            self.status = "Exit mouse demo first"
            return

        if not self.reader:
            return

        _, stats = self.reader.snapshot()

        if stats[3] > .5:
            self.status = "Sensor not streaming"
            return

        self.clear_tracks()

        self.background_enabled = False
        self.baseline = None
        self.cap_samples = {}

        self.cap_end = (
            time.monotonic()+CAPTURE_SECONDS
        )

        self.auto_done = True
        self.status = "Capturing EMPTY floor"

    def collect_capture(self, scan):
        for j, a, d, intensity in scan:
            if (
                MIN_RANGE <= d <= MAX_RANGE
                and intensity >= MIN_INTENSITY
            ):
                self.cap_samples.setdefault(
                    j, []
                ).append(d)

        if time.monotonic() >= self.cap_end:
            self.baseline = {
                j: sorted(v)[len(v)//2]
                for j, v in self.cap_samples.items()
            }

            self.cap_end = None
            self.cap_samples = {}
            self.background_enabled = True

            self.status = (
                f"Captured {len(self.baseline)} bins"
            )

    # =========================================
    # MOUSE DEMO
    # =========================================

    def size(self):
        return (
            max(2, self.canvas.winfo_width()),
            max(2, self.canvas.winfo_height())
        )

    def mouse_pos(self, event):
        w, h = self.size()

        return (
            max(0, min(
                FULL_W, event.x/w*FULL_W
            )),
            max(0, min(
                HEIGHT, event.y/h*HEIGHT
            ))
        )

    def on_down(self, event):
        if not self.demo:
            return

        x, y = self.mouse_pos(event)

        if zone_index(x, y) is None:
            return

        self.demo_down = True

        self.tracks[0] = self.new_track(x, y)
        tr = self.tracks[0]

        self.emit(
            "touch_down",
            0, x, y, tr["zone"]
        )

    def on_drag(self, event):
        if not self.demo_down:
            return

        tr = self.tracks.get(0)
        if tr is None:
            return

        x, y = self.mouse_pos(event)

        tr["x"], tr["y"] = x, y
        tr["seen"] = time.monotonic()
        tr["zone"] = zone_index(x, y)

        self.emit(
            "touch_move",
            0, x, y, tr["zone"]
        )

    def on_up(self, event):
        if not self.demo_down:
            return

        self.demo_down = False
        tr = self.tracks.pop(0, None)

        if tr:
            self.emit(
                "touch_up",
                0,
                tr["x"],
                tr["y"],
                tr["zone"]
            )

    # =========================================
    # LIDAR MAP: 149 x 30 ONLY
    # =========================================

    def draw(self, points):
        c = self.canvas
        c.delete("all")

        w, h = self.size()
        sx = w/FULL_W
        sy = h/HEIGHT

        active = {
            tr["zone"]
            for tr in self.tracks.values()
            if tr["zone"] is not None
        }

        c.create_rectangle(
            0, 0, w, h,
            fill="#0b1628",
            outline=""
        )

        for i, (zone, note, color) in enumerate(ZONES):
            x0 = (
                DEAD_LEFT_IN+i*ZONE_WIDTH_IN
            )*MM*sx

            x1 = (
                DEAD_LEFT_IN+(i+1)*ZONE_WIDTH_IN
            )*MM*sx

            highlighted = i in active

            c.create_rectangle(
                x0, 0, x1, h,
                fill=color if highlighted else "#172740",
                outline="#40516a",
                width=1
            )

            c.create_rectangle(
                x0, 0, x1, 5,
                fill=color,
                outline=""
            )

            center = (x0+x1)/2

            c.create_text(
                center, h*.53,
                text=zone,
                fill="#132034" if highlighted else color,
                font=("Segoe UI", 18, "bold")
            )

            c.create_text(
                center, h*.72,
                text=note,
                fill="#132034" if highlighted else "white",
                font=("Segoe UI", 13, "bold")
            )

        # Dead zones remain 4 inches.
        c.create_rectangle(
            0, 0, X0*sx, h,
            fill="#603747",
            outline="#f394aa",
            width=2
        )

        c.create_rectangle(
            X1*sx, 0, w, h,
            fill="#603747",
            outline="#f394aa",
            width=2
        )

        if self.show_grid:
            for inch in range(0, 150, 10):
                px=inch*MM*sx
                c.create_line(
                    px, 0, px, h,
                    fill="#6a7890",
                    dash=(3,6)
                )

            for inch in range(0, 31, 10):
                py=inch*MM*sy
                c.create_line(
                    0, py, w, py,
                    fill="#6a7890",
                    dash=(3,6)
                )

        # Sensor marker.
        sensor_y = self.sensor_y*MM*sy

        c.create_polygon(
            0, sensor_y-10,
            18, sensor_y,
            0, sensor_y+10,
            fill="#56f5d7"
        )

        # LiDAR scan points.
        for _, x, y in points:
            px=x*sx
            py=y*sy

            c.create_oval(
                px-2, py-2,
                px+2, py+2,
                fill="#69b8ff",
                outline=""
            )

        # Foot tracking points.
        for tid, tr in self.tracks.items():
            px=tr["x"]*sx
            py=tr["y"]*sy

            c.create_oval(
                px-16, py-16,
                px+16, py+16,
                outline="#52f5d8",
                width=3
            )

            c.create_oval(
                px-5, py-5,
                px+5, py+5,
                fill="#ffe18c",
                outline="white"
            )

            index=tr["zone"]
            label=(
                ZONES[index][0]
                if index is not None else "-"
            )

            c.create_text(
                px+22, py-18,
                text=f"{tid}: {label}",
                anchor="w",
                fill="white",
                font=("Consolas", 11, "bold")
            )

        if self.show_info:
            lines = [
                self.status,
                self.stats_text,
                "LiDAR: 149 x 30 in | Game: 149 x 80 in",
                "Foot+toe merge 2 in | "
                f"Release {TRACK_TTL:.2f}s",
                f"Sensor Y={self.sensor_y:.1f} | "
                f"Angle={self.forward:.0f} | "
                f"Mirror={self.mirror}",
                "D demo | C capture | H info | "
                "G grid | M mirror | Arrows adjust"
            ]

            c.create_rectangle(
                20, 5, min(w-5, 1080), 133,
                fill="#091522",
                outline="#4b627f"
            )

            for i, line in enumerate(lines):
                c.create_text(
                    30, 15+i*21,
                    text=line,
                    anchor="w",
                    fill="#ecf4ff",
                    font=("Consolas", 9)
                )

    # =========================================
    # MAIN LOOP
    # =========================================

    def tick(self):
        if self.demo != self.was_demo:
            self.clear_tracks()
            self.demo_down = False
            self.was_demo = self.demo

        if self.demo:
            self.status = "Mouse simulation"
            self.draw([])

        else:
            scan, stats = self.reader.snapshot()

            ok, bad, rpm, age, error = stats
            now = time.monotonic()

            if self.cap_end is not None:
                self.collect_capture(scan)

            elif (
                not self.auto_done
                and ok >= 50
                and age < .5
            ):
                if self.first_data is None:
                    self.first_data = now

                elif (
                    now-self.first_data
                    >= AUTO_CAPTURE_DELAY
                ):
                    self.start_capture()

            points = self.filtered(scan)

            if (
                self.cap_end is None
                and self.background_enabled
                and age < .5
            ):
                detections = self.clusters(points)
                detections = self.merge_nearby(
                    detections
                )

                self.track(detections)
            else:
                self.clear_tracks()

            self.stats_text = (
                f"Packets {ok} | CRC {bad} | "
                f"RPM {rpm:.1f} | "
                f"Points {len(points)} | "
                f"Age {age*1000:.0f}ms | "
                f"Contacts {len(self.tracks)}"
            )

            if error:
                self.status = "Error: "+error

            elif age >= .5:
                self.status = "Waiting for sensor"

            elif (
                self.background_enabled
                and self.cap_end is None
            ):
                self.status = f"LIVE {PORT}"

            self.draw(points)

        self.root.after(20, self.tick)

    # =========================================
    # CLOSE
    # =========================================

    def close(self):
        self.clear_tracks()

        if self.reader:
            self.reader.close()

        self.root.destroy()


if __name__ == "__main__":
    root=tk.Tk()
    Studio(root)
    root.mainloop()

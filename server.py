#!/usr/bin/env python3
"""
Phone Mirror - local web dashboard that mirrors an Android device over adb.

Video path:  adb exec-out screenrecord --output-format=h264 -  ->  ffmpeg ->  MJPEG fan-out
(20-35 fps over wireless adb, vs ~0.5 fps if you loop `screencap`.)

Stdlib only. Pillow/numpy are used opportunistically for motion / black-screen metrics.

Usage:
    python3 server.py                 # foreground
    python3 server.py --detach        # background (double-fork, survives the shell)
    python3 server.py --stop          # stop a previously detached server
    python3 server.py --port=8735     # override port
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0.0"
CAPTURE_DIR = os.path.join(ROOT, "captures")
STATE_FILE = os.path.join(ROOT, "mirror.state.json")
SETTINGS_FILE = os.path.join(ROOT, "mirror.settings.json")
SWJS = os.path.join(ROOT, "sw.js")
LOG_FILE = os.path.join(ROOT, "mirror.log")
INDEX = os.path.join(ROOT, "index.html")


def which(name, guess):
    if os.path.exists(guess):
        return guess
    from shutil import which as _w
    return _w(name) or guess


ADB = os.environ.get("ADB") or which("adb", "/opt/homebrew/bin/adb")
FFMPEG = os.environ.get("FFMPEG") or which("ffmpeg", "/opt/homebrew/bin/ffmpeg")

# preset "width" = short edge of the mirrored image
PRESETS = {
    "low":      {"label": "Low",      "width": 360, "bit_rate": 1_500_000, "fps": 20, "q": 8},
    "balanced": {"label": "Balanced", "width": 480, "bit_rate": 3_000_000, "fps": 30, "q": 6},
    "hd":       {"label": "HD",       "width": 720, "bit_rate": 6_000_000, "fps": 25, "q": 5},
}

KEYS = {
    "back": 12, "home": 3, "recents": 187, "power": 26, "wake": 224,
    "sleep": 223, "vol_up": 24, "vol_down": 25, "menu": 82, "enter": 66,
    "delete": 67, "tab": 61, "camera": 27, "capture_btn": 54,
}

try:
    from PIL import Image
    import numpy as np
    HAVE_IMAGING = True
except Exception:
    HAVE_IMAGING = False


# --------------------------------------------------------------------------- #
# adb helpers
# --------------------------------------------------------------------------- #

def adb_shell(serial, args, timeout=15):
    """Run a device shell command, return stdout text ('' on any failure)."""
    try:
        p = subprocess.run([ADB, "-s", serial, "shell"] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception:
        return ""


def adb_bytes(serial, args, timeout=30):
    try:
        p = subprocess.run([ADB, "-s", serial, "exec-out"] + list(args),
                           capture_output=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else b""
    except Exception:
        return b""


def adb_device_lines():
    """[(serial, state)] from `adb devices`, ignoring the banner and daemon lines."""
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of"):
            continue
        m = re.match(r"^(\S+)\s+(\S+)", line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def discover_device():
    """First online adb serial; prefer the wireless ip:port transport."""
    serials = [s for s, state in adb_device_lines() if state == "device"]
    if not serials:
        return None
    return next((s for s in serials if ":" in s), serials[0])


def mdns_targets():
    """Endpoints adb's mDNS browser has seen, which it may not have dialled."""
    try:
        out = subprocess.run([ADB, "mdns", "services"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return []
    hits = []
    for line in out.splitlines():
        for m in re.finditer(r"([A-Za-z0-9][A-Za-z0-9._-]*?\.local\.?|\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})",
                             line):
            t = "%s:%s" % (m.group(1).rstrip("."), m.group(2))
            if t not in hits:
                hits.append(t)
    return hits


def remember_serial(serial):
    """Keep a short most-recent list of endpoints so a reboot can retry them."""
    if not serial:
        return
    known = [s for s in (SETTINGS.get("known_serials") or []) if s != serial]
    SETTINGS["known_serials"] = ([serial] + known)[:5]
    save_settings()


def try_auto_connect():
    """Ask adb to dial mDNS-advertised and remembered endpoints.

    Returns (serial_or_None, notes) - notes are short strings for the log and UI.
    """
    targets = list(mdns_targets())
    for s in SETTINGS.get("known_serials") or []:
        if s not in targets:
            targets.append(s)
    notes = []
    for t in targets[:3]:
        # a device may have reappeared while we were dialling a dead endpoint
        got = discover_device()
        if got:
            return got, notes
        try:
            # short timeout: an unreachable address otherwise blocks the retry loop
            p = subprocess.run([ADB, "connect", t], capture_output=True, text=True, timeout=6)
            lines = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
            msg = lines[-1] if lines else "no output"
        except subprocess.TimeoutExpired:
            msg = "timeout after 6 s (unreachable)"
        except Exception as ex:
            msg = "failed: %s" % ex
        notes.append("%s -> %s" % (t, msg[:70]))
        if "already connected" in msg or "connected to" in msg:
            got = discover_device()
            if got:
                return got, notes
    return None, notes


def even(n, min_v=16):
    n = int(round(n))
    if n % 2:
        n += 1
    return max(min_v, n)


class JpegSplitter:
    """Carve whole JPEGs out of a continuous byte stream (ffmpeg image2pipe)."""

    SOI, EOI = b"\xff\xd8", b"\xff\xd9"

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        out = []
        buf = self.buf + data
        while True:
            s = buf.find(self.SOI)
            if s == -1:
                buf = b""
                break
            buf = buf[s:]
            e = buf.find(self.EOI, 4)
            if e == -1:
                break
            out.append(buf[:e + 2])
            buf = buf[e + 2:]
        if len(buf) > 6_000_000:          # runaway / never-matching EOI
            buf = buf[-500_000:]
        self.buf = buf
        return out


# --------------------------------------------------------------------------- #
# mirror engine
# --------------------------------------------------------------------------- #

class Mirror:
    STALE_KILL = 6.0

    def __init__(self):
        self.cv = threading.Condition()
        self.frame = None
        self.gen = 0
        self.frame_ts = 0.0
        self.frame_times = deque(maxlen=150)
        self.src_times = deque(maxlen=300)
        self.jpeg_sizes = deque(maxlen=40)

        self.state = "starting"          # starting | mirroring | stalled | lost
        self.error = ""
        self.restarts = 0
        self.started_at = time.time()

        self.serial = None
        self.dev_w = self.dev_h = 0
        self.density = ""
        self.rotation = 0
        self.pipeline_rotation = 0
        self.stream_w = self.stream_h = 0
        self.preset = "balanced"
        self.insets = None            # measured device insets, px
        self.crop_top_px = 0          # crop actually applied, device px
        self.crop_bottom_px = 0
        self.crop_source = "off"

        self.motion = None
        self.luma = None
        self._prev_sample = None
        self._sampled_at = 0.0
        self._first_logged = False

        self.logs = deque(maxlen=250)
        self.log_seq = 0
        self.stop = threading.Event()
        self._procs = []
        self.status = {"online": False}
        self.status_updated = 0.0
        self.captures = []
        # dwell tracking (see update_dwell)
        self.fg_pkg = None
        self.fg_activity = None
        self.fg_started = 0.0
        self.fg_awake = 0.0
        self.last_tick = 0.0
        self.alerted_at = 0.0
        self.app_totals = {}
        self.sessions = []          # closed foreground sessions: pkg/start/end/secs
        self.alerts = deque(maxlen=50)
        self.alert_seq = 0

    # -- logging ---------------------------------------------------------- #
    def log(self, msg, level="info"):
        with self.cv:
            self.log_seq += 1
            self.logs.append({"seq": self.log_seq, "ts": time.time(),
                              "level": level, "msg": msg})
            self.cv.notify_all()
        print("%s [%s] %s" % (time.strftime("%H:%M:%S"), level, msg), flush=True)

    def events_since(self, seq):
        return [e for e in self.logs if e["seq"] > seq]

    # -- geometry --------------------------------------------------------- #
    def refresh_geometry(self):
        if not self.serial:
            return
        m = re.search(r"(\d+)x(\d+)", adb_shell(self.serial, ["wm", "size"], timeout=8))
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            self.dev_w, self.dev_h = min(a, b), max(a, b)   # stored portrait
        m = re.search(r"(\d+)", adb_shell(self.serial, ["wm", "density"], timeout=8))
        self.density = m.group(1) if m else ""
        self.insets = measure_insets()
        self.refresh_rotation()

    def refresh_rotation(self):
        v = adb_shell(self.serial, ["settings", "get", "system", "user_rotation"], timeout=6).strip()
        try:
            self.rotation = int(v)
        except Exception:
            self.rotation = 0

    def display_dims(self):
        """Current logical display size in the mirrored orientation."""
        if not self.dev_w:
            return None
        return (self.dev_h, self.dev_w) if self.rotation in (1, 3) else (self.dev_w, self.dev_h)

    def target_dims(self):
        """Exact-aspect stream size, so screenrecord never pads with black.

        Rounding the requested size to any even number leaves the box a hair off
        the display aspect, and screenrecord fills that sliver with black bars
        top and bottom. Snapping to an integer multiple of the display's reduced
        ratio (1224x2700 -> 204x450) makes the aspect mathematically exact.
        """
        p = PRESETS[self.preset]
        if not self.dev_w or not self.dev_h:
            return 480, 1056
        from math import gcd
        g = gcd(self.dev_w, self.dev_h)
        unit_w, unit_h = self.dev_w // g, self.dev_h // g      # reduced ratio
        landscape = self.rotation in (1, 3)
        short_unit = min(unit_w, unit_h)
        k = max(1, -(-p["width"] // short_unit))               # ceil, keeps quality
        if k % 2:
            k += 1        # screenrecord needs even dims; an even k guarantees them
        w, h = unit_w * k, unit_h * k
        if landscape:
            w, h = h, w
        return w, h

    # -- processes -------------------------------------------------------- #
    def _kill_procs(self):
        for pr in list(self._procs):
            try:
                pr.kill()
            except Exception:
                pass

    def request_restart(self, reason):
        self.log("restarting stream: %s" % reason, "info")
        self._kill_procs()

    def set_preset(self, name):
        if name not in PRESETS or name == self.preset:
            return False
        self.preset = name
        self.request_restart("quality -> %s" % name)
        return True

    def crop_plan(self):
        """Encode size, crop filter and resulting frame size.

        screenrecord scales the whole display to --size, so the system bars have
        to be removed proportionally in stream space. Returns
        ((w, h), (out_w, out_h), ffmpeg_args, (top_px, bottom_px), source).
        """
        p = PRESETS[self.preset]
        w, h = self.target_dims()
        top, bot, src = effective_crop()
        landscape = self.rotation in (1, 3)
        long_side = w if landscape else h        # axis the bars sit along
        scale = (long_side / self.dev_h) if self.dev_h else 0.0
        t_px = int(round(top * scale))
        b_px = int(round(bot * scale))
        keep = long_side - t_px - b_px
        if keep < long_side // 2 or keep < 64:   # never crop away the screen
            t_px = b_px = 0
            keep = long_side
        if keep % 2:                             # mjpeg needs even dims
            keep -= 1
        if not (t_px or b_px):
            return (w, h), (w, h), [], (0, 0), "off"
        if landscape:
            return (w, h), (keep, h), ["-vf", "crop=%d:ih:%d:0" % (keep, t_px)], (top, bot), src
        return (w, h), (w, keep), ["-vf", "crop=iw:%d:0:%d" % (keep, t_px)], (top, bot), src

    def _spawn(self):
        p = PRESETS[self.preset]
        self.pipeline_rotation = self.rotation
        (w, h), (ow, oh), vf, (top, bot), src = self.crop_plan()
        self.stream_w, self.stream_h = ow, oh
        self.crop_top_px = top
        self.crop_bottom_px = bot
        self.crop_source = src
        src_cmd = [ADB, "-s", self.serial, "exec-out", "screenrecord",
                   "--output-format=h264", "--size", "%dx%d" % (w, h),
                   "--bit-rate", str(p["bit_rate"]), "--time-limit", "180", "-"]
        dec = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "h264",
               "-i", "pipe:0"] + vf + [
               "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", str(p["q"]), "-"]
        s_p = subprocess.Popen(src_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        d_p = subprocess.Popen(dec, stdin=s_p.stdout, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
        s_p.stdout.close()
        self._procs = [d_p, s_p]
        self.log("pipeline up %s: %dx%d -> %dx%d @%d fps / %d kbps (bars %s)" %
                 (self.preset, w, h, ow, oh, p["fps"],
                  p["bit_rate"] // 1000, src), "info")
        return d_p, s_p

    # -- metrics ---------------------------------------------------------- #
    def _sample(self, jpeg):
        if not HAVE_IMAGING:
            return
        now = time.time()
        if now - self._sampled_at < 0.5:
            return
        self._sampled_at = now
        try:
            a = np.asarray(Image.open(io.BytesIO(jpeg)).convert("L").resize((48, 96)),
                           dtype=np.int16)
            self.luma = float(a.mean())
            if self._prev_sample is not None and self._prev_sample.shape == a.shape:
                self.motion = float(np.abs(a - self._prev_sample).mean()) / 255.0
            self._prev_sample = a
        except Exception:
            pass

    def _set_frame(self, jpeg):
        with self.cv:
            self.frame = jpeg
            self.gen += 1
            self.frame_ts = time.time()
            self.frame_times.append(self.frame_ts)
            self.jpeg_sizes.append(len(jpeg))
            if self.state != "mirroring":
                self.state = "mirroring"
                self.error = ""
            self.cv.notify_all()
        self._sample(jpeg)

    def fps(self):
        now = time.time()
        recent = [t for t in self.frame_times if now - t < 2.0]
        return len(recent) / 2.0 if len(recent) > 1 else 0.0

    def source_fps(self):
        """Rate screenrecord/ffmpeg actually produced (tracks what changes on the phone)."""
        now = time.time()
        recent = [t for t in self.src_times if now - t < 2.0]
        return len(recent) / 2.0 if len(recent) > 1 else 0.0

    def latency(self):
        return (time.time() - self.frame_ts) if self.frame_ts else None

    # -- loops ------------------------------------------------------------ #
    def run(self):
        backoff = 0.4
        last_autoc = 0.0
        while not self.stop.is_set():
            if not self.serial:
                self.serial = discover_device()
                if not self.serial:
                    if self.state != "lost":
                        self.state = "lost"
                        self.error = "no adb device online"
                        self.log("waiting for a device on adb...", "warn")
                    # a lingering "offline" row can stop the transport from being
                    # re-added, so kick it rather than wait for the user
                    if SETTINGS.get("auto_connect"):
                        for s, st in adb_device_lines():
                            if st == "offline":
                                self.log("device %s is offline - kicking with "
                                         "`adb reconnect offline`" % s, "warn")
                                subprocess.run([ADB, "reconnect", "offline"],
                                               capture_output=True, timeout=20)
                                break
                    if SETTINGS.get("auto_connect") and time.time() - last_autoc > 20:
                        last_autoc = time.time()
                        got, notes = try_auto_connect()
                        for note in notes:
                            self.log("auto-connect: %s" % note, "info" if got else "warn")
                        if got:
                            self.serial = got
                    if not self.serial:
                        self._wait(2.0)
                        continue
                self.log("device online: %s" % self.serial, "info")
                remember_serial(self.serial)
                self.refresh_geometry()
                self.log("display %dx%d @%s dpi, rotation %d" %
                         (self.dev_w, self.dev_h, self.density or "?", self.rotation), "info")

            self.state = "starting"
            self._first_logged = False
            try:
                d_p, s_p = self._spawn()
            except Exception as ex:
                self.error = "spawn failed: %s" % ex
                self.log(self.error, "error")
                self._wait(2.0)
                continue

            got = False
            splitter = JpegSplitter()
            min_iv = 0.9 / max(5, PRESETS[self.preset]["fps"])
            last_fwd = 0.0
            try:
                while not self.stop.is_set():
                    chunk = d_p.stdout.read(8192)
                    if not chunk:
                        break
                    frames = splitter.feed(chunk)
                    if not frames:
                        continue
                    got = True
                    backoff = 0.4
                    now = time.time()
                    self.src_times.extend([now] * len(frames))
                    if not self._first_logged:
                        self._first_logged = True
                        self.log("frames flowing (%.1f KB each)" % (len(frames[-1]) / 1024), "info")
                    # ffmpeg cannot meter this stream (raw h264 carries no timing
                    # info), so frames arrive in bursts. Forward only the *newest*
                    # frame of each burst once the target interval has elapsed:
                    # older frames are dropped rather than queued, which keeps the
                    # browser at the freshest picture instead of drifting behind.
                    if now - last_fwd >= min_iv:
                        last_fwd = now
                        self._set_frame(frames[-1])
            except Exception as ex:
                self.error = "stream error: %s" % ex
                self.log(self.error, "error")
            finally:
                self._kill_procs()
                for pr in self._procs:
                    try:
                        pr.wait(timeout=2)
                    except Exception:
                        pass

            if self.stop.is_set():
                break
            self.restarts += 1
            self.state = "stalled"
            if not got:
                self.error = "screenrecord produced no frames"
                self.log("no frames from screenrecord (another capture holding the "
                         "display? or device asleep) - backing off", "error")
            else:
                self.log("stream ended, restart #%d" % self.restarts, "info")
            self.refresh_geometry()
            self._wait(backoff if got else min(backoff * 3, 8.0))
            if not got:
                backoff = min(backoff * 3, 8.0)
                if discover_device() is None:
                    self.serial = None

    def _wait(self, secs):
        self.stop.wait(secs)

    def watchdog(self):
        while not self.stop.is_set():
            self._wait(2.0)
            if self.stop.is_set():
                break
            lat = self.latency()
            if self.state == "mirroring" and lat and lat > self.STALE_KILL:
                self.log("no frames for %.1fs - forcing reconnect" % lat, "warn")
                self.state = "stalled"
                self.error = "stream stalled"
                self._kill_procs()
            online = discover_device()
            if online != self.serial:
                self.log("adb transport changed: %s -> %s" % (self.serial, online), "warn")
                self.serial = online
                self._kill_procs()
            self.refresh_rotation()
            if online and self.rotation != self.pipeline_rotation:
                self.log("device rotated to %d - reconfiguring stream" % self.rotation, "info")
                self._kill_procs()


MIRROR = Mirror()


# --------------------------------------------------------------------------- #
# dwell ("still on this app") tracking + alerts
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS = {
    "dwell_enabled": True,
    "dwell_minutes": 10,      # alert after this long on one app
    "repeat_minutes": 10,     # reminder interval; 0 = only once per app session
    "mac_toasts": True,       # also raise a macOS notification via osascript
    "muted": [],              # packages never to alert about
    "crop_enabled": True,     # drop the status bar + gesture bar from the mirror
    "crop_top": None,         # device px; None = derive from density / measured insets
    "crop_bottom": None,
    "auto_connect": True,     # dial mDNS-advertised and remembered endpoints on our own
    "known_serials": [],      # most-recent wireless endpoints, for retry after a reboot
}
SETTINGS = dict(DEFAULT_SETTINGS)

# Android's status bar is 24dp (more where a cutout intrudes) and the gesture
# pill strip is about 24dp; used only if the device will not report real insets.
DEFAULT_TOP_DP = 28
DEFAULT_BOTTOM_DP = 24


def dp_to_px(dp):
    try:
        dpi = int(MIRROR.density) or 160
    except Exception:
        dpi = 160
    return int(round(dp * dpi / 160.0))


def measure_insets():
    """Best-effort real status/gesture bar heights from the device, in px.

    OEM dumps vary, so this only fills in what it can find; anything missing
    falls back to the Android dp defaults, and the UI can override both.
    """
    if not MIRROR.serial:
        return None
    out = {}
    txt = adb_shell(MIRROR.serial, ["dumpsys", "window"], timeout=12)
    pats = {
        "top": [r"mStatusBarHeight=(\d+)", r"statusBar.*?Rect\(0,\s*(\d+)",
                r"mStableInsets.*?,\s*(\d+)\)"],
        "bottom": [r"mNavigationBarHeight=(\d+)", r"navigation.*?Rect\(0,\s*(\d+)\)"],
    }
    for key, plist in pats.items():
        for pat in plist:
            m = re.search(pat, txt, re.I)
            if m:
                v = int(m.group(1))
                if 0 < v < (MIRROR.dev_h or 3000) // 3:
                    out[key] = v
                    break
    src = adb_shell(MIRROR.serial, ["dumpsys", "display"], timeout=12)
    if "top" not in out:
        m = re.search(r"mCutout.*?(\d{2,4})\s*\)", src)
        if m:
            v = int(m.group(1))
            if 0 < v < (MIRROR.dev_h or 3000) // 3:
                out["top"] = v
    return out or None


def effective_crop():
    """(top, bottom, source) in device px for the current settings."""
    if not SETTINGS.get("crop_enabled", True):
        return 0, 0, "off"
    ins = MIRROR.insets or {}
    parts = []
    top = SETTINGS.get("crop_top")
    if top is None:
        top = ins.get("top") or dp_to_px(DEFAULT_TOP_DP)
        parts.append("measured" if ins.get("top") else "%ddp" % DEFAULT_TOP_DP)
    else:
        top = int(top)
        parts.append("manual")
    bot = SETTINGS.get("crop_bottom")
    if bot is None:
        bot = ins.get("bottom") or dp_to_px(DEFAULT_BOTTOM_DP)
        parts.append("measured" if ins.get("bottom") else "%ddp" % DEFAULT_BOTTOM_DP)
    else:
        bot = int(bot)
        parts.append("manual")
    return max(0, top), max(0, bot), "top:%s bottom:%s" % (parts[0], parts[1])

LAUNCHER_HINTS = ("launcher", "home", "desktop", "systemui", "system_ui", "carryflow")


def app_name(pkg):
    if not pkg:
        return "nothing"
    low = pkg.lower()
    if any(h in low for h in LAUNCHER_HINTS):
        return "Home screen"
    if "settings" in low:
        return "Settings"
    return pkg.rsplit(".", 1)[-1]


def fmt_dur(secs):
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def load_settings():
    global SETTINGS
    try:
        with open(SETTINGS_FILE) as fh:
            saved = json.load(fh)
        SETTINGS = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
    except FileNotFoundError:
        SETTINGS = dict(DEFAULT_SETTINGS)
    except Exception as ex:
        print("settings load failed (%s), using defaults" % ex, flush=True)


def save_settings():
    try:
        with open(SETTINGS_FILE, "w") as fh:
            json.dump(SETTINGS, fh, indent=2)
    except Exception as ex:
        MIRROR.log("could not save settings: %s" % ex, "error")


def update_settings(patch):
    changed = {}
    for key in ("dwell_enabled", "mac_toasts", "crop_enabled", "auto_connect"):
        if key in patch:
            SETTINGS[key] = bool(patch[key])
            changed[key] = SETTINGS[key]
    if patch.get("forget_devices"):
        SETTINGS["known_serials"] = []
        changed["known_serials"] = []
    for key in ("crop_top", "crop_bottom"):
        if key in patch:
            v = patch[key]
            if v is None or v == "" or v == "auto":
                SETTINGS[key] = None
            else:
                SETTINGS[key] = max(0, min(int(v), 600))
            changed[key] = SETTINGS[key]
    for key, lo, hi in (("dwell_minutes", 0, 720), ("repeat_minutes", 0, 240)):
        if key in patch:
            try:
                v = max(lo, min(int(patch[key]), hi))
            except (TypeError, ValueError):
                continue
            SETTINGS[key] = v
            changed[key] = v
    if "mute_pkg" in patch:
        pkg = str(patch["mute_pkg"])
        if patch.get("unmute"):
            SETTINGS["muted"] = [p for p in SETTINGS["muted"] if p != pkg]
        elif pkg not in SETTINGS["muted"]:
            SETTINGS["muted"] = SETTINGS["muted"] + [pkg]
        changed["muted"] = SETTINGS["muted"]
    save_settings()
    if patch.get("remeasure"):
        MIRROR.insets = measure_insets()
        changed["insets"] = MIRROR.insets
        MIRROR.log("measured insets: %s" % (MIRROR.insets or "device gave none, using dp defaults"), "info")
    if any(k in changed for k in ("crop_enabled", "crop_top", "crop_bottom")) or patch.get("remeasure"):
        MIRROR.request_restart("crop changed")
    # a shorter threshold may already be overdue; let the next tick decide
    MIRROR.alerted_at = 0.0 if SETTINGS["dwell_minutes"] == 0 else MIRROR.alerted_at
    return changed


def mac_notify(title, body):
    """Best-effort macOS notification so alerts still land with the tab closed."""
    def asa(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = "display notification %s with title %s subtitle %s sound name %s" % (
        asa(body), asa(title), asa("Phone Mirror"), asa("Glass"))
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except Exception as ex:
        MIRROR.log("mac notification failed: %s" % ex, "error")
        return False


def raise_dwell_alert(secs, test=False):
    pkg = MIRROR.fg_pkg
    if test:
        title = "Phone Mirror: test alert"
        body = "Notifications are wired up correctly."
    else:
        title = "Still on %s" % app_name(pkg)
        body = "%s has been in the foreground for %s" % (app_name(pkg), fmt_dur(secs))
    with MIRROR.cv:
        MIRROR.alert_seq += 1
        ev = {"id": MIRROR.alert_seq, "ts": time.time(), "pkg": pkg,
              "name": app_name(pkg), "activity": MIRROR.fg_activity,
              "secs": int(secs), "test": test, "title": title, "body": body}
        MIRROR.alerts.append(ev)
        if not test:
            MIRROR.alerted_at = time.time()
        MIRROR.cv.notify_all()
    MIRROR.log("dwell alert: %s for %s%s" % (pkg or "?", fmt_dur(secs),
                                             " (test)" if test else ""), "alert")
    if SETTINGS.get("mac_toasts"):
        mac_notify(title, body)
    return ev


def update_dwell(pkg, awake):
    """Accumulate *awake* time per foreground app and decide when to alert.

    Called from the status loop (~every 3 s). A missing package or a lost adb
    transport freezes the timer instead of resetting it, so a hiccup does not
    restart the 10-minute clock.
    """
    now = time.time()
    dt = min(max(0.0, now - MIRROR.last_tick), 15.0) if MIRROR.last_tick else 0.0
    MIRROR.last_tick = now

    if pkg is None:                      # unknown (offline / parse failed): freeze
        return
    if pkg != MIRROR.fg_pkg:
        prev, prev_secs = MIRROR.fg_pkg, MIRROR.fg_awake
        if prev:
            MIRROR.sessions.append({"pkg": prev, "name": app_name(prev),
                                    "start": MIRROR.fg_started, "end": now,
                                    "secs": round(prev_secs),
                                    "alerted": bool(MIRROR.alerted_at)})
            del MIRROR.sessions[:-30]
        MIRROR.fg_pkg, MIRROR.fg_started, MIRROR.fg_awake, MIRROR.alerted_at = pkg, now, 0.0, 0.0
        MIRROR.log("foreground: %s -> %s (was %s)" %
                   (app_name(prev) if prev else "none", app_name(pkg),
                    fmt_dur(prev_secs) if prev else "-"), "app")
    if awake:
        MIRROR.fg_awake += dt
        MIRROR.app_totals[pkg] = MIRROR.app_totals.get(pkg, 0.0) + dt

    thr = SETTINGS["dwell_minutes"] * 60
    if not SETTINGS["dwell_enabled"] or thr <= 0 or pkg in SETTINGS["muted"]:
        return
    overdue = MIRROR.fg_awake >= thr
    rep = SETTINGS["repeat_minutes"] * 60
    repeat_due = bool(MIRROR.alerted_at) and rep > 0 and (now - MIRROR.alerted_at) >= rep
    if overdue and (not MIRROR.alerted_at or repeat_due):
        raise_dwell_alert(MIRROR.fg_awake)


def dwell_state():
    thr = SETTINGS["dwell_minutes"] * 60
    secs = MIRROR.fg_awake
    return {
        "pkg": MIRROR.fg_pkg,
        "name": app_name(MIRROR.fg_pkg),
        "activity": MIRROR.fg_activity,
        "secs": round(secs),
        "since": MIRROR.fg_started,
        "threshold_secs": thr,
        "remaining": max(0, round(thr - secs)) if thr else None,
        "enabled": SETTINGS["dwell_enabled"],
        "muted": MIRROR.fg_pkg in SETTINGS["muted"],
        "alerted": round(MIRROR.alerted_at) if MIRROR.alerted_at else None,
        "alerts_raised": MIRROR.alert_seq,
        "settings": dict(SETTINGS),
        "sessions": list(reversed(MIRROR.sessions[-10:])),
        "recent_alerts": list(reversed([{"id": a["id"], "ts": a["ts"], "title": a["title"],
                                         "body": a["body"], "pkg": a["pkg"], "test": a["test"]}
                                        for a in MIRROR.alerts][-6:])),
        "measured_at": time.time(),
        "totals": sorted(([p, round(t)] for p, t in MIRROR.app_totals.items()),
                         key=lambda kv: -kv[1])[:8],
    }


# --------------------------------------------------------------------------- #
# device status collector
# --------------------------------------------------------------------------- #

CHARGE_CODES = {"1": "unknown", "2": "charging", "3": "discharging", "4": "full",
                "5": "not charging", "6": "discharging", "7": "full"}


def collect_status():
    serial = MIRROR.serial
    out = {"online": bool(serial), "serial": serial, "version": VERSION,
           "presets": {k: v["label"] for k, v in PRESETS.items()}}
    if serial:
        out["model"] = adb_shell(serial, ["getprop", "ro.product.model"], timeout=6).strip() or "?"
        out["manufacturer"] = adb_shell(serial, ["getprop", "ro.product.manufacturer"], timeout=6).strip()
        out["android"] = adb_shell(serial, ["getprop", "ro.build.version.release"], timeout=6).strip()
        out["transport"] = "wireless adb" if ":" in str(serial) else "usb adb"

        bat = adb_shell(serial, ["dumpsys", "battery"], timeout=8)
        m = re.search(r"^\s*level:\s*(\d+)", bat, re.M)
        out["battery"] = int(m.group(1)) if m else None
        m = re.search(r"^\s*status:\s*(\d)", bat, re.M)
        out["charging"] = CHARGE_CODES.get(m.group(1), "unknown") if m else "unknown"
        m = re.search(r"AC powered:\s*(true|false)", bat)
        out["ac_powered"] = (m.group(1) == "true") if m else False
        m = re.search(r"^\s*temperature:\s*(\d+)", bat, re.M)
        out["temp_c"] = int(m.group(1)) / 10 if m else None

        pw = adb_shell(serial, ["dumpsys", "power"], timeout=10)
        m = re.search(r"mWakefulness=(\w+)", pw)
        out["wakefulness"] = m.group(1) if m else "?"

        win = adb_shell(serial, ["dumpsys", "window"], timeout=10)
        m = re.search(r"mCurrentFocus=Window\{[^ ]*\s+([^\s/]+)/([^\s}]+)", win)
        if not m:
            m = re.search(r"mFocusedApp=ActivityRecord\{\d+\s+u0\s+([^\s/]+)/([^\s}]+)", win)
        out["foreground_pkg"] = m.group(1) if m else None
        out["foreground_activity"] = "%s/%s" % (m.group(1), m.group(2)) if m else None
        MIRROR.fg_activity = out["foreground_activity"]
        update_dwell(out["foreground_pkg"], out["wakefulness"] == "Awake")

        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)",
                      adb_shell(serial, ["ip", "-f", "inet", "addr", "show", "wlan0"], timeout=8))
        out["device_ip"] = m.group(1) if m else None

        cols = adb_shell(serial, ["df", "-h", "/data"], timeout=8).strip().splitlines()
        if len(cols) >= 2:
            c = cols[-1].split()
            if len(c) >= 5:
                out["storage"] = {"size": c[1], "used": c[2], "avail": c[3], "used_pct": c[4]}

        m = re.match(r"(\d+)", adb_shell(serial, ["cat", "/proc/uptime"], timeout=6).strip())
        if m:
            secs = int(m.group(1))
            d, rem = divmod(secs, 86400)
            h, rem = divmod(rem, 3600)
            out["uptime"] = ("%dd " % d if d else "") + "%02d:%02d" % (h, rem // 60)
        la = adb_shell(serial, ["cat", "/proc/loadavg"], timeout=6).split()
        out["load"] = la[0] if la else None

        disp = MIRROR.display_dims()
        out["screen"] = "%dx%d" % disp if disp else None
        out["rotation"] = MIRROR.rotation
        out["density"] = MIRROR.density

    out["mirror"] = {
        "state": MIRROR.state,
        "error": MIRROR.error,
        "fps": round(MIRROR.fps(), 1),
        "source_fps": round(MIRROR.source_fps(), 1),
        "latency_ms": round(MIRROR.latency() * 1000) if MIRROR.latency() is not None else None,
        "preset": MIRROR.preset,
        "stream": "%dx%d" % (MIRROR.stream_w, MIRROR.stream_h) if MIRROR.stream_w else None,
        "display": "%dx%d" % (MIRROR.dev_w, MIRROR.dev_h) if MIRROR.dev_w else None,
        "crop": {"top": MIRROR.crop_top_px, "bottom": MIRROR.crop_bottom_px,
                 "source": MIRROR.crop_source,
                 "enabled": bool(SETTINGS.get("crop_enabled", True)),
                 "manual_top": SETTINGS.get("crop_top"),
                 "manual_bottom": SETTINGS.get("crop_bottom"),
                 "insets": MIRROR.insets,
                 "defaults": {"top": dp_to_px(DEFAULT_TOP_DP),
                              "bottom": dp_to_px(DEFAULT_BOTTOM_DP)}},
        "restarts": MIRROR.restarts,
        "frames": MIRROR.gen,
        "avg_jpeg_kb": round(sum(MIRROR.jpeg_sizes) / len(MIRROR.jpeg_sizes) / 1024, 1)
                       if MIRROR.jpeg_sizes else None,
        "motion": round(MIRROR.motion, 4) if MIRROR.motion is not None else None,
        "luma": round(MIRROR.luma, 1) if MIRROR.luma is not None else None,
        "black_screen": bool(MIRROR.luma is not None and MIRROR.luma < 12),
        "imaging": HAVE_IMAGING,
        "uptime_s": round(time.time() - MIRROR.started_at),
    }
    out["connect"] = {"auto": bool(SETTINGS.get("auto_connect", True)),
                      "known": SETTINGS.get("known_serials") or [],
                      "mdns": [] if MIRROR.serial else mdns_targets(),
                      "seen": [{"serial": s, "state": st} for s, st in adb_device_lines()]}
    out["dwell"] = dwell_state()
    return out


def status_loop():
    while not MIRROR.stop.is_set():
        try:
            MIRROR.status = collect_status()
            MIRROR.status_updated = time.time()
        except Exception as ex:
            MIRROR.log("status collection failed: %s" % ex, "error")
        MIRROR.stop.wait(3.0)


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #

def act_tap(x, y):
    adb_shell(MIRROR.serial, ["input", "tap", str(int(x)), str(int(y))], timeout=12)
    MIRROR.log("tap %d,%d" % (x, y), "action")


def act_swipe(x1, y1, x2, y2, dur):
    dur = max(80, min(int(dur or 260), 1500))
    adb_shell(MIRROR.serial, ["input", "swipe", str(int(x1)), str(int(y1)),
                              str(int(x2)), str(int(y2)), str(dur)], timeout=12)
    MIRROR.log("swipe %d,%d -> %d,%d (%d ms)" % (x1, y1, x2, y2, dur), "action")


def act_key(name):
    code = KEYS.get(str(name).lower())
    if code is None:
        raise ValueError("unknown key %r (known: %s)" % (name, ", ".join(sorted(KEYS))))
    adb_shell(MIRROR.serial, ["input", "keyevent", str(code)], timeout=12)
    MIRROR.log("key %s (%d)" % (name, code), "action")


def act_text(value):
    if not value:
        return 0
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", str(value))[:400]
    if not cleaned.strip():
        raise ValueError("nothing to type")
    if re.search(r"[^\x20-\x7e]", cleaned):
        raise ValueError("adb input text only covers ASCII; paste of unicode/emoji unsupported")
    cmd = "input text %s" % shlex.quote(cleaned)
    out = adb_shell(MIRROR.serial, [cmd], timeout=20)
    if "Error" in out:
        raise RuntimeError("device rejected the text: %s" % out.strip()[:120])
    MIRROR.log("typed %d chars" % len(cleaned), "action")
    return len(cleaned)


def act_screenshot(kind):
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if kind == "hd":
        data = adb_bytes(MIRROR.serial, ["screencap", "-p"], timeout=40)
        if not data:
            raise RuntimeError("screencap failed (device offline, or capture blocked)")
        path = os.path.join(CAPTURE_DIR, "full-%s.png" % stamp)
        MIRROR.log("HD capture started (full %s PNG takes ~2s)" %
                   ("%dx%d" % (MIRROR.dev_w, MIRROR.dev_h)), "info")
    else:
        with MIRROR.cv:
            data = MIRROR.frame
        if not data:
            raise RuntimeError("no live frame yet")
        path = os.path.join(CAPTURE_DIR, "live-%s.jpg" % stamp)
    with open(path, "wb") as fh:
        fh.write(data)
    MIRROR.log("saved %s (%.1f MB)" % (os.path.basename(path), len(data) / 1e6), "action")
    MIRROR.captures.insert(0, {"name": os.path.basename(path), "ts": time.time(),
                               "kb": round(len(data) / 1024, 1)})
    del MIRROR.captures[12:]
    return path


def handle_action(body):
    if not MIRROR.serial:
        if (body.get("type") or "").lower() in ("reconnect",):
            MIRROR.request_restart("manual reconnect")
            return {"ok": True, "note": "no device yet, retrying discovery"}
        raise RuntimeError("no device connected over adb")
    kind = (body.get("type") or "").lower()
    if kind == "tap":
        act_tap(body["x"], body["y"])
    elif kind == "swipe":
        act_swipe(body["x1"], body["y1"], body["x2"], body["y2"], body.get("duration"))
    elif kind == "key":
        act_key(body.get("key") or body.get("value"))
    elif kind == "text":
        act_text(body.get("value") or "")
    elif kind in ("screenshot", "capture"):
        p = act_screenshot(body.get("quality") or "live")
        return {"path": p, "url": "/captures/" + os.path.basename(p)}
    elif kind == "reconnect":
        MIRROR.serial = None
        MIRROR.request_restart("manual reconnect")
    elif kind == "restart_stream":
        MIRROR.request_restart("manual restart")
    else:
        raise ValueError("unsupported action %r" % kind)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PhoneMirror/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, ctype, data, extra=None):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj))

    def do_GET(self):
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path in ("/", "/index.html"):
            try:
                with open(INDEX, "rb") as fh:
                    self._send(200, "text/html; charset=utf-8", fh.read())
            except Exception as ex:
                self._send(500, "text/plain; charset=utf-8", "index.html missing: %s" % ex)
        elif path == "/stream.mjpg":
            self.stream()
        elif path == "/frame.jpg":
            with MIRROR.cv:
                data = MIRROR.frame
            self._send(200 if data else 503, "image/jpeg",
                       data or b"no frame yet", {"Content-Disposition": "inline"})
        elif path == "/api/status":
            d = dict(MIRROR.status)
            d["updated"] = MIRROR.status_updated
            self._json(d)
        elif path == "/api/events":
            since = int((q.get("since") or ["0"])[0])
            self._json({"seq": MIRROR.log_seq, "events": MIRROR.events_since(since)})
        elif path == "/api/captures":
            self._json({"captures": MIRROR.captures})
        elif path == "/api/alerts":
            since = int((q.get("since") or ["0"])[0])
            pending = [a for a in MIRROR.alerts if a["id"] > since]
            self._json({"seq": MIRROR.alert_seq, "alerts": pending, "dwell": dwell_state()})
        elif path == "/sw.js":
            try:
                with open(SWJS, "rb") as fh:
                    self._send(200, "application/javascript; charset=utf-8", fh.read())
            except Exception as ex:
                self._send(500, "text/plain", "sw.js missing: %s" % ex)
        elif path.startswith("/captures/"):
            name = os.path.basename(path)
            fp = os.path.join(CAPTURE_DIR, name)
            if os.path.isfile(fp) and name.endswith((".png", ".jpg")):
                ctype = "image/png" if name.endswith(".png") else "image/jpeg"
                with open(fp, "rb") as fh:
                    self._send(200, ctype, fh.read())
            else:
                self._send(404, "text/plain; charset=utf-8", "not found")
        elif path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
        else:
            self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception as ex:
            self._json({"error": "bad json: %s" % ex}, 400)
            return
        try:
            if path == "/api/action":
                self._json(handle_action(body))
            elif path == "/api/quality":
                changed = MIRROR.set_preset(body.get("preset", ""))
                self._json({"ok": True, "preset": MIRROR.preset, "changed": changed})
            elif path == "/api/settings":
                changed = update_settings(body)
                MIRROR.log("alert settings: %s" % json.dumps(changed), "info")
                self._json({"ok": True, "settings": SETTINGS, "dwell": dwell_state()})
            elif path == "/api/alerts/test":
                secs = MIRROR.fg_awake or SETTINGS["dwell_minutes"] * 60
                self._json({"ok": True, "alert": raise_dwell_alert(secs, test=True)})
            elif path == "/api/alerts/mute":
                self._json(update_settings({
                    "mute_pkg": body.get("pkg") or MIRROR.fg_pkg,
                    "unmute": bool(body.get("unmute"))}))
            else:
                self._send(404, "text/plain; charset=utf-8", "not found")
        except Exception as ex:
            MIRROR.log("action failed: %s" % ex, "error")
            self._json({"error": str(ex)}, 500)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Connection", "close")
        self.end_headers()
        seen, last_push = -1, 0.0
        try:
            while not MIRROR.stop.is_set():
                with MIRROR.cv:
                    if MIRROR.gen == seen:
                        MIRROR.cv.wait(0.4)
                    if MIRROR.gen == seen and time.time() - last_push < 2.0:
                        continue
                    frame, gen = MIRROR.frame, MIRROR.gen
                if not frame:
                    time.sleep(0.3)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: %d\r\n\r\n" % len(frame))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                seen, last_push = gen, time.time()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            return


def pick_port(preferred):
    for p in [preferred] + list(range(preferred + 1, preferred + 12)):
        if wait_port_free(p, 2.0):
            return p
    raise SystemExit("no free port near %d" % preferred)


def detach():
    """Double-fork: a plain nohup'd child gets reaped by the tool shell here."""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    lh = open(LOG_FILE, "ab", buffering=0)
    os.dup2(lh.fileno(), 1)
    os.dup2(lh.fileno(), 2)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)


def kill_orphan_captures():
    """Kill Mac-side `adb ... exec-out screenrecord` processes whose server is gone.

    Orphans get reparented to launchd (ppid 1); a capture still owned by a live
    server is left alone. The device-side screenrecord exits as soon as its adb
    stdout closes, so clearing these frees the phone's capture slot.
    """
    try:
        pids = subprocess.run(["pgrep", "-f", "exec-out screenrecord"],
                              capture_output=True, text=True).stdout.split()
    except Exception:
        return []
    killed = []
    for p in pids:
        try:
            p = int(p)
            ppid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(p)],
                                      capture_output=True, text=True).stdout.strip() or 0)
        except Exception:
            continue
        if ppid <= 1:
            try:
                os.kill(p, signal.SIGKILL)
                killed.append(p)
            except Exception:
                pass
    return killed


def port_free(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port_free(port, secs=8.0):
    deadline = time.time() + secs
    while time.time() < deadline:
        if port_free(port):
            return True
        time.sleep(0.25)
    return False


def stop_existing():
    if not os.path.exists(STATE_FILE):
        print("no state file; nothing to stop")
        return
    try:
        st = json.load(open(STATE_FILE))
        os.kill(int(st["pid"]), signal.SIGTERM)
        port = int(st.get("port") or 0)
        if port:
            wait_port_free(port)
        time.sleep(0.5)
        killed = kill_orphan_captures()
        print("stopped pid %s (%s); orphan captures cleaned: %s" %
              (st["pid"], st["url"], killed or "none"))
        os.remove(STATE_FILE)
    except Exception as ex:
        print("stop failed: %s" % ex)


def load_captures():
    """Re-seed the capture gallery from disk so it survives a server restart."""
    if not os.path.isdir(CAPTURE_DIR):
        return
    names = sorted([n for n in os.listdir(CAPTURE_DIR) if n.endswith((".png", ".jpg"))],
                   reverse=True)
    for n in names[:12]:
        try:
            stt = os.stat(os.path.join(CAPTURE_DIR, n))
        except OSError:
            continue
        MIRROR.captures.append({"name": n, "ts": stt.st_mtime,
                                "kb": round(stt.st_size / 1024, 1)})


def main():
    args = sys.argv[1:]
    if "--stop" in args:
        stop_existing()
        return
    preferred = 8730
    for a in args:
        if a.startswith("--port="):
            preferred = int(a.split("=", 1)[1])
    port = pick_port(preferred)
    if "--detach" in args:
        detach()

    os.makedirs(CAPTURE_DIR, exist_ok=True)
    load_settings()
    load_captures()
    orphans = kill_orphan_captures()
    if orphans:
        MIRROR.log("cleared %d orphaned adb capture process(es): %s" % (len(orphans), orphans), "warn")
    MIRROR.serial = discover_device()
    if MIRROR.serial:
        MIRROR.refresh_geometry()
    threading.Thread(target=MIRROR.run, daemon=True, name="frames").start()
    threading.Thread(target=MIRROR.watchdog, daemon=True, name="watchdog").start()
    threading.Thread(target=status_loop, daemon=True, name="status").start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    json.dump({"pid": os.getpid(), "port": port, "url": url, "started": time.time(),
               "serial": MIRROR.serial}, open(STATE_FILE, "w"), indent=2)
    MIRROR.log("dashboard listening on %s (device: %s)" % (url, MIRROR.serial or "none"), "info")
    print("LISTENING %s pid=%d" % (url, os.getpid()), flush=True)
    def _bye(signum, _frame):
        """SIGTERM must free the phone's capture slot, not just exit."""
        try:
            MIRROR.log("signal %d - shutting down" % signum, "warn")
        except Exception:
            pass
        MIRROR.stop.set()
        MIRROR._kill_procs()
        time.sleep(0.4)
        kill_orphan_captures()
        os._exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MIRROR.stop.set()
        MIRROR._kill_procs()
        httpd.server_close()


if __name__ == "__main__":
    main()

# Phone Mirror

Browser dashboard that mirrors an Android phone over adb, tracks what is on
screen, and alerts you when one app has held the phone too long.

Runs entirely on your machine. No accounts, no network egress, no build step,
no Node - Python standard library plus `adb` and `ffmpeg`.

## Requirements

| Tool | Why |
| --- | --- |
| `adb` (Android platform tools) | device access, screen capture, input |
| `ffmpeg` | decodes the H.264 stream into JPEG frames |
| Python 3.9+ | the server (stdlib only) |
| Pillow + numpy | optional: motion level and black-screen detection |

The phone needs USB debugging (or wireless debugging) enabled and to be
authorised from the machine.

## Quick start

```bash
chmod +x start.sh         # only needed if the clone lost the executable bit
./start.sh start          # starts the server in the background, prints the URL
open http://127.0.0.1:8730/
./start.sh status         # live device + stream health as JSON
./start.sh log            # last server log lines
./start.sh stop
```

## What you get

- **Live mirror** at 20-30 fps over wireless adb, with touch-through:
  click = tap, drag = swipe, hold = long press, coordinates mapped to the
  device's own pixel space.
- **System-bar crop** - drops the status bar and gesture bar so only app
  content is sent and shown. Uses the device's measured insets when available,
  otherwise Android dp defaults, with manual override boxes.
- **Device condition panel** - foreground app, awake/locked, battery, charge
  state, temperature, `/data` free, IP, uptime, load.
- **Stream health** - fps the phone produces vs fps sent to the browser,
  frame age, restart count, average frame size, motion level, black-frame
  detection.
- **Dwell alerts** - notify me after N minutes on one app. Fires a browser
  notification (via a service worker, so it works with the tab backgrounded),
  a macOS notification, an in-page banner and a live-activity pill.
- **Live activity island** - a persistent pill showing the current app, a
  ticking dwell timer and a progress ring; click it for the session feed.
- **Captures** - one-click snapshot of the live frame, or a full-resolution
  `screencap` to `captures/`.
- **Light / dark / system** theme, rectangle or rounded frame.

## How the video path works

`adb exec-out screenrecord --output-format=h264 -` is piped straight into
ffmpeg, which emits JPEG frames that a small HTTP server fans out as an MJPEG
stream.

Two non-obvious things this design works around:

- Looping `adb exec-out screencap -p` is unusable: each PNG is ~1.4 MB and
  takes 1-2 s over wireless adb, i.e. under 1 fps.
- ffmpeg cannot meter this stream with `-vf fps=` or `-r` because a raw H.264
  stream carries no timing info, so frames arrive in bursts. The server instead
  forwards the *newest* frame of each burst once the target interval has
  elapsed, which keeps the picture fresh instead of queueing behind latency.

The encode size is snapped to an integer multiple of the display's reduced
aspect ratio (e.g. 1224x2700 -> 34x75 unit), otherwise `screenrecord` pads the
sub-pixel difference with black bars.

## Layout

```
server.py    HTTP server, capture pipeline, device polling, dwell alerts
index.html   the dashboard (vanilla JS, no dependencies)
sw.js        service worker so notifications work from a background tab
start.sh     start / stop / status / log
```

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET /` | dashboard |
| `GET /stream.mjpg` | live MJPEG stream |
| `GET /frame.jpg` | newest single frame |
| `GET /api/status` | device + stream + dwell state |
| `GET /api/events` | server event log |
| `GET /api/alerts` | dwell alerts raised |
| `POST /api/action` | tap / swipe / key / text / capture / reconnect |
| `POST /api/quality` | switch encode preset |
| `POST /api/settings` | dwell and crop settings (persisted) |

The server binds to `127.0.0.1` only.

## Notes and limits

- DRM and other secure screens are blacked out by Android itself.
- `adb input text` is ASCII only, so emoji and CJK cannot be typed.
- The dwell clock pauses while the screen is off or adb drops, rather than
  resetting, and restarts from zero when the server restarts.
- Nothing here force-stops or restarts an app; input is limited to taps,
  swipes and key events.

## Credits and prior art

No code in this repository was copied from the projects below. They are cited
because the tool depends on some of them at runtime, and because the rest are
the prior art this stands on.

Runtime dependencies (invoked, not bundled or relicensed):

| Project | Licence | Used for |
| --- | --- | --- |
| [`adb`, Android platform-tools](https://developer.android.com/tools/adb) | Android SDK terms | device transport, `screenrecord`, `input` |
| [FFmpeg](https://ffmpeg.org/legal.html) | LGPL-2.1 or later (GPL-2.0+ if the optional parts are built in) | H.264 decode, JPEG encode |
| [Pillow](https://raw.githubusercontent.com/python-pillow/Pillow/main/LICENSE) | MIT-like HPND | optional motion / black-frame metrics |
| [NumPy](https://raw.githubusercontent.com/numpy/numpy/main/LICENSE.txt) | BSD-3-Clause | optional motion / black-frame metrics |

Prior art and references:

- [scrcpy](https://github.com/Genymobile/scrcpy) (Apache-2.0) established
  mirroring and controlling an Android device over adb. This project borrows no
  code from it and takes a deliberately narrower route - stock `adb`
  `screenrecord` plus `input`, with no on-device server - which is simpler to
  audit but lower quality and lower latency than scrcpy. If you need the best
  experience, use scrcpy; if you need a browser page and a dashboard, this.
- [ws-scrcpy](https://github.com/NetrisTV/ws-scrcpy) (MIT), a web client
  prototype for the scrcpy protocol, is the closest existing thing to what this
  page does. It decodes H.264 in the browser; this one converts to JPEG
  server-side and streams MJPEG instead, which trades quality for having no
  codec or WebSocket machinery at all.
- [RFC 2046](https://www.rfc-editor.org/rfc/rfc2046) for the multipart media
  type that `multipart/x-mixed-replace` streaming is built on.
- [Android `WindowInsets`](https://developer.android.com/reference/android/view/WindowInsets)
  for the status-bar and gesture-area sizing used as crop defaults.
- [WCAG 2.1](https://www.w3.org/TR/WCAG21/) for the 4.5:1 contrast target the
  light theme is tuned against.

## Provenance

This code was written by an AI coding assistant (Qoder) working on the author's
machine on 2026-10-02, then tested and published by the author. The measurements
quoted above - fps, frame sizes, crop offsets, contrast ratios - were taken on a
HONOR AMP-AN10 (1224x2700 @ 520 dpi) over wireless adb and will differ on other
devices.

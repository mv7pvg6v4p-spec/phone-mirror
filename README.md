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

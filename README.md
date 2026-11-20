<p align="center"><img src="airgus/static/logo-wordmark.svg" alt="Airgus" width="420"></p>

# Airgus

Live log monitor for ASIAIR and NINA: watches PHD2 and session logs over SMB, detects guiding failures,
failed meridian flips and stalled sessions, and alerts via Pushover or Home Assistant (MQTT).
Web UI, runs in Docker.

> **Status:** early prototype. ASIAIR + PHD2 log parsing, rule engine, web UI (replay with adjustable
> speed, live folder monitoring, guiding chart, 3D mount view, settings) and Pushover work.
> NINA adapter and Home Assistant/MQTT are planned (see roadmap).

## What it detects

| Rule | Detects | Severity |
|---|---|---|
| `runaway` | Guide error keeps growing although corrections are at their limit (collision, cable drag, clutch slip) | high → emergency |
| `star-lost` | Guide star lost for longer than a short glitch, or ASIAIR reports "Guide star lost" | high |
| `settle` | Repeated settle failures after dithering | warning |
| `meridian-flip` | Failed flip, endless auto-center loop, flip without pier-side change | high |
| `past-meridian` | Target tracked far past the meridian on the pre-meridian pier side | high |
| `rms` | Rolling total RMS above threshold | warning |
| `snr-drop` | Guide-star SNR falls well below its starting value (clouds, dew, dawn) | warning |
| `stale-log` | Sequence running but no new log lines | high |
| `tracking-off` | Mount tracking off while the sequence runs (Autorun log "Stop Tracking", or live `ScopeTrack` on port 4400 – also when switched off by hand; longer grace during a meridian flip) | high |
| `plate-solve` | Plate solve failed: preview shots info, during a sequence warning, repeated failures high ¹ | info / warning / high |
| `device-lost` | Guide camera or mount disconnected from the ASIAIR (high during a sequence, else info); reconnect is listed too ¹ | high / info |
| `main-camera` | Main camera: every `CameraStateChange` on port 4700 is listed (the event does not say whether it was connected or disconnected; warning during a sequence), high if no exposure starts within 3 min; port 4700 queries such as `get_camera_state` are only answered to the verified ASIAIR app ¹ | warning / high / info |
| `focuser` | EAF: reopened during a sequence (port 4700 `OpenEAF`; closing sends nothing) is a warning; the live position (`get_focuser_info`) is only answered to the verified ASIAIR app ¹ | high / warning / info |
| `mount-home` | Mount driven to its home position (port 4400 `ScopeHome`): warning while a sequence runs, info when it arrives ¹ | warning / info |
| `controller-health` | ASIAIR reports undervoltage, overcurrent or overheating ¹ | high / warning |
| `wifi` | ASIAIR WLAN signal stays weak ¹ | warning |
| `controller-offline` | Live connection to the ASIAIR lost while a sequence runs ¹ | high |

¹ needs the live ASIAIR connection.

## Live logs over SMB

In live mode Airgus can read the logs straight from an SMB share (web UI → Einstellungen →
SMB-Freigabe, then Quelle → Live → SMB-Freigabe). It polls the folder (inotify does not work on SMB)
and only reads what was appended since the last poll.

- Several shares can be watched at once (one per line, `<share>/<path>`). Default for the ASIAIR:
  `TF Images/ASIAIR/log` (SD card) and `EMMC Images/log` (internal storage) - the ASIAIR logs to the
  storage selected in the app. A share that is missing (e.g. no SD card) is skipped, the others keep working.
- The ASIAIR only offers guest sessions without SMB signing; with a user name (e.g. a NINA PC)
  signing stays on.
- Only files changed within the last 12 hours are watched, so finished nights are not read again.
- "Verbindung testen" lists the newest log files the settings point to.

## Live connection to the ASIAIR (experimental)

Besides the logs, Airgus can listen to the ASIAIR's control port (TCP 4700) and guider port (TCP 4400)
in live mode
(web UI → Einstellungen → ASIAIR live). ZWO does not document this interface; it was
reverse-engineered from the ASIAIR app's traffic and may change with any firmware update.

- Airgus only sends read-only requests (`test_connection` as keep-alive every 5 s,
  `get_disk_volume`, `get_target_sequences`); control commands are refused in code.
- Events (`PiStatus`: temperature, undervoltage, overcurrent; `Station`: WLAN signal;
  `Sequence`, `AutoExp`) reach every client, also while the ASIAIR app is connected.
- Queries such as `get_disk_volume` were only answered while no app held the main connection
  (firmware 14.39); disk and plan values then stay empty.
- Guide camera and mount are polled with the read-only `get_connected` on the guider port every 5 s
  (the ASIAIR sends no event when they drop); answers are recorded only when they change.
- The guider port also reports tracking on/off (`ScopeTrack`) the moment it changes, including manual
  changes in the app that no log records, and the mount driver's messages (`INDIServer`).
- The guider port speaks the PHD2 event-server format (`GuideStep`, `StarLost`, `SettleDone`, ...).
  While it is connected, guide frames come from there in real time and the same frames from the
  PHD2 log are dropped; the log still supplies pier side and hour angle from its guiding header.
- Received messages can be recorded to `data/recordings/Airgus_ASIAIR4700_<date>.jsonl`.
  Copied into a log folder, such recordings can be replayed like logs.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# Start the web UI on http://localhost:8080
.venv/bin/airgus --logs logs
```

In the web UI you pick a log folder below `--logs` and either **replay** it (1× … 1200× or maximum
speed, pause/resume, speed changes while running) or follow it **live**. Pushover keys, site latitude
and all rule thresholds are set there too and stored in `--data` (default `./data/settings.json`).
Environment variables `AIRGUS_PUSHOVER_TOKEN` / `AIRGUS_PUSHOVER_USER` override stored keys.

The 3D mount view shows a Sky-Watcher Explorer 150P (150/750 Newtonian, side focuser with camera,
guide scope) on an EQ6-R Pro, to scale. It is driven by hour angle, declination and pier side from the
PHD2 log and the site latitude, and highlights a counterweight-up position. It loads three.js from a CDN.

Headless commands are still available:

```bash
.venv/bin/airgus replay logs/2026-11-14                          # rules over a finished night
.venv/bin/airgus --config airgus.toml watch /Volumes/ASIAIR/log  # live, console output
```

The headless commands use [`airgus.example.toml`](airgus.example.toml) style config files.

### Docker

```bash
cp docker-compose.example.yml docker-compose.yml   # adjust the CIFS share
docker compose up -d --build                         # web UI on port 8080
```

Live logs are read directly over SMB by Airgus itself (no host mount, container runs unprivileged).
Enter the ASIAIR's IP address in the web UI: containers usually cannot resolve `asiair.local` (mDNS).

## Architecture

```
log files ──▶ parsers (PHD2, ASIAIR, NINA…) ──▶ neutral domain events ──▶ rig state
                                                         │
                                                         ▼
                                              rules ──▶ alerts (de-duplicated) ──▶ Pushover / console / (MQTT)
```

- `airgus/events.py` – system-neutral domain events; the contract between adapters and rules
- `airgus/parsers/` – one stateful line parser per log format
- `airgus/rules.py` – detection rules, configurable thresholds
- `airgus/engine.py` – applies events to the state, runs rules, de-duplicates, notifies
- `airgus/sources.py` – replay of finished logs and polling tail of a live folder

Rules never see raw log text, so adding NINA only means adding a parser.

## Roadmap

- [x] Live connection to the ASIAIR control port (4700)
- [x] Live guiding events from the ASIAIR guider port (4400, PHD2 event format)
- [x] Built-in SMB client for live logs
- [ ] NINA log adapter (and optionally live events via the NINA Advanced API plugin)
- [x] Web UI: state, guiding graph, alert history, configuration, replay speed, 3D mount view
- [ ] Authentication for the web UI (currently binds to localhost by default)
- [ ] Home Assistant via MQTT discovery (sensors, alert events, mute/acknowledge)
- [ ] Multiple rigs in one instance, SQLite persistence
- [ ] Twilight-aware suppression of SNR/star-lost alerts

## License

GPL-3.0 – see [LICENSE](LICENSE).

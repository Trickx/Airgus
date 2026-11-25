"""Runtime behind the web UI: runs one source (replay or live) at a time,
feeds the engine and broadcasts state, guide frames and alerts to subscribers.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from datetime import datetime
from enum import Enum
from pathlib import Path

from .alerts import Alert, Severity
from .asiair_rpc import CONTROLLER, GUIDER, AsiairRpcClient, Recorder
from .engine import Engine
from .events import Event, GuideFrame
from .notify import PushoverNotifier
from .rules import default_rules
from .settings import SettingsStore
from .fs import MultiFS, SmbFS
from .sources import FolderTail, log_files, replay
from .timeline import Timeline
from .mqtt import MqttPublisher
from .nina_api import RECORD_PREFIX as NINA_RECORD_PREFIX, NinaApiClient

PACKAGE_DIR = Path(__file__).resolve().parent


def code_stamp() -> float:
    """Newest modification time of Airgus' own code and web files."""
    return max((f.stat().st_mtime for f in PACKAGE_DIR.rglob("*") if f.suffix in (".py", ".js", ".html", ".css")),
               default=0.0)


# Code as loaded at start; the web UI is served fresh from disk, so after an update it can be newer
# than the running backend and must say so instead of misreading fields the old backend lacks.
STARTED_CODE = code_stamp()

log = logging.getLogger(__name__)

TICK_S = 0.5
FRAME_BUFFER = 20_000  # ~5.5 h of 1 s guide frames


class RunStatus(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"


class _Collector:
    """Notifier that hands alerts to the monitor."""

    def __init__(self, monitor: "Monitor") -> None:
        self.monitor = monitor

    def send(self, alert: Alert) -> None:
        self.monitor._on_alert(alert)


class Monitor:
    def __init__(self, store: SettingsStore, logs_root: Path) -> None:
        self.store = store
        self.logs_root = logs_root.resolve()
        self.engine = Engine(default_rules())
        self.status = RunStatus.IDLE
        self.mode: str | None = None
        self.folder: str | None = None
        self.speed: float = 0.0  # 0 = as fast as possible
        self.error: str | None = None
        self.push_enabled = False
        self.done = 0
        self.total = 0
        self.sim_time: datetime | None = None
        self.tail: FolderTail | None = None

        self._task: asyncio.Task | None = None
        self._resume = asyncio.Event()
        self._resume.set()
        self._anchor: tuple[float, datetime] | None = None  # (loop time, sim time)
        self._subscribers: set[asyncio.Queue] = set()
        self._frames: deque[dict] = deque(maxlen=FRAME_BUFFER)
        self.timeline = Timeline()
        self.mqtt: MqttPublisher | None = None
        self._new_frames: list[dict] = []
        self._push_queue: asyncio.Queue[Alert] = asyncio.Queue()
        self._background: list[asyncio.Task] = []

    # --- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        self._background = [asyncio.create_task(self._ticker()), asyncio.create_task(self._pusher())]

    async def shutdown(self) -> None:
        await self.stop()
        for t in self._background:
            t.cancel()

    # --- folders -------------------------------------------------------------

    def resolve_folder(self, folder: str) -> Path:
        path = (self.logs_root / folder).resolve()
        if path != self.logs_root and self.logs_root not in path.parents:
            raise ValueError("folder must be inside the logs directory")
        if not path.is_dir():
            raise ValueError(f"folder not found: {folder}")
        return path

    def folders(self) -> list[dict]:
        out = []
        if not self.logs_root.is_dir():
            return out
        candidates = [self.logs_root] + sorted(p for p in self.logs_root.rglob("*") if p.is_dir())
        for d in candidates:
            files = [f for f in log_files(d) if f.parent == d]
            if files:
                rel = d.relative_to(self.logs_root).as_posix()
                out.append({"folder": "" if rel == "." else rel, "files": [f.name for f in files]})
        return out

    # --- control -------------------------------------------------------------

    async def start(self, mode: str, folder: str, speed: float = 0.0, interval_s: float = 5.0,
                    from_start: bool = False, pushover: bool = False, location: str = "local") -> None:
        if mode == "live" and location == "smb":
            tail = FolderTail(fs=MultiFS([SmbFS(c) for c in self.store.smb_configs()]), from_start=from_start,
                              max_age_h=self.store.get()["smb"]["max_age_h"])
            await self.stop()
            self._reset(mode, tail.fs.describe(), speed, pushover)
            self._task = asyncio.create_task(self._run_live(tail, interval_s))
            return
        path = self.resolve_folder(folder)
        await self.stop()
        self._reset(mode, folder, speed, pushover)
        if mode == "replay":
            self._task = asyncio.create_task(self._run_replay(path))
        elif mode == "live":
            self._task = asyncio.create_task(self._run_live(FolderTail(path, from_start=from_start), interval_s))
        else:
            raise ValueError(f"unknown mode: {mode}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._resume.set()
        if self.status in (RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.LOADING):
            self.status = RunStatus.IDLE

    def pause(self) -> None:
        if self.status is RunStatus.RUNNING and self.mode == "replay":
            self._resume.clear()
            self.status = RunStatus.PAUSED

    def resume(self) -> None:
        if self.status is RunStatus.PAUSED:
            self._anchor = None
            self.status = RunStatus.RUNNING
            self._resume.set()

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.0, float(speed))
        self._anchor = None  # re-anchor so the change takes effect immediately

    # --- runners -------------------------------------------------------------

    def _reset(self, mode: str, folder: str, speed: float, pushover: bool) -> None:
        settings = self.store.get()
        self.engine = Engine(default_rules(settings["rules"]), [_Collector(self)])
        self.mode, self.folder, self.speed = mode, folder, max(0.0, float(speed))
        self.push_enabled = pushover
        self.error = None
        self.done = self.total = 0
        self.sim_time = None
        self._anchor = None
        self._frames.clear()
        self.timeline = Timeline()
        self.tail = None
        self._new_frames = []
        self._publish("reset", {})

    async def _run_replay(self, path: Path) -> None:
        loop = asyncio.get_running_loop()
        try:
            self.status = RunStatus.LOADING
            events = await asyncio.to_thread(lambda: list(replay(path)))
            self.total = len(events)
            self.status = RunStatus.RUNNING
            for i, ev in enumerate(events):
                if not self._resume.is_set():
                    await self._resume.wait()
                if self.speed > 0:
                    if self._anchor is None:
                        self._anchor = (loop.time(), ev.time)
                    wall0, sim0 = self._anchor
                    due = wall0 + (ev.time - sim0).total_seconds() / self.speed
                    delay = due - loop.time()
                    if delay > 0.01:
                        await asyncio.sleep(delay)
                    elif delay < -2.0:
                        self._anchor = None  # fell behind (e.g. after a slow step); do not burst
                elif i % 500 == 0:
                    await asyncio.sleep(0)  # max speed: stay responsive
                self._handle(ev)
                self.done = i + 1
            self.status = RunStatus.FINISHED
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("replay failed")
            self.status, self.error = RunStatus.ERROR, str(exc)

    def _asiair_clients(self) -> list[AsiairRpcClient]:
        cfg = self.store.get()["asiair"]
        if not cfg["enabled"] or not cfg["host"]:
            return []
        folder = self.store.path.parent / "recordings"
        channels = [(CONTROLLER, cfg["port"])] + ([(GUIDER, cfg["guider_port"])] if cfg["guider"] else [])
        return [AsiairRpcClient(cfg["host"], self._handle, port=int(port), channel=ch,
                                recorder=Recorder(folder, ch.record_prefix) if cfg["record"] else None)
                for ch, port in channels]

    def _nina_clients(self) -> list[NinaApiClient]:
        cfg = self.store.get()["nina"]
        if not cfg["enabled"] or not cfg["host"]:
            return []
        folder = self.store.path.parent / "recordings"
        return [NinaApiClient(cfg["host"], self._handle, port=int(cfg["port"]),
                              recorder=Recorder(folder, NINA_RECORD_PREFIX) if cfg["record"] else None)]

    def _start_mqtt(self) -> None:
        cfg = self.store.get()["mqtt"]
        if not cfg["enabled"] or not cfg["host"]:
            return
        try:
            self.mqtt = MqttPublisher(cfg)
            self.mqtt.start()
        except Exception as exc:  # noqa: BLE001 - monitoring goes on without MQTT
            log.warning("MQTT not started: %s", exc)
            self._publish("notice", {"level": "error", "text": f"MQTT nicht gestartet: {exc}"})
            self.mqtt = None

    def _stop_mqtt(self) -> None:
        if self.mqtt:
            self.mqtt.stop()
            self.mqtt = None

    async def _run_live(self, tail: FolderTail, interval_s: float) -> None:
        self.tail = tail
        self._start_mqtt()
        rpcs = [asyncio.create_task(c.run()) for c in [*self._asiair_clients(), *self._nina_clients()]]
        self.status = RunStatus.RUNNING
        try:
            while True:
                events = await asyncio.to_thread(tail.poll)
                for ev in events:
                    self._handle(ev)
                    self.done += 1
                self.engine.tick(datetime.now())
                await asyncio.sleep(max(1.0, interval_s))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("live monitoring failed")
            self.status, self.error = RunStatus.ERROR, str(exc)
        finally:
            for t in rpcs:
                t.cancel()
            self._stop_mqtt()

    def _handle(self, ev: Event) -> None:
        if self.engine.is_duplicate(ev):
            return  # the live guider already delivered this frame
        self.engine.process(ev)
        self.sim_time = ev.time
        self.timeline.add(ev)
        if isinstance(ev, GuideFrame):
            scale = self.engine.state.pixel_scale
            f = {"t": ev.time.isoformat(), "ra": round(ev.ra_px * scale, 2), "dec": round(ev.dec_px * scale, 2),
                 "snr": ev.snr}
            self._frames.append(f)
            self._new_frames.append(f)

    # --- alerts --------------------------------------------------------------

    def _on_alert(self, alert: Alert) -> None:
        self.timeline.add_alert(alert)
        if self.mqtt and self.mode == "live":
            self.mqtt.publish_alert(alert, self.snapshot())
        self._publish("alert", alert_dict(alert))
        if self.mode == "live" or self.push_enabled:
            po = self.store.pushover()
            if po["token"] and po["user"] and alert.severity >= Severity[po["min_severity"]]:
                self._push_queue.put_nowait(alert)

    async def _pusher(self) -> None:
        while True:
            alert = await self._push_queue.get()
            po = self.store.pushover()
            try:
                notifier = PushoverNotifier(po["token"], po["user"], po["device"] or None)
                await asyncio.to_thread(notifier.send, alert)
            except Exception as exc:
                log.warning("pushover failed: %s", exc)
                self._publish("notice", {"level": "error", "text": f"Pushover fehlgeschlagen: {exc}"})

    async def test_smb(self) -> dict:
        """Check every configured share and list its newest log files (for the 'test' button)."""
        from .parsers import parser_for

        age = self.store.get()["smb"]["max_age_h"] * 3600
        now = datetime.now().timestamp()

        async def check(cfg) -> dict:
            fs = SmbFS(cfg, timeout_s=10)
            try:
                files = await asyncio.to_thread(fs.files)
            except Exception as exc:
                return {"location": fs.describe(), "error": f"{type(exc).__name__}: {exc}", "total": 0, "files": []}
            logs = sorted((f for f in files if parser_for(f.name)), key=lambda f: f.mtime, reverse=True)
            return {"location": fs.describe(), "error": None, "total": len(logs),
                    "files": [{"name": f.name, "size": f.size, "mtime": datetime.fromtimestamp(f.mtime).isoformat(),
                               "watched": now - f.mtime <= age} for f in logs[:5]]}

        # one after another: the SMB client shares a single connection per server
        return {"shares": [await check(c) for c in self.store.smb_configs()]}

    async def send_test_push(self) -> None:
        po = self.store.pushover()
        if not (po["token"] and po["user"]):
            raise ValueError("Pushover token and user key are not set")
        alert = Alert(time=datetime.now(), rule="test", severity=Severity.WARNING,
                      title="Testnachricht", message="Airgus kann dich erreichen.", key="test")
        await asyncio.to_thread(PushoverNotifier(po["token"], po["user"], po["device"] or None).send, alert)

    # --- state & streaming ---------------------------------------------------

    def _restart_needed(self) -> bool:
        now = datetime.now()
        if getattr(self, "_stamp_checked", None) is None or (now - self._stamp_checked).total_seconds() > 10:
            self._stamp_checked, self._stamp_newer = now, code_stamp() > STARTED_CODE + 1
        return self._stamp_newer

    def snapshot(self) -> dict:
        state = self.engine.state
        return {
            "run": {
                "status": self.status.value, "mode": self.mode, "folder": self.folder, "speed": self.speed,
                "done": self.done, "total": self.total, "pushover": self.push_enabled,
                "error": self.error or (self.tail.error if self.tail and self.mode == "live" else None),
                "sim_time": self.sim_time.isoformat() if self.sim_time else None,
            },
            "rig": {
                **state.snapshot(),
                "hour_angle": _round(state.hour_angle(self.sim_time)) if self.sim_time else None,
                # live: the countdown runs on the wall clock, not on the time of the last event
                "pose": state.pose(datetime.now() if self.mode == "live" else self.sim_time),
                **self._rms(),
            },
            "site": self.store.get()["site"],
            "asiair": {k: v for k, v in self.store.get()["asiair"].items() if k != "record"},
            "alert_count": len(self.engine.alerts),
            "restart_needed": self._restart_needed(),
            "code_started": STARTED_CODE,  # the page reloads itself when this changes (backend restarted)
        }

    def _rms(self, minutes: float = 10.0) -> dict:
        if not self._frames or self.sim_time is None:
            return {"rms_ra": None, "rms_dec": None, "rms_total": None}
        cutoff = self.sim_time.timestamp() - minutes * 60
        ra = dec = 0.0
        n = 0
        for f in reversed(self._frames):
            if datetime.fromisoformat(f["t"]).timestamp() < cutoff:
                break
            ra += f["ra"] ** 2
            dec += f["dec"] ** 2
            n += 1
        if n == 0:
            return {"rms_ra": None, "rms_dec": None, "rms_total": None}
        return {"rms_ra": _round(math.sqrt(ra / n)), "rms_dec": _round(math.sqrt(dec / n)),
                "rms_total": _round(math.sqrt((ra + dec) / n))}

    def frames(self) -> list[dict]:
        return list(self._frames)

    def alerts(self) -> list[dict]:
        return [alert_dict(a) for a in self.engine.alerts]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self, kind: str, data) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait((kind, data))
            except asyncio.QueueFull:
                pass  # slow client; it will resync from the next status message

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            if self.mqtt and self.mode == "live":
                self.mqtt.publish_state(self.snapshot())
            if self._subscribers:
                if self._new_frames:
                    self._publish("frames", self._new_frames)
                self._publish("status", self.snapshot())
            self._new_frames = []


def alert_dict(a: Alert) -> dict:
    return {"time": a.time.isoformat(), "rule": a.rule, "severity": a.severity.name,
            "title": a.title, "message": a.message}


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)

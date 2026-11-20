"""Read-only live clients for the ASIAIR's TCP ports.

ZWO does not document these interfaces; this follows the traffic of the ASIAIR app
(firmware 14.39, 2026-11-20):

- Port 4700 ("ASI AIR imager"): control. Events ``PiStatus`` (temperature, under-voltage),
  ``Station`` (WLAN signal), ``Sequence``, ``AutoExp``; queries ``get_disk_volume``,
  ``get_target_sequences``.
- Port 4400 ("ASI AIR guider"): guiding, in the format of the PHD2 event server
  (``GuideStep``, ``StarLost``, ``SettleDone``, ``GuidingDithered``, ``AppState``, ...),
  plus ``ScopeTrack`` (``state`` on/off, also when switched by hand in the app) and
  ``INDIServer`` (messages of the mount driver, ``Msg`` with [INFO]/[WARNING] level).

Common protocol:

- JSON objects separated by CR LF.
- Requests: ``{"id": n, "method": "..."}``. Responses carry ``"jsonrpc": "2.0"``,
  the method, ``result``, ``code`` (0 = ok) and the request id.
- Unsolicited events carry ``"Event"``.
- The server drops clients that stay silent, so - like the app - we send
  ``test_connection`` every few seconds.
- Only the last request of a TCP segment is answered, so requests are spaced out.
- Events reach every client. On 4700, queries such as ``get_disk_volume`` were only
  answered on the app's connection, not on a second one while the app was connected.

Airgus only ever *reads*: every request goes through the channel's allowlist.
The ASIAIR also accepts control commands on these ports (start_exposure, dither, ...);
those are refused here so a monitor can never disturb a running session.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .events import (
    ControllerActivity,
    ControllerConnected,
    ControllerDisconnected,
    ControllerHealth,
    DeviceStatus,
    DiskSpace,
    Dither,
    ExposureProgress,
    FocuserOpened,
    MainCameraChanged,
    MountMotion,
    MountStatus,
    MainCameraStatus,
    FocuserStatus,
    Event,
    GuideFrame,
    GuiderConnected,
    GuiderDisconnected,
    GuiderInfo,
    GuidingStopped,
    SequencePlan,
    SettleCompleted,
    SettleFailed,
    SolveProgress,
    Source,
    StarLost,
    TrackingChanged,
    WifiSignal,
)

log = logging.getLogger(__name__)

DEFAULT_PORT = 4700
GUIDER_PORT = 4400
STREAM_LIMIT = 4 * 1024 * 1024  # plans with many slots can be large
REQUEST_GAP_S = 0.3
RECORD_PREFIX = "Airgus_ASIAIR4700_"
GUIDER_RECORD_PREFIX = "Airgus_ASIAIR4400_"


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Mapper(Protocol):
    def __call__(self, obj: dict, time: datetime) -> list[Event]: ...


# --- port 4700: controller -------------------------------------------------------

def map_message(obj: dict, time: datetime) -> list[Event]:
    """Translate one decoded message from port 4700 into domain events."""
    src = Source.ASIAIR
    kind = obj.get("Event")
    if kind == "PiStatus":
        return [ControllerHealth(time=time, source=src, live=True, temp_c=_num(obj.get("temp")),
                                 overtemp=bool(obj.get("is_overtemp")), undervolt=bool(obj.get("is_undervolt")),
                                 overcurrent=bool(obj.get("is_over_current")))]
    if kind == "Station":
        dbm = _num(obj.get("sig_lev"))
        return [WifiSignal(time=time, source=src, live=True, dbm=dbm)] if dbm is not None else []
    if kind == "OpenEAF":
        # sent twice per state (working, complete); only the finished open counts
        return [FocuserOpened(time=time, source=src, live=True)] if obj.get("state") == "complete" else []
    if kind == "CameraStateChange":
        return [MainCameraChanged(time=time, source=src, live=True)]
    if kind == "Exposure":
        us = _num(obj.get("exp_us"))
        return [ExposureProgress(time=time, source=src, live=True, page=str(obj.get("page") or ""),
                                 state=str(obj.get("state") or ""), duration_s=us / 1e6 if us else None)]
    if kind == "PlateSolve":
        result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
        stars = _num(obj.get("star_number", result.get("star_number")))
        return [SolveProgress(time=time, source=src, live=True, page=str(obj.get("page") or ""),
                              state=str(obj.get("state") or ""), stars=int(stars) if stars is not None else None,
                              error=str(obj["error"]) if obj.get("error") else None)]
    if kind:
        return [ControllerActivity(time=time, source=src, live=True, kind=str(kind),
                                   state=str(obj.get("state") or ""))]

    if obj.get("code") not in (0, None) or not isinstance(obj.get("result"), dict):
        return []
    method, result = obj.get("method"), obj["result"]
    if method == "get_disk_volume":
        total, free = _num(result.get("totalMB")), _num(result.get("freeMB"))
        if total is not None and free is not None:
            return [DiskSpace(time=time, source=src, live=True, total_mb=total, free_mb=free)]
    elif method == "get_camera_state":
        state = str(result.get("state") or "")
        if state:
            return [MainCameraStatus(time=time, source=src, live=True, connected=state != "close",
                                     name=str(result["name"]) if result.get("name") else None)]
    elif method == "get_focuser_info":
        pos = _num(result.get("position"))
        if pos is not None:
            return [FocuserStatus(time=time, source=src, live=True, connected=True, position=int(pos),
                                  temp_c=_num(result.get("temperature")))]
        if result.get("state") == "close":
            return [FocuserStatus(time=time, source=src, live=True, connected=False)]
    elif method == "get_target_sequences":
        flip = result.get("merid_flip")
        return [SequencePlan(time=time, source=src, live=True, name=str(result.get("group_name") or ""),
                             left_time_s=_num(result.get("left_time_sec")),
                             left_size_mb=_num(result.get("left_size_m")),
                             meridian_flip=None if flip is None else bool(flip))]
    return []


# --- port 4400: guider (PHD2 event-server format) ----------------------------------

class GuiderMapper:
    """Stateful: PHD2 reports settling as SettleBegin ... SettleDone around the guide steps.

    Guide events are tagged ``Source.PHD2`` so the rules treat them exactly like guide-log
    frames (e.g. a lost star counts only after a few seconds), and ``live=True`` so the
    engine can drop the same frames when they arrive later from the log.
    """

    def __init__(self) -> None:
        self.settling = False

    def __call__(self, obj: dict, time: datetime) -> list[Event]:
        src = Source.PHD2
        kind = obj.get("Event")
        if kind == "GuideStep":
            return [GuideFrame(time=time, source=src, live=True, frame=int(_num(obj.get("Frame")) or 0),
                               ra_px=_num(obj.get("RADistanceRaw")) or 0.0,
                               dec_px=_num(obj.get("DECDistanceRaw")) or 0.0,
                               ra_pulse_ms=abs(_num(obj.get("RADuration")) or 0.0),
                               dec_pulse_ms=abs(_num(obj.get("DECDuration")) or 0.0),
                               snr=_num(obj.get("SNR")) or 0.0, mass=_num(obj.get("StarMass")) or 0.0,
                               settling=self.settling, ra_limited=bool(obj.get("RALimited")),
                               dec_limited=bool(obj.get("DecLimited")))]
        if kind == "StarLost":
            reason = str(obj.get("Status") or obj.get("ErrorCode") or "star lost")
            return [StarLost(time=time, source=src, live=True, reason=reason)]
        if kind == "SettleBegin":
            self.settling = True
            return []
        if kind == "SettleDone":
            self.settling = False
            if obj.get("Status", 0) == 0:
                return [SettleCompleted(time=time, source=src, live=True)]
            return [SettleFailed(time=time, source=src, live=True, message=str(obj.get("Error") or "settle failed"))]
        if kind == "GuidingDithered":
            return [Dither(time=time, source=src, live=True)]
        if kind == "GuidingStopped":
            self.settling = False
            return [GuidingStopped(time=time, source=src, live=True)]
        if kind == "AppState":
            return [GuiderInfo(time=time, source=Source.ASIAIR, live=True, app_state=str(obj.get("State")))]
        if kind in ("StartGuiding", "Paused", "Resumed", "LockPositionLost", "Calibrating", "LoopingExposures"):
            state = {"StartGuiding": "Guiding", "Paused": "Paused", "Resumed": "Guiding",
                     "LockPositionLost": "LostLock", "Calibrating": "Calibrating",
                     "LoopingExposures": "Looping"}[kind]
            return [GuiderInfo(time=time, source=Source.ASIAIR, live=True, app_state=state)]
        if kind == "ScopeTrack":  # tracking switched on/off, also by hand in the app (not in any log)
            state = str(obj.get("state", "")).lower()
            if state in ("on", "off"):
                return [TrackingChanged(time=time, source=Source.ASIAIR, live=True, on=state == "on")]
            return []
        if kind and kind.startswith("Scope") and "state" in obj:  # ScopeHome (verified), ScopeGoto etc.
            return [MountMotion(time=time, source=Source.ASIAIR, live=True, kind=kind[5:],
                                state=str(obj.get("state") or ""), ra_h=_num(obj.get("RA")),
                                dec_deg=_num(obj.get("Dec")))]
        if kind == "INDIServer":  # messages of the mount driver, e.g. "[WARNING] Stopping Tracking (Sidereal)."
            return [ControllerActivity(time=time, source=Source.ASIAIR, live=True, kind="INDI",
                                       state=str(obj.get("Msg") or "").strip())]
        if kind == "Alert":
            return [ControllerActivity(time=time, source=Source.ASIAIR, live=True, kind="GuiderAlert",
                                       state=str(obj.get("Msg") or ""))]

        if obj.get("code") not in (0, None) or "result" not in obj:
            return []
        method, result = obj.get("method"), obj["result"]
        if method == "get_app_state" and isinstance(result, str):
            return [GuiderInfo(time=time, source=Source.ASIAIR, live=True, app_state=result)]
        if method == "get_connected" and isinstance(result, dict):
            cam = result.get("camera")
            mount = result.get("mount_name") or (str(result["mount"]) if result.get("mount") is not None else None)
            return [DeviceStatus(time=time, source=Source.ASIAIR, live=True,
                                 guide_camera=str(cam.get("name") or "?") if isinstance(cam, dict) else None,
                                 mount=mount)]
        if method == "scope_get_ra_dec" and isinstance(result, list) and len(result) >= 3:
            ra, dec, lst = (_num(v) for v in result[:3])
            if None not in (ra, dec, lst):
                return [MountStatus(time=time, source=Source.ASIAIR, live=True, ra_h=ra, dec_deg=dec, lst_h=lst)]
        if method == "scope_get_pierside" and result in ("pier_east", "pier_west"):
            return [MountStatus(time=time, source=Source.ASIAIR, live=True,
                                pier_side="East" if result == "pier_east" else "West")]
        if method == "scope_get_track_state" and isinstance(result, bool):
            return [MountStatus(time=time, source=Source.ASIAIR, live=True, tracking=result)]
        if method == "scope_get_location" and isinstance(result, list) and len(result) >= 2:
            lat, lon = _num(result[0]), _num(result[1])
            if lat is not None and lon is not None:
                return [MountStatus(time=time, source=Source.ASIAIR, live=True, latitude=lat, longitude=lon)]
        if method == "get_pixel_scale" and _num(result):
            return [GuiderInfo(time=time, source=Source.ASIAIR, live=True, pixel_scale=_num(result))]
        return []


# --- channels ------------------------------------------------------------------

@dataclass(frozen=True)
class Channel:
    name: str
    port: int
    record_prefix: str
    allowed: frozenset[str]
    polls: tuple[str, ...]  # read-only queries sent on connect and every poll interval
    mapper_factory: Callable[[], Mapper]
    # read-only queries sent with every heartbeat: (method, params); answers are recorded only on change
    watch: tuple[tuple[str, list | None], ...] = ()

    def connection_events(self, record: dict, time: datetime) -> list[Event]:
        host = str(record.get("host", ""))
        up = record.get("conn") == "up"
        if self.name == "guider":
            ev = GuiderConnected(time=time, source=Source.ASIAIR, live=True, host=host) if up else \
                GuiderDisconnected(time=time, source=Source.ASIAIR, live=True, host=host,
                                   reason=str(record.get("reason", "")))
        else:
            ev = ControllerConnected(time=time, source=Source.ASIAIR, live=True, host=host) if up else \
                ControllerDisconnected(time=time, source=Source.ASIAIR, live=True, host=host,
                                       reason=str(record.get("reason", "")))
        return [ev]


CONTROLLER = Channel("controller", DEFAULT_PORT, RECORD_PREFIX,
                     frozenset({"test_connection", "get_disk_volume", "get_target_sequences", "get_app_state"}),
                     ("get_disk_volume", "get_target_sequences"), lambda: map_message)
# Port 4700 answers queries only to a client that passed get_verify_str / verify_client (a signature
# made with a key inside the ASIAIR app). Airgus does not do that, so on 4700 it only gets the
# broadcast events; the answers mapped in map_message come from recordings of the app's traffic.
GUIDER = Channel("guider", GUIDER_PORT, GUIDER_RECORD_PREFIX,
                 frozenset({"test_connection", "get_app_state", "get_pixel_scale", "get_connected",
                            "scope_get_ra_dec", "scope_get_pierside", "scope_get_track_state", "scope_get_location"}),
                 ("get_app_state", "get_pixel_scale", "scope_get_location"), GuiderMapper,
                 # guide camera and mount connection (no event when they drop or reconnect) and the
                 # mount position for the 3D view; all answered without client verification on 4400
                 watch=(("get_connected", [True]), ("scope_get_ra_dec", None), ("scope_get_pierside", None),
                        ("scope_get_track_state", None)))
CHANNELS = (CONTROLLER, GUIDER)
READ_ONLY_METHODS = CONTROLLER.allowed


def map_connection(record: dict, time: datetime) -> list[Event]:
    return CONTROLLER.connection_events(record, time)


# --- recording ---------------------------------------------------------------------

class AsiairRecordParser:
    """Replays a recording written by ``AsiairRpcClient`` (``Airgus_ASIAIR4700_*.jsonl`` etc.)."""

    def __init__(self, channel: Channel = CONTROLLER) -> None:
        self.channel = channel
        self.mapper = channel.mapper_factory()

    def feed(self, line: str) -> list[Event]:
        line = line.strip()
        if not line:
            return []
        try:
            rec = json.loads(line)
            time = datetime.fromisoformat(rec["t"])
        except (ValueError, KeyError, TypeError):
            return []
        if "conn" in rec:
            return self.channel.connection_events(rec, time)
        msg = rec.get("msg")
        return self.mapper(msg, time) if isinstance(msg, dict) else []


def record_parser_for(name: str) -> AsiairRecordParser | None:
    for ch in CHANNELS:
        if name.startswith(ch.record_prefix) and name.endswith(".jsonl"):
            return AsiairRecordParser(ch)
    return None


class Recorder:
    """Appends every received message with its arrival time, one file per day."""

    def __init__(self, folder: Path, prefix: str = RECORD_PREFIX) -> None:
        self.folder = folder
        self.prefix = prefix

    def write(self, time: datetime, record: dict) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / f"{self.prefix}{time:%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.isoformat(timespec="milliseconds"), **record},
                                    ensure_ascii=False) + "\n")
        except OSError as exc:  # recording is a diagnostic aid, never fatal
            log.warning("cannot record ASIAIR message: %s", exc)


# --- client --------------------------------------------------------------------------

class AsiairRpcClient:
    def __init__(self, host: str, on_event: Callable[[Event], None], port: int | None = None,
                 recorder: Recorder | None = None, heartbeat_s: float = 5.0, poll_s: float = 60.0,
                 timeout_s: float = 20.0, retry_s: float = 5.0, max_retry_s: float = 60.0,
                 clock: Callable[[], datetime] = datetime.now, channel: Channel = CONTROLLER,
                 request_gap_s: float = REQUEST_GAP_S) -> None:
        self.host = host
        self.channel = channel
        self.port = port or channel.port
        self.on_event = on_event
        self.recorder = recorder
        self.heartbeat_s, self.poll_s, self.timeout_s = heartbeat_s, poll_s, timeout_s
        self.retry_s, self.max_retry_s = retry_s, max_retry_s
        self.request_gap_s = request_gap_s
        self.clock = clock
        self._ids = itertools.count(1000)
        self._connected: bool | None = None
        self._mapper = channel.mapper_factory()
        self._watched = {m for m, _ in channel.watch}
        self._last_answer: dict[str, object] = {}

    async def run(self) -> None:
        """Connect, read, reconnect - until cancelled."""
        delay = self.retry_s
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, limit=STREAM_LIMIT), self.timeout_s)
            except (OSError, asyncio.TimeoutError) as exc:
                self._down(f"Verbindung fehlgeschlagen: {str(exc) or type(exc).__name__}")
            else:
                delay = self.retry_s
                try:
                    await self._session(reader, writer)
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
                    self._down(f"Verbindung verloren: {str(exc) or type(exc).__name__}")
                finally:
                    writer.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_retry_s)

    # --- connection -------------------------------------------------------

    def _emit(self, events: list[Event]) -> None:
        for ev in events:
            try:
                self.on_event(ev)
            except Exception:
                log.exception("handling %s failed", type(ev).__name__)

    def _up(self) -> None:
        self._connected = True
        self._mapper = self.channel.mapper_factory()  # fresh settling state per connection
        self._last_answer = {}
        rec = {"conn": "up", "host": self.host}
        self._record(rec)
        self._emit(self.channel.connection_events(rec, self.clock()))

    def _down(self, reason: str) -> None:
        if self._connected is False:
            return  # report an outage once, not on every retry
        self._connected = False
        log.info("ASIAIR %s:%s: %s", self.host, self.port, reason)
        rec = {"conn": "down", "host": self.host, "reason": reason}
        self._record(rec)
        self._emit(self.channel.connection_events(rec, self.clock()))

    def _record(self, record: dict) -> None:
        if self.recorder:
            self.recorder.write(self.clock(), record)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._up()
        sender = asyncio.create_task(self._send_loop(writer))
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), self.timeout_s)
                if not line:
                    raise ConnectionResetError("ASIAIR hat die Verbindung beendet")
                self._receive(line)
                if sender.done():
                    sender.result()  # re-raise write errors
        finally:
            sender.cancel()

    def _receive(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            obj = json.loads(text)
        except ValueError:
            log.debug("not JSON from ASIAIR: %.200s", text)
            return
        if not isinstance(obj, dict):
            return
        now = self.clock()
        method = obj.get("method")
        if method in self._watched:  # polled every few seconds: record only when the answer changes
            if self._last_answer.get(method) != obj.get("result"):
                self._last_answer[method] = obj.get("result")
                self._record({"msg": obj})
        elif method != "test_connection":  # heartbeats would only bloat the recording
            self._record({"msg": obj})
        self._emit(self._mapper(obj, now))

    async def _send_loop(self, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        next_poll = loop.time()
        while True:
            if loop.time() >= next_poll:
                for method in self.channel.polls:
                    await self.call(writer, method)
                    await asyncio.sleep(self.request_gap_s)
                next_poll = loop.time() + self.poll_s
            await self.call(writer, "test_connection")
            for method, params in self.channel.watch:
                await asyncio.sleep(self.request_gap_s)
                await self.call(writer, method, params)
            await asyncio.sleep(self.heartbeat_s)

    async def call(self, writer: asyncio.StreamWriter, method: str, params: list | None = None) -> None:
        if method not in self.channel.allowed:
            raise PermissionError(f"Airgus sendet nur lesende Anfragen, nicht {method!r}")
        req: dict = {"id": next(self._ids), "method": method}
        if params is not None:
            req["params"] = params
        payload = json.dumps(req) + "\r\n"
        writer.write(payload.encode())
        await writer.drain()

"""Live connection to N.I.N.A. through the "Advanced API" plugin (ninaAPI by christian-photo, API v2).

- WebSocket ``ws://<host>:<port>/v2/socket``: NINA pushes events such as ``IMAGE-SAVE``,
  ``SEQUENCE-STARTING`` / ``SEQUENCE-FINISHED``, ``MOUNT-BEFORE-FLIP``, ``ERROR-AF``.
  Every message is ``{"Response": {"Event": "...", ...}, "Success": true, "Type": "Socket", ...}``.
- REST ``http://<host>:<port>/v2/api``: only the read-only paths in ``READ_ONLY_PATHS`` are used.
  The plugin also has GET endpoints that move the mount (``/equipment/mount/park``, ``/flip``,
  ``/slew`` ...), so every request is checked against that list.

Airgus never sends commands over the socket either. Event names and the IMAGE-SAVE fields follow
the plugin's source (Advanced API 2.2.x); unknown events are ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from .events import (
    ApplicationError,
    AutofocusCompleted,
    DeviceConnection,
    Event,
    ImageSaved,
    MeridianFlipCompleted,
    MeridianFlipStarted,
    MountMotion,
    MountStatus,
    NinaConnected,
    NinaDisconnected,
    SequenceEnded,
    SequenceStarted,
    SequenceStepFailed,
    SolveProgress,
    Source,
)

log = logging.getLogger(__name__)

DEFAULT_PORT = 1888
RECORD_PREFIX = "Airgus_NINA_"
READ_ONLY_PATHS = frozenset({"/version", "/equipment/mount/info"})

_DEVICES = {"MOUNT": "mount", "CAMERA": "camera", "FOCUSER": "focuser", "GUIDER": "guider",
            "FILTERWHEEL": "filterwheel", "ROTATOR": "rotator"}


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_event(obj: dict, time: datetime) -> list[Event]:
    """Translate one WebSocket message into domain events."""
    resp = obj.get("Response") if isinstance(obj, dict) else None
    if not isinstance(resp, dict):
        return []
    name = str(resp.get("Event") or "")
    s = Source.NINA

    if name == "IMAGE-SAVE":
        st = resp.get("ImageStatistics") if isinstance(resp.get("ImageStatistics"), dict) else {}
        stars = _num(st.get("Stars"))
        return [ImageSaved(time=time, source=s, live=True, frame_type=str(st.get("ImageType") or "light").lower(),
                           duration_s=_num(st.get("ExposureTime")), target=st.get("TargetName") or None,
                           filter=st.get("Filter") or None, hfr=_num(st.get("HFR")),
                           stars=int(stars) if stars is not None else None)]
    if name == "SEQUENCE-STARTING":
        return [SequenceStarted(time=time, source=s, live=True, target="")]
    if name == "SEQUENCE-FINISHED":
        return [SequenceEnded(time=time, source=s, live=True, reason="Sequenz beendet")]
    if name == "SEQUENCE-ENTITY-FAILED":
        entity = resp.get("Entity") or resp.get("Name") or "Sequenzschritt"
        return [SequenceStepFailed(time=time, source=s, live=True, step=str(entity),
                                   error=str(resp.get("Error") or obj.get("Error") or ""))]
    if name == "MOUNT-BEFORE-FLIP":
        return [MeridianFlipStarted(time=time, source=s, live=True)]
    if name == "MOUNT-AFTER-FLIP":
        return [MeridianFlipCompleted(time=time, source=s, live=True)]
    if name in ("MOUNT-HOMED", "MOUNT-PARKED"):
        return [MountMotion(time=time, source=s, live=True, kind="Home" if name == "MOUNT-HOMED" else "Park",
                            state="complete")]
    if name == "AUTOFOCUS-FINISHED":
        return [AutofocusCompleted(time=time, source=s, live=True, success=True, message="NINA: Autofokus fertig")]
    if name == "ERROR-AF":
        return [AutofocusCompleted(time=time, source=s, live=True, success=False,
                                   message=f"NINA: Autofokus fehlgeschlagen {resp.get('Error') or ''}".strip())]
    if name == "ERROR-PLATESOLVE":
        return [SolveProgress(time=time, source=s, live=True, page="sequence", state="fail",
                              error=str(resp.get("Error") or "plate solve failed"))]
    if name == "CAMERA-DOWNLOAD-TIMEOUT":
        return [ApplicationError(time=time, source=s, live=True, message="NINA: Kamera-Download-Timeout", fatal=False)]
    device, _, change = name.partition("-")
    if device in _DEVICES and change in ("CONNECTED", "DISCONNECTED"):
        return [DeviceConnection(time=time, source=s, live=True, device=_DEVICES[device],
                                 connected=change == "CONNECTED")]
    return []


_PIER = {"pierEast": "East", "pierWest": "West", "0": "East", "1": "West", "East": "East", "West": "West"}


def map_mount_info(info: dict, time: datetime) -> list[Event]:
    """``/equipment/mount/info`` (NINA TelescopeInfo) -> MountStatus; missing fields stay unknown."""
    if not isinstance(info, dict) or info.get("Connected") is False:
        return []
    ra, dec, lst = _num(info.get("RightAscension")), _num(info.get("Declination")), _num(info.get("SiderealTime"))
    pier = _PIER.get(str(info.get("SideOfPier")))
    tracking = info.get("TrackingEnabled")
    position = None not in (ra, dec, lst)
    if not position and pier is None and not isinstance(tracking, bool):
        return []
    return [MountStatus(time=time, source=Source.NINA, live=True,
                        ra_h=ra if position else None, dec_deg=dec if position else None,
                        lst_h=lst if position else None, pier_side=pier,
                        tracking=tracking if isinstance(tracking, bool) else None)]


class NinaRecordParser:
    """Replays a recording written by ``NinaApiClient`` (``Airgus_NINA_<date>.jsonl``)."""

    def feed(self, line: str) -> list[Event]:
        try:
            rec = json.loads(line)
            time = datetime.fromisoformat(rec["t"])
        except (ValueError, KeyError, TypeError):
            return []
        if "conn" in rec:
            host = str(rec.get("host", ""))
            if rec["conn"] == "up":
                return [NinaConnected(time=time, source=Source.NINA, live=True, host=host, version=rec.get("version"))]
            return [NinaDisconnected(time=time, source=Source.NINA, live=True, host=host,
                                     reason=str(rec.get("reason", "")))]
        if "mount" in rec:
            return map_mount_info(rec["mount"], time)
        return map_event(rec.get("msg"), time)


class NinaApiClient:
    """Keeps the WebSocket open (reconnecting with backoff) and polls the mount every few seconds."""

    def __init__(self, host: str, on_event: Callable[[Event], None], port: int = DEFAULT_PORT,
                 recorder=None, mount_poll_s: float = 5.0, retry_s: float = 5.0, max_retry_s: float = 60.0,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.host, self.port, self.on_event, self.recorder = host, port, on_event, recorder
        self.mount_poll_s, self.retry_s, self.max_retry_s, self.clock = mount_poll_s, retry_s, max_retry_s, clock
        self._down_reported = False
        self._last_mount: dict | None = None

    @property
    def socket_url(self) -> str:
        return f"ws://{self.host}:{self.port}/v2/socket"

    def get(self, path: str, timeout_s: float = 5.0):
        """Read-only REST GET; anything outside READ_ONLY_PATHS is refused."""
        if path not in READ_ONLY_PATHS:
            raise PermissionError(f"refusing NINA API request {path!r}: only read-only paths are allowed")
        with urllib.request.urlopen(f"http://{self.host}:{self.port}/v2/api{path}", timeout=timeout_s) as resp:
            body = json.loads(resp.read() or b"{}")
        return body.get("Response") if isinstance(body, dict) else None

    def _record(self, record: dict) -> None:
        if self.recorder:
            self.recorder.write(self.clock(), record)

    def _emit(self, events: list[Event]) -> None:
        for ev in events:
            self.on_event(ev)

    async def run(self) -> None:
        import websockets

        delay = self.retry_s
        while True:
            try:
                async with websockets.connect(self.socket_url, open_timeout=10, ping_interval=20,
                                              ping_timeout=20, max_size=2**22) as ws:
                    version = None
                    try:
                        version = await asyncio.to_thread(self.get, "/version")
                    except Exception:  # noqa: BLE001 - the socket alone is enough
                        pass
                    now = self.clock()
                    self._down_reported, self._last_mount, delay = False, None, self.retry_s
                    self._record({"conn": "up", "host": self.host, "version": version})
                    self._emit([NinaConnected(time=now, source=Source.NINA, live=True, host=self.host,
                                              version=str(version) if version is not None else None)])
                    poller = asyncio.create_task(self._poll_mount())
                    try:
                        async for raw in ws:
                            now = self.clock()
                            try:
                                obj = json.loads(raw)
                            except (TypeError, ValueError):
                                continue
                            self._record({"msg": obj})
                            self._emit(map_event(obj, now))
                    finally:
                        poller.cancel()
                    reason = "Verbindung von NINA geschlossen"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any network error means: retry later
                reason = str(exc) or type(exc).__name__
            if not self._down_reported:
                self._down_reported = True
                self._record({"conn": "down", "host": self.host, "reason": reason})
                self._emit([NinaDisconnected(time=self.clock(), source=Source.NINA, live=True, host=self.host,
                                             reason=reason)])
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_retry_s)

    async def _poll_mount(self) -> None:
        while True:
            try:
                info = await asyncio.to_thread(self.get, "/equipment/mount/info")
                if isinstance(info, dict):
                    if info != self._last_mount:  # record only changes
                        self._last_mount = info
                        self._record({"mount": info})
                    self._emit(map_mount_info(info, self.clock()))
            except Exception as exc:  # noqa: BLE001 - mount info is optional
                log.debug("NINA mount info failed: %s", exc)
            await asyncio.sleep(self.mount_poll_s)


def record_folder(data_dir: Path) -> Path:
    return data_dir / "recordings"

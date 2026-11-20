"""Parser for N.I.N.A. logs (``YYYYMMDD-HHMMSS-<version>.<pid>-YYYYMM.log``, NINA 3.x).

Line format: ``DATE|LEVEL|SOURCE|MEMBER|LINE|MESSAGE``; lines without a date continue the previous
message (stack traces) and are ignored. Only the messages the rules need are mapped.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..events import (
    ApplicationError,
    AutoCenterResult,
    AutofocusCompleted,
    DeviceConnection,
    Event,
    ExposureStarted,
    FocuserPosition,
    FramesStarted,
    MeridianFlipCompleted,
    MeridianFlipFailed,
    MeridianFlipStarted,
    PlateSolved,
    SequenceEnded,
    SequenceStarted,
    SettleFailed,
    Source,
    TrackingChanged,
)

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?\|(\w+)\|([^|]*)\|[^|]*\|[^|]*\|(.*)$")
_TARGET = re.compile(r"Container: NINA\.Sequencer\.Container\.(?:SimpleDSOContainer|DeepSkyObjectContainer)"
                     r".*?Target: (.+?) RA:")
_TAKE_EXPOSURE = re.compile(r"^Starting Category: [^,]*, Item: (?:TakeExposure|TakeManyExposures|SmartExposure), "
                            r"ExposureTime ([\d.]+).*?ImageType (\w+)")
_FOCUSER_MOVE = re.compile(r"^Moving Focuser to position (\d+)")
_SOLVED = re.compile(r"^Platesolve successful: .*Position Angle: (-?[\d.]+)")
_UNHANDLED = re.compile(r"^An unhandled (?:domain )?exception has occurred of type ([\w.]+)")
_DEVICES = {"Disconnected mount": ("mount", False), "Disconnected Camera": ("camera", False),
            "Disconnected Focuser": ("focuser", False)}
_CONNECTED = re.compile(r"^Successfully connected (mount|Camera|Focuser)\. Id: [^ ]+ Name: (.*)$")
# Lines NINA writes as a consequence of a sequence the user cancelled (e.g. centering by hand afterwards).
_FAILURE_EVENTS = (AutoCenterResult, MeridianFlipFailed, SettleFailed)


class NinaParser:
    source = Source.NINA

    def __init__(self) -> None:
        self._sequence = False
        self._cancelled = False
        self._index = 0
        self._frame_type: str | None = None
        self._focuser: int | None = None

    def feed(self, line: str) -> list[Event]:
        m = _LINE.match(line.rstrip("\r\n"))
        if not m:
            return []
        t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
        events = self._map(t, m.group(2), m.group(3), m.group(4).strip())
        if self._cancelled:
            events = [e for e in events if not isinstance(e, _FAILURE_EVENTS)]
        return events

    def _map(self, t: datetime, level: str, src: str, msg: str) -> list[Event]:
        s = self.source

        # --- sequence -------------------------------------------------------
        if level == "INFO" and (mm := _TARGET.search(msg)) and msg.startswith("Starting"):
            self._sequence, self._cancelled, self._index, self._frame_type = True, False, 0, None
            return [SequenceStarted(time=t, source=s, target=mm.group(1).strip())]
        if msg == "Sequence run was cancelled":
            self._cancelled = True
            return self._end(t, "Sequenz abgebrochen")
        if msg in ("Simple Sequence finished", "Advanced Sequence finished"):
            return self._end(t, "Sequenz beendet")
        if mm := _TAKE_EXPOSURE.match(msg):
            out: list[Event] = []
            frame_type = mm.group(2).lower()
            if frame_type == "snapshot":
                return []
            if frame_type != self._frame_type:
                self._frame_type = frame_type
                out.append(FramesStarted(time=t, source=s, frame_type=frame_type))
            self._index += 1
            out.append(ExposureStarted(time=t, source=s, index=self._index, duration_s=float(mm.group(1))))
            return out

        # --- meridian flip and tracking ---------------------------------------
        if src == "MeridianFlipVM.cs":
            if msg.startswith("Meridian Flip - Initializing Meridian Flip"):
                return [MeridianFlipStarted(time=t, source=s)]
            if msg.startswith("Meridian Flip - Exiting meridian flip"):
                return [MeridianFlipCompleted(time=t, source=s)]
            if msg.startswith("Meridian Flip - Stopping tracking"):
                return [TrackingChanged(time=t, source=s, on=False)]
            if msg.startswith("Meridian Flip - Resuming tracking"):
                return [TrackingChanged(time=t, source=s, on=True)]
            if level == "ERROR":
                return [MeridianFlipFailed(time=t, source=s, message=msg)]
        if level == "ERROR" and msg.startswith("Telescope is not tracking"):
            return [TrackingChanged(time=t, source=s, on=False)]

        # --- centering, plate solving -----------------------------------------
        if mm := _SOLVED.match(msg):
            return [PlateSolved(time=t, source=s, angle_deg=float(mm.group(1)))]
        if level == "ERROR" and src == "SequenceItem.cs" and ", Item: Center" in msg:
            return [AutoCenterResult(time=t, source=s, centered=False, distance_deg=None,
                                     message="NINA: Zentrieren fehlgeschlagen")]

        # --- guiding --------------------------------------------------------
        if level == "ERROR" and src == "PHD2Guider.cs" and "settle" in msg.lower():
            return [SettleFailed(time=t, source=s, message=f"NINA: {msg}")]

        # --- focus ----------------------------------------------------------
        if mm := _FOCUSER_MOVE.match(msg):
            self._focuser = int(mm.group(1))
            return [FocuserPosition(time=t, source=s, position=self._focuser)]
        if src == "AutoFocusVM.cs" and msg == "AutoFocus completed":
            out = [AutofocusCompleted(time=t, source=s, success=True, message=msg)]
            if self._focuser is not None:
                out.append(FocuserPosition(time=t, source=s, position=self._focuser, final=True))
            return out
        if src == "AutoFocusVM.cs" and level == "ERROR":
            return [AutofocusCompleted(time=t, source=s, success=False, message=msg)]

        # --- devices and application ------------------------------------------
        if msg in _DEVICES:
            device, connected = _DEVICES[msg]
            return [DeviceConnection(time=t, source=s, device=device, connected=connected)]
        if mm := _CONNECTED.match(msg):
            return [DeviceConnection(time=t, source=s, device=mm.group(1).lower(), connected=True,
                                     name=mm.group(2).strip())]
        if level == "ERROR" and (mm := _UNHANDLED.match(msg)):
            return [ApplicationError(time=t, source=s, message=f"NINA: {mm.group(1)}",
                                     fatal="Application must terminate: True" in msg)]
        return []

    def _end(self, t: datetime, reason: str) -> list[Event]:
        if not self._sequence:
            return []
        self._sequence = False
        return [SequenceEnded(time=t, source=self.source, reason=reason)]

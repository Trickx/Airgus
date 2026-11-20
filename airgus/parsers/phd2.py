"""Parser for PHD2 guide logs (PHD2_GuideLog_*.txt), as written by PHD2 and by ASIAIR."""

from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta

from ..events import (
    CalibrationFailed,
    Dither,
    Event,
    GuideFrame,
    GuidingStarted,
    GuidingStopped,
    LogError,
    SettleCompleted,
    SettleFailed,
    Source,
    StarLost,
)

_TS = "%Y-%m-%d %H:%M:%S"
_BEGINS = re.compile(r"^Guiding Begins at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_ENDS = re.compile(r"^Guiding Ends at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_CAL_BEGINS = re.compile(r"^Calibration Begins at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_KV = re.compile(r"([A-Za-z][A-Za-z ._-]*?)\s*=\s*([^,]+)")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    m = _NUM.search(value)
    return float(m.group()) if m else None


class Phd2Parser:
    """Stateful line parser. Only lines inside a guiding section produce events."""

    source = Source.PHD2

    def __init__(self) -> None:
        self._start: datetime | None = None
        self._in_guiding = False
        self._header: dict[str, str] = {}
        self._last_time: datetime | None = None
        self._settling = False
        self._calibration: datetime | None = None  # start of a running calibration
        self._calibration_failed = False

    def feed(self, line: str) -> list[Event]:
        line = line.rstrip("\r\n")
        if not line:
            return []

        if m := _BEGINS.match(line):
            self._start = datetime.strptime(m.group(1), _TS)
            self._last_time = self._start
            self._in_guiding = True
            self._header = {}
            self._settling = False
            self._calibration = None
            return []

        if m := _ENDS.match(line):
            was_guiding = self._in_guiding
            self._in_guiding = False
            self._settling = False
            if not was_guiding:
                return []
            return [GuidingStopped(time=datetime.strptime(m.group(1), _TS), source=self.source)]

        if m := _CAL_BEGINS.match(line):
            self._in_guiding = False
            self._calibration = datetime.strptime(m.group(1), _TS)
            self._calibration_failed = False
            return []

        if self._calibration is not None:
            # The calibration section has no per-step times: report once, at its start time.
            if line.startswith("Calibration complete"):
                self._calibration = None
            elif line.startswith("INFO: STAR LOST during calibration") and not self._calibration_failed:
                self._calibration_failed = True
                status = line.split("Status=", 1)[-1].strip() if "Status=" in line else "star lost"
                return [CalibrationFailed(time=self._calibration, source=self.source, message=status)]
            if line.startswith("Calibration"):
                return []

        if not self._in_guiding or self._start is None:
            return []

        if line.startswith("Frame,Time"):
            return [self._guiding_started()]

        if line.startswith("INFO:"):
            return self._info(line)

        if line.startswith("ALERT:"):
            return [LogError(time=self._now(), source=self.source, message=line[6:].strip())]

        if line[0].isdigit():
            return self._frame(line)

        # Header line of the guiding section ("key = value, key = value").
        for key, value in _KV.findall(line):
            self._header[key.strip().lower()] = value.strip()
        return []

    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        assert self._last_time is not None
        return self._last_time

    def _guiding_started(self) -> GuidingStarted:
        h = self._header
        return GuidingStarted(
            time=self._now(),
            source=self.source,
            pier_side=h.get("pier side"),
            hour_angle_h=_num(h.get("hour angle")),
            dec_deg=_num(h.get("dec")),
            pixel_scale=_num(h.get("pixel scale")),
            max_ra_ms=_num(h.get("max ra duration")),
            max_dec_ms=_num(h.get("max dec duration")),
            x_angle=_num(h.get("xangle")),
            parity=h.get("parity"),
        )

    def _info(self, line: str) -> list[Event]:
        t = self._now()
        if "Settling started" in line:
            self._settling = True
        elif "Settling complete" in line:
            self._settling = False
            return [SettleCompleted(time=t, source=self.source)]
        elif "Settling failed" in line:
            self._settling = False
            return [SettleFailed(time=t, source=self.source, message="PHD2 settling failed")]
        elif "DITHER by" in line:
            return [Dither(time=t, source=self.source)]
        return []

    def _frame(self, line: str) -> list[Event]:
        cols = next(csv.reader([line]))
        if len(cols) < 18:
            return []
        try:
            t = self._start + timedelta(seconds=float(cols[1]))  # type: ignore[operator]
        except ValueError:
            return []
        self._last_time = t

        if cols[2] == "DROP":
            reason = cols[18] if len(cols) > 18 else "star lost"
            return [StarLost(time=t, source=self.source, reason=reason)]

        def f(i: int) -> float:
            try:
                return float(cols[i]) if cols[i] else 0.0
            except ValueError:
                return 0.0

        return [
            GuideFrame(
                time=t,
                source=self.source,
                frame=int(cols[0]),
                ra_px=f(5),
                dec_px=f(6),
                ra_pulse_ms=f(9),
                dec_pulse_ms=f(11),
                mass=f(15),
                snr=f(16),
                settling=self._settling,
            )
        ]

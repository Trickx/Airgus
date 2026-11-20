"""Parser for iOptron Commander serial logs (``iOptronCommander2017Log_YYYYMMDD.txt``).

Each line is ``YYYY-MM-DD HH:MM:SS.fff <text>``: a command sent to the mount (``:GLS#``) or the mount's
reply. The reply to ``:GLS#`` carries the system status as its 19th character (iOptron RS-232
command language): 0 stopped (not at zero), 1 tracking PEC off, 2 slewing, 3 auto-guiding,
4 meridian flipping, 5 tracking PEC on, 6 parked, 7 stopped at zero (home).

This is what the mount itself reports, so it shows tracking stopped even when the imaging software
does not log it. Only changes between tracking and not tracking are reported; slewing and
flipping keep the previous state.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..events import Event, Source, TrackingChanged

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)? (.*)$")
_STATUS_REPLY = re.compile(r"^[+-]\d{22}#$")
_TRACKING = {"1": True, "3": True, "5": True, "0": False, "6": False, "7": False}


class IoptronParser:
    source = Source.IOPTRON

    def __init__(self) -> None:
        self._last_command: str | None = None
        self._tracking: bool | None = None

    def feed(self, line: str) -> list[Event]:
        m = _LINE.match(line.rstrip("\r\n"))
        if not m:
            return []
        text = m.group(2).strip()
        if text.startswith(":"):
            self._last_command = text
            return []
        command, self._last_command = self._last_command, None
        if command != ":GLS#" or not _STATUS_REPLY.match(text):
            return []
        tracking = _TRACKING.get(text[18])
        if tracking is None or tracking == self._tracking:
            return []
        self._tracking = tracking
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        return [TrackingChanged(time=t, source=self.source, on=tracking)]

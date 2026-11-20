"""Parser for ASIAIR autorun logs (Autorun_Log_*.txt, English variant)."""

from __future__ import annotations

import re
from datetime import datetime

from ..events import (
    AutoCenterResult,
    AutofocusCompleted,
    FocuserPosition,
    Event,
    ExposureStarted,
    GuideStarSelectFailed,
    MeridianFlipCompleted,
    MeridianFlipFailed,
    MeridianFlipStarted,
    PlateSolved,
    SequenceEnded,
    SequenceStarted,
    SettleFailed,
    SlewFailed,
    Source,
    StarLost,
    TrackingChanged,
)

_LINE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
_SEQ_BEGIN = re.compile(r"^\[Autorun\|Begin\] (.*?) Start$")
_SEQ_END = re.compile(r"^\[Autorun\|End\] (.*)$")
_EXPOSURE = re.compile(r"^Exposure ([\d.]+)s image (\d+)#")
_CENTER_END = re.compile(r"^\[AutoCenter\|End\] (.*)$")
_DISTANCE = re.compile(r"distance = [\d.]+%\(([\d.]+)°?\)")
_SOLVED = re.compile(r"^Solve succeeded: .*Angle = (-?[\d.]+)")
_FLIP_END = re.compile(r"^\[Meridian Flip\|End\] (.*)$")
_AF_END = re.compile(r"^\[AutoFocus\|End\] (.*)$")
_EAF_POS = re.compile(r"EAF position (\d+)")
_EAF_FOCUSED = re.compile(r"focused position is (\d+)")


class AsiairAutorunParser:
    source = Source.ASIAIR

    def feed(self, line: str) -> list[Event]:
        m = _LINE.match(line.rstrip("\r\n"))
        if not m:
            return []
        t = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
        msg = m.group(2).strip()
        s = self.source

        if mm := _SEQ_BEGIN.match(msg):
            return [SequenceStarted(time=t, source=s, target=mm.group(1))]
        if mm := _SEQ_END.match(msg):
            return [SequenceEnded(time=t, source=s, reason=mm.group(1))]
        if mm := _EXPOSURE.match(msg):
            return [ExposureStarted(time=t, source=s, index=int(mm.group(2)), duration_s=float(mm.group(1)))]
        if mm := _CENTER_END.match(msg):
            text = mm.group(1)
            if "centered" in text:
                return [AutoCenterResult(time=t, source=s, centered=True, distance_deg=None, message=text)]
            if "slews failed" in text:
                return [SlewFailed(time=t, source=s, message=text)]
            if "Too far" in text:
                d = _DISTANCE.search(text)
                return [AutoCenterResult(time=t, source=s, centered=False,
                                         distance_deg=float(d.group(1)) if d else None, message=text)]
            return []
        if mm := _SOLVED.match(msg):
            return [PlateSolved(time=t, source=s, angle_deg=float(mm.group(1)))]
        if msg.startswith("[Meridian Flip|Begin]"):
            return [MeridianFlipStarted(time=t, source=s)]
        if mm := _FLIP_END.match(msg):
            text = mm.group(1)
            if "fail" in text.lower() or "cancel" in text.lower():
                return [MeridianFlipFailed(time=t, source=s, message=text)]
            return [MeridianFlipCompleted(time=t, source=s)]
        if mm := _AF_END.match(msg):
            text = mm.group(1)
            return [AutofocusCompleted(time=t, source=s, success="succeeded" in text, message=text)]
        if mm := _EAF_FOCUSED.search(msg):
            return [FocuserPosition(time=t, source=s, position=int(mm.group(1)), final=True)]
        if mm := _EAF_POS.search(msg):
            return [FocuserPosition(time=t, source=s, position=int(mm.group(1)))]
        if msg in ("Start Tracking", "Stop Tracking"):
            return [TrackingChanged(time=t, source=s, on=msg.startswith("Start"))]
        if msg == "[Guide] Settle Timeout":
            return [SettleFailed(time=t, source=s, message="ASIAIR settle timeout")]
        if msg == "[Guide] Guide star lost":
            return [StarLost(time=t, source=s, reason="ASIAIR: guide star lost")]
        if msg.startswith("[Guide] Select Guide Star failed"):
            return [GuideStarSelectFailed(time=t, source=s, message=msg[8:])]
        return []

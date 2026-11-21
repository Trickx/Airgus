"""Session timeline for the web UI: which part of the rig was active when.

Lanes: exposures (ticks), the imaging software's log activity per source (ASIAIR, NINA), guiding
(PHD2 frames), mount tracking; plus periods in which capture stalled (`capture-stalled` alert) and
markers for pushed alerts. Activity is kept in one-minute buckets, so a whole night stays small.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .alerts import Alert, Severity
from .events import (
    ControllerEvent,
    Event,
    ExposureProgress,
    ExposureStarted,
    GuideFrame,
    ImageSaved,
    MountStatus,
    Source,
    TrackingChanged,
)

BUCKET = timedelta(minutes=1)
LOG_GAP = timedelta(minutes=10)    # longer silences of a log are shown as gaps
GUIDE_GAP = timedelta(minutes=2)
SOURCE_LANES = {Source.ASIAIR: "ASIAIR", Source.NINA: "NINA"}


def _bucket(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _segments(buckets: set[datetime], gap: timedelta) -> list[list[str]]:
    out: list[list[datetime]] = []
    for b in sorted(buckets):
        if out and b - out[-1][1] <= gap:
            out[-1][1] = b + BUCKET
        else:
            out.append([b, b + BUCKET])
    return [[s.isoformat(), e.isoformat()] for s, e in out]


class Timeline:
    def __init__(self) -> None:
        self.start: datetime | None = None
        self.end: datetime | None = None
        self.exposures: list[datetime] = []
        self.activity: dict[str, set[datetime]] = {}
        self.guiding: set[datetime] = set()
        self.tracking: list[tuple[datetime, bool]] = []
        self.alerts: list[Alert] = []
        self._log_exposure: datetime | None = None

    def add(self, ev: Event) -> None:
        t = ev.time
        self.start = t if self.start is None or t < self.start else self.start
        self.end = t if self.end is None or t > self.end else self.end
        if isinstance(ev, GuideFrame):
            self.guiding.add(_bucket(t))
            return
        lane = SOURCE_LANES.get(ev.source)
        if lane and not isinstance(ev, ControllerEvent):  # log lines, not the live status reports
            self.activity.setdefault(lane, set()).add(_bucket(t))
        if isinstance(ev, ExposureStarted):
            self._log_exposure = t
        if isinstance(ev, ImageSaved) and self._log_exposure and t - self._log_exposure < timedelta(minutes=15):
            return  # the log already marks this exposure at its start
        if (isinstance(ev, (ExposureStarted, ImageSaved))
                or (isinstance(ev, ExposureProgress) and ev.state == "start")):
            # live and log report the same exposure: keep one
            if not self.exposures or abs(t - self.exposures[-1]) > timedelta(seconds=30):
                self.exposures.append(t)
        tracking = ev.on if isinstance(ev, TrackingChanged) else ev.tracking if isinstance(ev, MountStatus) else None
        if tracking is not None and (not self.tracking or self.tracking[-1][1] != tracking):
            self.tracking.append((t, tracking))

    def add_alert(self, alert: Alert) -> None:
        if alert.severity >= Severity.WARNING or alert.rule == "capture-stalled":
            self.alerts.append(alert)

    def _stalls(self) -> list[list[str]]:
        """From the last exposure before a capture-stalled alert to the next one after it."""
        out = []
        for a in self.alerts:
            if a.rule != "capture-stalled" or a.severity < Severity.HIGH:
                continue
            before = [x for x in self.exposures if x <= a.time]
            after = [x for x in self.exposures if x > a.time]
            start = before[-1] if before else a.time
            end = after[0] if after else self.end or a.time
            out.append([start.isoformat(), end.isoformat()])
        return out

    def _tracking(self) -> list[list]:
        out = []
        for i, (t, on) in enumerate(self.tracking):
            end = self.tracking[i + 1][0] if i + 1 < len(self.tracking) else self.end or t
            out.append([t.isoformat(), end.isoformat(), "on" if on else "off"])
        return out

    def to_dict(self) -> dict:
        lanes = [{"name": f"{name}-Log", "segments": _segments(b, LOG_GAP)} for name, b in sorted(self.activity.items())]
        if self.guiding:
            lanes.append({"name": "PHD2-Guiding", "segments": _segments(self.guiding, GUIDE_GAP)})
        if self.tracking:
            lanes.append({"name": "Nachführung", "segments": self._tracking()})
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "exposures": [t.isoformat() for t in self.exposures],
            "lanes": lanes,
            "stalls": self._stalls(),
            "alerts": [{"time": a.time.isoformat(), "severity": a.severity.name, "title": a.title}
                       for a in self.alerts if a.severity >= Severity.WARNING],
        }

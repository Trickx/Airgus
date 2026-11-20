"""Detection rules. They only see neutral domain events and the rig state.

Thresholds are constructor arguments so they can be set from the config file.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable

from .alerts import Alert, Severity
from .events import (
    AutoCenterResult,
    ControllerDisconnected,
    ControllerHealth,
    DeviceStatus,
    Event,
    ExposureProgress,
    ExposureStarted,
    FocuserOpened,
    FocuserStatus,
    GuideFrame,
    GuideStarSelectFailed,
    GuiderDisconnected,
    GuidingStarted,
    GuidingStopped,
    MainCameraChanged,
    MainCameraStatus,
    MountMotion,
    MeridianFlipCompleted,
    MeridianFlipFailed,
    MeridianFlipStarted,
    SequenceEnded,
    SettleFailed,
    SolveProgress,
    Source,
    StarLost,
    WifiSignal,
)
from .state import RigState


class Rule:
    name = "rule"
    cooldown = timedelta(minutes=15)

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        return ()

    def on_tick(self, now: datetime, state: RigState) -> Iterable[Alert]:
        return ()

    def alert(self, time: datetime, severity: Severity, title: str, message: str, key: str | None = None) -> Alert:
        return Alert(time=time, rule=self.name, severity=severity, title=title, message=message,
                     key=key or self.name)


def _where(state: RigState, now: datetime) -> str:
    parts = []
    if state.target:
        parts.append(state.target)
    if state.exposure_index is not None:
        parts.append(f"Bild #{state.exposure_index}")
    if state.pier_side:
        parts.append(f"Pier {state.pier_side}")
    ha = state.hour_angle(now)
    if ha is not None:
        parts.append(f"HA {ha:+.1f} h")
    return ", ".join(parts)


class RunawayRule(Rule):
    """Guide star keeps running away although corrections are at their limit.

    This is the signature of a mechanical problem (collision, cable drag, clutch slip)
    or of a guide-rate/calibration problem - not of seeing.
    """

    name = "runaway"

    def __init__(self, threshold_arcsec: float = 30.0, min_duration_s: float = 30.0,
                 emergency_after_s: float = 120.0, saturation: float = 0.9) -> None:
        self.threshold = threshold_arcsec
        self.min_duration = min_duration_s
        self.emergency_after = emergency_after_s
        self.saturation = saturation
        self._since: datetime | None = None
        self._level = Severity.INFO
        self._peak = 0.0

    def _reset(self) -> None:
        self._since = None
        self._level = Severity.INFO
        self._peak = 0.0

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, (GuidingStarted, GuidingStopped, StarLost)):
            self._reset()
            return
        if not isinstance(ev, GuideFrame):
            return
        gs = state.guiding_session
        dist = math.hypot(ev.ra_px, ev.dec_px) * state.pixel_scale
        if dist <= self.threshold:
            self._reset()
            return
        max_ra = gs.max_ra_ms if gs and gs.max_ra_ms else None
        max_dec = gs.max_dec_ms if gs and gs.max_dec_ms else None
        saturated = ev.ra_limited or ev.dec_limited or \
            (max_ra is not None and ev.ra_pulse_ms >= self.saturation * max_ra) or \
            (max_dec is not None and ev.dec_pulse_ms >= self.saturation * max_dec)
        if self._since is None:
            if not saturated:
                return
            self._since = ev.time
        self._peak = max(self._peak, dist)
        duration = (ev.time - self._since).total_seconds()

        if duration >= self.emergency_after and self._level < Severity.EMERGENCY:
            self._level = Severity.EMERGENCY
            yield self.alert(ev.time, Severity.EMERGENCY, "Leitstern läuft davon",
                             f"Abweichung {dist:.0f}\" seit {duration:.0f} s trotz maximaler Korrektur "
                             f"(Spitze {self._peak:.0f}\"). Mechanik prüfen! {_where(state, ev.time)}",
                             key="runaway-emergency")
        elif duration >= self.min_duration and self._level < Severity.HIGH:
            self._level = Severity.HIGH
            yield self.alert(ev.time, Severity.HIGH, "Guiding-Abweichung wird nicht korrigiert",
                             f"Abweichung {dist:.0f}\" seit {duration:.0f} s, Korrekturen am Limit. "
                             f"{_where(state, ev.time)}")


class StarLostRule(Rule):
    """Guide star lost: PHD2 drops lasting longer than a short glitch, or an ASIAIR star-lost message."""

    name = "star-lost"
    cooldown = timedelta(minutes=10)

    def __init__(self, min_lost_s: float = 15.0) -> None:
        self.min_lost = timedelta(seconds=min_lost_s)
        self._first_drop: datetime | None = None
        self._fired = False
        self._select_failures = 0

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, (GuideFrame, GuidingStarted)):
            self._first_drop = None
            self._fired = False
            self._select_failures = 0
        elif isinstance(ev, StarLost):
            if ev.source is Source.PHD2:
                if self._first_drop is None:
                    self._first_drop = ev.time
                lost = ev.time - self._first_drop
                if not self._fired and lost >= self.min_lost:
                    self._fired = True
                    yield self.alert(ev.time, Severity.HIGH, "Leitstern verloren",
                                     f"{ev.reason} (seit {lost.total_seconds():.0f} s). {_where(state, ev.time)}")
            else:
                yield self.alert(ev.time, Severity.HIGH, "Leitstern verloren",
                                 f"{ev.reason}. {_where(state, ev.time)}")
        elif isinstance(ev, GuideStarSelectFailed):
            self._select_failures += 1
            if self._select_failures == 3:
                yield self.alert(ev.time, Severity.HIGH, "Kein Leitstern gefunden",
                                 f"Sternauswahl {self._select_failures}x fehlgeschlagen (Wolken, Dämmerung, Tau?). "
                                 f"{_where(state, ev.time)}", key="star-select")


class SettleRule(Rule):
    """Repeated settle failures after dithering."""

    name = "settle"
    cooldown = timedelta(minutes=30)

    def __init__(self, count: int = 3, window_min: float = 60.0, merge_s: float = 30.0) -> None:
        self.count = count
        self.window = timedelta(minutes=window_min)
        self.merge = timedelta(seconds=merge_s)
        self._failures: deque[datetime] = deque()

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if not isinstance(ev, SettleFailed):
            return
        # ASIAIR and PHD2 both report the same failure - count it once.
        if self._failures and ev.time - self._failures[-1] < self.merge:
            return
        self._failures.append(ev.time)
        while self._failures and ev.time - self._failures[0] > self.window:
            self._failures.popleft()
        if len(self._failures) >= self.count:
            yield self.alert(ev.time, Severity.WARNING, "Guiding beruhigt sich nicht",
                             f"{len(self._failures)} Settle-Fehler in {self.window.total_seconds() / 60:.0f} min. "
                             f"{_where(state, ev.time)}")


class MeridianFlipRule(Rule):
    """Failed flips, endless auto-center loops and flips that did not change the pier side."""

    name = "meridian-flip"
    cooldown = timedelta(minutes=30)

    def __init__(self, max_center_attempts: int = 5) -> None:
        self.max_center_attempts = max_center_attempts
        self._center_failures = 0
        self._flip_pending = False
        self._side_before_flip: str | None = None

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, AutoCenterResult):
            if ev.centered:
                self._center_failures = 0
                return
            self._center_failures += 1
            if self._center_failures == self.max_center_attempts:
                dist = f", Abstand {ev.distance_deg:.2f}°" if ev.distance_deg is not None else ""
                ctx = " während des Meridian-Flips" if self._flip_pending else ""
                yield self.alert(ev.time, Severity.HIGH, "Auto-Center gelingt nicht",
                                 f"{self._center_failures} Versuche ohne Erfolg{ctx}{dist}. "
                                 f"{_where(state, ev.time)}", key="auto-center")
        elif isinstance(ev, MeridianFlipStarted):
            self._flip_pending = True
            self._side_before_flip = state.pier_side
        elif isinstance(ev, MeridianFlipFailed):
            yield self.alert(ev.time, Severity.HIGH, "Meridian-Flip fehlgeschlagen",
                             f"{ev.message}. {_where(state, ev.time)}", key="flip-failed")
        elif isinstance(ev, MeridianFlipCompleted):
            pass  # verified on the next GuidingStarted
        elif isinstance(ev, GuidingStarted) and self._flip_pending:
            self._flip_pending = False
            if ev.pier_side and self._side_before_flip and ev.pier_side == self._side_before_flip:
                yield self.alert(ev.time, Severity.HIGH, "Flip ohne Seitenwechsel",
                                 f"Nach dem Meridian-Flip steht die Montierung weiterhin auf Pier {ev.pier_side}. "
                                 f"{_where(state, ev.time)}", key="flip-no-side-change")


class PastMeridianRule(Rule):
    """Target is tracked far past the meridian on the side it was on before the meridian."""

    name = "past-meridian"
    cooldown = timedelta(hours=2)

    def __init__(self, limit_h: float = 1.0) -> None:
        self.limit = limit_h
        self._side_before_meridian: str | None = None
        self._fired_for_side: str | None = None

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, GuidingStarted):
            if ev.hour_angle_h is not None and ev.hour_angle_h < 0 and ev.pier_side:
                self._side_before_meridian = ev.pier_side
                self._fired_for_side = None
            return
        if not isinstance(ev, GuideFrame) or self._side_before_meridian is None:
            return
        ha = state.hour_angle(ev.time)
        if ha is None or ha < self.limit:
            return
        if state.pier_side == self._side_before_meridian and self._fired_for_side != state.pier_side:
            self._fired_for_side = state.pier_side
            yield self.alert(ev.time, Severity.HIGH, "Kein Meridian-Flip",
                             f"Ziel steht {ha:.1f} h hinter dem Meridian, Montierung noch auf Pier "
                             f"{state.pier_side}. Kollisionsgefahr! {_where(state, ev.time)}")


class RmsRule(Rule):
    """Rolling total RMS (RA+DEC) above a threshold.

    Single large outliers (mis-detections, satellites) and runaway excursions are
    excluded; the latter are reported by RunawayRule.
    """

    name = "rms"
    cooldown = timedelta(minutes=30)

    def __init__(self, threshold_arcsec: float = 2.5, window_min: float = 10.0, min_fill: float = 0.5,
                 outlier_arcsec: float = 30.0) -> None:
        self.threshold = threshold_arcsec
        self.outlier_sq = outlier_arcsec ** 2
        self.window = timedelta(minutes=window_min)
        self.min_fill = min_fill
        self._samples: deque[tuple[datetime, float]] = deque()
        self._sum = 0.0
        self._start: datetime | None = None

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, GuidingStarted):
            self._samples.clear()
            self._sum = 0.0
            self._start = ev.time
            return
        if not isinstance(ev, GuideFrame) or ev.settling or self._start is None:
            return
        sq = (ev.ra_px ** 2 + ev.dec_px ** 2) * state.pixel_scale ** 2
        if sq > self.outlier_sq:
            return
        self._samples.append((ev.time, sq))
        self._sum += sq
        while self._samples and ev.time - self._samples[0][0] > self.window:
            self._sum -= self._samples.popleft()[1]
        if ev.time - self._start < self.window * self.min_fill:
            return
        rms = math.sqrt(max(self._sum, 0.0) / len(self._samples))
        if rms > self.threshold:
            yield self.alert(ev.time, Severity.WARNING, "Guiding-RMS zu hoch",
                             f"Gesamt-RMS {rms:.2f}\" über {self.window.total_seconds() / 60:.0f} min "
                             f"(Grenze {self.threshold:.2f}\"). {_where(state, ev.time)}")


class SnrDropRule(Rule):
    """Guide-star SNR drops well below its value at the start of the guiding session."""

    name = "snr-drop"
    cooldown = timedelta(minutes=30)

    def __init__(self, ratio: float = 0.5, baseline_min: float = 5.0, window_min: float = 5.0) -> None:
        self.ratio = ratio
        self.baseline_window = timedelta(minutes=baseline_min)
        self.window = timedelta(minutes=window_min)
        self._start: datetime | None = None
        self._baseline_samples: list[float] = []
        self._baseline: float | None = None
        self._recent: deque[tuple[datetime, float]] = deque()

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, GuidingStarted):
            self._start = ev.time
            self._baseline_samples = []
            self._baseline = None
            self._recent.clear()
            return
        if not isinstance(ev, GuideFrame) or self._start is None:
            return
        if self._baseline is None:
            self._baseline_samples.append(ev.snr)
            if ev.time - self._start >= self.baseline_window:
                self._baseline = median(self._baseline_samples)
            return
        self._recent.append((ev.time, ev.snr))
        while self._recent and ev.time - self._recent[0][0] > self.window:
            self._recent.popleft()
        if ev.time - self._recent[0][0] < self.window * 0.8:
            return
        mean = sum(s for _, s in self._recent) / len(self._recent)
        if mean < self._baseline * self.ratio:
            yield self.alert(ev.time, Severity.WARNING, "Leitstern wird schwächer",
                             f"SNR {mean:.1f} statt {self._baseline:.1f} zu Beginn (Wolken, Tau, Dämmerung?). "
                             f"{_where(state, ev.time)}")


class StaleLogRule(Rule):
    """Sequence is running but no new log events arrive (ASIAIR hung, offline, share lost)."""

    name = "stale-log"
    cooldown = timedelta(minutes=30)

    def __init__(self, timeout_min: float = 10.0) -> None:
        self.timeout = timedelta(minutes=timeout_min)

    def on_tick(self, now: datetime, state: RigState) -> Iterable[Alert]:
        if state.sequence_active and state.last_event_time and now - state.last_event_time > self.timeout:
            mins = (now - state.last_event_time).total_seconds() / 60
            yield self.alert(now, Severity.HIGH, "Keine neuen Log-Einträge",
                             f"Seit {mins:.0f} min keine Aktivität, obwohl die Sequenz läuft.")


class TrackingOffRule(Rule):
    """Mount tracking stays off while a sequence runs (meridian flip stuck, tracking stopped by hand).

    The ASIAIR stops tracking right before a meridian flip; during a flip the longer grace applies.
    """

    name = "tracking-off"
    cooldown = timedelta(minutes=30)

    def __init__(self, max_off_min: float = 5.0, flip_grace_min: float = 10.0) -> None:
        self.max_off = timedelta(minutes=max_off_min)
        self.flip_grace = timedelta(minutes=flip_grace_min)
        self._flip_active = False

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, MeridianFlipStarted):
            self._flip_active = True
        elif isinstance(ev, (MeridianFlipCompleted, MeridianFlipFailed, SequenceEnded)):
            self._flip_active = False
        return ()

    def on_tick(self, now: datetime, state: RigState) -> Iterable[Alert]:
        if state.tracking is not False or not state.imaging or state.tracking_since is None:
            return
        off = now - state.tracking_since
        if off < (self.flip_grace if self._flip_active else self.max_off):
            return
        ctx = " (Meridian-Flip läuft)" if self._flip_active else ""
        yield self.alert(now, Severity.HIGH, "Nachführung aus",
                         f"Die Montierung führt seit {off.total_seconds() / 60:.0f} min nicht nach{ctx}, "
                         f"obwohl die Sequenz läuft. {_where(state, now)}")


class PlateSolveRule(Rule):
    """Plate solve failed (live connection): every failure is listed, repeated failures escalate.

    Failures of preview shots are informational only; during a sequence they are warnings.
    """

    name = "plate-solve"
    cooldown = timedelta(seconds=30)  # one entry per failure, not per state message

    def __init__(self, escalate_after: int = 3) -> None:
        self.escalate_after = escalate_after
        self._failures = 0

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if not isinstance(ev, SolveProgress):
            return
        if ev.state in ("complete", "success", "succeeded", "solved"):
            self._failures = 0
            return
        if ev.state != "fail":
            return
        self._failures += 1
        page = {"preview": "Vorschau", "autorun": "Sequenz"}.get(ev.page, ev.page or "unbekannt")
        stars = f", {ev.stars} Sterne erkannt" if ev.stars is not None else ""
        detail = f"{ev.error or 'fehlgeschlagen'}{stars} ({page})."
        if self._failures >= self.escalate_after:
            yield self.alert(ev.time, Severity.HIGH, "Plate Solve scheitert wiederholt",
                             f"{self._failures} Fehlschläge in Folge: {detail} Fokus, Wolken, Tau oder Bildfeld prüfen. "
                             f"{_where(state, ev.time)}".strip(), key="plate-solve-repeated")
        else:
            severity = Severity.INFO if ev.page == "preview" else Severity.WARNING
            yield self.alert(ev.time, severity, "Plate Solve fehlgeschlagen",
                             f"{detail} {_where(state, ev.time)}".strip(), key=f"plate-solve-{ev.page}")


class DeviceLostRule(Rule):
    """Guide camera or mount disconnected from the ASIAIR (USB cable, power dip) - live connection.

    High while a sequence runs, informational otherwise (e.g. reconnecting by hand in daylight).
    """

    name = "device-lost"
    cooldown = timedelta(seconds=30)
    DEVICES = (("guide_camera", "Guide-Kamera"), ("mount", "Montierung"))

    def __init__(self) -> None:
        self._last: DeviceStatus | None = None

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, GuiderDisconnected):
            self._last = None  # no answers now: unknown, not lost
            return
        if not isinstance(ev, DeviceStatus):
            return
        last, self._last = self._last, ev
        if last is None:
            return
        for attr, label in self.DEVICES:
            before, now = getattr(last, attr), getattr(ev, attr)
            if before and not now:
                severity = Severity.HIGH if state.imaging else Severity.INFO
                yield self.alert(ev.time, severity, f"{label} getrennt",
                                 f"{before} ist nicht mehr mit der ASIAIR verbunden (Kabel, Stromversorgung?). "
                                 f"{_where(state, ev.time)}".strip(), key=f"device-lost-{attr}")
            elif now and not before:
                yield self.alert(ev.time, Severity.INFO, f"{label} verbunden",
                                 f"{now} ist wieder mit der ASIAIR verbunden.", key=f"device-back-{attr}")


class MainCameraRule(Rule):
    """Main camera connection on the ASIAIR (cable, power, app) - live connection.

    Port 4700 "CameraStateChange" says only *that* the state changed, not to what: each change is
    listed (warning during a sequence). If no exposure starts within `grace_s` after a change during
    a sequence it becomes high. Real disconnects are reported when get_camera_state is answered
    (only from a recording of the app's traffic: the ASIAIR answers queries on port 4700 only to a verified client).
    """

    name = "main-camera"
    cooldown = timedelta(seconds=2)

    def __init__(self, grace_s: float = 180.0) -> None:
        self.grace = timedelta(seconds=grace_s)
        self._changed: datetime | None = None
        self._silent_alerted = False
        self._last: MainCameraStatus | None = None

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if isinstance(ev, ControllerDisconnected):
            self._changed, self._last = None, None  # no events now: unknown
        elif isinstance(ev, MainCameraChanged):
            self._changed, self._silent_alerted = ev.time, False
            text = ("Die ASIAIR meldet einen Statuswechsel der Hauptkamera: getrennt oder verbunden - "
                    "welches von beiden, sagt sie nicht.")
            if state.imaging:
                yield self.alert(ev.time, Severity.WARNING, "Hauptkamera: Statuswechsel",
                                 f"{text} Airgus prüft, ob die Belichtungen weiterlaufen. "
                                 f"{_where(state, ev.time)}".strip(), key="main-camera-change")
            else:
                yield self.alert(ev.time, Severity.INFO, "Hauptkamera: Statuswechsel", text,
                                 key="main-camera-change")
        elif isinstance(ev, MainCameraStatus):
            last, self._last = self._last, ev
            if last is None or last.connected == ev.connected:
                return
            if ev.connected:
                yield self.alert(ev.time, Severity.INFO, "Hauptkamera verbunden",
                                 f"{ev.name or 'Die Hauptkamera'} ist mit der ASIAIR verbunden.", key="main-camera-on")
            else:
                yield self.alert(ev.time, Severity.HIGH if state.imaging else Severity.INFO, "Hauptkamera getrennt",
                                 f"{last.name or 'Die Hauptkamera'} ist nicht mehr mit der ASIAIR verbunden "
                                 f"(Kabel, Stromversorgung?). {_where(state, ev.time)}".strip(), key="main-camera-off")
        elif isinstance(ev, (ExposureProgress, ExposureStarted)) and self._changed and ev.time >= self._changed:
            if self._silent_alerted:
                yield self.alert(ev.time, Severity.INFO, "Hauptkamera belichtet wieder",
                                 "Nach dem Statuswechsel laufen die Belichtungen wieder.", key="main-camera-back")
            self._changed, self._silent_alerted = None, False

    def on_tick(self, now: datetime, state: RigState) -> Iterable[Alert]:
        if self._changed is None or self._silent_alerted or not state.imaging:
            return
        if now - self._changed >= self.grace:
            self._silent_alerted = True
            minutes = (now - self._changed).total_seconds() / 60
            yield self.alert(now, Severity.HIGH, "Hauptkamera belichtet nicht",
                             f"Seit dem Statuswechsel der Hauptkamera vor {minutes:.0f} min ist keine Belichtung "
                             f"gestartet. Kamera, Kabel und Stromversorgung prüfen. {_where(state, now)}".strip(),
                             key="main-camera-silent")


class FocuserRule(Rule):
    """EAF connection on the ASIAIR - live connection.

    Port 4700 "OpenEAF" announces only the open, not the close: a reopen during a sequence means the
    focuser was gone in between, so the focus may have shifted. Real disconnects are reported when
    get_focuser_info is answered (only from a recording of the app's traffic: the ASIAIR answers queries on port 4700 only to a verified client).
    """

    name = "focuser"
    cooldown = timedelta(seconds=30)

    def __init__(self) -> None:
        self._last: FocuserStatus | None = None

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        pos = f" Letzte bekannte Position: {state.focus.position}." if state.focus else ""
        if isinstance(ev, ControllerDisconnected):
            self._last = None
        elif isinstance(ev, FocuserStatus):
            last, self._last = self._last, ev
            if last is None or last.connected == ev.connected:
                return
            if ev.connected:
                yield self.alert(ev.time, Severity.INFO, "EAF verbunden",
                                 f"Der Fokussierer (EAF) ist mit der ASIAIR verbunden, Position {ev.position}.",
                                 key="focuser-on")
            else:
                yield self.alert(ev.time, Severity.HIGH if state.imaging else Severity.INFO, "EAF getrennt",
                                 f"Der Fokussierer (EAF) ist nicht mehr mit der ASIAIR verbunden, letzte Position "
                                 f"{last.position}. {_where(state, ev.time)}".strip(), key="focuser-off")
        elif isinstance(ev, FocuserOpened):
            if state.imaging:
                yield self.alert(ev.time, Severity.WARNING, "EAF neu verbunden",
                                 "Der Fokussierer (EAF) wurde während der Sequenz neu geöffnet - war er getrennt "
                                 f"(Kabel, Stromversorgung)? Fokus prüfen.{pos} {_where(state, ev.time)}".strip(),
                                 key="focuser-open")
            else:
                yield self.alert(ev.time, Severity.INFO, "EAF geöffnet",
                                 f"Der Fokussierer (EAF) wurde an der ASIAIR geöffnet.{pos}", key="focuser-open")


class MountHomeRule(Rule):
    """Mount sent to its home position (port 4400 "ScopeHome") - live connection.

    Expected after a sequence; while a sequence runs it means the target is lost (warning).
    """

    name = "mount-home"
    cooldown = timedelta(minutes=1)

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if not isinstance(ev, MountMotion) or ev.kind != "Home":
            return
        if ev.state == "working" and state.imaging:
            yield self.alert(ev.time, Severity.WARNING, "Montierung fährt in Home-Position",
                             "Die Montierung fährt während der Sequenz in die Home-Position - das Ziel geht "
                             f"verloren. {_where(state, ev.time)}".strip(), key="mount-home-start")
        elif ev.state == "complete":
            yield self.alert(ev.time, Severity.INFO, "Montierung in Home-Position",
                             "Die Montierung hat die Home-Position erreicht.", key="mount-home-done")


class ControllerHealthRule(Rule):
    """ASIAIR reports undervoltage, overcurrent or overheating (live connection)."""

    name = "controller-health"
    cooldown = timedelta(minutes=30)

    def __init__(self, max_temp_c: float = 75.0) -> None:
        self.max_temp = max_temp_c

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if not isinstance(ev, ControllerHealth):
            return
        if ev.undervolt:
            yield self.alert(ev.time, Severity.HIGH, "ASIAIR: Unterspannung",
                             "Die ASIAIR meldet Unterspannung. Netzteil, Kabel und Last (Taukappen, Kühlung) "
                             "prüfen, sonst drohen Abstürze und Verbindungsabbrüche.", key="controller-undervolt")
        if ev.overcurrent:
            yield self.alert(ev.time, Severity.HIGH, "ASIAIR: Überstrom",
                             "Die ASIAIR meldet Überstrom an den Ausgängen.", key="controller-overcurrent")
        if ev.overtemp or (ev.temp_c is not None and ev.temp_c >= self.max_temp):
            temp = f" ({ev.temp_c:.0f} °C)" if ev.temp_c is not None else ""
            yield self.alert(ev.time, Severity.WARNING, "ASIAIR: zu heiß",
                             f"Die ASIAIR ist überhitzt{temp}.", key="controller-temp")


class WifiRule(Rule):
    """WLAN signal of the ASIAIR stays weak (live connection)."""

    name = "wifi"
    cooldown = timedelta(minutes=30)

    def __init__(self, min_dbm: float = -75.0, min_duration_s: float = 120.0) -> None:
        self.min_dbm = min_dbm
        self.min_duration = timedelta(seconds=min_duration_s)
        self._weak_since: datetime | None = None

    def on_event(self, ev: Event, state: RigState) -> Iterable[Alert]:
        if not isinstance(ev, WifiSignal):
            return
        if ev.dbm >= self.min_dbm:
            self._weak_since = None
            return
        self._weak_since = self._weak_since or ev.time
        if ev.time - self._weak_since >= self.min_duration:
            yield self.alert(ev.time, Severity.WARNING, "ASIAIR: schwaches WLAN",
                             f"Signal {ev.dbm:.0f} dBm seit {(ev.time - self._weak_since).total_seconds() / 60:.0f} min.")


class ControllerOfflineRule(Rule):
    """Live connection to the ASIAIR lost while a sequence runs (power, crash, WLAN)."""

    name = "controller-offline"
    cooldown = timedelta(minutes=30)

    def __init__(self, offline_s: float = 60.0) -> None:
        self.offline = timedelta(seconds=offline_s)

    def on_tick(self, now: datetime, state: RigState) -> Iterable[Alert]:
        c = state.controller
        if c.connected is False and c.disconnected_since and state.imaging \
                and now - c.disconnected_since >= self.offline:
            mins = (now - c.disconnected_since).total_seconds() / 60
            yield self.alert(now, Severity.HIGH, "ASIAIR nicht erreichbar",
                             f"Keine Verbindung zu {c.host} seit {mins:.0f} min, obwohl eine Sequenz läuft "
                             f"(Stromversorgung, Absturz, WLAN?).")


RULE_CLASSES: list[type[Rule]] = [RunawayRule, StarLostRule, SettleRule, MeridianFlipRule,
                                   PastMeridianRule, RmsRule, SnrDropRule, StaleLogRule, TrackingOffRule,
                                   PlateSolveRule, DeviceLostRule, MainCameraRule, FocuserRule, MountHomeRule,
                                   ControllerHealthRule, WifiRule, ControllerOfflineRule]


def rule_schema() -> list[dict]:
    """Rule names, descriptions and tunable parameters with defaults (for the web UI)."""
    import inspect

    out = []
    for cls in RULE_CLASSES:
        params = []
        for p in inspect.signature(cls.__init__).parameters.values():
            if p.name == "self" or p.default is inspect.Parameter.empty:
                continue
            params.append({"name": p.name, "default": p.default, "type": type(p.default).__name__})
        doc = (cls.__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
        out.append({"name": cls.name, "doc": doc, "params": params})
    return out


def default_rules(config: dict | None = None) -> list[Rule]:
    """Build all rules; `config` maps rule name -> keyword arguments."""
    config = config or {}
    rules = []
    for cls in RULE_CLASSES:
        opts = dict(config.get(cls.name, {}))
        if opts.pop("enabled", True) is False:
            continue
        rules.append(cls(**opts))
    return rules

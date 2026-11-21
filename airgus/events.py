"""System-neutral domain events.

Every adapter (ASIAIR, NINA, PHD2) translates its log lines into these events.
Rules only ever see these types, never raw log text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Source(str, Enum):
    PHD2 = "phd2"
    ASIAIR = "asiair"
    NINA = "nina"
    IOPTRON = "ioptron"


@dataclass(frozen=True, kw_only=True)
class Event:
    time: datetime
    source: Source
    live: bool = False  # from a live connection instead of a log file


# --- Session -----------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class SequenceStarted(Event):
    target: str


@dataclass(frozen=True, kw_only=True)
class SequenceEnded(Event):
    reason: str


@dataclass(frozen=True, kw_only=True)
class ExposureStarted(Event):
    index: int
    duration_s: float


@dataclass(frozen=True, kw_only=True)
class FramesStarted(Event):
    """ "Shooting 25 dark frames, ...": kind of frames the sequence takes now (light / flat / dark / bias)."""

    frame_type: str


@dataclass(frozen=True, kw_only=True)
class TrackingStartFailed(Event):
    """ASIAIR could not start tracking ("[Guide] Start Tracking failed")."""


@dataclass(frozen=True, kw_only=True)
class DeviceConnection(Event):
    """A device was connected or disconnected in the imaging software (NINA: mount, camera, focuser)."""

    device: str  # "mount" / "camera" / "focuser"
    connected: bool
    name: str | None = None


@dataclass(frozen=True, kw_only=True)
class ApplicationError(Event):
    """Unhandled exception in the imaging software; `fatal` = the application terminates."""

    message: str
    fatal: bool


@dataclass(frozen=True, kw_only=True)
class CalibrationFailed(Event):
    """Guider calibration lost its star (PHD2 "STAR LOST during calibration")."""

    message: str


# --- Mount -------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class AutoCenterResult(Event):
    centered: bool
    distance_deg: float | None
    message: str


@dataclass(frozen=True, kw_only=True)
class SlewFailed(Event):
    message: str


@dataclass(frozen=True, kw_only=True)
class PlateSolved(Event):
    angle_deg: float


@dataclass(frozen=True, kw_only=True)
class MeridianFlipStarted(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class MeridianFlipFailed(Event):
    message: str


@dataclass(frozen=True, kw_only=True)
class MeridianFlipCompleted(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class TrackingChanged(Event):
    """Mount tracking switched on or off (ASIAIR: "Start Tracking" / "Stop Tracking")."""

    on: bool


# --- Focus -------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class AutofocusCompleted(Event):
    success: bool
    message: str


@dataclass(frozen=True, kw_only=True)
class FocuserPosition(Event):
    """EAF position in steps, e.g. from the Autorun log during autofocus; `final` = focused position."""

    position: int
    final: bool = False


# --- Guiding -----------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class GuidingStarted(Event):
    pier_side: str | None = None
    hour_angle_h: float | None = None
    dec_deg: float | None = None
    pixel_scale: float | None = None  # arcsec per guide-camera pixel
    max_ra_ms: float | None = None
    max_dec_ms: float | None = None
    x_angle: float | None = None
    parity: str | None = None


@dataclass(frozen=True, kw_only=True)
class GuidingStopped(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class GuideFrame(Event):
    frame: int
    ra_px: float
    dec_px: float
    ra_pulse_ms: float
    dec_pulse_ms: float
    snr: float
    mass: float
    settling: bool
    ra_limited: bool = False  # correction capped at the max duration (live guider only)
    dec_limited: bool = False


@dataclass(frozen=True, kw_only=True)
class StarLost(Event):
    reason: str


@dataclass(frozen=True, kw_only=True)
class GuideStarSelectFailed(Event):
    message: str


@dataclass(frozen=True, kw_only=True)
class SettleCompleted(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class SettleFailed(Event):
    message: str


@dataclass(frozen=True, kw_only=True)
class Dither(Event):
    pass


# --- Controller (ASIAIR box / imaging PC) --------------------------------------
# Status reports from a live connection to the controller, not from log files.
# They do not count as log activity (see RigState.apply).

@dataclass(frozen=True, kw_only=True)
class ControllerEvent(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class ControllerConnected(ControllerEvent):
    host: str


@dataclass(frozen=True, kw_only=True)
class ControllerDisconnected(ControllerEvent):
    host: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class ControllerHealth(ControllerEvent):
    temp_c: float | None
    overtemp: bool
    undervolt: bool
    overcurrent: bool


@dataclass(frozen=True, kw_only=True)
class WifiSignal(ControllerEvent):
    dbm: float


@dataclass(frozen=True, kw_only=True)
class DiskSpace(ControllerEvent):
    total_mb: float
    free_mb: float


@dataclass(frozen=True, kw_only=True)
class SequencePlan(ControllerEvent):
    name: str
    left_time_s: float | None
    left_size_mb: float | None
    meridian_flip: bool | None


@dataclass(frozen=True, kw_only=True)
class GuiderConnected(ControllerEvent):
    host: str


@dataclass(frozen=True, kw_only=True)
class GuiderDisconnected(ControllerEvent):
    host: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class GuiderInfo(ControllerEvent):
    """Guider state ("Guiding", "Stopped", "LostLock", ...) and/or pixel scale from the live guider."""

    app_state: str | None = None
    pixel_scale: float | None = None


@dataclass(frozen=True, kw_only=True)
class DeviceStatus(ControllerEvent):
    """Connected devices as reported by the guider (None = not connected)."""

    guide_camera: str | None
    mount: str | None


@dataclass(frozen=True, kw_only=True)
class MainCameraChanged(ControllerEvent):
    """Port 4700 "CameraStateChange": main camera closed or (re)opened. The event carries no state,
    and the state query is not answered to a second client while the app is connected."""


@dataclass(frozen=True, kw_only=True)
class MountStatus(ControllerEvent):
    """Mount state as answered on port 4400 (scope_get_ra_dec / _pierside / _track_state / _location).

    Each answer fills only its own fields; None = not part of this answer.
    """

    ra_h: float | None = None
    dec_deg: float | None = None
    lst_h: float | None = None       # local sidereal time of the mount
    pier_side: str | None = None     # "East" / "West" (ASCOM/PHD2 convention, same as INDI)
    tracking: bool | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, kw_only=True)
class MountMotion(ControllerEvent):
    """Mount movement event on port 4400, e.g. "ScopeHome" -> kind "Home", state working / complete."""

    kind: str
    state: str
    ra_h: float | None = None
    dec_deg: float | None = None


@dataclass(frozen=True, kw_only=True)
class NinaConnected(ControllerEvent):
    """Live connection to NINA's Advanced API (WebSocket) is up."""

    host: str
    version: str | None = None


@dataclass(frozen=True, kw_only=True)
class NinaDisconnected(ControllerEvent):
    host: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class ImageSaved(Event):
    """NINA saved an image (Advanced API "IMAGE-SAVE"), with its statistics."""

    frame_type: str            # light / flat / dark / bias / snapshot
    duration_s: float | None = None
    target: str | None = None
    filter: str | None = None
    hfr: float | None = None
    stars: int | None = None


@dataclass(frozen=True, kw_only=True)
class SequenceStepFailed(Event):
    """A sequence instruction failed (NINA "SEQUENCE-ENTITY-FAILED")."""

    step: str
    error: str


@dataclass(frozen=True, kw_only=True)
class MainCameraStatus(ControllerEvent):
    """Answer to get_camera_state on port 4700 (only answered to a verified client, i.e. the ASIAIR app; seen in recordings)."""

    connected: bool
    name: str | None = None


@dataclass(frozen=True, kw_only=True)
class FocuserStatus(ControllerEvent):
    """Answer to get_focuser_info on port 4700 (only answered to a verified client, i.e. the ASIAIR app; seen in recordings)."""

    connected: bool
    position: int | None = None
    temp_c: float | None = None


@dataclass(frozen=True, kw_only=True)
class FocuserOpened(ControllerEvent):
    """Port 4700 "OpenEAF": the EAF was opened (connected) on the ASIAIR; closing sends nothing."""


@dataclass(frozen=True, kw_only=True)
class ExposureProgress(ControllerEvent):
    """Main-camera exposure: state start / downloading / complete (...); page e.g. "preview"."""

    page: str
    state: str
    duration_s: float | None = None


@dataclass(frozen=True, kw_only=True)
class SolveProgress(ControllerEvent):
    """Plate solve: state start / solving / fail / complete (...)."""

    page: str
    state: str
    stars: int | None = None
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class ControllerActivity(ControllerEvent):
    """Any other controller event, e.g. kind "Sequence" / "AutoExp" with state "start"."""

    kind: str
    state: str


# --- Generic -----------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class LogError(Event):
    message: str

"""Current state of one rig, derived from the event stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .events import (
    ControllerActivity,
    ControllerConnected,
    ControllerDisconnected,
    ControllerEvent,
    ControllerHealth,
    DeviceStatus,
    DiskSpace,
    Event,
    ExposureProgress,
    MainCameraChanged,
    FocuserOpened,
    FramesStarted,
    FocuserStatus,
    MainCameraStatus,
    MountMotion,
    MountStatus,
    FocuserPosition,
    GuiderConnected,
    GuiderDisconnected,
    GuiderInfo,
    ExposureStarted,
    GuideFrame,
    GuidingStarted,
    GuidingStopped,
    SequenceEnded,
    SequencePlan,
    SequenceStarted,
    SolveProgress,
    TrackingChanged,
    WifiSignal,
)

SIDEREAL_HOURS_PER_HOUR = 1.0027379
RUNNING_STATES = {"start", "started", "running", "resume", "working"}


@dataclass
class ControllerState:
    """What a live connection to the controller (ASIAIR port 4700) reports."""

    host: str | None = None
    connected: bool | None = None  # None = no live connection configured / seen
    disconnected_since: datetime | None = None
    last_seen: datetime | None = None
    temp_c: float | None = None
    overtemp: bool = False
    undervolt: bool = False
    overcurrent: bool = False
    wifi_dbm: float | None = None
    disk_total_mb: float | None = None
    disk_free_mb: float | None = None
    plan: SequencePlan | None = None
    sequence_running: bool = False
    guider_connected: bool | None = None
    guider_state: str | None = None
    guider_pixel_scale: float | None = None
    devices_known: bool = False  # get_connected answered since the guider (re)connected
    guide_camera: str | None = None
    mount: str | None = None
    exposure: dict | None = None  # last main-camera exposure
    main_camera_changed: datetime | None = None  # last CameraStateChange
    main_camera_exposed: bool = False  # an exposure started after that change
    # Facts only: None = unknown. Set by a get_camera_state answer or by an exposure (proves connected);
    # a CameraStateChange makes it unknown again, since that event does not say in which direction.
    main_camera_connected: bool | None = None
    main_camera_name: str | None = None
    main_camera_fact: datetime | None = None
    main_camera_source: str | None = None  # "Abfrage" (get_camera_state) or "Belichtung"
    focuser_connected: bool | None = None
    focuser_position: int | None = None
    focuser_temp_c: float | None = None
    focuser_fact: datetime | None = None
    focuser_opened: datetime | None = None  # last OpenEAF
    # Mount, answered by the ASIAIR on port 4400 (each value with the time it was reported).
    mount_ra_h: float | None = None
    mount_dec: float | None = None
    mount_lst_h: float | None = None
    mount_pos_time: datetime | None = None
    mount_pier: str | None = None
    mount_tracking: bool | None = None
    site_lat: float | None = None
    site_lon: float | None = None
    mount_motion: dict | None = None  # last ScopeHome / ScopeGoto ...: kind, state, time
    solve: dict | None = None     # last plate solve

    def apply(self, ev: ControllerEvent) -> None:
        self.last_seen = ev.time
        if isinstance(ev, ControllerConnected):
            self.host, self.connected, self.disconnected_since = ev.host, True, None
        elif isinstance(ev, ControllerDisconnected):
            if self.connected is not False:
                self.disconnected_since = ev.time
            self.host, self.connected = ev.host, False
        elif isinstance(ev, GuiderConnected):
            self.guider_connected = True
        elif isinstance(ev, GuiderDisconnected):
            # last known values stay (the web UI greys them); rules treat the outage as unknown
            self.guider_connected = False
        elif isinstance(ev, DeviceStatus):
            self.devices_known = True
            self.guide_camera, self.mount = ev.guide_camera, ev.mount
        elif isinstance(ev, GuiderInfo):
            if ev.app_state is not None:
                self.guider_state = ev.app_state
            if ev.pixel_scale:
                self.guider_pixel_scale = ev.pixel_scale
        elif isinstance(ev, MountStatus):
            if ev.ra_h is not None and ev.dec_deg is not None and ev.lst_h is not None:
                self.mount_ra_h, self.mount_dec, self.mount_lst_h = ev.ra_h, ev.dec_deg, ev.lst_h
                self.mount_pos_time = ev.time
            if ev.pier_side:
                self.mount_pier = ev.pier_side
            if ev.tracking is not None:
                self.mount_tracking = ev.tracking
            if ev.latitude is not None:
                self.site_lat, self.site_lon = ev.latitude, ev.longitude
        elif isinstance(ev, MountMotion):
            self.mount_motion = {"kind": ev.kind, "state": ev.state, "time": ev.time.isoformat()}
            if ev.ra_h is not None and ev.dec_deg is not None and self.mount_lst_h is not None:
                # position during the move; sidereal time carried on from the last answer
                self.mount_lst_h = self.live_lst(ev.time)
                self.mount_ra_h, self.mount_dec, self.mount_pos_time = ev.ra_h, ev.dec_deg, ev.time
        elif isinstance(ev, FocuserOpened):
            self.focuser_opened = ev.time
        elif isinstance(ev, MainCameraChanged):
            self.main_camera_changed, self.main_camera_exposed = ev.time, False
            self.main_camera_connected = None
        elif isinstance(ev, MainCameraStatus):
            self.main_camera_connected, self.main_camera_fact = ev.connected, ev.time
            self.main_camera_source = "Abfrage"
            if ev.name:
                self.main_camera_name = ev.name
        elif isinstance(ev, FocuserStatus):
            self.focuser_connected, self.focuser_fact = ev.connected, ev.time
            if ev.connected:
                self.focuser_position, self.focuser_temp_c = ev.position, ev.temp_c
        elif isinstance(ev, ExposureProgress):
            if self.main_camera_changed and ev.time >= self.main_camera_changed:
                self.main_camera_exposed = True
            self.main_camera_connected, self.main_camera_fact = True, ev.time
            self.main_camera_source = "Belichtung"
            if ev.state == "start" or self.exposure is None:
                self.exposure = {"page": ev.page, "duration_s": ev.duration_s, "started": ev.time.isoformat()}
            self.exposure.update(state=ev.state, updated=ev.time.isoformat())
            if ev.duration_s:
                self.exposure["duration_s"] = ev.duration_s
        elif isinstance(ev, SolveProgress):
            if ev.state == "start" or self.solve is None:
                self.solve = {"page": ev.page, "stars": None, "error": None, "started": ev.time.isoformat()}
            self.solve.update(state=ev.state, updated=ev.time.isoformat())
            if ev.stars is not None:
                self.solve["stars"] = ev.stars
            if ev.error:
                self.solve["error"] = ev.error
        elif isinstance(ev, ControllerHealth):
            self.temp_c, self.overtemp = ev.temp_c, ev.overtemp
            self.undervolt, self.overcurrent = ev.undervolt, ev.overcurrent
        elif isinstance(ev, WifiSignal):
            self.wifi_dbm = ev.dbm
        elif isinstance(ev, DiskSpace):
            self.disk_total_mb, self.disk_free_mb = ev.total_mb, ev.free_mb
        elif isinstance(ev, SequencePlan):
            self.plan = ev
        elif isinstance(ev, ControllerActivity) and ev.kind == "Sequence":
            self.sequence_running = ev.state.lower() in RUNNING_STATES

    def live_lst(self, now: datetime) -> float | None:
        if self.mount_lst_h is None or self.mount_pos_time is None:
            return None
        return (self.mount_lst_h + (now - self.mount_pos_time).total_seconds() / 3600 * SIDEREAL_HOURS_PER_HOUR) % 24

    def live_hour_angle(self) -> float | None:
        """Hour angle at the time of the last reported position (-12 .. 12 h)."""
        if self.mount_ra_h is None or self.mount_lst_h is None:
            return None
        return (self.mount_lst_h - self.mount_ra_h + 12) % 24 - 12

    def snapshot(self) -> dict:
        p = self.plan
        return {
            "host": self.host, "connected": self.connected,
            "temp_c": self.temp_c, "overtemp": self.overtemp, "undervolt": self.undervolt,
            "overcurrent": self.overcurrent, "wifi_dbm": self.wifi_dbm,
            "disk_total_mb": self.disk_total_mb, "disk_free_mb": self.disk_free_mb,
            "plan": p.name if p else None, "plan_left_s": p.left_time_s if p else None,
            "sequence_running": self.sequence_running,
            "guider_connected": self.guider_connected, "guider_state": self.guider_state,
            "exposure": self.exposure, "solve": self.solve,
            "devices_known": self.devices_known, "guide_camera": self.guide_camera, "mount": self.mount,
            "main_camera_changed": self.main_camera_changed.isoformat() if self.main_camera_changed else None,
            "main_camera_exposed": self.main_camera_exposed,
            "main_camera_connected": self.main_camera_connected, "main_camera_name": self.main_camera_name,
            "main_camera_source": self.main_camera_source,
            "main_camera_fact": self.main_camera_fact.isoformat() if self.main_camera_fact else None,
            "focuser_connected": self.focuser_connected, "focuser_position": self.focuser_position,
            "focuser_temp_c": self.focuser_temp_c,
            "focuser_fact": self.focuser_fact.isoformat() if self.focuser_fact else None,
            "focuser_opened": self.focuser_opened.isoformat() if self.focuser_opened else None,
            "mount_ra_h": self.mount_ra_h, "mount_dec": self.mount_dec, "mount_pier": self.mount_pier,
            "mount_hour_angle": self.live_hour_angle(), "mount_tracking": self.mount_tracking,
            "mount_pos_time": self.mount_pos_time.isoformat() if self.mount_pos_time else None,
            "mount_motion": self.mount_motion, "site_lat": self.site_lat, "site_lon": self.site_lon,
        }


@dataclass
class RigState:
    sequence_active: bool = False
    target: str | None = None
    exposure_index: int | None = None
    guiding: bool = False
    guiding_session: GuidingStarted | None = None
    pier_side: str | None = None
    last_event_time: datetime | None = None
    last_frame: GuideFrame | None = None
    tracking: bool | None = None  # None = unknown (no Start/Stop Tracking seen yet)
    tracking_since: datetime | None = None
    tracking_since_is_change: bool = True  # False: first seen in a poll answer, the switch itself was earlier
    focus: FocuserPosition | None = None  # last known EAF position
    frame_type: str | None = None  # light / flat / dark / bias, from "Shooting ... frames"
    sequence_started: datetime | None = None
    events_seen: int = 0
    controller: ControllerState = field(default_factory=ControllerState)
    _extra: dict = field(default_factory=dict)

    def apply(self, ev: Event) -> None:
        self.events_seen += 1
        if isinstance(ev, ControllerEvent):
            # Live status reports must not hide a log that stopped growing.
            self.controller.apply(ev)
            if isinstance(ev, MountStatus) and ev.tracking is not None and ev.tracking != self.tracking:
                self.tracking, self.tracking_since = ev.tracking, ev.time
                self.tracking_since_is_change = False
            return
        self.last_event_time = ev.time
        if isinstance(ev, SequenceStarted):
            self.sequence_active = True
            self.target = ev.target
            self.frame_type = None
            self.sequence_started = ev.time
        elif isinstance(ev, FramesStarted):
            self.frame_type = ev.frame_type
        elif isinstance(ev, SequenceEnded):
            self.sequence_active = False
        elif isinstance(ev, ExposureStarted):
            self.exposure_index = ev.index
        elif isinstance(ev, GuidingStarted):
            self.guiding = True
            self.guiding_session = ev
            if ev.pier_side:
                self.pier_side = ev.pier_side
        elif isinstance(ev, GuidingStopped):
            self.guiding = False
        elif isinstance(ev, GuideFrame):
            self.last_frame = ev
        elif isinstance(ev, FocuserPosition):
            self.focus = ev
        elif isinstance(ev, TrackingChanged):
            if ev.on != self.tracking:
                self.tracking_since, self.tracking_since_is_change = ev.time, True
            self.tracking = ev.on

    @property
    def pixel_scale(self) -> float:
        gs = self.guiding_session
        if gs and gs.pixel_scale:
            return gs.pixel_scale
        return self.controller.guider_pixel_scale or 1.0

    LIVE_POSITION_MAX_AGE = timedelta(minutes=2)

    def hour_angle(self, now: datetime) -> float | None:
        """Hour angle at `now`: from the live mount position if recent, else extrapolated from the
        last guiding header."""
        c = self.controller
        if c.mount_pos_time is not None and abs(now - c.mount_pos_time) <= self.LIVE_POSITION_MAX_AGE:
            ha = c.live_hour_angle()
            if c.mount_tracking is not False:  # tracking: the hour angle grows with sidereal time
                ha = (c.live_lst(now) - c.mount_ra_h + 12) % 24 - 12
            return ha
        gs = self.guiding_session
        if gs is None or gs.hour_angle_h is None:
            return None
        elapsed_h = (now - gs.time).total_seconds() / 3600.0
        return gs.hour_angle_h + elapsed_h * SIDEREAL_HOURS_PER_HOUR

    def snapshot(self) -> dict:
        lf = self.last_frame
        return {
            "sequence_active": self.sequence_active,
            "focus": None if self.focus is None else {"position": self.focus.position, "final": self.focus.final,
                                                       "time": self.focus.time.isoformat()},
            "target": self.target,
            "frame_type": self.frame_type,
            "exposure_index": self.exposure_index,
            "guiding": self.guiding,
            "pier_side": self.pier_side,
            "tracking": self.tracking,
            "tracking_since": self.tracking_since.isoformat() if self.tracking_since else None,
            "tracking_since_is_change": self.tracking_since_is_change,
            "dec": self.guiding_session.dec_deg if self.guiding_session else None,
            "last_event_time": self.last_event_time.isoformat() if self.last_event_time else None,
            "snr": lf.snr if lf else None,
            "controller": self.controller.snapshot(),
        }

    def pose(self, now: datetime | None) -> dict | None:
        """Telescope pose for the 3D view, with its source and time; None = nothing known.

        Live from the ASIAIR (exactly as reported, never extrapolated), otherwise the PHD2 guiding
        header (hour angle extrapolated with sidereal time, marked as estimate).
        """
        c = self.controller
        if c.mount_pos_time is not None:
            return {"ha": c.live_hour_angle(), "dec": c.mount_dec, "pier": c.mount_pier, "lat": c.site_lat,
                    "source": "live", "time": c.mount_pos_time.isoformat(),
                    "meridian_s": self.meridian_seconds(now)}
        gs = self.guiding_session
        if gs is None or gs.hour_angle_h is None or gs.dec_deg is None or now is None:
            return None
        return {"ha": self.hour_angle(now), "dec": gs.dec_deg, "pier": self.pier_side, "lat": None,
                "source": "phd2", "time": gs.time.isoformat()}

    def meridian_seconds(self, now: datetime | None) -> float | None:
        """Seconds until the target crosses the meridian (negative: already past), as the ASIAIR app's
        "Meridian" countdown: -HA in sidereal time. Only from a fresh live position while tracking;
        without tracking the pointing never reaches the meridian."""
        c = self.controller
        if now is None or c.mount_pos_time is None or c.mount_tracking is not True:
            return None
        if abs(now - c.mount_pos_time) > self.LIVE_POSITION_MAX_AGE:
            return None
        ha = self.hour_angle(now)
        return None if ha is None else -ha * 3600 / SIDEREAL_HOURS_PER_HOUR

    @property
    def imaging(self) -> bool:
        """A sequence runs according to the logs or the controller."""
        return self.sequence_active or self.controller.sequence_running

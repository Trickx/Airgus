"""Session timeline for the web UI."""

from datetime import datetime, timedelta

from airgus.alerts import Alert, Severity
from airgus.events import ExposureStarted, GuideFrame, MountStatus, SequenceStarted, Source, TrackingChanged
from airgus.timeline import Timeline

T0 = datetime(2026, 11, 15, 0, 0)


def frame(t):
    return GuideFrame(time=t, source=Source.PHD2, frame=0, ra_px=0, dec_pulse_ms=0, dec_px=0, ra_pulse_ms=0,
                      snr=30, mass=1000, settling=False)


def test_freeze_shows_as_gap_with_stall():
    tl = Timeline()
    tl.add(SequenceStarted(time=T0, source=Source.NINA, target="NGC 7635"))
    for i in range(10):  # an exposure every 5 min until 00:45
        tl.add(ExposureStarted(time=T0 + timedelta(minutes=5 * i), source=Source.NINA, index=i + 1, duration_s=300))
    for s in range(0, 3 * 3600, 60):  # guiding all the time
        tl.add(frame(T0 + timedelta(seconds=s)))
    tl.add(ExposureStarted(time=T0 + timedelta(hours=2, minutes=50), source=Source.NINA, index=11, duration_s=300))
    tl.add_alert(Alert(time=T0 + timedelta(minutes=60), rule="capture-stalled", severity=Severity.HIGH,
                       title="Aufnahme hängt", message="", key="capture-stalled"))
    d = tl.to_dict()
    nina = next(l for l in d["lanes"] if l["name"] == "NINA-Log")["segments"]
    assert nina == [["2026-11-15T00:00:00", "2026-11-15T00:46:00"], ["2026-11-15T02:50:00", "2026-11-15T02:51:00"]]
    guiding = next(l for l in d["lanes"] if l["name"] == "PHD2-Guiding")["segments"]
    assert guiding == [["2026-11-15T00:00:00", "2026-11-15T03:00:00"]]
    assert d["stalls"] == [["2026-11-15T00:45:00", "2026-11-15T02:50:00"]]
    assert [a["title"] for a in d["alerts"]] == ["Aufnahme hängt"] and len(d["exposures"]) == 11


def test_tracking_lane_and_live_duplicates():
    tl = Timeline()
    tl.add(TrackingChanged(time=T0, source=Source.IOPTRON, on=True))
    tl.add(MountStatus(time=T0 + timedelta(minutes=1), source=Source.ASIAIR, live=True, tracking=True))
    tl.add(TrackingChanged(time=T0 + timedelta(minutes=30), source=Source.IOPTRON, on=False))
    tl.add(ExposureStarted(time=T0 + timedelta(minutes=40), source=Source.ASIAIR, index=1, duration_s=300))
    tl.add(ExposureStarted(time=T0 + timedelta(minutes=40, seconds=5), source=Source.ASIAIR, index=1, duration_s=300))
    d = tl.to_dict()
    (lane,) = [l for l in d["lanes"] if l["name"] == "Nachführung"]
    assert [s[2] for s in lane["segments"]] == ["on", "off"]
    assert len(d["exposures"]) == 1

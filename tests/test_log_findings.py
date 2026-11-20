"""Cases found by scanning the ASIAIR logs of May - November 2026 (lines copied from the real logs)."""

from datetime import datetime, timedelta

from airgus.alerts import Severity
from airgus.engine import Engine
from airgus.events import (
    CalibrationFailed,
    ExposureStarted,
    FramesStarted,
    SettleFailed,
    Source,
    StarLost,
    TrackingStartFailed,
)
from airgus.parsers.asiair import AsiairAutorunParser
from airgus.parsers.phd2 import Phd2Parser
from airgus.rules import CalibrationRule, MeridianFlipRule, SettleRule, StarLostRule, TrackingOffRule


def feed(lines, parser=None):
    parser = parser or AsiairAutorunParser()
    return [ev for line in lines for ev in parser.feed(line)]


def run(rules, events, until=None):
    engine = Engine(rules)
    alerts = [a for ev in events for a in engine.process(ev)]
    if until:
        alerts += engine.tick(until)
    return alerts


def test_exposure_in_ms_and_us():
    evs = feed(["2026/05/23 08:03:22 Exposure 10.0ms image 1#",
                "2026/07/30 08:28:21 Exposure 1.0us image 2#",
                "2026/05/22 23:10:43 Exposure 180.0s image 3#"])
    assert [(e.index, e.duration_s) for e in evs if isinstance(e, ExposureStarted)] == \
        [(1, 0.01), (2, 1e-6), (3, 180.0)]


def test_no_tracking_alert_for_flats_darks_bias():
    # 2026-07-30 morning: darks after the night, tracking stopped on purpose
    lines = ["2026/07/30 08:28:20 [Autorun|Begin] Eastern Veil Start",
             "2026/07/30 08:28:20 Shooting 25 bias frames, exposure 1.0ms Bin1",
             "2026/07/30 08:28:21 Stop Tracking",
             "2026/07/30 08:29:31 Shooting 25 dark frames, exposure 300.0s Bin1",
             "2026/07/30 08:29:31 Exposure 300.0s image 1#"]
    evs = feed(lines)
    assert [e.frame_type for e in evs if isinstance(e, FramesStarted)] == ["bias", "dark"]
    assert run([TrackingOffRule()], evs, until=datetime(2026, 7, 30, 10, 0)) == []
    # the same with light frames is reported
    light = feed([lines[0], "2026/07/30 08:28:20 Shooting 25 light frames, exposure 300.0s Bin1", lines[2]])
    (alert,) = run([TrackingOffRule()], light, until=datetime(2026, 7, 30, 8, 40))
    assert alert.title == "Nachführung aus"


def test_start_tracking_failed():
    # 2026-10-23 18:52 at the start of IC 1805
    evs = feed(["2026/10/23 18:52:43 [Autorun|Begin] IC 1805 Start",
                "2026/10/23 18:52:43 Shooting 3 light frames, exposure 10.0s Bin1",
                "2026/10/23 18:52:43 Start Tracking",
                "2026/10/23 18:52:43 [Guide] Start Tracking failed"])
    assert any(isinstance(e, TrackingStartFailed) for e in evs)
    (alert,) = run([TrackingOffRule()], evs)
    assert alert.title == "Nachführung startet nicht" and alert.severity is Severity.HIGH


def test_settle_failed_line():
    # 2026-07-08 23:26: settling failed, the sequence exposed anyway
    (ev,) = feed(["2026/07/08 23:26:37 [Guide] Settle failed"])
    assert isinstance(ev, SettleFailed)


def test_failures_after_manual_stop_are_dropped():
    # 2026-05-25 21:02: the user stopped autorun while centering during a flip
    lines = ["2026/05/25 21:00:00 [Autorun|Begin] M 51 Start",
             "2026/05/25 21:01:00 [Meridian Flip|Begin] Meridian Flip 1#",
             "2026/05/25 21:02:39 Stop Autorun Manually",
             "2026/05/25 21:02:40 [AutoCenter|End] Exposure failed",
             "2026/05/25 21:02:41 Meridian Flip 1# failed",
             "2026/05/25 21:02:41 [Meridian Flip|End] Cancel, Meridian Flip failed",
             "2026/05/25 21:02:41 [Guide] Settle failed",
             "2026/05/25 21:02:42 [Guide] Select Guide Star failed, no star found",
             "2026/05/25 21:02:43 [Autorun|End] Pause Autorun"]
    evs = feed(lines)
    names = [type(e).__name__ for e in evs]
    assert "MeridianFlipFailed" not in names and "SettleFailed" not in names
    assert "AutoCenterResult" not in names and "GuideStarSelectFailed" not in names
    assert run([MeridianFlipRule(), SettleRule()], evs) == []
    # a new sequence reports failures again
    (ev,) = feed(["2026/05/25 21:10:00 [Autorun|Begin] M 51 Start", "2026/05/25 21:11:00 [Guide] Settle failed"],
                 AsiairAutorunParser())[1:]
    assert isinstance(ev, SettleFailed)


def test_autocenter_exposure_failed_counts_as_failure():
    (ev,) = feed(["2026/05/25 21:02:40 [AutoCenter|End] Exposure failed"])
    assert ev.centered is False and ev.message == "Exposure failed"


def test_calibration_star_lost():
    # PHD2_GuideLog_2026-06-24: the calibration lost its star
    lines = ["Calibration Begins at 2026-06-24 23:47:13",
             "Calibration step = phdlab_placeholder, Max RA duration = 300, Max DEC duration = 300, DEC guide mode = Auto",
             "East,8,-4.375,25.532,679.311,454.420,25.904",
             "INFO: STAR LOST during calibration, Mass= 4145, SNR= 41.15, Error= 6, Status=Star lost - mass changed",
             "INFO: STAR LOST during calibration, Mass= 0, SNR= 0.00, Error= 2, Status=Star lost - low SNR"]
    evs = feed(lines, Phd2Parser())
    assert evs == [CalibrationFailed(time=datetime(2026, 6, 24, 23, 47, 13), source=Source.PHD2,
                                     message="Star lost - mass changed")]
    (alert,) = run([CalibrationRule()], evs)
    assert alert.title == "Kalibrierung fehlgeschlagen" and alert.severity is Severity.WARNING


def test_repeated_star_losses_are_bundled():
    # 2026-09-11: clouds, the ASIAIR reported a lost star every ~10 min
    t0 = datetime(2026, 9, 11, 2, 29)
    losses = [StarLost(time=t0 + timedelta(minutes=11 * i), source=Source.ASIAIR, reason="ASIAIR: guide star lost")
              for i in range(8)]
    alerts = run([StarLostRule()], losses)
    pushed = [a for a in alerts if a.severity >= Severity.WARNING]
    assert [a.title for a in pushed] == ["Leitstern verloren", "Leitstern verloren", "Leitstern immer wieder verloren"]
    assert len(alerts) == 8  # the others are still listed (info)
    # after a quiet hour a single loss is pushed normally again
    later = run([StarLostRule()], losses + [StarLost(time=t0 + timedelta(hours=4), source=Source.ASIAIR,
                                                     reason="ASIAIR: guide star lost")])
    assert later[-1].title == "Leitstern verloren" and later[-1].severity is Severity.HIGH


def test_tracking_off_counts_from_sequence_start():
    # 2026-05-25 09:20: test shots by day, tracking off since the end of the night (04:14)
    evs = feed(["2026/05/25 04:14:17 Stop Tracking",
                "2026/05/25 09:20:19 [Autorun|Begin] M 92 Start",
                "2026/05/25 09:20:19 Shooting 100 light frames, exposure 10.0s Bin1",
                "2026/05/25 09:20:30 Exposure 10.0s image 1#"])
    assert run([TrackingOffRule()], evs, until=datetime(2026, 5, 25, 9, 22)) == []
    (alert,) = run([TrackingOffRule()], evs, until=datetime(2026, 5, 25, 9, 26))
    assert "seit 6 min" in alert.message


def test_normal_flip_wait_is_not_tracking_off():
    # 2026-07-09 03:45: tracking stopped ~5 min before the flip, restarted in the same second it ended
    evs = feed(["2026/07/09 03:30:00 [Autorun|Begin] IC 1396 Start",
                "2026/07/09 03:30:00 Shooting 100 light frames, exposure 180.0s Bin1",
                "2026/07/09 03:30:01 Start Tracking",
                "2026/07/09 03:45:36 Stop Tracking",
                "2026/07/09 03:45:36 [Meridian Flip|Begin] Wait 4min55s to Meridian Flip",
                "2026/07/09 03:52:13 [Meridian Flip|End] Meridian Flip succeeded",
                "2026/07/09 03:52:13 Start Tracking"])
    assert run([TrackingOffRule()], evs, until=datetime(2026, 7, 9, 4, 0)) == []

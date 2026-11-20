"""NINA 3.x and iOptron Commander logs (lines copied from a real night, 2026-11-14/15)."""

from datetime import datetime

from airgus.alerts import Severity
from airgus.engine import Engine
from airgus.events import (
    ApplicationError,
    AutoCenterResult,
    DeviceConnection,
    ExposureStarted,
    FocuserPosition,
    FramesStarted,
    MeridianFlipCompleted,
    MeridianFlipStarted,
    SequenceEnded,
    SequenceStarted,
    SettleFailed,
    Source,
    TrackingChanged,
)
from airgus.parsers import IoptronParser, NinaParser, parser_for
from airgus.rules import ApplicationErrorRule, DeviceLostRule, TrackingOffRule

SEQ_START = ("2026-11-14T20:22:28.4300|INFO|SequenceItem.cs|Run|208|Starting Category: , Container: "
             "NINA.Sequencer.Container.SimpleDSOContainer, Strategy: SequentialStrategy, Items[CREATED]: 1, "
             "Target: NGC 7635 RA: 23:17:00; Dec: 60° 43' 25\"; Epoch: J2000 105.97999999999999")
LIGHT = ("2026-11-14T20:22:29.1000|INFO|SequenceItem.cs|Run|208|Starting Category: Kamera, Item: TakeExposure, "
         "ExposureTime 300, Gain 100, Offset 200, ImageType LIGHT, Binning 1x1")


def feed(lines, parser=None):
    parser = parser or NinaParser()
    return [ev for line in lines for ev in parser.feed(line)]


def test_file_names():
    assert isinstance(parser_for("20261114-154630-3.2.0.9001.10632-202611.log"), NinaParser)
    assert isinstance(parser_for("iOptronCommander2017Log_20261114.txt"), IoptronParser)
    assert parser_for("PHD2_DebugLog_2026-11-14_155932.txt") is None


def test_sequence_and_exposures():
    evs = feed([SEQ_START, LIGHT, LIGHT.replace("20:22:29", "20:27:30"),
                "2026-11-14T22:07:49.1000|INFO|Sequencer.cs|StartSequence|120|Sequence run was cancelled",
                "2026-11-14T22:07:49.1100|INFO|SimpleSequenceVM.cs|StartSequence|289|Simple Sequence finished"])
    assert isinstance(evs[0], SequenceStarted) and evs[0].target == "NGC 7635"
    assert isinstance(evs[1], FramesStarted) and evs[1].frame_type == "light"
    assert [(e.index, e.duration_s) for e in evs if isinstance(e, ExposureStarted)] == [(1, 300.0), (2, 300.0)]
    ends = [e for e in evs if isinstance(e, SequenceEnded)]
    assert len(ends) == 1 and ends[0].reason == "Sequenz abgebrochen"


def test_meridian_flip_and_tracking():
    evs = feed([
        "2026-11-14T19:56:52.5000|INFO|MeridianFlipVM.cs|DoMeridianFlip|128|Meridian Flip - Initializing Meridian Flip. ",
        "2026-11-14T19:56:53.0000|INFO|MeridianFlipVM.cs|PassMeridian|300|Meridian Flip - Stopping tracking to pass meridian",
        "2026-11-14T20:03:47.0000|INFO|MeridianFlipVM.cs|PassMeridian|320|Meridian Flip - Resuming tracking after passing meridian",
        "2026-11-14T20:09:09.0000|INFO|MeridianFlipVM.cs|DoMeridianFlip|180|Meridian Flip - Exiting meridian flip"])
    assert [type(e) for e in evs] == [MeridianFlipStarted, TrackingChanged, TrackingChanged, MeridianFlipCompleted]
    assert [e.on for e in evs if isinstance(e, TrackingChanged)] == [False, True]
    # 7 minutes without tracking during the flip are within its grace
    engine = Engine([TrackingOffRule()])
    alerts = [a for ev in feed([SEQ_START, LIGHT]) + evs for a in engine.process(ev)]
    assert alerts + engine.tick(datetime(2026, 11, 14, 20, 10)) == []


def test_errors():
    settle, center, crash = feed([
        "2026-11-14T19:51:52.4034|ERROR|PHD2Guider.cs|Run|500|PHD2 error:timed-out waiting for guider to settle",
        "2026-11-15T03:22:33.9315|ERROR|SequenceItem.cs|Run|230|Category: , Item: Center, Coordinates RA: 23:17:00; "
        "Dec: 60° 43' 25\"; Epoch: J2000 - ",
        "2026-11-15T19:57:52.3950|ERROR|App.xaml.cs|CurrentDomain_UnhandledException|300|An unhandled domain "
        "exception has occurred of type System.NullReferenceException. Application must terminate: True"])
    assert isinstance(settle, SettleFailed)
    assert isinstance(center, AutoCenterResult) and center.centered is False
    assert isinstance(crash, ApplicationError) and crash.fatal
    (alert,) = Engine([ApplicationErrorRule()]).process(crash)
    assert alert.title == "Aufnahmeprogramm abgestürzt" and alert.severity is Severity.WARNING  # no sequence


def test_failures_after_cancel_are_dropped():
    # 03:19 the user cancelled, then centered by hand: the failed center is not a sequence failure
    evs = feed([SEQ_START, "2026-11-15T03:19:05.4276|INFO|Sequencer.cs|StartSequence|120|Sequence run was cancelled",
                "2026-11-15T03:22:33.9315|ERROR|SequenceItem.cs|Run|230|Category: , Item: Center, Coordinates RA: "
                "23:17:00; Dec: 60° 43' 25\"; Epoch: J2000 - "])
    assert not any(isinstance(e, AutoCenterResult) for e in evs)


def test_focus_and_devices():
    evs = feed(["2026-11-14T21:20:58.0000|INFO|FocuserVM.cs|MoveFocuser|200|Moving Focuser to position 33398",
                "2026-11-14T21:21:22.0000|INFO|AutoFocusVM.cs|StartAutoFocus|400|AutoFocus completed",
                "2026-11-15T03:23:16.0000|INFO|TelescopeVM.cs|Disconnect|500|Disconnected mount",
                "2026-11-15T03:23:22.0000|INFO|TelescopeVM.cs|Connect|400|Successfully connected mount. Id: "
                "ASCOM.iOptron2017.Telescope Name: iOptron CEM120,70,40,26, GEM, HEM, HAE, HAZ series and SkyHunter Mount"])
    finals = [e for e in evs if isinstance(e, FocuserPosition) and e.final]
    assert finals and finals[0].position == 33398
    lost, back = [e for e in evs if isinstance(e, DeviceConnection)]
    assert (lost.device, lost.connected, back.connected) == ("mount", False, True)
    # outside a sequence closing NINA disconnects everything: not reported
    assert Engine([DeviceLostRule()]).process(lost) == []
    engine = Engine([DeviceLostRule()])
    for ev in feed([SEQ_START]):
        engine.process(ev)
    (alert,) = engine.process(lost)
    assert alert.title == "Montierung getrennt" and alert.severity is Severity.HIGH


def test_ioptron_status():
    p = IoptronParser()
    lines = ["2026-11-14 15:57:37.283 :GLS#", "2026-11-14 15:57:37.299 +0357192051253200070521#",  # home
             "2026-11-14 17:41:33.000 :GLS#", "2026-11-14 17:41:33.020 +0357192051253200010521#",  # tracking
             "2026-11-14 17:43:31.000 :GLS#", "2026-11-14 17:43:31.020 +0357192051253200030521#",  # guiding pulse
             "2026-11-14 19:56:54.000 :GLS#", "2026-11-14 19:56:54.020 +0357192051253200000521#",  # stopped
             "2026-11-14 19:56:55.000 :GUT#", "2026-11-14 19:56:55.020 +06000847940283000#",
             "2026-11-15 06:45:50.000 :GLS#", "2026-11-15 06:45:50.020 +0357192051253200060521#",  # parked
             "2026-11-15 03:25:26.023 ERROR: Received invalid response:"]
    evs = [e for line in lines for e in p.feed(line)]
    assert [(e.time.strftime("%H:%M"), e.on) for e in evs] == [("15:57", False), ("17:41", True), ("19:56", False)]
    assert all(e.source is Source.IOPTRON for e in evs)

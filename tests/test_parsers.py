from datetime import datetime

from airgus.events import (
    AutoCenterResult,
    GuideFrame,
    GuidingStarted,
    GuidingStopped,
    MeridianFlipFailed,
    MeridianFlipStarted,
    SettleFailed,
    StarLost,
)
from airgus.parsers import AsiairAutorunParser, Phd2Parser, parser_for

PHD2_SAMPLE = """\
PHD2 version, Log version 2.5. Log enabled at 2026-11-14 19:12:47

Guiding Begins at 2026-11-14 19:12:48
Pixel scale = 6.45 arc-sec/px, Binning = 1, Focal length = 120 mm
Mount = EQMod Mount,  connected, guiding enabled, xAngle = -136.0, xRate = 1.194, yAngle = -48.1, yRate = 1.180, parity = +/+,
Calibration step = phdlab_placeholder, Max RA duration = 300, Max DEC duration = 300, DEC guide mode = Auto
Dec = 34.5 deg, Hour angle = -6.83 hr, Pier side = West, Rotator pos = Unknown
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode
INFO: SETTLING STATE CHANGE, Settling started
1,0.604,"Mount",-0.216,-0.117,0.236,-0.075,0.106,0.000,89,W,0,,,,8072,54.84,0
INFO: SETTLING STATE CHANGE, Settling failed
2,1.119,"DROP",,,,,,,,,,,,,1035,21.65,6,"Star lost - mass changed"
Guiding Ends at 2026-11-14 19:14:58
"""


def feed(parser, text):
    out = []
    for line in text.splitlines():
        out += parser.feed(line)
    return out


def test_phd2_guiding_section():
    events = feed(Phd2Parser(), PHD2_SAMPLE)
    started = events[0]
    assert isinstance(started, GuidingStarted)
    assert started.pier_side == "West"
    assert started.hour_angle_h == -6.83
    assert started.pixel_scale == 6.45
    assert started.max_ra_ms == 300
    assert started.x_angle == -136.0
    assert started.parity == "+/+"

    frame = events[1]
    assert isinstance(frame, GuideFrame)
    assert frame.settling is True
    assert frame.ra_px == 0.236 and frame.ra_pulse_ms == 89 and frame.snr == 54.84
    assert frame.time == datetime(2026, 11, 14, 19, 12, 48, 604000)

    assert isinstance(events[2], SettleFailed)
    assert isinstance(events[3], StarLost) and events[3].reason == "Star lost - mass changed"
    assert isinstance(events[4], GuidingStopped)


def test_phd2_ignores_calibration_frames():
    text = "Calibration Begins at 2026-11-14 19:15:16\nWest,1,0.000,0.000,0.000,0.000\n"
    assert feed(Phd2Parser(), text) == []


def test_asiair_events():
    text = """\
2026/11/15 01:59:25 [Meridian Flip|Begin] Wait 1min48s to Meridian Flip
2026/11/15 02:01:32 [AutoCenter|End] Too far from center, distance = 31%(0.354235°)
2026/11/15 03:01:52 [Meridian Flip|End] Cancel, Meridian Flip failed
2026/11/15 03:03:24 [AutoCenter|End] The target is centered
2026/11/15 04:39:11 [Guide] Settle Timeout
2026/11/15 04:41:47 [Guide] Guide star lost
"""
    events = feed(AsiairAutorunParser(), text)
    assert [type(e) for e in events] == [
        MeridianFlipStarted, AutoCenterResult, MeridianFlipFailed, AutoCenterResult, SettleFailed, StarLost,
    ]
    assert events[1].centered is False and abs(events[1].distance_deg - 0.354235) < 1e-9
    assert events[3].centered is True


def test_parser_selection():
    assert isinstance(parser_for("PHD2_GuideLog_2026-11-14_191247.txt"), Phd2Parser)
    assert isinstance(parser_for("Autorun_Log_2026-11-14_182220.txt"), AsiairAutorunParser)
    assert parser_for("Autorun_Log_2026-11-14_182220_CHN.txt") is None
    assert parser_for("notes.txt") is None

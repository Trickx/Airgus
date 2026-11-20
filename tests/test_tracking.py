from datetime import datetime, timedelta

from airgus.engine import Engine
from airgus.events import (
    MeridianFlipCompleted,
    MeridianFlipStarted,
    SequenceEnded,
    SequenceStarted,
    Source,
    TrackingChanged,
)
from airgus.parsers import AsiairAutorunParser
from airgus.rules import TrackingOffRule

T0 = datetime(2026, 11, 20, 22, 0)


def ev(cls, minutes, **kw):
    return cls(time=T0 + timedelta(minutes=minutes), source=Source.ASIAIR, **kw)


def test_parser():
    p = AsiairAutorunParser()
    (on,) = p.feed("2026/11/14 19:12:44 Start Tracking")
    (off,) = p.feed("2026/11/15 01:59:25 Stop Tracking")
    assert on.on is True and off.on is False and off.time == datetime(2026, 11, 15, 1, 59, 25)


def test_state_shows_tracking():
    engine = Engine([])
    assert engine.state.snapshot()["tracking"] is None
    engine.process(ev(TrackingChanged, 0, on=True))
    engine.process(ev(TrackingChanged, 5, on=True))  # repeated start keeps the original time
    snap = engine.state.snapshot()
    assert snap["tracking"] is True and snap["tracking_since"] == T0.isoformat()


def test_alert_when_tracking_stays_off_during_sequence():
    engine = Engine([TrackingOffRule(max_off_min=5)])
    engine.process(ev(SequenceStarted, 0, target="IC 405"))
    engine.process(ev(TrackingChanged, 1, on=False))
    assert engine.tick(T0 + timedelta(minutes=5)) == []
    (alert,) = engine.tick(T0 + timedelta(minutes=6, seconds=1))
    assert "seit 5 min" in alert.message


def test_flip_gets_longer_grace():
    engine = Engine([TrackingOffRule(max_off_min=5, flip_grace_min=10)])
    engine.process(ev(SequenceStarted, 0, target="IC 405"))
    engine.process(ev(TrackingChanged, 1, on=False))
    engine.process(ev(MeridianFlipStarted, 1))
    assert engine.tick(T0 + timedelta(minutes=8)) == []
    (alert,) = engine.tick(T0 + timedelta(minutes=11))
    assert "Meridian-Flip" in alert.message
    # the grace ends when tracking runs again, not with the flip's end line: the ASIAIR writes
    # "Start Tracking" in the same second, after "Meridian Flip succeeded"
    engine.process(ev(MeridianFlipCompleted, 12))
    assert engine.rules[0]._flip_active is True
    engine.process(ev(TrackingChanged, 12, on=True))
    assert engine.rules[0]._flip_active is False


def test_no_alert_after_sequence_end_or_when_unknown():
    engine = Engine([TrackingOffRule(max_off_min=5)])
    engine.process(ev(SequenceStarted, 0, target="IC 405"))
    assert engine.tick(T0 + timedelta(minutes=30)) == []  # tracking never reported: unknown
    engine.process(ev(SequenceEnded, 31, reason="done"))
    engine.process(ev(TrackingChanged, 31, on=False))
    assert engine.tick(T0 + timedelta(hours=2)) == []

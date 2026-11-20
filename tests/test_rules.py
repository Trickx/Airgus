from datetime import datetime, timedelta

from airgus.alerts import Severity
from airgus.engine import Engine
from airgus.events import (
    AutoCenterResult,
    GuideFrame,
    GuidingStarted,
    MeridianFlipStarted,
    SequenceStarted,
    Source,
    StarLost,
)
from airgus.rules import MeridianFlipRule, RunawayRule, StaleLogRule, StarLostRule

T0 = datetime(2026, 11, 15, 4, 0, 0)
P = Source.PHD2


def guiding(t=T0, side="West", ha=-1.0):
    return GuidingStarted(time=t, source=P, pier_side=side, hour_angle_h=ha, pixel_scale=6.45,
                          max_ra_ms=300, max_dec_ms=300)


def frame(t, ra=0.1, dec=0.1, ra_ms=0, dec_ms=0, snr=30.0):
    return GuideFrame(time=t, source=P, frame=0, ra_px=ra, dec_px=dec, ra_pulse_ms=ra_ms,
                      dec_pulse_ms=dec_ms, snr=snr, mass=1000, settling=False)


def run(engine, events):
    out = []
    for ev in events:
        out += engine.process(ev)
    return out


def test_runaway_escalates_to_emergency():
    engine = Engine([RunawayRule()])
    events = [guiding()] + [frame(T0 + timedelta(seconds=s), ra=10 + s * 0.1, ra_ms=300) for s in range(1, 150)]
    alerts = run(engine, events)
    assert [a.severity for a in alerts] == [Severity.HIGH, Severity.EMERGENCY]


def test_runaway_ignores_large_error_without_saturated_pulses():
    engine = Engine([RunawayRule()])
    events = [guiding()] + [frame(T0 + timedelta(seconds=s), ra=10, ra_ms=50) for s in range(1, 150)]
    assert run(engine, events) == []


def test_short_star_loss_is_not_reported():
    engine = Engine([StarLostRule(min_lost_s=15)])
    events = [guiding()] + [StarLost(time=T0 + timedelta(seconds=s), source=P, reason="x") for s in range(1, 8)]
    events.append(frame(T0 + timedelta(seconds=9)))
    assert run(engine, events) == []


def test_long_star_loss_is_reported_once():
    engine = Engine([StarLostRule(min_lost_s=15)])
    events = [guiding()] + [StarLost(time=T0 + timedelta(seconds=s), source=P, reason="x") for s in range(1, 40)]
    assert len(run(engine, events)) == 1


def test_flip_without_side_change():
    engine = Engine([MeridianFlipRule()])
    events = [
        guiding(T0, "West"),
        MeridianFlipStarted(time=T0 + timedelta(minutes=1), source=Source.ASIAIR),
        guiding(T0 + timedelta(minutes=5), "West", ha=0.1),
    ]
    alerts = run(engine, events)
    assert [a.key for a in alerts] == ["flip-no-side-change"]


def test_auto_center_loop():
    engine = Engine([MeridianFlipRule(max_center_attempts=5)])
    events = [AutoCenterResult(time=T0 + timedelta(seconds=20 * i), source=Source.ASIAIR, centered=False,
                               distance_deg=0.3, message="Too far") for i in range(10)]
    alerts = run(engine, events)
    assert len(alerts) == 1 and alerts[0].key == "auto-center"


def test_stale_log():
    engine = Engine([StaleLogRule(timeout_min=10)])
    run(engine, [SequenceStarted(time=T0, source=Source.ASIAIR, target="IC 405")])
    assert engine.tick(T0 + timedelta(minutes=5)) == []
    assert len(engine.tick(T0 + timedelta(minutes=11))) == 1


def test_cooldown_deduplicates():
    engine = Engine([MeridianFlipRule(max_center_attempts=1)])
    events = [AutoCenterResult(time=T0 + timedelta(minutes=i), source=Source.ASIAIR, centered=i % 2 == 1,
                               distance_deg=0.3, message="") for i in range(6)]
    assert len(run(engine, events)) == 1

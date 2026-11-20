"""Regression test against the real night of 2026-11-14 (IC 405 / AE Aurigae).

Expected incidents, found by manual analysis of the logs:
- failed meridian flip at 02:01 (endless auto-center, mount stays on pier West)
- imaging continued far past the meridian
- guide star running away at ~04:35 with corrections at their limit
- guide star lost at dawn (06:39)
"""

from pathlib import Path

import pytest

from airgus.alerts import Severity
from airgus.engine import Engine
from airgus.rules import default_rules
from airgus.sources import replay

LOGS = Path(__file__).resolve().parent.parent / "logs" / "2026-11-14"

pytestmark = pytest.mark.skipif(not LOGS.is_dir(), reason="sample logs not present")


@pytest.fixture(scope="module")
def alerts():
    engine = Engine(default_rules())
    for ev in replay(LOGS):
        engine.process(ev)
    return engine.alerts


def first(alerts, key):
    return next(a for a in alerts if a.key == key)


def test_flip_problems_detected(alerts):
    assert first(alerts, "auto-center").time.strftime("%H:%M") == "02:02"
    assert first(alerts, "past-meridian").time.strftime("%H:%M") == "03:03"


def test_manual_stop_is_not_a_failed_flip(alerts):
    # 03:01:51 "Stop Autorun Manually": the cancelled center and flip that follow are the user's doing,
    # the stuck flip itself was already reported at 02:02 (endless auto-center).
    assert not [a for a in alerts if a.key in ("flip-failed", "flip-no-side-change")]


def test_runaway_detected(alerts):
    emergency = [a for a in alerts if a.severity is Severity.EMERGENCY]
    assert emergency and emergency[0].time.strftime("%H:%M") == "04:37"


def test_dawn_star_loss_detected(alerts):
    assert any(a.key == "star-lost" and a.time.strftime("%H:%M") == "06:39" for a in alerts)


def test_no_alerts_during_quiet_first_hours(alerts):
    assert not [a for a in alerts if a.time.hour in (19, 20, 21)]


def test_tracking_off_during_failed_flip(alerts):
    # "Stop Tracking" at 01:59:25 right before the flip, "Start Tracking" only at 03:01:52.
    off = [a for a in alerts if a.rule == "tracking-off"]
    assert off and "02:09" <= off[0].time.strftime("%H:%M") <= "02:11"
    assert all(a.time.strftime("%H:%M") < "03:02" for a in off)

"""Pipeline: events -> state -> rules -> de-duplicated alerts -> notifiers."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Protocol

from .alerts import Alert, Severity
from .events import (
    Dither,
    Event,
    GuideFrame,
    GuidingStopped,
    SettleCompleted,
    SettleFailed,
    Source,
    StarLost,
)
from .rules import Rule
from .state import RigState

log = logging.getLogger(__name__)

# Guide-log events that the live guider connection reports as well (and earlier).
LIVE_GUIDER_DUPLICATES = (GuideFrame, StarLost, SettleCompleted, SettleFailed, Dither, GuidingStopped)


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


class Engine:
    def __init__(self, rules: list[Rule], notifiers: Iterable[Notifier] = (),
                 min_severity: Severity = Severity.INFO) -> None:
        self.rules = rules
        self.notifiers = list(notifiers)
        self.min_severity = min_severity
        self.state = RigState()
        self.alerts: list[Alert] = []
        self._last_sent: dict[str, datetime] = {}

    def process(self, ev: Event) -> list[Alert]:
        # Detect silence between events before the new event updates the state.
        out = self.tick(ev.time)
        if self.is_duplicate(ev):
            return out
        self.state.apply(ev)
        for rule in self.rules:
            out += self._accept(rule, rule.on_event(ev, self.state))
        return out

    def is_duplicate(self, ev: Event) -> bool:
        """While the live guider is connected, the PHD2 log only repeats what it already sent.

        The log's guiding header (pier side, hour angle, pixel scale) is still used.
        """
        return (not ev.live and ev.source is Source.PHD2 and isinstance(ev, LIVE_GUIDER_DUPLICATES)
                and self.state.controller.guider_connected is True)

    def tick(self, now: datetime) -> list[Alert]:
        out: list[Alert] = []
        for rule in self.rules:
            out += self._accept(rule, rule.on_tick(now, self.state))
        return out

    def _accept(self, rule: Rule, alerts: Iterable[Alert]) -> list[Alert]:
        accepted = []
        for alert in alerts:
            last = self._last_sent.get(alert.key)
            if last is not None and alert.time - last < rule.cooldown:
                continue
            self._last_sent[alert.key] = alert.time
            self.alerts.append(alert)
            accepted.append(alert)
            if alert.severity >= self.min_severity:
                for n in self.notifiers:
                    try:
                        n.send(alert)
                    except Exception:  # a broken notifier must not stop monitoring
                        log.exception("notifier %s failed", type(n).__name__)
        return accepted

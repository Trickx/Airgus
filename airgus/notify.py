"""Notifiers. Pushover uses only the standard library."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from .alerts import Alert, Severity

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PRIORITY = {Severity.INFO: -1, Severity.WARNING: 0, Severity.HIGH: 1, Severity.EMERGENCY: 2}


class ConsoleNotifier:
    def send(self, alert: Alert) -> None:
        print(alert, flush=True)


class PushoverNotifier:
    def __init__(self, token: str, user: str, device: str | None = None,
                 retry_s: int = 60, expire_s: int = 1800, timeout_s: float = 10.0) -> None:
        self.token = token
        self.user = user
        self.device = device
        self.retry_s = retry_s
        self.expire_s = expire_s
        self.timeout_s = timeout_s

    def send(self, alert: Alert) -> None:
        data = {
            "token": self.token,
            "user": self.user,
            "title": f"Airgus: {alert.title}",
            "message": alert.message,
            "priority": PRIORITY[alert.severity],
            "timestamp": int(alert.time.timestamp()),
        }
        if self.device:
            data["device"] = self.device
        if alert.severity is Severity.EMERGENCY:
            data["retry"] = self.retry_s
            data["expire"] = self.expire_s
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(PUSHOVER_URL, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            result = json.loads(resp.read() or b"{}")
        if result.get("status") != 1:
            raise RuntimeError(f"Pushover rejected message: {result}")
        log.info("pushover sent: %s", alert.title)

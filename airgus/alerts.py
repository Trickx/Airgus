from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum


class Severity(IntEnum):
    INFO = 0
    WARNING = 1
    HIGH = 2
    EMERGENCY = 3


@dataclass(frozen=True)
class Alert:
    time: datetime
    rule: str
    severity: Severity
    title: str
    message: str
    key: str  # alerts with the same key are de-duplicated within the cooldown

    def __str__(self) -> str:
        return f"{self.time:%Y-%m-%d %H:%M:%S} [{self.severity.name:<9}] {self.title} - {self.message}"

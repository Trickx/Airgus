"""Configuration: optional TOML file, secrets from the environment.

Example airgus.toml:

    [pushover]
    min_severity = "WARNING"      # INFO, WARNING, HIGH, EMERGENCY

    [rules.runaway]
    threshold_arcsec = 30
    min_duration_s = 30

    [rules.snr-drop]
    enabled = false

Secrets: AIRGUS_PUSHOVER_TOKEN, AIRGUS_PUSHOVER_USER (optional AIRGUS_PUSHOVER_DEVICE).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .alerts import Severity


@dataclass
class Config:
    rules: dict = field(default_factory=dict)
    pushover_token: str | None = None
    pushover_user: str | None = None
    pushover_device: str | None = None
    pushover_min_severity: Severity = Severity.WARNING

    @property
    def pushover_enabled(self) -> bool:
        return bool(self.pushover_token and self.pushover_user)


def load(path: Path | None) -> Config:
    raw: dict = {}
    if path is not None:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    po = raw.get("pushover", {})
    return Config(
        rules=raw.get("rules", {}),
        pushover_token=os.environ.get("AIRGUS_PUSHOVER_TOKEN") or po.get("token"),
        pushover_user=os.environ.get("AIRGUS_PUSHOVER_USER") or po.get("user"),
        pushover_device=os.environ.get("AIRGUS_PUSHOVER_DEVICE") or po.get("device"),
        pushover_min_severity=Severity[str(po.get("min_severity", "WARNING")).upper()],
    )

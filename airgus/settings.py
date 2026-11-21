"""Settings edited in the web UI, persisted as JSON in the data directory.

Pushover secrets from the environment (AIRGUS_PUSHOVER_TOKEN / _USER / _DEVICE)
take precedence over stored values.
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path

from .alerts import Severity
from .rules import rule_schema

DEFAULTS: dict = {
    "pushover": {"token": "", "user": "", "device": "", "min_severity": "WARNING", "in_replay": False},
    "rules": {},
    "site": {"latitude": 51.0},
    "asiair": {"enabled": False, "host": "asiair.local", "port": 4700, "guider": True, "guider_port": 4400,
               "record": True},
    # Each entry: "<share>/<path>". The ASIAIR writes logs to the storage selected in the app.
    "nina": {"enabled": False, "host": "", "port": 1888, "record": True},
    "smb": {"server": "asiair.local", "shares": ["TF Images/ASIAIR/log", "EMMC Images/log"], "username": "",
            "password": "", "max_age_h": 12.0},
    "source": {"mode": "replay", "location": "local", "folder": "", "speed": 60.0, "interval_s": 5.0,
               "from_start": False},
}

_ENV = {"token": "AIRGUS_PUSHOVER_TOKEN", "user": "AIRGUS_PUSHOVER_USER", "device": "AIRGUS_PUSHOVER_DEVICE"}


def _merge(base: dict, update: dict) -> dict:
    out = deepcopy(base)
    for k, v in update.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "settings.json"
        self._lock = threading.Lock()
        self._data = deepcopy(DEFAULTS)
        if self.path.exists():
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            legacy = stored.get("smb", {})
            if "share" in legacy and "shares" not in legacy:  # single share before 0.1.x
                first = "/".join(p for p in (legacy.pop("share"), legacy.pop("path", "")) if p)
                legacy["shares"] = list(dict.fromkeys([first, *DEFAULTS["smb"]["shares"]]))
            self._data = _merge(DEFAULTS, stored)

    # --- reading -----------------------------------------------------------

    def get(self) -> dict:
        with self._lock:
            return deepcopy(self._data)

    def pushover(self) -> dict:
        po = self.get()["pushover"]
        for key, env in _ENV.items():
            if os.environ.get(env):
                po[key] = os.environ[env]
        return po

    def public(self) -> dict:
        """Settings for the UI: secrets are never sent back, only whether they are set."""
        data = self.get()
        po = self.pushover()
        data["pushover"] = {
            "token_set": bool(po["token"]),
            "user_set": bool(po["user"]),
            "device": po["device"],
            "min_severity": po["min_severity"],
            "in_replay": po["in_replay"],
            "from_env": {k: bool(os.environ.get(env)) for k, env in _ENV.items()},
        }
        smb = data["smb"]
        smb["password_set"] = bool(smb.pop("password"))
        return data

    # --- writing -----------------------------------------------------------

    def update(self, patch: dict) -> dict:
        patch = deepcopy(patch)
        po = patch.get("pushover")
        if isinstance(po, dict):
            # Empty secret fields mean "keep the stored value".
            for key in ("token", "user"):
                if po.get(key) in (None, ""):
                    po.pop(key, None)
            if "min_severity" in po:
                Severity[str(po["min_severity"]).upper()]  # validate
        if "site" in patch:
            lat = float(patch["site"].get("latitude", 51.0))
            if not -90 <= lat <= 90:
                raise ValueError("latitude must be between -90 and 90")
            patch["site"] = {"latitude": lat}
        if "smb" in patch:
            patch["smb"] = self._validate_smb(patch["smb"])
        if "asiair" in patch:
            patch["asiair"] = self._validate_asiair(patch["asiair"])
        if "nina" in patch:
            patch["nina"] = self._validate_nina(patch["nina"])
        if "rules" in patch:
            patch["rules"] = self._validate_rules(patch["rules"])
            with self._lock:
                self._data["rules"] = {}  # rules are always sent complete
        with self._lock:
            self._data = _merge(self._data, patch)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        return self.public()

    @staticmethod
    def _validate_smb(opts: dict) -> dict:
        out: dict = {}
        for key in ("server", "username"):
            if key in opts:
                out[key] = str(opts[key]).strip()
        if "server" in out and (not out["server"] or any(c.isspace() or c in "/\\:@" for c in out["server"])):
            raise ValueError("server must be a plain host name or IP address")
        if "shares" in opts:
            shares = []
            for entry in opts["shares"]:
                parts = [p.strip() for p in str(entry).replace("\\", "/").split("/") if p.strip()]
                if not parts:
                    continue
                if ".." in parts:
                    raise ValueError("share paths must not contain '..'")
                shares.append("/".join(parts))
            if not shares:
                raise ValueError("at least one share is required, e.g. 'TF Images/ASIAIR/log'")
            out["shares"] = list(dict.fromkeys(shares))
        if opts.get("password"):  # empty = keep the stored password
            out["password"] = str(opts["password"])
        if opts.get("clear_password"):
            out["password"] = ""
        if "max_age_h" in opts:
            age = float(opts["max_age_h"])
            if not 0 < age <= 24 * 30:
                raise ValueError("max_age_h must be between 0 and 720")
            out["max_age_h"] = age
        return out

    def smb_configs(self) -> list:
        from .fs import SmbConfig

        s = self.get()["smb"]
        out = []
        for entry in s["shares"]:
            share, _, path = entry.partition("/")
            out.append(SmbConfig(s["server"], share, path, s["username"], s["password"]))
        return out

    @staticmethod
    def _validate_asiair(opts: dict) -> dict:
        out: dict = {}
        if "enabled" in opts:
            out["enabled"] = bool(opts["enabled"])
        for key in ("record", "guider"):
            if key in opts:
                out[key] = bool(opts[key])
        if "host" in opts:
            host = str(opts["host"]).strip()
            if not host or any(c.isspace() or c in "/:@" for c in host):
                raise ValueError("host must be a plain host name or IP address")
            out["host"] = host
        for key in ("port", "guider_port"):
            if key in opts:
                port = int(opts[key])
                if not 1 <= port <= 65535:
                    raise ValueError("port must be between 1 and 65535")
                out[key] = port
        return out

    @staticmethod
    def _validate_nina(opts: dict) -> dict:
        out: dict = {}
        for key in ("enabled", "record"):
            if key in opts:
                out[key] = bool(opts[key])
        if "host" in opts:
            host = str(opts["host"]).strip()
            if any(c.isspace() or c in "/:@" for c in host):
                raise ValueError("host must be a plain host name or IP address")
            out["host"] = host
        if "port" in opts:
            port = int(opts["port"])
            if not 1 <= port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            out["port"] = port
        if out.get("enabled") and not out.get("host", "x"):
            raise ValueError("host is required")
        return out

    @staticmethod
    def _validate_rules(rules: dict) -> dict:
        schema = {r["name"]: {p["name"]: p for p in r["params"]} for r in rule_schema()}
        clean: dict = {}
        for name, opts in rules.items():
            if name not in schema:
                raise ValueError(f"unknown rule: {name}")
            out = {}
            for key, value in opts.items():
                if key == "enabled":
                    out[key] = bool(value)
                elif key in schema[name]:
                    out[key] = float(value) if schema[name][key]["type"] == "float" else int(value)
                else:
                    raise ValueError(f"unknown parameter {name}.{key}")
            clean[name] = out
        return clean

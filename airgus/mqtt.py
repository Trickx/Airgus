"""Publish Airgus' live state and alerts to MQTT, with Home Assistant MQTT discovery.

Topics (prefix ``airgus`` by default):
- ``airgus/status``  ``online`` / ``offline`` (retained, also the last will) - HA availability
- ``airgus/state``   one JSON object with all values (retained), at most every few seconds
- ``airgus/alert``   one JSON message per alert, for the HA event entity and automations
- ``homeassistant/<component>/airgus/<key>/config``  discovery configs (retained)

Only facts are published: a value Airgus does not know is ``null`` and shows as "unknown" in HA.
Nothing is ever read from MQTT; Airgus only publishes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from . import __version__
from .alerts import Alert

log = logging.getLogger(__name__)

SEVERITIES = ["info", "warning", "high", "emergency"]

# key, component, name, extra discovery fields
ENTITIES: list[tuple[str, str, str, dict]] = [
    ("sequence", "binary_sensor", "Sequence running", {"device_class": "running", "icon": "mdi:telescope"}),
    ("guiding", "binary_sensor", "Guiding", {"device_class": "running", "icon": "mdi:crosshairs-gps"}),
    ("tracking", "binary_sensor", "Tracking", {"device_class": "running", "icon": "mdi:rotate-orbit"}),
    ("asiair_connected", "binary_sensor", "ASIAIR connected", {"device_class": "connectivity"}),
    ("nina_connected", "binary_sensor", "NINA connected", {"device_class": "connectivity"}),
    ("target", "sensor", "Target", {"icon": "mdi:star-four-points-outline"}),
    ("image", "sensor", "Image", {"icon": "mdi:image-multiple-outline", "state_class": "measurement"}),
    ("frame_type", "sensor", "Frame type", {"icon": "mdi:image-filter-center-focus"}),
    ("rms_total", "sensor", "Guiding RMS", {"unit_of_measurement": "″", "state_class": "measurement",
                                             "suggested_display_precision": 2, "icon": "mdi:chart-bell-curve"}),
    ("rms_ra", "sensor", "Guiding RMS RA", {"unit_of_measurement": "″", "state_class": "measurement",
                                             "suggested_display_precision": 2}),
    ("rms_dec", "sensor", "Guiding RMS Dec", {"unit_of_measurement": "″", "state_class": "measurement",
                                               "suggested_display_precision": 2}),
    ("snr", "sensor", "Guide star SNR", {"state_class": "measurement", "suggested_display_precision": 1}),
    ("pier_side", "sensor", "Pier side", {"icon": "mdi:swap-horizontal"}),
    ("hour_angle", "sensor", "Hour angle", {"unit_of_measurement": "h", "state_class": "measurement",
                                            "suggested_display_precision": 2}),
    ("meridian_min", "sensor", "Meridian in", {"unit_of_measurement": "min", "device_class": "duration",
                                               "suggested_display_precision": 0, "icon": "mdi:timer-sand"}),
    ("focus", "sensor", "Focuser position", {"icon": "mdi:image-filter-tilt-shift", "state_class": "measurement"}),
    ("asiair_temp", "sensor", "ASIAIR temperature", {"unit_of_measurement": "°C", "device_class": "temperature",
                                                     "state_class": "measurement",
                                                     "suggested_display_precision": 1}),
    ("wifi", "sensor", "ASIAIR WLAN", {"unit_of_measurement": "dBm", "device_class": "signal_strength",
                                       "state_class": "measurement", "entity_category": "diagnostic"}),
    ("last_event", "sensor", "Last log event", {"device_class": "timestamp", "entity_category": "diagnostic"}),
    ("alert_count", "sensor", "Alerts", {"icon": "mdi:bell-alert-outline", "state_class": "total"}),
    ("last_alert", "sensor", "Last alert", {"icon": "mdi:alert-outline"}),
]


def _aware(iso: str | None) -> str | None:
    """Local log time -> ISO with offset (HA timestamps need a time zone)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


def state_payload(snap: dict, last_alert: Alert | None) -> dict:
    """The values for ``airgus/state`` from a monitor snapshot; unknown stays None."""
    rig, c = snap.get("rig", {}), snap.get("rig", {}).get("controller", {}) or {}
    pose = rig.get("pose") or {}
    live_pose = pose.get("source") == "live"
    meridian = pose.get("meridian_s")
    focus = rig.get("focus") or {}
    return {
        "sequence": bool(rig.get("sequence_active") or c.get("sequence_running")),
        "guiding": rig.get("guiding"),
        "tracking": rig.get("tracking"),
        "asiair_connected": c.get("connected"),
        "nina_connected": c.get("nina_connected"),
        "target": rig.get("target"),
        "image": rig.get("exposure_index"),
        "frame_type": rig.get("frame_type"),
        "rms_total": rig.get("rms_total"),
        "rms_ra": rig.get("rms_ra"),
        "rms_dec": rig.get("rms_dec"),
        "snr": rig.get("snr"),
        "pier_side": pose.get("pier") if live_pose else None,
        "hour_angle": round(pose["ha"], 3) if live_pose and pose.get("ha") is not None else None,
        "meridian_min": round(meridian / 60, 1) if isinstance(meridian, (int, float)) else None,
        "focus": focus.get("position"),
        "asiair_temp": c.get("temp_c"),
        "wifi": c.get("wifi_dbm"),
        "last_event": _aware(rig.get("last_event_time")),
        "alert_count": snap.get("alert_count"),
        "last_alert": last_alert.title if last_alert else None,
        "last_alert_attr": None if last_alert is None else {
            "severity": last_alert.severity.name.lower(), "message": last_alert.message,
            "time": _aware(last_alert.time.isoformat()), "rule": last_alert.rule},
    }


class MqttPublisher:
    def __init__(self, cfg: dict, client: Any = None, min_interval_s: float = 5.0,
                 clock=time.monotonic) -> None:
        self.cfg = cfg
        self.prefix = cfg.get("topic_prefix") or "airgus"
        self.discovery = cfg.get("discovery_prefix") or "homeassistant"
        self.client = client
        self.min_interval = min_interval_s
        self.clock = clock
        self._last_sent: dict | None = None
        self._last_time = -1e9
        self._last_alert: Alert | None = None
        self.connected = False

    # --- topics and payloads ---------------------------------------------------

    @property
    def status_topic(self) -> str:
        return f"{self.prefix}/status"

    @property
    def state_topic(self) -> str:
        return f"{self.prefix}/state"

    @property
    def alert_topic(self) -> str:
        return f"{self.prefix}/alert"

    def device(self) -> dict:
        return {"identifiers": ["airgus"], "name": "Airgus", "manufacturer": "Airgus",
                "model": "Astro rig monitor", "sw_version": __version__}

    def discovery_messages(self) -> list[tuple[str, dict]]:
        out = []
        base = {"availability_topic": self.status_topic, "device": self.device()}
        for key, component, name, extra in ENTITIES:
            cfg = {**base, "name": name, "unique_id": f"airgus_{key}", "state_topic": self.state_topic, **extra}
            if component == "binary_sensor":
                cfg["value_template"] = ("{{ none if value_json.%s is none else "
                                         "('ON' if value_json.%s else 'OFF') }}" % (key, key))
            else:
                cfg["value_template"] = "{{ value_json.%s }}" % key
            if key == "last_alert":
                cfg["json_attributes_topic"] = self.state_topic
                cfg["json_attributes_template"] = "{{ value_json.last_alert_attr | tojson }}"
            out.append((f"{self.discovery}/{component}/airgus/{key}/config", cfg))
        out.append((f"{self.discovery}/event/airgus/alert/config",
                    {**base, "name": "Alert", "unique_id": "airgus_alert", "state_topic": self.alert_topic,
                     "event_types": SEVERITIES, "icon": "mdi:bell-ring-outline"}))
        return out

    # --- connection ------------------------------------------------------------

    def start(self) -> None:
        if self.client is None:
            import paho.mqtt.client as paho

            self.client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="airgus")
        c = self.client
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.will_set(self.status_topic, "offline", qos=1, retain=True)
        if self.cfg.get("username"):
            c.username_pw_set(self.cfg["username"], self.cfg.get("password") or None)
        if self.cfg.get("tls"):
            c.tls_set()
        c.reconnect_delay_set(min_delay=2, max_delay=60)
        c.connect_async(self.cfg["host"], int(self.cfg.get("port") or 1883), keepalive=30)
        c.loop_start()

    def stop(self) -> None:
        if self.client is None:
            return
        try:
            self.client.publish(self.status_topic, "offline", qos=1, retain=True)
            self.client.disconnect()
            self.client.loop_stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("mqtt stop: %s", exc)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.warning("MQTT connection refused: %s", reason_code)
            return
        self.connected = True
        for topic, cfg in self.discovery_messages():
            client.publish(topic, json.dumps(cfg), qos=1, retain=True)
        client.publish(self.status_topic, "online", qos=1, retain=True)
        if self._last_sent is not None:
            client.publish(self.state_topic, json.dumps(self._last_sent), qos=0, retain=True)
        log.info("MQTT connected to %s, Home Assistant discovery published", self.cfg.get("host"))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected = False
        log.info("MQTT disconnected: %s", reason_code)

    # --- publishing ------------------------------------------------------------

    def publish_state(self, snap: dict, force: bool = False) -> bool:
        """Publish when something changed, at most every `min_interval_s` (and at least once a minute)."""
        if self.client is None:
            return False
        payload = state_payload(snap, self._last_alert)
        now = self.clock()
        due = now - self._last_time >= self.min_interval
        if not force and not (due and (payload != self._last_sent or now - self._last_time >= 60)):
            return False
        self._last_sent, self._last_time = payload, now
        self.client.publish(self.state_topic, json.dumps(payload), qos=0, retain=True)
        return True

    def publish_alert(self, alert: Alert, snap: dict | None = None) -> None:
        if self.client is None:
            return
        self._last_alert = alert
        msg = {"event_type": alert.severity.name.lower(), "title": alert.title, "message": alert.message,
               "rule": alert.rule, "time": _aware(alert.time.isoformat())}
        self.client.publish(self.alert_topic, json.dumps(msg), qos=1, retain=False)
        if snap is not None:
            self.publish_state(snap, force=True)

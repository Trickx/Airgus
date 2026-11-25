"""MQTT publishing with Home Assistant discovery (verified against Mosquitto + HA on 2026-11-23)."""

import json
from datetime import datetime

import pytest

from airgus.alerts import Alert, Severity
from airgus.mqtt import ENTITIES, MqttPublisher, state_payload
from airgus.settings import SettingsStore


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


SNAP = {"alert_count": 2, "rig": {
    "sequence_active": True, "target": "NGC 7635", "exposure_index": 52, "frame_type": "light", "guiding": True,
    "tracking": True, "rms_total": 0.74, "rms_ra": 0.5, "rms_dec": 0.55, "snr": 31.2,
    "last_event_time": "2026-11-21T22:05:00", "focus": {"position": 33398, "final": True, "time": "x"},
    "pose": {"ha": -0.1985, "dec": 56.7, "pier": "West", "source": "live", "meridian_s": 712.6},
    "controller": {"connected": True, "nina_connected": None, "temp_c": 29.4, "wifi_dbm": -45.0,
                   "sequence_running": False}}}


def test_discovery_configs():
    pub = MqttPublisher({"host": "broker"})
    msgs = dict(pub.discovery_messages())
    assert len(msgs) == len(ENTITIES) + 1
    seq = msgs["homeassistant/binary_sensor/airgus/sequence/config"]
    assert seq["unique_id"] == "airgus_sequence" and seq["availability_topic"] == "airgus/status"
    assert seq["device"]["identifiers"] == ["airgus"] and seq["state_topic"] == "airgus/state"
    alert = msgs["homeassistant/event/airgus/alert/config"]
    assert alert["event_types"] == ["info", "warning", "high", "emergency"]
    last = msgs["homeassistant/sensor/airgus/last_alert/config"]
    assert last["json_attributes_topic"] == "airgus/state"


def test_state_values_and_unknowns():
    s = state_payload(SNAP, None)
    assert (s["sequence"], s["target"], s["image"], s["rms_total"], s["pier_side"]) == (True, "NGC 7635", 52, 0.74, "West")
    assert s["meridian_min"] == 11.9 and s["focus"] == 33398 and s["asiair_temp"] == 29.4
    assert s["nina_connected"] is None and s["last_alert"] is None  # unknown stays unknown
    assert s["last_event"].startswith("2026-11-21T22:05:00") and s["last_event"][19] in "+-"
    # an estimated pose (PHD2 log) is not published as a fact
    est = json.loads(json.dumps(SNAP))
    est["rig"]["pose"]["source"] = "phd2"
    assert state_payload(est, None)["pier_side"] is None


def test_publish_throttle_and_alert():
    t = [0.0]
    client = FakeClient()
    pub = MqttPublisher({"host": "broker"}, client=client, min_interval_s=5, clock=lambda: t[0])
    assert pub.publish_state(SNAP) is True
    t[0] = 2
    assert pub.publish_state(SNAP) is False   # too soon
    t[0] = 10
    assert pub.publish_state(SNAP) is False   # nothing changed
    t[0] = 70
    assert pub.publish_state(SNAP) is True    # refresh once a minute
    alert = Alert(time=datetime(2026, 11, 21, 22, 10), rule="star-lost", severity=Severity.HIGH,
                  title="Leitstern verloren", message="Stern weg", key="star-lost")
    pub.publish_alert(alert, SNAP)
    topic, payload, retain = client.published[-2]
    assert topic == "airgus/alert" and retain is False
    assert json.loads(payload)["event_type"] == "high"
    state = json.loads(client.published[-1][1])
    assert state["last_alert"] == "Leitstern verloren" and state["last_alert_attr"]["severity"] == "high"


def test_on_connect_publishes_discovery_and_online():
    client = FakeClient()
    pub = MqttPublisher({"host": "broker"}, client=client)
    pub._on_connect(client, None, None, 0)
    topics = [t for t, _, _ in client.published]
    assert topics[-1] == "airgus/status" and client.published[-1][1] == "online"
    assert all(r for _, _, r in client.published)  # discovery and status are retained


def test_settings_mask_password_and_validate(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"mqtt": {"enabled": True, "host": "192.168.0.144", "username": "u", "password": "secret"}})
    assert store.get()["mqtt"]["password"] == "secret"
    pub = store.public()["mqtt"]
    assert "password" not in pub and pub["password_set"] is True
    store.update({"mqtt": {"password": ""}})  # empty keeps the stored one
    assert store.get()["mqtt"]["password"] == "secret"
    with pytest.raises(ValueError):
        store.update({"mqtt": {"topic_prefix": "air/#"}})

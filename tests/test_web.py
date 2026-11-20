import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from airgus.web import create_app

LOGS = Path(__file__).resolve().parent.parent / "logs"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data", LOGS)) as c:
        yield c


def wait_for(client, status, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get("/api/status").json()
        if snap["run"]["status"] == status:
            return snap
        time.sleep(0.05)
    raise AssertionError(f"status {status!r} not reached: {snap['run']}")


def test_index_and_static(client):
    assert "Airgus" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/mount3d.js").status_code == 200


def test_settings_roundtrip_hides_secrets(client):
    r = client.put("/api/settings", json={"pushover": {"token": "abc", "user": "def", "min_severity": "HIGH"},
                                          "site": {"latitude": 50.5}})
    assert r.status_code == 200
    body = r.json()
    assert body["pushover"]["token_set"] is True
    assert "abc" not in r.text
    assert body["site"]["latitude"] == 50.5
    # empty token keeps the stored one
    body = client.put("/api/settings", json={"pushover": {"token": ""}}).json()
    assert body["pushover"]["token_set"] is True


def test_invalid_rule_parameter_rejected(client):
    assert client.put("/api/settings", json={"rules": {"rms": {"bogus": 1}}}).status_code == 400
    assert client.put("/api/settings", json={"rules": {"rms": {"threshold_arcsec": 3, "enabled": True}}}).status_code == 200


def test_rule_schema(client):
    names = [r["name"] for r in client.get("/api/rules").json()]
    assert "runaway" in names and "meridian-flip" in names


def test_folder_outside_logs_rejected(client):
    r = client.post("/api/run", json={"mode": "replay", "folder": "../..", "speed": 0})
    assert r.status_code == 400


@pytest.mark.skipif(not (LOGS / "2026-11-14").is_dir(), reason="sample logs not present")
def test_replay_at_max_speed(client):
    folders = [f["folder"] for f in client.get("/api/folders").json()["folders"]]
    assert "2026-11-14" in folders
    assert client.post("/api/run", json={"mode": "replay", "folder": "2026-11-14", "speed": 0}).status_code == 200
    snap = wait_for(client, "finished")
    assert snap["run"]["done"] == snap["run"]["total"] > 30000
    assert snap["rig"]["pier_side"] == "East"
    alerts = client.get("/api/alerts").json()
    assert any(a["severity"] == "EMERGENCY" for a in alerts)
    assert len(client.get("/api/frames").json()) > 10000


@pytest.mark.skipif(not (LOGS / "2026-11-14").is_dir(), reason="sample logs not present")
def test_replay_pause_speed_stop(client):
    client.post("/api/run", json={"mode": "replay", "folder": "2026-11-14", "speed": 1})
    wait_for(client, "running")
    assert client.post("/api/run/pause").json()["run"]["status"] == "paused"
    assert client.post("/api/run/resume").json()["run"]["status"] == "running"
    assert client.post("/api/run/speed", json={"speed": 600}).json()["run"]["speed"] == 600
    assert client.post("/api/run/stop").json()["run"]["status"] == "idle"


def test_ui_is_revalidated(client):
    assert client.get("/static/style.css").headers["cache-control"] == "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"

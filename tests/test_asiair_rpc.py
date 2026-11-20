import asyncio
import json
from datetime import datetime, timedelta

import pytest

from airgus.alerts import Severity
from airgus.asiair_rpc import AsiairRecordParser, AsiairRpcClient, Recorder, map_message
from airgus.engine import Engine
from airgus.events import (
    ControllerActivity,
    ControllerConnected,
    ControllerDisconnected,
    ControllerHealth,
    DiskSpace,
    SequencePlan,
    SequenceStarted,
    Source,
    WifiSignal,
)
from airgus.parsers import parser_for
from airgus.rules import ControllerOfflineRule, StaleLogRule, default_rules
from airgus.settings import SettingsStore

T0 = datetime(2026, 11, 20, 22, 0, 0)

# Messages as captured from the ASIAIR app's traffic on port 4700.
PI_STATUS = {"Event": "PiStatus", "Timestamp": "576.02", "is_overtemp": False, "temp": 27.777,
             "is_undervolt": False, "is_over_current": False}
STATION = {"Event": "Station", "Timestamp": "573.97", "sig_lev": -47}
SEQUENCE = {"Event": "Sequence", "Timestamp": "582.55", "state": "start"}
DISK = {"jsonrpc": "2.0", "Timestamp": "580.47", "method": "get_disk_volume",
        "result": {"totalMB": 488124, "freeMB": 351210}, "code": 0, "id": 400}
PLAN = {"jsonrpc": "2.0", "Timestamp": "580.53", "method": "get_target_sequences",
        "result": {"delay_first": 0, "merid_flip": True, "group_name": "IC 405", "left_size_m": 171.36,
                   "left_time_sec": 102, "target_coord": [19.16, 90.0], "slots": []}, "code": 0, "id": 401}


def test_map_messages():
    (h,) = map_message(PI_STATUS, T0)
    assert isinstance(h, ControllerHealth) and h.temp_c == pytest.approx(27.777) and not h.undervolt
    assert h.source is Source.ASIAIR
    (w,) = map_message(STATION, T0)
    assert isinstance(w, WifiSignal) and w.dbm == -47
    (d,) = map_message(DISK, T0)
    assert isinstance(d, DiskSpace) and d.free_mb == 351210
    (p,) = map_message(PLAN, T0)
    assert isinstance(p, SequencePlan) and p.name == "IC 405" and p.left_time_s == 102 and p.meridian_flip
    (a,) = map_message(SEQUENCE, T0)
    assert isinstance(a, ControllerActivity) and (a.kind, a.state) == ("Sequence", "start")
    assert map_message({"jsonrpc": "2.0", "method": "test_connection", "result": "server connected!",
                        "code": 0, "id": 110}, T0) == []
    assert map_message({**DISK, "code": 1}, T0) == []


def test_control_commands_are_refused():
    client = AsiairRpcClient("x", lambda ev: None)

    class Writer:
        def write(self, data):
            raise AssertionError("must not send")

    with pytest.raises(PermissionError):
        asyncio.run(client.call(Writer(), "start_exposure"))


def test_recording_round_trip(tmp_path):
    rec = Recorder(tmp_path)
    rec.write(T0, {"conn": "up", "host": "asiair.local"})
    rec.write(T0 + timedelta(seconds=1), {"msg": PI_STATUS})
    rec.write(T0 + timedelta(seconds=2), {"conn": "down", "host": "asiair.local", "reason": "weg"})
    (path,) = tmp_path.iterdir()
    assert path.name == "Airgus_ASIAIR4700_2026-11-20.jsonl"
    parser = parser_for(path)
    assert isinstance(parser, AsiairRecordParser)
    events = [ev for line in path.read_text().splitlines() for ev in parser.feed(line)]
    assert [type(e) for e in events] == [ControllerConnected, ControllerHealth, ControllerDisconnected]
    assert events[1].time == T0 + timedelta(seconds=1)


def test_client_against_fake_asiair(tmp_path):
    received: list[str] = []
    events = []

    async def scenario():
        async def handle(reader, writer):
            writer.write((json.dumps(STATION) + "\r\n").encode())
            writer.write((json.dumps({**PI_STATUS, "is_undervolt": True}) + "\r\n").encode())
            for _ in range(3):
                req = json.loads(await reader.readline())
                received.append(req["method"])
                if req["method"] == "get_disk_volume":
                    writer.write((json.dumps({**DISK, "id": req["id"]}) + "\r\n").encode())
                elif req["method"] == "get_target_sequences":
                    writer.write((json.dumps({**PLAN, "id": req["id"]}) + "\r\n").encode())
                else:
                    writer.write((json.dumps({"jsonrpc": "2.0", "method": "test_connection",
                                              "result": "server connected!", "code": 0,
                                              "id": req["id"]}) + "\r\n").encode())
                await writer.drain()
            writer.close()  # ASIAIR goes away

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AsiairRpcClient("127.0.0.1", events.append, port=port, recorder=Recorder(tmp_path),
                                 heartbeat_s=0.05, retry_s=0.05, max_retry_s=0.05, timeout_s=2)
        task = asyncio.create_task(client.run())
        for _ in range(100):
            if any(isinstance(e, ControllerDisconnected) for e in events):
                break
            await asyncio.sleep(0.02)
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.3)  # reconnect attempts fail; must not repeat the outage event
        task.cancel()

    asyncio.run(scenario())
    assert received == ["get_disk_volume", "get_target_sequences", "test_connection"]
    kinds = [type(e) for e in events]
    assert kinds[0] is ControllerConnected
    assert {WifiSignal, ControllerHealth, DiskSpace, SequencePlan} <= set(kinds)
    assert kinds.count(ControllerDisconnected) == 1
    assert next(e for e in events if isinstance(e, ControllerHealth)).undervolt
    recorded = (tmp_path / f"Airgus_ASIAIR4700_{datetime.now():%Y-%m-%d}.jsonl").read_text()
    assert "test_connection" not in recorded and '"conn": "down"' in recorded


def _ev(cls, seconds, **kw):
    return cls(time=T0 + timedelta(seconds=seconds), source=Source.ASIAIR, **kw)


def test_controller_rules():
    engine = Engine(default_rules())
    alerts = engine.process(_ev(ControllerConnected, 0, host="asiair.local"))
    alerts += engine.process(_ev(ControllerHealth, 1, temp_c=50.0, overtemp=False, undervolt=True,
                                 overcurrent=False))
    alerts += engine.process(_ev(DiskSpace, 2, total_mb=60000, free_mb=1500))
    assert {(a.rule, a.severity) for a in alerts} == {("controller-health", Severity.HIGH)}
    assert engine.state.snapshot()["controller"]["undervolt"] is True


def test_offline_only_alerts_while_imaging():
    engine = Engine([ControllerOfflineRule(offline_s=60)])
    engine.process(_ev(ControllerConnected, 0, host="asiair.local"))
    engine.process(_ev(ControllerDisconnected, 10, host="asiair.local", reason="weg"))
    assert engine.tick(T0 + timedelta(minutes=5)) == []  # nothing running: user switched it off
    engine.process(_ev(ControllerActivity, 400, kind="Sequence", state="start"))
    (alert,) = engine.tick(T0 + timedelta(minutes=8))
    assert alert.severity is Severity.HIGH


def test_controller_events_do_not_hide_a_stale_log():
    engine = Engine([StaleLogRule(timeout_min=10)])
    engine.process(SequenceStarted(time=T0, source=Source.ASIAIR, target="IC 405"))
    alerts = []
    for m in range(1, 15):
        alerts += engine.process(_ev(WifiSignal, m * 60, dbm=-50))
    assert [a.rule for a in alerts] == ["stale-log"]


def test_asiair_settings_validation(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.get()["asiair"] == {"enabled": False, "host": "asiair.local", "port": 4700, "guider": True,
                                     "guider_port": 4400, "record": True}
    store.update({"asiair": {"enabled": True, "host": "192.168.0.236", "port": 4700}})
    assert store.get()["asiair"]["host"] == "192.168.0.236"
    for bad in ({"host": "http://x"}, {"host": ""}, {"port": 70000}, {"guider_port": 0}):
        with pytest.raises(ValueError):
            store.update({"asiair": bad})


# Preview exposure with failed plate solve, recorded 2026-11-20 12:59 (daylight).
PREVIEW_SHOT = [
    {"Event": "Exposure", "page": "preview", "state": "start", "exp_us": 10000000},
    {"Event": "Exposure", "page": "preview", "state": "downloading"},
    {"Event": "Exposure", "page": "preview", "state": "complete"},
    {"Event": "Annotate", "page": "preview", "tag": "Exposure", "state": "start"},
    {"Event": "PlateSolve", "page": "preview", "tag": "Exposure", "state": "start"},
    {"Event": "PlateSolve", "page": "preview", "tag": "Exposure", "state": "solving", "star_number": 167},
    {"Event": "PlateSolve", "page": "preview", "tag": "Exposure", "error": "solve failed", "code": 251,
     "state": "fail", "result": {"star_number": 167, "duration_ms": 5638}},
]


def test_exposure_and_solve_progress():
    engine = Engine([])
    events = []
    for i, msg in enumerate(PREVIEW_SHOT):
        for ev in map_message(msg, T0 + timedelta(seconds=i)):
            events.append(ev)
            engine.process(ev)
            if i == 0:
                assert engine.state.controller.exposure["state"] == "start"
    exp = [e for e in events if e.__class__.__name__ == "ExposureProgress"]
    assert exp[0].duration_s == 10.0 and exp[0].page == "preview"
    c = engine.state.snapshot()["controller"]
    assert c["exposure"] == {"page": "preview", "duration_s": 10.0, "started": T0.isoformat(),
                             "state": "complete", "updated": (T0 + timedelta(seconds=2)).isoformat()}
    assert c["solve"]["state"] == "fail" and c["solve"]["stars"] == 167 and c["solve"]["error"] == "solve failed"


def _solve(i, state, page="preview"):
    msg = {"Event": "PlateSolve", "page": page, "state": state, "star_number": 167}
    if state == "fail":
        msg["error"] = "solve failed"
    return map_message(msg, T0 + timedelta(minutes=i))


def test_failed_preview_solve_is_listed_but_not_pushed():
    from airgus.rules import PlateSolveRule
    engine = Engine([PlateSolveRule()])
    alerts = []
    for i, msg in enumerate(PREVIEW_SHOT):
        for ev in map_message(msg, T0 + timedelta(seconds=i)):
            alerts += engine.process(ev)
    (alert,) = alerts
    assert alert.severity is Severity.INFO and alert.title == "Plate Solve fehlgeschlagen"
    assert "solve failed, 167 Sterne erkannt (Vorschau)" in alert.message


def test_solve_failures_escalate_and_reset():
    from airgus.rules import PlateSolveRule
    engine = Engine([PlateSolveRule(escalate_after=3)])
    sev = [a.severity for i in range(3) for ev in _solve(i, "fail", "autorun") for a in engine.process(ev)]
    assert sev == [Severity.WARNING, Severity.WARNING, Severity.HIGH]
    for ev in _solve(3, "complete", "autorun"):
        engine.process(ev)
    sev = [a.severity for ev in _solve(4, "fail", "autorun") for a in engine.process(ev)]
    assert sev == [Severity.WARNING]


# Captured 2026-11-20 14:40 while the main camera (Canon EOS 550D) was closed and reopened in the app.
CAMERA_CHANGE = {"Event": "CameraStateChange", "Timestamp": "18719.091305782"}


def _camera(minutes):
    return map_message(CAMERA_CHANGE, T0 + timedelta(minutes=minutes))


def test_main_camera_change_outside_sequence_is_info():
    from airgus.rules import MainCameraRule
    engine = Engine([MainCameraRule()])
    alerts = [a for m in (0, 0.05) for ev in _camera(m) for a in engine.process(ev)]  # close + open
    assert [(a.title, a.severity) for a in alerts] == [("Hauptkamera: Statuswechsel", Severity.INFO)] * 2
    assert engine.tick(T0 + timedelta(minutes=10)) == []  # no sequence: no follow-up check
    c = engine.state.snapshot()["controller"]
    assert c["main_camera_changed"] == (T0 + timedelta(minutes=0.05)).isoformat()
    assert c["main_camera_connected"] is None  # the event does not say which way


def test_main_camera_silent_during_sequence_escalates():
    from airgus.rules import MainCameraRule
    engine = Engine([MainCameraRule(grace_s=180)])
    for ev in map_message(SEQUENCE, T0 - timedelta(minutes=5)):
        engine.process(ev)
    (warn,) = [a for ev in _camera(0) for a in engine.process(ev)]
    assert warn.severity is Severity.WARNING
    assert engine.tick(T0 + timedelta(minutes=2)) == []
    (high,) = engine.tick(T0 + timedelta(minutes=4))
    assert high.title == "Hauptkamera belichtet nicht" and high.severity is Severity.HIGH
    assert engine.tick(T0 + timedelta(minutes=10)) == []  # only once
    shot = {"Event": "Exposure", "page": "autorun", "state": "start", "exp_us": 300000000}
    (back,) = [a for ev in map_message(shot, T0 + timedelta(minutes=11)) for a in engine.process(ev)]
    assert back.title == "Hauptkamera belichtet wieder"
    c = engine.state.snapshot()["controller"]
    assert c["main_camera_exposed"] is True and c["main_camera_connected"] is True
    assert c["main_camera_source"] == "Belichtung"


def test_main_camera_change_followed_by_exposure_stays_warning():
    from airgus.rules import MainCameraRule
    engine = Engine([MainCameraRule()])
    for ev in map_message(SEQUENCE, T0 - timedelta(minutes=5)):
        engine.process(ev)
    for ev in _camera(0):
        engine.process(ev)
    shot = {"Event": "Exposure", "page": "autorun", "state": "start", "exp_us": 300000000}
    for ev in map_message(shot, T0 + timedelta(minutes=1)):
        assert engine.process(ev) == []
    assert engine.tick(T0 + timedelta(minutes=30)) == []


# Captured 2026-11-20 14:44: EAF closed and reopened in the app (each state is sent twice).
OPEN_EAF = [{"Event": "OpenEAF", "Timestamp": "18951.99", "state": "working", "lapse_ms": 1, "route": []},
            {"Event": "OpenEAF", "Timestamp": "18951.99", "state": "working", "lapse_ms": 1, "route": []},
            {"Event": "OpenEAF", "Timestamp": "18952.06", "state": "complete", "lapse_ms": 64, "route": []},
            {"Event": "OpenEAF", "Timestamp": "18952.06", "state": "complete", "lapse_ms": 64, "route": []}]


def test_focuser_open_and_position():
    from airgus.events import FocuserPosition
    from airgus.parsers.asiair import AsiairAutorunParser
    from airgus.rules import FocuserRule
    engine = Engine([FocuserRule()])
    p = AsiairAutorunParser()
    for line in ["2026/11/20 21:20:01 Find Focus Star: detect and calculate star size 4.2 ,  EAF position 5846",
                 "2026/11/20 21:22:30 Auto focus succeeded, the focused position is 5820"]:
        for ev in p.feed(line):
            engine.process(ev)
    focus = engine.state.snapshot()["focus"]
    assert focus == {"position": 5820, "final": True, "time": "2026-11-20T21:22:30"}

    alerts = [a for msg in OPEN_EAF for ev in map_message(msg, T0) for a in engine.process(ev)]
    assert [(a.title, a.severity) for a in alerts] == [("EAF geöffnet", Severity.INFO)]
    assert "5820" in alerts[0].message
    assert engine.state.snapshot()["controller"]["focuser_opened"] == T0.isoformat()

    for ev in map_message(SEQUENCE, T0 + timedelta(minutes=5)):
        engine.process(ev)
    (warn,) = [a for ev in map_message(OPEN_EAF[2], T0 + timedelta(minutes=10)) for a in engine.process(ev)]
    assert warn.severity is Severity.WARNING and warn.title == "EAF neu verbunden"


def test_main_camera_recorded_close_open_pairs():
    """Airgus recording 2026-11-20 14:51: camera disabled and enabled twice in the app - every change is listed."""
    from airgus.rules import MainCameraRule
    engine = Engine([MainCameraRule()])
    times = ["14:51:23.768", "14:51:27.155", "14:51:55.572", "14:52:08.498"]
    alerts = [a for t in times for ev in map_message(CAMERA_CHANGE, datetime.fromisoformat(f"2026-11-20T{t}"))
              for a in engine.process(ev)]
    assert [a.title for a in alerts] == ["Hauptkamera: Statuswechsel"] * 4


# Answers as seen in the app's traffic (2026-11-20); a second client only gets them while the app is off.
CAM_IDLE = {"jsonrpc": "2.0", "method": "get_camera_state", "code": 0, "id": 1,
            "result": {"state": "idle", "name": "Canon EOS 550D", "path": "Canon EOS 550D 04a9:31ea"}}
CAM_CLOSE = {"jsonrpc": "2.0", "method": "get_camera_state", "code": 0, "id": 2, "result": {"state": "close"}}
EAF_INFO = {"jsonrpc": "2.0", "method": "get_focuser_info", "code": 0, "id": 3,
            "result": {"position": 5848, "temperature": 19.01, "max_step": 20000, "firmware_version": "3.3.8"}}
EAF_CLOSE = {"jsonrpc": "2.0", "method": "get_focuser_info", "code": 0, "id": 4, "result": {"state": "close"}}


def test_main_camera_status_answers():
    from airgus.rules import MainCameraRule
    engine = Engine([MainCameraRule()])
    for ev in map_message(SEQUENCE, T0 - timedelta(minutes=5)):
        engine.process(ev)
    alerts = [a for i, msg in enumerate([CAM_IDLE, CAM_IDLE, CAM_CLOSE, CAM_IDLE])
              for ev in map_message(msg, T0 + timedelta(seconds=5 * i)) for a in engine.process(ev)]
    assert [(a.title, a.severity) for a in alerts] == [("Hauptkamera getrennt", Severity.HIGH),
                                                       ("Hauptkamera verbunden", Severity.INFO)]
    assert "Canon EOS 550D" in alerts[0].message
    c = engine.state.snapshot()["controller"]
    assert (c["main_camera_connected"], c["main_camera_name"], c["main_camera_source"]) == (True, "Canon EOS 550D",
                                                                                             "Abfrage")


def test_focuser_status_answers():
    from airgus.rules import FocuserRule
    engine = Engine([FocuserRule()])
    alerts = [a for i, msg in enumerate([EAF_INFO, EAF_CLOSE, EAF_INFO])
              for ev in map_message(msg, T0 + timedelta(minutes=i)) for a in engine.process(ev)]
    assert [(a.title, a.severity) for a in alerts] == [("EAF getrennt", Severity.INFO), ("EAF verbunden", Severity.INFO)]
    assert "5848" in alerts[0].message
    c = engine.state.snapshot()["controller"]
    assert (c["focuser_connected"], c["focuser_position"], c["focuser_temp_c"]) == (True, 5848, 19.01)

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from airgus.alerts import Severity
from airgus.asiair_rpc import GUIDER, AsiairRecordParser, AsiairRpcClient, GuiderMapper, Recorder
from airgus.engine import Engine
from airgus.events import (
    Dither,
    GuideFrame,
    GuiderConnected,
    GuiderDisconnected,
    GuiderInfo,
    GuidingStarted,
    SettleCompleted,
    SettleFailed,
    Source,
    StarLost,
)
from airgus.parsers import parser_for
from airgus.rules import RunawayRule, default_rules

T0 = datetime(2026, 11, 20, 22, 0, 0)

# PHD2 event-server format (OpenPHDGuiding wiki "EventMonitoring"); the ASIAIR guider adds Timestamp.
GUIDE_STEP = {"Event": "GuideStep", "Timestamp": "1.0", "Frame": 42, "Time": 120.3, "Mount": "Mount",
              "dx": 0.2, "dy": -0.1, "RADistanceRaw": 0.31, "DECDistanceRaw": -0.12,
              "RADistanceGuide": 0.25, "DECDistanceGuide": 0.0, "RADuration": 180, "RADirection": "West",
              "DECDuration": 0, "StarMass": 15000, "SNR": 42.5, "HFD": 2.1, "AvgDist": 0.3}


def at(s):
    return T0 + timedelta(seconds=s)


def test_guider_mapper():
    m = GuiderMapper()
    (f,) = m(GUIDE_STEP, at(0))
    assert isinstance(f, GuideFrame) and f.live and f.source is Source.PHD2
    assert (f.frame, f.ra_px, f.dec_px, f.ra_pulse_ms, f.snr, f.settling) == (42, 0.31, -0.12, 180, 42.5, False)
    assert m({"Event": "GuidingDithered", "dx": 3, "dy": 1}, at(1))[0].__class__ is Dither
    assert m({"Event": "SettleBegin"}, at(1)) == []
    assert m(GUIDE_STEP, at(2))[0].settling is True
    assert isinstance(m({"Event": "SettleDone", "Status": 0}, at(3))[0], SettleCompleted)
    assert m(GUIDE_STEP, at(4))[0].settling is False
    (fail,) = m({"Event": "SettleDone", "Status": 1, "Error": "timed-out waiting for guider to settle"}, at(5))
    assert isinstance(fail, SettleFailed) and "timed-out" in fail.message
    (lost,) = m({"Event": "StarLost", "Frame": 43, "Status": -2, "SNR": 0}, at(6))
    assert isinstance(lost, StarLost) and lost.source is Source.PHD2
    assert m({"Event": "AppState", "State": "Guiding"}, at(7))[0].app_state == "Guiding"
    assert m({"jsonrpc": "2.0", "method": "get_app_state", "result": "Stopped", "code": 0, "id": 1},
             at(8))[0].app_state == "Stopped"
    assert m({"jsonrpc": "2.0", "method": "get_pixel_scale", "result": 3.86, "code": 0, "id": 2},
             at(8))[0].pixel_scale == 3.86
    assert m({"Event": "Version", "name": "ASI AIR guider"}, at(9)) == []


def test_live_guider_replaces_log_frames():
    engine = Engine(default_rules())
    log_frame = GuideFrame(time=at(0), source=Source.PHD2, frame=1, ra_px=0.1, dec_px=0.1, ra_pulse_ms=0,
                           dec_pulse_ms=0, snr=40, mass=1, settling=False)
    assert not engine.is_duplicate(log_frame)
    engine.process(GuiderConnected(time=at(1), source=Source.ASIAIR, live=True, host="asiair"))
    assert engine.is_duplicate(log_frame)
    assert not engine.is_duplicate(GuideFrame(**{**log_frame.__dict__, "live": True}))
    header = GuidingStarted(time=at(2), source=Source.PHD2, pier_side="West", hour_angle_h=-1.0,
                            dec_deg=-5.0, pixel_scale=3.86)
    assert not engine.is_duplicate(header)  # pier side and hour angle still come from the log
    engine.process(GuiderDisconnected(time=at(3), source=Source.ASIAIR, live=True, host="asiair", reason="x"))
    assert not engine.is_duplicate(log_frame)  # log is the fallback again


def test_pixel_scale_from_live_guider():
    engine = Engine([])
    engine.process(GuiderInfo(time=at(0), source=Source.ASIAIR, live=True, pixel_scale=3.86))
    assert engine.state.pixel_scale == 3.86


def test_runaway_uses_limited_flag_without_log_header():
    engine = Engine([RunawayRule(threshold_arcsec=30, min_duration_s=30, emergency_after_s=120)])
    engine.process(GuiderInfo(time=at(0), source=Source.ASIAIR, live=True, pixel_scale=4.0))
    alerts = []
    for i in range(40):
        alerts += engine.process(GuideFrame(time=at(i * 2), source=Source.PHD2, live=True, frame=i,
                                            ra_px=10 + i, dec_px=0, ra_pulse_ms=2500, dec_pulse_ms=0,
                                            snr=30, mass=1, settling=False, ra_limited=True))
    assert alerts and alerts[0].severity >= Severity.HIGH


def test_guider_recording_round_trip(tmp_path):
    rec = Recorder(tmp_path, GUIDER.record_prefix)
    rec.write(at(0), {"conn": "up", "host": "asiair"})
    rec.write(at(1), {"msg": {"Event": "SettleBegin"}})
    rec.write(at(2), {"msg": GUIDE_STEP})
    (path,) = tmp_path.iterdir()
    assert path.name == "Airgus_ASIAIR4400_2026-11-20.jsonl"
    parser = parser_for(path)
    assert isinstance(parser, AsiairRecordParser) and parser.channel is GUIDER
    events = [ev for line in path.read_text().splitlines() for ev in parser.feed(line)]
    assert isinstance(events[0], GuiderConnected) and events[1].settling is True


def test_guider_client_against_fake_server():
    received, events = [], []

    async def scenario():
        async def handle(reader, writer):
            writer.write(b'{"Event":"Version","name":"ASI AIR guider","svr_ver_int":12}\r\n')
            for _ in range(3):
                req = json.loads(await reader.readline())
                received.append(req["method"])
                result = {"get_app_state": "Guiding", "get_pixel_scale": 3.86}.get(req["method"], "server connected!")
                writer.write((json.dumps({"jsonrpc": "2.0", "method": req["method"], "result": result,
                                          "code": 0, "id": req["id"]}) + "\r\n").encode())
                writer.write((json.dumps(GUIDE_STEP) + "\r\n").encode())
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AsiairRpcClient("127.0.0.1", events.append, port=port, channel=GUIDER, heartbeat_s=0.05,
                                 retry_s=5, timeout_s=2, request_gap_s=0.01)
        task = asyncio.create_task(client.run())
        for _ in range(100):
            if any(isinstance(e, GuiderDisconnected) for e in events):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        server.close()

    asyncio.run(scenario())
    assert received == ["get_app_state", "get_pixel_scale", "scope_get_location"]
    assert isinstance(events[0], GuiderConnected)
    assert sum(isinstance(e, GuideFrame) for e in events) == 3
    infos = [e for e in events if isinstance(e, GuiderInfo)]
    assert infos[0].app_state == "Guiding" and infos[1].pixel_scale == 3.86
    assert isinstance(events[-1], GuiderDisconnected)


def test_scope_track_events():
    # Captured 2026-11-20 while tracking was switched off and on in the ASIAIR app.
    m = GuiderMapper()
    (off,) = m({"Event": "ScopeTrack", "Timestamp": "4086.91", "state": "off"}, at(0))
    (on,) = m({"Event": "ScopeTrack", "Timestamp": "4090.05", "state": "on"}, at(3))
    assert (off.on, on.on) == (False, True) and on.live
    (indi,) = m({"Event": "INDIServer", "Timestamp": "4086.86",
                 "Msg": "2026-11-20T09:36:45: [WARNING] Stopping Tracking (Sidereal). "}, at(0))
    assert indi.kind == "INDI" and "Stopping Tracking" in indi.state
    engine = Engine([])
    engine.process(off)
    assert engine.state.snapshot()["tracking"] is False


# get_connected answers captured 2026-11-20 13:16 while the guide camera was reconnected in the app.
WITH_CAM = {"jsonrpc": "2.0", "method": "get_connected", "code": 0, "id": 1,
            "result": {"camera": {"name": "ZWO ASI120MM Mini", "path": "bus5.port:1,4,"}, "mount": 18,
                       "mount_name": "EQMod Mount", "mount_specify_name": "EQMod Mount"}}
NO_CAM = {"jsonrpc": "2.0", "method": "get_connected", "code": 0, "id": 2,
          "result": {"mount": 18, "mount_name": "EQMod Mount", "mount_specify_name": "EQMod Mount"}}


def test_get_connected_mapping():
    (s,) = GuiderMapper()(WITH_CAM, at(0))
    assert (s.guide_camera, s.mount) == ("ZWO ASI120MM Mini", "EQMod Mount")
    (s,) = GuiderMapper()(NO_CAM, at(0))
    assert (s.guide_camera, s.mount) == (None, "EQMod Mount")


def test_device_lost_and_back():
    from airgus.events import SequenceStarted
    from airgus.rules import DeviceLostRule
    engine = Engine([DeviceLostRule()])
    m = GuiderMapper()
    alerts = []
    for i, msg in enumerate([WITH_CAM, WITH_CAM, NO_CAM, NO_CAM, WITH_CAM]):
        for ev in m(msg, at(i * 60)):
            alerts += engine.process(ev)
    assert [(a.title, a.severity) for a in alerts] == [("Guide-Kamera getrennt", Severity.INFO),
                                                       ("Guide-Kamera verbunden", Severity.INFO)]
    assert engine.state.snapshot()["controller"]["guide_camera"] == "ZWO ASI120MM Mini"
    engine.process(SequenceStarted(time=at(400), source=Source.ASIAIR, target="IC 405"))
    (lost,) = [a for ev in m(NO_CAM, at(460)) for a in engine.process(ev)]
    assert lost.severity is Severity.HIGH and "IC 405" in lost.message


def test_guider_outage_is_not_a_device_loss():
    from airgus.rules import DeviceLostRule
    engine = Engine([DeviceLostRule()])
    m = GuiderMapper()
    for ev in m(WITH_CAM, at(0)):
        engine.process(ev)
    engine.process(GuiderDisconnected(time=at(10), source=Source.ASIAIR, live=True, host="x", reason="weg"))
    c = engine.state.snapshot()["controller"]
    assert c["guider_connected"] is False and c["guide_camera"] == "ZWO ASI120MM Mini"  # last known, shown grey
    assert [a for ev in m(NO_CAM, at(60)) for a in engine.process(ev)] == []  # first answer after reconnect


def test_watch_poll_sends_params_and_records_only_changes(tmp_path):
    sent, events = [], []

    async def scenario():
        answers = iter([WITH_CAM, WITH_CAM, WITH_CAM, NO_CAM])

        async def handle(reader, writer):
            while True:
                line = await reader.readline()
                if not line:
                    return
                req = json.loads(line)
                sent.append(req)
                if req["method"] == "get_connected":
                    try:
                        ans = next(answers)
                    except StopIteration:
                        writer.close()
                        return
                    writer.write((json.dumps({**ans, "id": req["id"]}) + "\r\n").encode())
                    await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AsiairRpcClient("127.0.0.1", events.append, port=port, channel=GUIDER, heartbeat_s=0.02,
                                 request_gap_s=0.01, retry_s=5, timeout_s=2, recorder=Recorder(tmp_path, "rec_"))
        task = asyncio.create_task(client.run())
        for _ in range(200):
            if any(isinstance(e, GuiderDisconnected) for e in events):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        server.close()

    asyncio.run(scenario())
    polls = [r for r in sent if r["method"] == "get_connected"]
    assert polls and all(r["params"] == [True] for r in polls)
    assert {r["method"] for r in sent} <= GUIDER.allowed
    recorded = "".join(p.read_text() for p in tmp_path.iterdir())
    assert recorded.count('"get_connected"') == 2  # with camera once, then the change


# Answers and events from the real ASIAIR on port 4400 (2026-11-20 15:14-15:16, mount sent home; site coordinates replaced).
RA_DEC = {"jsonrpc": "2.0", "method": "scope_get_ra_dec", "result": [12.910011, 89.0, 18.910227], "code": 0, "id": 3}
PIER = {"jsonrpc": "2.0", "method": "scope_get_pierside", "result": "pier_east", "code": 0, "id": 5}
TRACK = {"jsonrpc": "2.0", "method": "scope_get_track_state", "result": False, "code": 0, "id": 7}
LOCATION = {"jsonrpc": "2.0", "method": "scope_get_location", "result": [51.0, 10.0], "code": 0, "id": 9}
HOME_WORKING = {"Event": "ScopeHome", "Timestamp": "20749.04", "state": "working", "lapse_ms": 1070,
                "RA": 12.780359, "Dec": 89.0, "route": []}
HOME_DONE = {"Event": "ScopeHome", "Timestamp": "20754.43", "state": "complete", "lapse_ms": 6454,
             "RA": 12.877824, "Dec": 89.0, "route": []}


def test_live_mount_pose():
    engine = Engine([])
    m = GuiderMapper()
    for i, msg in enumerate([LOCATION, RA_DEC, PIER, TRACK]):
        for ev in m(msg, at(i)):
            engine.process(ev)
    pose = engine.state.pose(at(10))
    assert pose["source"] == "live" and pose["pier"] == "East" and pose["lat"] == 51.0
    assert pose["ha"] == pytest.approx(6.0002, abs=1e-3) and pose["dec"] == 89.0
    assert pose["time"] == at(1).isoformat()  # exactly as reported, not extrapolated
    # tracking off: the hour angle stays where it was reported
    assert engine.state.hour_angle(at(60)) == pytest.approx(6.0002, abs=1e-3)
    snap = engine.state.snapshot()
    assert snap["tracking"] is False and snap["tracking_since_is_change"] is False
    assert snap["controller"]["mount_pier"] == "East"


def test_scope_home_motion_updates_position():
    engine = Engine([])
    m = GuiderMapper()
    for ev in m(RA_DEC, at(0)):
        engine.process(ev)
    for ev in m(HOME_WORKING, at(2)):
        engine.process(ev)
    c = engine.state.controller
    assert c.mount_motion == {"kind": "Home", "state": "working", "time": at(2).isoformat()}
    assert c.mount_ra_h == 12.780359 and c.mount_pos_time == at(2)
    for ev in m(HOME_DONE, at(7)):
        engine.process(ev)
    assert c.mount_motion["state"] == "complete" and c.mount_ra_h == 12.877824


def test_scope_track_is_not_a_motion():
    (ev,) = GuiderMapper()({"Event": "ScopeTrack", "state": "off"}, at(0))
    assert type(ev).__name__ == "TrackingChanged"


def test_mount_home_rule():
    from airgus.events import SequenceStarted
    from airgus.rules import MountHomeRule
    engine = Engine([MountHomeRule()])
    m = GuiderMapper()
    quiet = [a for msg in (HOME_WORKING, HOME_DONE) for ev in m(msg, at(0)) for a in engine.process(ev)]
    assert [(a.title, a.severity) for a in quiet] == [("Montierung in Home-Position", Severity.INFO)]
    engine.process(SequenceStarted(time=at(100), source=Source.ASIAIR, target="IC 405"))
    (warn,) = [a for ev in m(HOME_WORKING, at(200)) for a in engine.process(ev)]
    assert warn.severity is Severity.WARNING and "IC 405" in warn.message


def test_scope_goto_is_mapped_like_home():
    goto = {"Event": "ScopeGoto", "Timestamp": "21048.44", "state": "working", "lapse_ms": 2039,
            "RA": 13.096696, "Dec": 87.102305, "route": []}
    (ev,) = GuiderMapper()(goto, at(0))
    assert (ev.kind, ev.state, ev.ra_h, ev.dec_deg) == ("Goto", "working", 13.096696, 87.102305)


def test_meridian_countdown_matches_app():
    """2026-11-20 15:51:50: RA 19.699708 h, LST 19.501203 h; the app showed "Meridian: 12m 29s" at 15:51:16."""
    engine = Engine([])
    m = GuiderMapper()
    t = datetime(2026, 11, 20, 15, 51, 50)
    ra_dec = {"jsonrpc": "2.0", "method": "scope_get_ra_dec", "result": [19.699708, 56.661719, 19.501203], "code": 0}
    for msg in (ra_dec, {**TRACK, "result": True}):
        for ev in m(msg, t):
            engine.process(ev)
    at_screenshot = engine.state.meridian_seconds(datetime(2026, 11, 20, 15, 51, 16))
    assert abs(at_screenshot - (12 * 60 + 29)) < 5
    assert engine.state.pose(t)["meridian_s"] == pytest.approx(712.6, abs=1)
    for ev in m(TRACK, t):  # tracking off: no countdown
        engine.process(ev)
    assert engine.state.meridian_seconds(t) is None

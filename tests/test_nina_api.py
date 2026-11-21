"""NINA live connection through the Advanced API plugin.

Messages follow the plugin's source and wiki (Advanced API 2.2.x): every WebSocket message is
{"Response": {"Event": ...}, "Error": "", "StatusCode": 200, "Success": true, "Type": "Socket"}.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from airgus.alerts import Severity
from airgus.engine import Engine
from airgus.events import (
    AutofocusCompleted,
    DeviceConnection,
    ExposureStarted,
    GuideFrame,
    ImageSaved,
    MeridianFlipStarted,
    MountMotion,
    MountStatus,
    NinaConnected,
    NinaDisconnected,
    SequenceEnded,
    SequenceStarted,
    SolveProgress,
    Source,
)
from airgus.nina_api import NinaApiClient, NinaRecordParser, map_event, map_mount_info
from airgus.parsers import parser_for

T0 = datetime(2026, 11, 21, 22, 0)


def msg(event, **fields):
    return {"Response": {"Event": event, **fields}, "Error": "", "StatusCode": 200, "Success": True, "Type": "Socket"}


IMAGE_SAVE = msg("IMAGE-SAVE", ImageStatistics={
    "ExposureTime": 300.0, "ImageType": "LIGHT", "Filter": "", "RmsText": "0.62 (0.41/0.47)", "Temperature": -10.0,
    "CameraName": "ZWO ASI2600MC Pro", "TargetName": "NGC 7635", "Gain": 100, "Offset": 50,
    "Date": "2026-11-21T22:05:00", "TelescopeName": "iOptron", "FocalLength": 600, "StDev": 12.3, "Mean": 512.0,
    "Median": 505.0, "Min": 0, "Max": 65535, "Stars": 1295, "HFR": 2.81, "HFRStDev": 0.4, "IsBayered": True,
    "Filename": "D:\\\\NGC 7635\\\\LIGHT\\\\NGC 7635-0052.fits"})


def test_map_events():
    (img,) = map_event(IMAGE_SAVE, T0)
    assert isinstance(img, ImageSaved) and img.live and img.source is Source.NINA
    assert (img.frame_type, img.duration_s, img.target, img.hfr, img.stars) == ("light", 300.0, "NGC 7635", 2.81, 1295)
    assert isinstance(map_event(msg("SEQUENCE-STARTING"), T0)[0], SequenceStarted)
    assert isinstance(map_event(msg("SEQUENCE-FINISHED"), T0)[0], SequenceEnded)
    assert isinstance(map_event(msg("MOUNT-BEFORE-FLIP"), T0)[0], MeridianFlipStarted)
    (park,) = map_event(msg("MOUNT-PARKED"), T0)
    assert isinstance(park, MountMotion) and park.kind == "Park"
    (af,) = map_event(msg("ERROR-AF"), T0)
    assert isinstance(af, AutofocusCompleted) and af.success is False
    (solve,) = map_event(msg("ERROR-PLATESOLVE"), T0)
    assert isinstance(solve, SolveProgress) and solve.state == "fail"
    (cam,) = map_event(msg("CAMERA-DISCONNECTED"), T0)
    assert isinstance(cam, DeviceConnection) and (cam.device, cam.connected) == ("camera", False)
    assert map_event(msg("FILTERWHEEL-CHANGED"), T0) == [] and map_event({"nonsense": 1}, T0) == []


def test_mount_info():
    info = {"Connected": True, "RightAscension": 23.2833, "Declination": 60.72, "SiderealTime": 23.05,
            "SideOfPier": "pierWest", "TrackingEnabled": True, "AtPark": False}
    (ms,) = map_mount_info(info, T0)
    assert isinstance(ms, MountStatus) and (ms.ra_h, ms.dec_deg, ms.lst_h, ms.pier_side, ms.tracking) == \
        (23.2833, 60.72, 23.05, "West", True)
    assert map_mount_info({"Connected": False}, T0) == []
    (partial,) = map_mount_info({"Connected": True, "TrackingEnabled": False}, T0)
    assert partial.ra_h is None and partial.tracking is False


def test_only_read_only_rest_paths():
    client = NinaApiClient("nina-pc", lambda ev: None)
    for path in ("/equipment/mount/park", "/equipment/mount/flip", "/equipment/mount/slew", "/sequence/start"):
        with pytest.raises(PermissionError):
            client.get(path)


def test_record_round_trip_and_file_name():
    lines = [json.dumps({"t": "2026-11-21T22:00:00.000", "conn": "up", "host": "nina-pc", "version": "2.2.13.0"}),
             json.dumps({"t": "2026-11-21T22:05:00.000", "msg": IMAGE_SAVE}),
             json.dumps({"t": "2026-11-21T22:05:01.000", "mount": {"Connected": True, "TrackingEnabled": True}}),
             json.dumps({"t": "2026-11-21T22:09:00.000", "conn": "down", "host": "nina-pc", "reason": "weg"})]
    p = parser_for("Airgus_NINA_2026-11-21.jsonl")
    assert isinstance(p, NinaRecordParser)
    evs = [e for line in lines for e in p.feed(line)]
    assert [type(e) for e in evs] == [NinaConnected, ImageSaved, MountStatus, NinaDisconnected]
    assert evs[0].version == "2.2.13.0"


def test_log_duplicates_dropped_while_live():
    engine = Engine([])
    log_seq = SequenceStarted(time=T0, source=Source.NINA, target="NGC 7635")
    assert engine.is_duplicate(log_seq) is False  # NINA live not connected
    engine.process(NinaConnected(time=T0, source=Source.NINA, live=True, host="nina-pc"))
    assert engine.is_duplicate(log_seq) is True
    assert engine.is_duplicate(ExposureStarted(time=T0, source=Source.NINA, index=1, duration_s=300)) is False


def test_offline_and_stall_alerts():
    from airgus.rules import CaptureStalledRule, ControllerOfflineRule

    engine = Engine([ControllerOfflineRule(), CaptureStalledRule()])
    for ev in (NinaConnected(time=T0, source=Source.NINA, live=True, host="nina-pc"),
               *map_event(msg("SEQUENCE-STARTING"), T0), *map_event(IMAGE_SAVE, T0 + timedelta(minutes=5))):
        engine.process(ev)
    alerts = []
    t = T0 + timedelta(minutes=5)
    while t < T0 + timedelta(minutes=30):  # guiding goes on, no new image: NINA hangs
        alerts += engine.process(GuideFrame(time=t, source=Source.PHD2, frame=0, ra_px=0, dec_px=0, ra_pulse_ms=0,
                                            dec_pulse_ms=0, snr=30, mass=1000, settling=False))
        t += timedelta(seconds=10)
    assert [a.title for a in alerts] == ["Aufnahme hängt"]  # 5 min exposure + 10 min grace after the save
    engine.process(NinaDisconnected(time=t, source=Source.NINA, live=True, host="nina-pc", reason="weg"))
    (offline,) = engine.tick(t + timedelta(seconds=90))
    assert offline.title == "NINA nicht erreichbar" and offline.severity is Severity.HIGH


def test_client_against_fake_nina(tmp_path):
    import websockets

    from airgus.asiair_rpc import Recorder

    events = []

    async def scenario():
        async def handler(ws):
            await ws.send(json.dumps(msg("SEQUENCE-STARTING")))
            await ws.send(json.dumps(IMAGE_SAVE))
            await ws.send("not json")
            await asyncio.sleep(0.05)
            await ws.close()

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = NinaApiClient("127.0.0.1", events.append, port=port, recorder=Recorder(tmp_path, "Airgus_NINA_"),
                                   retry_s=5)
            task = asyncio.create_task(client.run())
            for _ in range(100):
                if any(isinstance(e, NinaDisconnected) for e in events):
                    break
                await asyncio.sleep(0.02)
            task.cancel()

    asyncio.run(scenario())
    kinds = [type(e).__name__ for e in events]
    assert kinds[:3] == ["NinaConnected", "SequenceStarted", "ImageSaved"] and kinds[-1] == "NinaDisconnected"
    (rec,) = list(tmp_path.glob("Airgus_NINA_*.jsonl"))
    replayed = [e for line in rec.read_text().splitlines() for e in NinaRecordParser().feed(line)]
    assert [type(e).__name__ for e in replayed][:3] == ["NinaConnected", "SequenceStarted", "ImageSaved"]

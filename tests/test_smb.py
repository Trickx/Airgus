import time

import pytest

from airgus.events import GuideFrame
from airgus.fs import FileInfo, MultiFS, SmbConfig
from airgus.settings import SettingsStore
from airgus.sources import FolderTail

HEADER = """Guiding Begins at 2026-11-20 22:00:00
Pixel scale = 6.45 arc-sec/px, Binning = 1, Focal length = 120 mm
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode
"""
FRAME = "{n},{t}.0,\"Mount\",0.1,0.1,0.20,-0.10,0.2,0.0,100,W,0,,,,15000,40.0,0\n"


class MemoryFS:
    """Stands in for an SMB share; failing=True simulates a lost connection."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.mtime: dict[str, float] = {}
        self.failing = False

    def put(self, name, text, age_h=0.0):
        self.data[name] = self.data.get(name, b"") + text.encode()
        self.mtime[name] = time.time() - age_h * 3600

    def files(self):
        if self.failing:
            raise ConnectionError("share gone")
        return [FileInfo(n, n, len(d), self.mtime[n]) for n, d in self.data.items()]

    def read(self, path, offset):
        return self.data[path][offset:]

    def describe(self):
        return "memory"


def test_tail_over_share_follows_new_lines_and_skips_old_files():
    fs = MemoryFS()
    fs.put("PHD2_GuideLog_2026-11-05_181618.txt", HEADER + FRAME.format(n=1, t=1), age_h=300)
    fs.put("PHD2_GuideLog_2026-11-20_215900.txt", HEADER + FRAME.format(n=1, t=1))
    fs.put("Preview.jpg", "binary")
    tail = FolderTail(fs=fs, max_age_h=12)
    assert tail.poll() == []  # existing content only primes the parser
    assert set(tail._tails) == {"PHD2_GuideLog_2026-11-20_215900.txt"}  # old night ignored
    fs.put("PHD2_GuideLog_2026-11-20_215900.txt", FRAME.format(n=2, t=2) + "3,3.0,\"Mou")  # partial line
    (frame,) = tail.poll()
    assert isinstance(frame, GuideFrame) and frame.frame == 2
    fs.put("PHD2_GuideLog_2026-11-20_215900.txt", 'nt",0.1,0.1,0.3,0.1,0.2,0.0,100,W,0,,,,15000,41.0,0\n')
    (frame,) = tail.poll()
    assert frame.frame == 3 and frame.snr == 41.0


def test_tail_survives_share_outage():
    fs = MemoryFS()
    fs.put("PHD2_GuideLog_2026-11-20_215900.txt", HEADER)
    tail = FolderTail(fs=fs)
    tail.poll()
    fs.failing = True
    assert tail.poll() == [] and "share gone" in tail.error
    fs.failing = False
    fs.put("PHD2_GuideLog_2026-11-20_215900.txt", FRAME.format(n=1, t=1))
    assert len(tail.poll()) == 1 and tail.error is None


def test_unc_paths():
    c = SmbConfig("192.168.0.236", "TF Images", "ASIAIR/log")
    assert c.unc() == r"\\192.168.0.236\TF Images\ASIAIR\log"
    assert c.unc("x.txt") == r"\\192.168.0.236\TF Images\ASIAIR\log\x.txt"
    assert SmbConfig("nas", "logs", "/").unc() == r"\\nas\logs"
    assert c.guest and "Gast" in c.describe()


def test_smb_settings(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.get()["smb"]["shares"] == ["TF Images/ASIAIR/log", "EMMC Images/log"]
    assert [(c.share, c.path) for c in store.smb_configs()] == [("TF Images", "ASIAIR/log"), ("EMMC Images", "log")]
    store.update({"smb": {"username": "sven", "password": "geheim", "server": "nina-pc",
                          "shares": [" Logs\\NINA ", "", "Logs/NINA"]}})
    assert store.get()["smb"]["shares"] == ["Logs/NINA"]
    pub = store.public()["smb"]
    assert pub["password_set"] is True and "password" not in pub
    store.update({"smb": {"password": ""}})  # empty keeps the stored password
    assert store.smb_configs()[0].password == "geheim"
    for bad in ({"server": "//nas"}, {"shares": ["a/../b"]}, {"shares": [""]}, {"max_age_h": 0}):
        with pytest.raises(ValueError):
            store.update({"smb": bad})


def test_legacy_single_share_is_migrated(tmp_path):
    (tmp_path / "settings.json").write_text(
        '{"smb": {"server": "asiair.local", "share": "TF Images", "path": "ASIAIR/log"}}')
    store = SettingsStore(tmp_path)
    assert store.get()["smb"]["shares"] == ["TF Images/ASIAIR/log", "EMMC Images/log"]
    assert "share" not in store.get()["smb"]


def test_one_missing_share_does_not_stop_the_others():
    good, bad = MemoryFS(), MemoryFS()
    good.put("PHD2_GuideLog_2026-11-20_215900.txt", HEADER)
    bad.failing = True
    good.owns = lambda path: True
    bad.owns = lambda path: False
    bad.describe = lambda: "TF Images"
    multi = MultiFS([bad, good])
    tail = FolderTail(fs=multi)
    tail.poll()
    good.put("PHD2_GuideLog_2026-11-20_215900.txt", FRAME.format(n=1, t=1))
    assert len(tail.poll()) == 1
    assert "TF Images" in tail.error  # shown in the UI, but monitoring continues
    good.failing = True
    assert tail.poll() == [] and "share gone" in tail.error

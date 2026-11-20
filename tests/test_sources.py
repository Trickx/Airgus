from pathlib import Path

from airgus import config
from airgus.events import MeridianFlipStarted, StarLost
from airgus.rules import RULE_CLASSES, default_rules
from airgus.sources import FolderTail

ROOT = Path(__file__).resolve().parent.parent


def test_tail_reads_only_new_complete_lines(tmp_path):
    log = tmp_path / "Autorun_Log_2026-11-15_010000.txt"
    log.write_text("2026/11/15 01:00:00 [Guide] Guide star lost\n")
    tail = FolderTail(tmp_path)
    assert tail.poll() == []  # existing content is skipped at startup

    with log.open("a") as fh:
        fh.write("2026/11/15 01:59:25 [Meridian Flip|Begin] Wait\n2026/11/15 02:00:00 [Guide] Guide st")
    events = tail.poll()
    assert [type(e) for e in events] == [MeridianFlipStarted]

    with log.open("a") as fh:
        fh.write("ar lost\n")
    assert [type(e) for e in tail.poll()] == [StarLost]


def test_tail_picks_up_new_files(tmp_path):
    tail = FolderTail(tmp_path)
    tail.poll()
    (tmp_path / "Autorun_Log_2026-11-15_020000.txt").write_text("2026/11/15 02:00:00 [Guide] Guide star lost\n")
    assert [type(e) for e in tail.poll()] == [StarLost]


def test_example_config_builds_all_rules():
    conf = config.load(ROOT / "airgus.example.toml")
    assert len(default_rules(conf.rules)) == len(RULE_CLASSES)

"""Line parsers that turn system-specific log text into domain events."""

from __future__ import annotations

from pathlib import PurePath
from typing import Protocol

from ..events import Event
from .asiair import AsiairAutorunParser
from .phd2 import Phd2Parser


class LineParser(Protocol):
    """Stateful parser: feed one line at a time, get zero or more events back."""

    def feed(self, line: str) -> list[Event]: ...


def parser_for(path: str | PurePath) -> LineParser | None:
    """Pick a parser by file name, or None if the file is not a known log."""
    name = PurePath(path).name
    if name.startswith("PHD2_GuideLog_") and name.endswith(".txt"):
        return Phd2Parser()
    if name.startswith("Autorun_Log_") and name.endswith(".txt") and not name.endswith("_CHN.txt"):
        return AsiairAutorunParser()
    if name.startswith("Airgus_ASIAIR") and name.endswith(".jsonl"):
        from ..asiair_rpc import record_parser_for
        return record_parser_for(name)
    return None


__all__ = ["LineParser", "Phd2Parser", "AsiairAutorunParser", "parser_for"]

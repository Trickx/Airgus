"""Log sources: replay of finished logs and live tailing of a folder.

The live source polls (no inotify) so it also works on SMB/CIFS mounts.
"""

from __future__ import annotations

import heapq
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .events import Event
from .fs import FileInfo, FileSystem, LocalFS
from .parsers import LineParser, parser_for

log = logging.getLogger(__name__)
ENCODING = "utf-8"


def read_events(path: Path) -> list[Event]:
    parser = parser_for(path)
    if parser is None:
        return []
    events: list[Event] = []
    with path.open(encoding=ENCODING, errors="replace") as fh:
        for line in fh:
            events += parser.feed(line)
    return events


def log_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and parser_for(p) is not None)


def replay(folder: Path) -> Iterator[Event]:
    """All events of all known logs below `folder`, merged in time order."""
    streams = [read_events(p) for p in log_files(folder)]
    # heapq.merge is stable per stream; key on time only.
    yield from heapq.merge(*streams, key=lambda e: e.time)


@dataclass
class _Tail:
    parser: LineParser
    offset: int = 0
    partial: str = ""


@dataclass
class FolderTail:
    """Polls a folder (local or SMB) for new log files and new lines in existing ones."""

    folder: Path | None = None
    from_start: bool = False  # False: skip what is already in the files on startup
    fs: FileSystem | None = None
    max_age_h: float | None = None  # ignore files not modified for this long (finished nights)
    _tails: dict[str, _Tail] = field(default_factory=dict)
    _initialised: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        if self.fs is None:
            assert self.folder is not None
            self.fs = LocalFS(Path(self.folder))

    def _candidates(self) -> list[FileInfo]:
        assert self.fs is not None
        files = [f for f in self.fs.files() if parser_for(f.name) is not None]
        if self.max_age_h is not None:
            cutoff = time.time() - self.max_age_h * 3600
            files = [f for f in files if f.mtime >= cutoff or f.path in self._tails]
        return sorted(files, key=lambda f: f.name)

    def poll(self) -> list[Event]:
        events: list[Event] = []
        try:
            files = self._candidates()
            partial = getattr(self.fs, "errors", None)
            self.error = "; ".join(f"{k}: {v}" for k, v in partial.items()) if partial else None
        except Exception as exc:  # network share gone, permissions, ...
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("cannot list %s: %s", self.fs.describe() if self.fs else self.folder, exc)
            return events
        for info in files:
            tail = self._tails.get(info.path)
            if tail is None:
                parser = parser_for(info.name)
                assert parser is not None
                tail = self._tails[info.path] = _Tail(parser)
                if not self._initialised and not self.from_start:
                    # Existing file at startup: feed it silently so parser state
                    # (guiding header, session start) is correct, but drop events.
                    self._read(info, tail)
                    continue
            events += self._read(info, tail)
        self._initialised = True
        events.sort(key=lambda e: e.time)
        return events

    def _read(self, info: FileInfo, tail: _Tail) -> list[Event]:
        assert self.fs is not None
        if info.size < tail.offset:  # truncated or replaced
            tail.offset, tail.partial = 0, ""
        if info.size == tail.offset:
            return []
        try:
            chunk = self.fs.read(info.path, tail.offset)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("cannot read %s: %s", info.path, exc)
            return []
        tail.offset += len(chunk)
        text = tail.partial + chunk.decode(ENCODING, errors="replace")
        lines = text.split("\n")
        tail.partial = lines.pop()  # incomplete last line, wait for the rest
        events: list[Event] = []
        for line in lines:
            events += tail.parser.feed(line)
        return events

    def follow(self, interval_s: float = 5.0) -> Iterator[list[Event]]:
        while True:
            yield self.poll()
            time.sleep(interval_s)

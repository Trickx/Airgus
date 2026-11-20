"""File access for the log tail: a local folder or an SMB share read directly over the network.

The SMB client (``smbprotocol``) needs no mount, no privileges and no OS support, so it also
works unprivileged in a Docker container. The ASIAIR exposes its storage as guest-only
Samba shares ("TF Images", "EMMC Images", "Udisk Images"); logs are in ``ASIAIR/log`` on the
storage selected in the app (2026-11, firmware 14.39).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

log = logging.getLogger(__name__)

MAX_DEPTH = 3


@dataclass(frozen=True)
class FileInfo:
    path: str  # key used for read(); unique per file
    name: str
    size: int
    mtime: float


class FileSystem(Protocol):
    def files(self) -> list[FileInfo]: ...
    def read(self, path: str, offset: int) -> bytes: ...
    def describe(self) -> str: ...


class LocalFS:
    def __init__(self, root: Path) -> None:
        self.root = root

    def files(self) -> list[FileInfo]:
        out = []
        for p in self.root.rglob("*"):
            if p.is_file():
                st = p.stat()
                out.append(FileInfo(str(p), p.name, st.st_size, st.st_mtime))
        return out

    def read(self, path: str, offset: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read()

    def describe(self) -> str:
        return str(self.root)


class MultiFS:
    """Several file systems (e.g. the ASIAIR's SD card and internal storage) seen as one.

    A source that fails (no SD card, share offline) is skipped; the others keep working.
    Only if all sources fail does ``files()`` raise.
    """

    def __init__(self, sources: list) -> None:
        self.sources = sources
        self.errors: dict[str, str] = {}

    def files(self) -> list[FileInfo]:
        out: list[FileInfo] = []
        self.errors = {}
        for fs in self.sources:
            try:
                out += fs.files()
            except Exception as exc:
                self.errors[fs.describe()] = f"{type(exc).__name__}: {exc}"
        if self.sources and len(self.errors) == len(self.sources):
            raise ConnectionError("; ".join(f"{k}: {v}" for k, v in self.errors.items()))
        return out

    def read(self, path: str, offset: int) -> bytes:
        for fs in self.sources:
            if fs.owns(path):
                return fs.read(path, offset)
        raise FileNotFoundError(path)

    def describe(self) -> str:
        return " + ".join(fs.describe() for fs in self.sources)


@dataclass(frozen=True)
class SmbConfig:
    server: str
    share: str
    path: str = ""
    username: str = ""
    password: str = ""
    port: int = 445

    @property
    def guest(self) -> bool:
        return not self.username

    def unc(self, *parts: str) -> str:
        rel = [p for p in PurePosixPath(self.path.replace("\\", "/")).parts if p not in ("/", "")]
        return "\\\\" + "\\".join([self.server, self.share, *rel, *parts])

    def describe(self) -> str:
        user = "Gast" if self.guest else self.username
        return f"smb://{self.server}/{self.share}/{self.path.strip('/')} ({user})"


_session_lock = threading.Lock()


class SmbFS:
    """Reads files from an SMB share. Reconnects transparently after network errors."""

    def __init__(self, config: SmbConfig, timeout_s: float = 15.0) -> None:
        self.config = config
        self.timeout_s = timeout_s
        self._registered = False

    def _session(self):
        import smbclient

        if not self._registered:
            c = self.config
            with _session_lock:
                if c.guest:
                    # Samba guest sessions cannot sign or encrypt; the ASIAIR only offers guest access.
                    smbclient.ClientConfig(require_secure_negotiate=False)
                    smbclient.register_session(c.server, username="guest", password="", port=c.port,
                                               auth_protocol="ntlm", require_signing=False, encrypt=False,
                                               connection_timeout=int(self.timeout_s))
                else:
                    smbclient.register_session(c.server, username=c.username, password=c.password, port=c.port,
                                               connection_timeout=int(self.timeout_s))
            self._registered = True
        return smbclient

    def _reset(self) -> None:
        self._registered = False
        try:
            import smbclient

            smbclient.reset_connection_cache()
        except Exception:  # the connection is gone anyway
            pass

    def _call(self, fn):
        try:
            return fn(self._session())
        except Exception:
            self._reset()  # next poll starts with a fresh connection
            raise

    def files(self) -> list[FileInfo]:
        def walk(smb, unc: str, depth: int, out: list[FileInfo]) -> None:
            for e in smb.scandir(unc, port=self.config.port):
                full = unc + "\\" + e.name
                if e.is_dir():
                    if depth < MAX_DEPTH:
                        walk(smb, full, depth + 1, out)
                else:
                    # Size and time come with the directory listing; stat() would cost a round trip per file.
                    info = e.smb_info
                    out.append(FileInfo(full, e.name, int(info.end_of_file), info.last_write_time.timestamp()))

        def run(smb):
            out: list[FileInfo] = []
            walk(smb, self.config.unc(), 0, out)
            return out

        return self._call(run)

    def read(self, path: str, offset: int) -> bytes:
        def run(smb):
            with smb.open_file(path, mode="rb", port=self.config.port, share_access="rwd") as fh:
                fh.seek(offset)
                return fh.read()

        return self._call(run)

    def owns(self, path: str) -> bool:
        return path.lower().startswith(self.config.unc().lower() + "\\")

    def describe(self) -> str:
        return self.config.describe()

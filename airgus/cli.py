"""Command line: `airgus replay <folder>` and `airgus watch <folder>`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, config as cfg
from .alerts import Severity
from .engine import Engine
from .notify import ConsoleNotifier, PushoverNotifier
from .rules import default_rules
from .sources import FolderTail, replay


def _engine(conf: cfg.Config, push: bool) -> Engine:
    notifiers: list = [ConsoleNotifier()]
    engine = Engine(default_rules(conf.rules), notifiers)
    if push:
        if not conf.pushover_enabled:
            sys.exit("Pushover requested but AIRGUS_PUSHOVER_TOKEN / AIRGUS_PUSHOVER_USER are not set")
        po = PushoverNotifier(conf.pushover_token, conf.pushover_user, conf.pushover_device)  # type: ignore[arg-type]
        engine.notifiers.append(_MinSeverity(po, conf.pushover_min_severity))
    return engine


class _MinSeverity:
    def __init__(self, inner, min_severity: Severity) -> None:
        self.inner, self.min_severity = inner, min_severity

    def send(self, alert) -> None:
        if alert.severity >= self.min_severity:
            self.inner.send(alert)


def cmd_replay(args: argparse.Namespace) -> int:
    conf = cfg.load(args.config)
    engine = _engine(conf, args.pushover)
    count = 0
    for ev in replay(args.folder):
        engine.process(ev)
        count += 1
    print(f"-- {count} events, {len(engine.alerts)} alerts", file=sys.stderr)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    conf = cfg.load(args.config)
    engine = _engine(conf, args.pushover)
    tail = FolderTail(args.folder, from_start=args.from_start)
    logging.info("watching %s every %.0f s", args.folder, args.interval)
    try:
        for events in tail.follow(args.interval):
            for ev in events:
                engine.process(ev)
            engine.tick(datetime.now())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    args.data.mkdir(parents=True, exist_ok=True)
    app = create_app(args.data, args.logs)
    print(f"Airgus läuft auf http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f"  (Logs: {args.logs.resolve()}, Daten: {args.data.resolve()})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _add_serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default=os.environ.get("AIRGUS_HOST", "127.0.0.1"),
                   help="bind address (0.0.0.0 for access from the network)")
    p.add_argument("--port", type=int, default=int(os.environ.get("AIRGUS_PORT", "8080")))
    p.add_argument("--logs", type=Path, default=Path(os.environ.get("AIRGUS_LOGS", "logs")),
                   help="folder that contains the log folders (replay and live)")
    p.add_argument("--data", type=Path, default=Path(os.environ.get("AIRGUS_DATA", "data")),
                   help="folder for settings")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="airgus", description="Live log monitor for ASIAIR and NINA. "
                                "Without a command the web UI is started.")
    p.add_argument("--version", action="version", version=f"airgus {__version__}")
    p.add_argument("-c", "--config", type=Path, help="TOML config file (replay/watch only)")
    p.add_argument("-v", "--verbose", action="store_true")
    _add_serve_args(p)
    p.set_defaults(func=cmd_serve)
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="start the web UI (default)")
    _add_serve_args(s)
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("replay", help="run the rules over finished logs")
    r.add_argument("folder", type=Path)
    r.add_argument("--pushover", action="store_true", help="also send alerts to Pushover")
    r.set_defaults(func=cmd_replay)

    w = sub.add_parser("watch", help="follow a log folder live")
    w.add_argument("folder", type=Path)
    w.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    w.add_argument("--from-start", action="store_true", help="also evaluate lines already in the files")
    w.add_argument("--pushover", action="store_true", help="send alerts to Pushover")
    w.set_defaults(func=cmd_watch)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The SMB library logs every request at INFO; with polling every few seconds that floods the console.
    for noisy in ("smbprotocol", "smbclient", "spnego"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

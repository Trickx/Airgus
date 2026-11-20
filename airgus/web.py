"""Web UI and JSON API."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .rules import rule_schema
from .runtime import Monitor
from .settings import SettingsStore

STATIC = Path(str(files("airgus") / "static"))
KEEPALIVE_S = 15.0


class RunRequest(BaseModel):
    mode: str = Field(pattern="^(replay|live)$")
    location: str = Field(default="local", pattern="^(local|smb)$")
    folder: str = ""
    speed: float = Field(default=60.0, ge=0)
    interval_s: float = Field(default=5.0, ge=1)
    from_start: bool = False
    pushover: bool = False


class SpeedRequest(BaseModel):
    speed: float = Field(ge=0)


def create_app(data_dir: Path, logs_root: Path) -> FastAPI:
    store = SettingsStore(data_dir)
    monitor = Monitor(store, logs_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await monitor.startup()
        yield
        await monitor.shutdown()

    app = FastAPI(title="Airgus", version=__version__, lifespan=lifespan)
    app.state.monitor = monitor
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # Always revalidate (cheap 304 via ETag) so a new Airgus version is never mixed with a cached UI.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def bad_request(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    # --- state ---------------------------------------------------------------

    @app.get("/api/status")
    def status():
        return monitor.snapshot()

    @app.get("/api/alerts")
    def alerts():
        return monitor.alerts()

    @app.get("/api/frames")
    def frames():
        return monitor.frames()

    @app.get("/api/folders")
    def folders():
        return {"root": str(monitor.logs_root), "folders": monitor.folders()}

    @app.get("/api/stream")
    async def stream(request: Request):
        queue = monitor.subscribe()

        async def gen():
            try:
                yield _sse("status", monitor.snapshot())
                while True:
                    try:
                        kind, data = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                        yield _sse(kind, data)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": keepalive\n\n"
            finally:
                monitor.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --- run control ---------------------------------------------------------

    @app.post("/api/run")
    async def run(req: RunRequest):
        try:
            await monitor.start(req.mode, req.folder, req.speed, req.interval_s, req.from_start, req.pushover,
                                req.location)
        except ValueError as exc:
            raise bad_request(exc)
        store.update({"source": req.model_dump(exclude={"pushover"})})
        return monitor.snapshot()

    @app.post("/api/run/stop")
    async def stop():
        await monitor.stop()
        return monitor.snapshot()

    @app.post("/api/run/pause")
    def pause():
        monitor.pause()
        return monitor.snapshot()

    @app.post("/api/run/resume")
    def resume():
        monitor.resume()
        return monitor.snapshot()

    @app.post("/api/run/speed")
    def speed(req: SpeedRequest):
        monitor.set_speed(req.speed)
        return monitor.snapshot()

    # --- settings ------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        return store.public()

    @app.put("/api/settings")
    def put_settings(patch: dict):
        try:
            return store.update(patch)
        except (ValueError, KeyError) as exc:
            raise bad_request(exc)

    @app.get("/api/rules")
    def rules():
        return rule_schema()

    @app.post("/api/smb/test")
    async def smb_test():
        try:
            return await monitor.test_smb()
        except Exception as exc:
            raise bad_request(exc)

    @app.post("/api/pushover/test")
    async def pushover_test():
        try:
            await monitor.send_test_push()
        except Exception as exc:
            raise bad_request(exc)
        return {"ok": True}

    return app


def _sse(kind: str, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

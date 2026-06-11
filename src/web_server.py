"""
FastAPI web server — runs as a daemon thread alongside the lighting engine.
Provides a browser UI for show control, status monitoring, and config editing.
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Pydantic models for the API
# ---------------------------------------------------------------------------

class ArtNetConfig(BaseModel):
    host: str
    port: int = 6454
    universe: int = 0


class FixtureConfig(BaseModel):
    name: str
    dmx_start: int


class OpusConfig(BaseModel):
    local_ip: str = ""
    beat_port: int = 50001
    status_port: int = 50002
    announce_port: int = 50000


class ShowConfig(BaseModel):
    fallback_bpm: float = 128.0
    active_show: str = "beat_strobe"
    brightness: float = 1.0
    transition: float = 0.3


class FullConfig(BaseModel):
    artnet: ArtNetConfig
    fixtures: list[FixtureConfig]
    opus_quad: OpusConfig
    show: ShowConfig


class ShowSwitch(BaseModel):
    name: str


class BrightnessUpdate(BaseModel):
    value: float

    @field_validator("value")
    @classmethod
    def clamp(cls, v):
        return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(engine, opus, config_path: str) -> FastAPI:
    app = FastAPI(title="OpusQuad Lighting")

    # Serve the single-page UI
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    # -----------------------------------------------------------------------
    # Status SSE stream — 2 Hz, clients reconnect automatically
    # -----------------------------------------------------------------------
    @app.get("/events")
    async def events():
        async def generator() -> AsyncGenerator[str, None]:
            while True:
                try:
                    data = json.dumps(engine.status_snapshot())
                    yield f"data: {data}\n\n"
                except Exception as e:
                    log.error("SSE error: %s", e)
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # -----------------------------------------------------------------------
    # Config
    # -----------------------------------------------------------------------
    @app.get("/api/config")
    async def get_config():
        with open(config_path) as f:
            return yaml.safe_load(f)

    @app.post("/api/config")
    async def save_config(cfg: FullConfig):
        data = cfg.model_dump()
        _write_config(config_path, data)
        _hot_reload(engine, opus, cfg, config_path)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Show & brightness — quick controls without a full config save
    # -----------------------------------------------------------------------
    @app.post("/api/show")
    async def set_show(body: ShowSwitch):
        from .shows import ALL_SHOWS
        if body.name not in ALL_SHOWS:
            raise HTTPException(400, f"Unknown show: {body.name}")
        engine.switch_show(body.name)
        _patch_config(config_path, ["show", "active_show"], body.name)
        return {"ok": True}

    @app.post("/api/brightness")
    async def set_brightness(body: BrightnessUpdate):
        engine.set_brightness(body.value)
        _patch_config(config_path, ["show", "brightness"], body.value)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Art-Net node discovery
    # -----------------------------------------------------------------------
    @app.post("/api/discover")
    async def discover():
        loop = asyncio.get_event_loop()
        import sys
        sys.path.insert(0, str(Path(config_path).parent))
        import discover as disc
        nodes = await loop.run_in_executor(None, lambda: disc.discover(timeout=3.0))
        return {"nodes": nodes}

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)


def _patch_config(path: str, keys: list[str], value) -> None:
    """Update a single nested key in the YAML config atomically."""
    with open(path) as f:
        data = yaml.safe_load(f)
    node = data
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    _write_config(path, data)


def _hot_reload(engine, opus, cfg: FullConfig, config_path: str) -> None:
    """Apply changed config values to the live engine without restarting."""
    from .artnet_sender import ArtNetSender
    from .fixture import SlimPar56

    # Art-Net target changed
    current_artnet = engine._artnet
    if cfg.artnet.host != current_artnet._host or cfg.artnet.universe != current_artnet._universe:
        new_artnet = ArtNetSender(
            host=cfg.artnet.host,
            universe=cfg.artnet.universe,
            port=cfg.artnet.port,
        )
        engine.reload_artnet(new_artnet)
        log.info("Art-Net reloaded → %s universe %d", cfg.artnet.host, cfg.artnet.universe)

    # Fixtures changed
    new_fixtures = [SlimPar56(name=f.name, dmx_start=f.dmx_start) for f in cfg.fixtures]
    engine.reload_fixtures(new_fixtures)

    # Show / brightness
    engine.set_brightness(cfg.show.brightness)
    engine.switch_show(cfg.show.active_show)
    engine.fallback_bpm = cfg.show.fallback_bpm

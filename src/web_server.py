"""
FastAPI web server for OpusQuad Lighting.
Provides the browser UI, SSE live status, and all control/config APIs.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ArtNetConfig(BaseModel):
    host: str; port: int = 6454; universe: int = 0

class SACNConfig(BaseModel):
    enabled: bool = False; universe: int = 1

class FixtureCfg(BaseModel):
    name: str; profile: str = "chauvet_slimpar56_7ch"
    dmx_start: int; universe: int = 0; group: str = "main"

class OpusCfg(BaseModel):
    local_ip: str = ""; beat_port: int = 50001
    status_port: int = 50002; announce_port: int = 50000

class AudioCfg(BaseModel):
    enabled: bool = False; device_index: Optional[int] = None
    channels: int = 2; samplerate: int = 48000
    blocksize: int = 1024; input_channel: int = 0

class MidiCfg(BaseModel):
    enabled: bool = False; port_name: Optional[str] = None

class OscCfg(BaseModel):
    enabled: bool = False; host: str = "0.0.0.0"; port: int = 8000

class DmxInputCfg(BaseModel):
    enabled: bool = False; universe: int = 2

class ShowCfg(BaseModel):
    fallback_bpm: float = 128.0; active_show: str = "beat_strobe"
    brightness: float = 1.0; transition_beats: float = 2.0
    auto_select_by_bpm: bool = False; strobe_max_hz: float = 10.0
    blackout: bool = False

class FullConfig(BaseModel):
    artnet: ArtNetConfig
    sacn: SACNConfig = SACNConfig()
    fixtures: list[FixtureCfg]
    opus_quad: OpusCfg = OpusCfg()
    audio: AudioCfg = AudioCfg()
    midi: MidiCfg = MidiCfg()
    osc: OscCfg = OscCfg()
    dmx_input: DmxInputCfg = DmxInputCfg()
    show: ShowCfg = ShowCfg()
    profiles_dir: str = "profiles"
    data_dir: str = "data"

class ShowSwitch(BaseModel):
    name: str

class BrightnessUpdate(BaseModel):
    value: float
    @field_validator("value")
    @classmethod
    def clamp(cls, v): return max(0.0, min(1.0, v))

class TapTempo(BaseModel):
    pass

class SceneSave(BaseModel):
    name: str
    fade_time: float = 0.0

class ScenePlay(BaseModel):
    name: str

class TrackSettingUpdate(BaseModel):
    track_id: str; show_name: str; brightness: float = 1.0

class ProfileOp(BaseModel):
    name: str

class ColorOverride(BaseModel):
    r: int = 0; g: int = 0; b: int = 0
    clear: bool = False


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(engine, opus, config_path: str,
               scene_manager=None, track_db=None, profile_manager=None) -> FastAPI:
    app = FastAPI(title="OpusQuad Lighting")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    # -----------------------------------------------------------------------
    # SSE — 2 Hz live status
    # -----------------------------------------------------------------------
    @app.get("/events")
    async def events():
        async def gen() -> AsyncGenerator[str, None]:
            while True:
                try:
                    yield f"data: {json.dumps(engine.status_snapshot())}\n\n"
                except Exception as e:
                    log.error("SSE error: %s", e)
                await asyncio.sleep(0.5)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -----------------------------------------------------------------------
    # Config
    # -----------------------------------------------------------------------
    @app.get("/api/config")
    async def get_config():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    @app.post("/api/config")
    async def save_config(cfg: FullConfig):
        _write_config(config_path, cfg.model_dump())
        _hot_reload(engine, cfg)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Show / brightness / blackout / tap tempo
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
        return {"ok": True, "brightness": body.value}

    @app.post("/api/blackout")
    async def toggle_blackout():
        engine.toggle_blackout()
        return {"ok": True, "blackout": engine.blackout}

    @app.post("/api/tap")
    async def tap_tempo():
        bpm = engine.tap_tempo()
        return {"ok": True, "bpm": bpm}

    @app.post("/api/color_override")
    async def color_override(body: ColorOverride):
        if body.clear:
            engine.clear_color_override()
            return {"ok": True, "active": False}
        r = max(0, min(255, body.r))
        g = max(0, min(255, body.g))
        b = max(0, min(255, body.b))
        engine.set_color_override(r, g, b)
        return {"ok": True, "active": True, "r": r, "g": g, "b": b}

    @app.post("/api/next_show")
    async def next_show():
        engine.next_show()
        return {"ok": True, "show": engine.show_name}

    @app.post("/api/prev_show")
    async def prev_show():
        engine.prev_show()
        return {"ok": True, "show": engine.show_name}

    # -----------------------------------------------------------------------
    # Audio devices
    # -----------------------------------------------------------------------
    @app.get("/api/audio/devices")
    async def list_audio_devices():
        try:
            from .audio_analyzer import list_audio_devices as _list
            return {"devices": _list()}
        except Exception as e:
            return {"devices": [], "error": str(e)}

    # -----------------------------------------------------------------------
    # MIDI ports
    # -----------------------------------------------------------------------
    @app.get("/api/midi/ports")
    async def list_midi_ports():
        try:
            from .midi_controller import list_midi_ports as _list
            return {"ports": _list()}
        except Exception as e:
            return {"ports": [], "error": str(e)}

    # -----------------------------------------------------------------------
    # Scenes
    # -----------------------------------------------------------------------
    @app.get("/api/scenes")
    async def list_scenes():
        if not scene_manager:
            return {"scenes": []}
        scenes = scene_manager.list_scenes()
        return {"scenes": [{"id": s.id, "name": s.name, "fade_time": s.fade_time,
                             "fixture_count": len(s.fixture_states)} for s in scenes]}

    @app.post("/api/scenes/capture")
    async def capture_scene(body: SceneSave):
        if not scene_manager:
            raise HTTPException(503, "Scene manager not available")
        scene = scene_manager.capture_scene(body.name, engine._fixtures)
        scene.fade_time = body.fade_time
        scene_manager.save_scene(scene)
        return {"ok": True, "id": scene.id}

    @app.post("/api/scenes/play")
    async def play_scene(body: ScenePlay):
        if not scene_manager:
            raise HTTPException(503, "Scene manager not available")
        scene = scene_manager.get_scene(body.name)
        if not scene:
            raise HTTPException(404, f"Scene '{body.name}' not found")
        engine.play_scene(scene)
        return {"ok": True}

    @app.post("/api/scenes/stop")
    async def stop_scene():
        engine.stop_scene()
        return {"ok": True}

    @app.delete("/api/scenes/{name}")
    async def delete_scene(name: str):
        if not scene_manager:
            raise HTTPException(503, "Scene manager not available")
        scene_manager.delete_scene(name)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Tracks
    # -----------------------------------------------------------------------
    @app.get("/api/tracks")
    async def list_tracks():
        if not track_db:
            return {"tracks": []}
        return {"tracks": track_db.list_tracks()}

    @app.post("/api/tracks/setting")
    async def set_track_setting(body: TrackSettingUpdate):
        if not track_db:
            raise HTTPException(503, "Track DB not available")
        track_db.set_track_show(body.track_id, body.show_name, body.brightness)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Profiles
    # -----------------------------------------------------------------------
    @app.get("/api/profiles")
    async def list_profiles():
        if not profile_manager:
            return {"profiles": []}
        return {"profiles": profile_manager.list_profiles()}

    @app.post("/api/profiles/save")
    async def save_profile(body: ProfileOp):
        if not profile_manager:
            raise HTTPException(503, "Profile manager not available")
        profile_manager.export_current(config_path, body.name)
        return {"ok": True}

    @app.post("/api/profiles/load")
    async def load_profile(body: ProfileOp):
        if not profile_manager:
            raise HTTPException(503, "Profile manager not available")
        cfg_data = profile_manager.load_profile(body.name)
        _write_config(config_path, cfg_data)
        # Rebuild engine from loaded config (basic fields)
        try:
            cfg = FullConfig(**cfg_data)
            _hot_reload(engine, cfg)
        except Exception as e:
            log.warning("Hot reload after profile load: %s", e)
        return {"ok": True}

    @app.delete("/api/profiles/{name}")
    async def delete_profile(name: str):
        if not profile_manager:
            raise HTTPException(503, "Profile manager not available")
        profile_manager.delete_profile(name)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Fixture profiles (built-in + OFL)
    # -----------------------------------------------------------------------
    @app.get("/api/fixture_profiles")
    async def list_fixture_profiles():
        from .fixture_library import BUILTIN_PROFILES
        profiles = [{"key": k, "name": v.name, "manufacturer": v.manufacturer,
                     "channels": v.channel_count, "tags": v.tags}
                    for k, v in BUILTIN_PROFILES.items()]
        # Also load from fixtures/ directory
        try:
            from .fixture_import import import_ofl_directory
            ofl_dir = Path(config_path).parent / "fixtures"
            if ofl_dir.exists():
                for p in import_ofl_directory(str(ofl_dir)):
                    key = f"{p.manufacturer}_{p.name}".lower().replace(" ", "_")
                    profiles.append({"key": key, "name": p.name,
                                     "manufacturer": p.manufacturer,
                                     "channels": p.channel_count, "tags": p.tags})
        except Exception as e:
            log.warning("OFL load: %s", e)
        return {"profiles": profiles}

    # -----------------------------------------------------------------------
    # Graceful shutdown
    # -----------------------------------------------------------------------
    @app.post("/api/shutdown")
    async def shutdown_server():
        fn = getattr(app.state, "shutdown_fn", None)
        if fn:
            import threading
            threading.Thread(target=fn, daemon=True).start()
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Art-Net discovery
    # -----------------------------------------------------------------------
    @app.post("/api/discover")
    async def discover():
        loop = asyncio.get_event_loop()
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
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)


def _patch_config(path: str, keys: list[str], value) -> None:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    _write_config(path, data)


def _hot_reload(engine, cfg: FullConfig) -> None:
    from .artnet_sender import ArtNetSender
    from .fixture_library import FixtureInstance, BUILTIN_PROFILES

    cur = engine._artnet
    if cfg.artnet.host != cur._host or cfg.artnet.universe != cur._universe:
        engine.reload_artnet(ArtNetSender(cfg.artnet.host, cfg.artnet.universe, cfg.artnet.port))

    new_fixtures = []
    for fc in cfg.fixtures:
        profile = BUILTIN_PROFILES.get(fc.profile)
        if profile is None:
            profile = BUILTIN_PROFILES["chauvet_slimpar56_7ch"]
        new_fixtures.append(FixtureInstance(
            name=fc.name, profile=profile, dmx_start=fc.dmx_start,
            universe=fc.universe, group=fc.group,
        ))
    engine.reload_fixtures(new_fixtures)
    engine.set_brightness(cfg.show.brightness)
    engine.switch_show(cfg.show.active_show)
    engine.fallback_bpm = cfg.show.fallback_bpm
    engine.blackout = cfg.show.blackout

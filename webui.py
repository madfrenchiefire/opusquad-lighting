#!/usr/bin/env python3
"""
OpusQuad Lighting — full-featured web UI launcher.

Usage:
    python webui.py                  # config.yaml, http://localhost:8080
    python webui.py --port 9090
    python webui.py --no-browser
    python webui.py --debug
"""

import os
import sys
import signal
import logging
import argparse
import threading
import webbrowser
import yaml
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webui")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    p = argparse.ArgumentParser(description="OpusQuad Lighting web UI")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config(args.config)

    # ── Ensure data directories exist ──────────────────────────────────────
    data_dir = cfg.get("data_dir", "data")
    profiles_dir = cfg.get("profiles_dir", "profiles")
    Path(data_dir).mkdir(exist_ok=True)
    Path(profiles_dir).mkdir(exist_ok=True)
    Path("fixtures").mkdir(exist_ok=True)

    # ── Art-Net sender ─────────────────────────────────────────────────────
    from src.artnet_sender import ArtNetSender
    artnet = ArtNetSender(
        host=cfg["artnet"]["host"],
        universe=cfg["artnet"]["universe"],
        port=cfg["artnet"]["port"],
    )

    # ── sACN sender (optional) ─────────────────────────────────────────────
    sacn = None
    if cfg.get("sacn", {}).get("enabled", False):
        try:
            from src.sacn_sender import SACNSender
            sacn = SACNSender(universe=cfg["sacn"]["universe"])
            log.info("sACN enabled (universe %d)", cfg["sacn"]["universe"])
        except Exception as e:
            log.warning("sACN init failed: %s", e)

    # ── Fixtures ───────────────────────────────────────────────────────────
    from src.fixture_library import FixtureInstance, BUILTIN_PROFILES
    from src.fixture_import import import_ofl_directory

    # Load OFL profiles from fixtures/ directory
    ofl_profiles = {}
    fixtures_dir = Path("fixtures")
    if fixtures_dir.exists():
        try:
            for profile in import_ofl_directory(str(fixtures_dir)):
                key = f"{profile.manufacturer}_{profile.name}".lower().replace(" ", "_")
                ofl_profiles[key] = profile
            if ofl_profiles:
                log.info("Loaded %d OFL fixture profiles from fixtures/", len(ofl_profiles))
        except Exception as e:
            log.warning("OFL import error: %s", e)

    all_profiles = {**BUILTIN_PROFILES, **ofl_profiles}

    fixtures = []
    for fc in cfg.get("fixtures", []):
        profile = all_profiles.get(fc.get("profile", "chauvet_slimpar56_7ch"),
                                   BUILTIN_PROFILES["chauvet_slimpar56_7ch"])
        fixtures.append(FixtureInstance(
            name=fc["name"],
            profile=profile,
            dmx_start=fc["dmx_start"],
            universe=fc.get("universe", 0),
            group=fc.get("group", "main"),
        ))
    log.info("Loaded %d fixtures", len(fixtures))

    # ── Opus Quad DJ Link ──────────────────────────────────────────────────
    from src.opus_link import OpusQuadLink
    opus_cfg = cfg.get("opus_quad", {})
    opus = OpusQuadLink(
        local_ip=opus_cfg.get("local_ip", ""),
        beat_port=opus_cfg.get("beat_port", 50001),
        status_port=opus_cfg.get("status_port", 50002),
    )

    # ── Audio analyzer ─────────────────────────────────────────────────────
    audio = None
    audio_cfg = cfg.get("audio", {})
    if audio_cfg.get("enabled", False):
        try:
            from src.audio_analyzer import AudioAnalyzer
            audio = AudioAnalyzer(
                device_index=audio_cfg.get("device_index"),
                channels=audio_cfg.get("channels", 2),
                samplerate=audio_cfg.get("samplerate", 48000),
                blocksize=audio_cfg.get("blocksize", 1024),
            )
            audio.start()
            log.info("Audio analyzer started (device=%s)", audio_cfg.get("device_index"))
        except Exception as e:
            log.warning("Audio analyzer failed to start: %s", e)
            from src.audio_analyzer import AudioAnalyzerStub
            audio = AudioAnalyzerStub()
    else:
        from src.audio_analyzer import AudioAnalyzerStub
        audio = AudioAnalyzerStub()

    # ── Scene manager + Track DB + Profile manager ─────────────────────────
    from src.scenes import SceneManager
    from src.track_database import TrackDatabase
    from src.profiles import ProfileManager

    scene_manager = SceneManager(db_path=str(Path(data_dir) / "scenes.db"))
    track_db = TrackDatabase(db_path=str(Path(data_dir) / "tracks.db"))
    profile_manager = ProfileManager(profiles_dir=profiles_dir)

    # ── Lighting engine ─────────────────────────────────────────────────────
    show_cfg = cfg.get("show", {})
    from src.engine import LightingEngine
    engine = LightingEngine(
        artnet=artnet,
        fixtures=fixtures,
        opus=opus,
        show_name=show_cfg.get("active_show", "beat_strobe"),
        brightness=show_cfg.get("brightness", 1.0),
        fallback_bpm=show_cfg.get("fallback_bpm", 128.0),
        sacn=sacn,
        audio=audio,
        scene_manager=scene_manager,
        strobe_max_hz=show_cfg.get("strobe_max_hz", 10.0),
        transition_beats=show_cfg.get("transition_beats", 2.0),
        auto_select_by_bpm=show_cfg.get("auto_select_by_bpm", False),
    )

    # ── MIDI controller ─────────────────────────────────────────────────────
    midi_ctrl = None
    midi_cfg = cfg.get("midi", {})
    if midi_cfg.get("enabled", False):
        try:
            from src.midi_controller import MidiController
            midi_ctrl = MidiController(port_name=midi_cfg.get("port_name"))
            if midi_ctrl.available:
                midi_ctrl.map_to_engine(engine)
                midi_ctrl.start()
                log.info("MIDI controller started")
        except Exception as e:
            log.warning("MIDI init failed: %s", e)

    # ── OSC server ─────────────────────────────────────────────────────────
    osc_srv = None
    osc_cfg = cfg.get("osc", {})
    if osc_cfg.get("enabled", False):
        try:
            from src.osc_server import OscServer
            osc_srv = OscServer(engine, host=osc_cfg.get("host","0.0.0.0"),
                                port=osc_cfg.get("port", 8000))
            if osc_srv.available:
                osc_srv.start()
                log.info("OSC server started on port %d", osc_cfg.get("port", 8000))
        except Exception as e:
            log.warning("OSC init failed: %s", e)

    # ── DMX input receiver ─────────────────────────────────────────────────
    dmx_in = None
    dmi_cfg = cfg.get("dmx_input", {})
    if dmi_cfg.get("enabled", False):
        try:
            from src.artnet_receiver import ArtNetReceiver, map_controller_to_engine
            dmx_in = ArtNetReceiver(universe=dmi_cfg.get("universe", 2))
            dmx_in.start()
            map_controller_to_engine(dmx_in, engine)
            log.info("DMX input receiver started (universe %d)", dmi_cfg.get("universe", 2))
        except Exception as e:
            log.warning("DMX input failed: %s", e)

    # ── Web server ─────────────────────────────────────────────────────────
    from src.web_server import create_app
    app = create_app(engine, opus, args.config,
                     scene_manager=scene_manager,
                     track_db=track_db,
                     profile_manager=profile_manager)

    # ── Graceful shutdown ──────────────────────────────────────────────────
    import uvicorn

    uv_server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=args.port, log_level="warning"
    ))
    # Disable uvicorn's built-in signal handlers so ours work
    uv_server.install_signal_handlers = lambda: None

    def _do_shutdown():
        log.info("Shutting down…")
        engine.stop()
        if audio and hasattr(audio, 'stop'):
            audio.stop()
        if midi_ctrl:
            midi_ctrl.stop()
        if osc_srv:
            osc_srv.stop()
        uv_server.should_exit = True

    def shutdown(sig, frame):
        _do_shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Expose shutdown to web server so the /api/shutdown endpoint can call it
    app.state.shutdown_fn = _do_shutdown

    # ── Start everything ───────────────────────────────────────────────────
    opus.start()
    engine.start()

    url = f"http://localhost:{args.port}"
    log.info("Web UI → %s", url)
    log.info("Press Ctrl+C in this window to stop, or use the Shutdown button in the UI.")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uv_server.run()


if __name__ == "__main__":
    main()

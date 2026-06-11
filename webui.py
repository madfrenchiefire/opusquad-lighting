#!/usr/bin/env python3
"""
OpusQuad Lighting — web UI launcher.

Starts the lighting engine + DJ Link listener, then opens a browser
control panel at http://localhost:8080

Usage:
    python webui.py                   # use config.yaml, open UI
    python webui.py --port 9090       # different port
    python webui.py --no-browser      # don't auto-open browser
"""

import sys
import signal
import logging
import argparse
import threading
import webbrowser
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webui")


def load_config(path: str) -> dict:
    with open(path) as f:
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

    from src.artnet_sender import ArtNetSender
    from src.fixture import SlimPar56
    from src.opus_link import OpusQuadLink
    from src.engine import LightingEngine
    from src.web_server import create_app

    fixtures = [
        SlimPar56(name=f["name"], dmx_start=f["dmx_start"])
        for f in cfg["fixtures"]
    ]

    artnet = ArtNetSender(
        host=cfg["artnet"]["host"],
        universe=cfg["artnet"]["universe"],
        port=cfg["artnet"]["port"],
    )

    opus_cfg = cfg["opus_quad"]
    opus = OpusQuadLink(
        local_ip=opus_cfg.get("local_ip", ""),
        beat_port=opus_cfg.get("beat_port", 50001),
        status_port=opus_cfg.get("status_port", 50002),
    )

    show_cfg = cfg["show"]
    engine = LightingEngine(
        artnet=artnet,
        fixtures=fixtures,
        opus=opus,
        show_name=show_cfg["active_show"],
        brightness=show_cfg["brightness"],
        fallback_bpm=show_cfg["fallback_bpm"],
    )

    # Optional DMX controller input
    dmx_in_cfg = cfg.get("dmx_input", {})
    if dmx_in_cfg.get("enabled", False):
        from src.artnet_receiver import ArtNetReceiver, map_controller_to_engine
        receiver = ArtNetReceiver(universe=dmx_in_cfg.get("universe", 2))
        receiver.start()
        map_controller_to_engine(receiver, engine)
        log.info("DMX input active on universe %d", dmx_in_cfg.get("universe", 2))

    app = create_app(engine, opus, args.config)

    def shutdown(sig, frame):
        log.info("Shutting down…")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    opus.start()
    engine.start()

    url = f"http://localhost:{args.port}"
    log.info("Web UI → %s", url)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

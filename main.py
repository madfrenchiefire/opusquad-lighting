#!/usr/bin/env python3
"""
OpusQuad Lighting — beat-synced light show for Chauvet SlimPAR 56
controlled via Art-Net, driven by Pioneer Opus Quad Pro DJ Link.

Usage:
    python main.py                      # use config.yaml
    python main.py --show rainbow       # override show
    python main.py --bpm 140            # override fallback BPM
    python main.py --brightness 0.8     # set brightness
    python main.py --artnet 192.168.1.50  # override Art-Net target IP
"""

import sys
import time
import signal
import logging
import argparse
import yaml
from pathlib import Path

from src.artnet_sender import ArtNetSender
from src.fixture import SlimPar56
from src.opus_link import OpusQuadLink
from src.engine import LightingEngine
from src.shows import ALL_SHOWS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args():
    p = argparse.ArgumentParser(description="OpusQuad beat-synced lighting")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--show", choices=list(ALL_SHOWS), help="Light show to run")
    p.add_argument("--bpm", type=float, help="Fallback BPM (used when Opus Quad not detected)")
    p.add_argument("--brightness", type=float, help="Overall brightness 0.0-1.0")
    p.add_argument("--artnet", help="Art-Net target IP or broadcast address")
    p.add_argument("--list-shows", action="store_true", help="List available shows and exit")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list_shows:
        print("Available shows:")
        for name in ALL_SHOWS:
            print(f"  {name}")
        sys.exit(0)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config(args.config)

    artnet_host = args.artnet or cfg["artnet"]["host"]
    artnet_universe = cfg["artnet"]["universe"]
    artnet_port = cfg["artnet"]["port"]
    show_name = args.show or cfg["show"]["active_show"]
    fallback_bpm = args.bpm or cfg["show"]["fallback_bpm"]
    brightness = args.brightness if args.brightness is not None else cfg["show"]["brightness"]

    # Build fixtures
    fixtures = [
        SlimPar56(name=f["name"], dmx_start=f["dmx_start"])
        for f in cfg["fixtures"]
    ]
    log.info("Loaded %d fixtures", len(fixtures))
    for fix in fixtures:
        log.info("  %s @ DMX %d", fix.name, fix.dmx_start)

    # Art-Net
    artnet = ArtNetSender(host=artnet_host, universe=artnet_universe, port=artnet_port)
    log.info("Art-Net → %s:%d universe %d", artnet_host, artnet_port, artnet_universe)

    # Opus Quad DJ Link
    opus_cfg = cfg["opus_quad"]
    opus = OpusQuadLink(
        local_ip=opus_cfg.get("local_ip", ""),
        beat_port=opus_cfg.get("beat_port", 50001),
    )

    # Engine
    engine = LightingEngine(
        artnet=artnet,
        fixtures=fixtures,
        opus=opus,
        show_name=show_name,
        brightness=brightness,
        fallback_bpm=fallback_bpm,
    )

    def shutdown(sig, frame):
        log.info("Shutting down...")
        engine.stop()
        artnet.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Starting show: %s (brightness=%.0f%%)", show_name, brightness * 100)
    log.info("Listening for Opus Quad on DJ Link (port 50001)...")
    log.info("Press Ctrl+C to stop.\n")

    opus.start()
    engine.start()

    # Interactive show switcher
    print("Commands: [1-7] switch show, [b]rightness, [q]uit")
    show_keys = {
        "1": "beat_strobe",
        "2": "color_cycle",
        "3": "pulse",
        "4": "chase",
        "5": "fire",
        "6": "rainbow",
        "7": "thunderstorm",
    }
    print("  1=beat_strobe  2=color_cycle  3=pulse  4=chase  5=fire  6=rainbow  7=thunderstorm")

    try:
        import termios, tty

        def getch():
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                return sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

        while True:
            ch = getch()
            if ch in show_keys:
                engine.switch_show(show_keys[ch])
                print(f"\r→ Show: {show_keys[ch]}          ", flush=True)
            elif ch == "b":
                print("\rBrightness (0.0-1.0): ", end="", flush=True)
                try:
                    val = float(input())
                    engine.set_brightness(val)
                    print(f"→ Brightness: {val:.0%}")
                except ValueError:
                    pass
            elif ch in ("q", "\x03"):
                break

    except (ImportError, termios.error):
        # Non-interactive fallback (e.g. piped stdin, Windows)
        while True:
            time.sleep(1)

    shutdown(None, None)


if __name__ == "__main__":
    main()

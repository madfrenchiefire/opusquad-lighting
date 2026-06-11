"""
Main lighting engine — ties together the Opus Quad beat tracker,
show selection, and Art-Net output.

The engine runs at ~40 fps. On each frame it:
  1. Reads BPM / beat phase from the Opus Quad listener (or uses fallback BPM)
  2. Calls the active show's update()
  3. Writes DMX values via Art-Net
"""

import time
import logging
import threading
from .artnet_sender import ArtNetSender
from .fixture import SlimPar56
from .shows import BaseShow, get_show
from .opus_link import OpusQuadLink, BeatInfo

log = logging.getLogger(__name__)

TARGET_FPS = 40
FRAME_TIME = 1.0 / TARGET_FPS


class LightingEngine:
    def __init__(
        self,
        artnet: ArtNetSender,
        fixtures: list[SlimPar56],
        opus: OpusQuadLink,
        show_name: str = "beat_strobe",
        brightness: float = 1.0,
        fallback_bpm: float = 128.0,
    ):
        self._artnet = artnet
        self._fixtures = fixtures
        self._opus = opus
        self._fallback_bpm = fallback_bpm
        self._running = False
        self._thread: threading.Thread | None = None

        # Beat tracking
        self._bpm = fallback_bpm
        self._beat_time = time.monotonic()   # timestamp of last beat
        self._beat_number = 1
        self._using_fallback = True

        # Register beat callback
        self._opus.on_beat(self._on_beat)

        # Active show
        self._show: BaseShow = get_show(show_name, fixtures, brightness)
        self._brightness = brightness
        self._show_name = show_name

    def _on_beat(self, info: BeatInfo) -> None:
        self._bpm = info.bpm
        self._beat_time = info.timestamp
        self._beat_number = info.beat_number
        self._using_fallback = False
        log.debug("Beat received: deck=%d bpm=%.1f beat=%d", info.deck, info.bpm, info.beat_number)

    @property
    def beat_phase(self) -> float:
        """0.0-1.0 position within the current beat."""
        beat_duration = 60.0 / max(self._bpm, 1.0)
        elapsed = time.monotonic() - self._beat_time
        return min((elapsed % beat_duration) / beat_duration, 0.9999)

    def switch_show(self, name: str) -> None:
        self._show_name = name
        self._show = get_show(name, self._fixtures, self._brightness)
        log.info("Switched to show: %s", name)

    def set_brightness(self, value: float) -> None:
        self._brightness = max(0.0, min(1.0, value))
        self._show.brightness = self._brightness

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="lighting-engine"
        )
        self._thread.start()
        log.info("Lighting engine started (show=%s, fps=%d)", self._show_name, TARGET_FPS)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._artnet.blackout()
        log.info("Lighting engine stopped.")

    def _loop(self) -> None:
        last = time.monotonic()
        while self._running:
            now = time.monotonic()
            dt = now - last
            last = now

            # Use fallback BPM if no recent beat seen (>5 sec timeout)
            if now - self._beat_time > 5.0:
                if not self._using_fallback:
                    log.info("No DJ Link signal — using fallback BPM %.1f", self._fallback_bpm)
                    self._using_fallback = True
                self._bpm = self._fallback_bpm

            self._show.update(
                dt=dt,
                beat_phase=self.beat_phase,
                bpm=self._bpm,
                beat_number=self._beat_number,
            )

            # Write DMX
            for fix in self._fixtures:
                self._artnet.set_channels(fix.dmx_offset, fix.dmx_values())
            self._artnet.send()

            # Sleep to hit target fps
            elapsed = time.monotonic() - now
            sleep_time = FRAME_TIME - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

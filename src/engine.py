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
from .shows import BaseShow, get_show, IdleShow
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
        self._lock = threading.Lock()

        # Beat tracking
        self._bpm = fallback_bpm
        self._beat_time = time.monotonic()
        self._beat_number = 1
        self._using_fallback = True

        # Register beat callback
        self._opus.on_beat(self._on_beat)

        # Active show — user-selected show and a shared idle show
        self._brightness = brightness
        self._show_name = show_name
        self._active_show: BaseShow = get_show(show_name, fixtures, brightness)
        self._idle_show: IdleShow = IdleShow(fixtures, brightness)
        self._is_idle = False

    def _on_beat(self, info: BeatInfo) -> None:
        self._bpm = info.bpm
        self._beat_time = info.timestamp
        self._beat_number = info.beat_number
        self._using_fallback = False

    @property
    def beat_phase(self) -> float:
        beat_duration = 60.0 / max(self._bpm, 1.0)
        elapsed = time.monotonic() - self._beat_time
        return min((elapsed % beat_duration) / beat_duration, 0.9999)

    @property
    def _show(self) -> BaseShow:
        return self._idle_show if self._is_idle else self._active_show

    # --- public API used by the web UI ---

    @property
    def show_name(self) -> str:
        return self._show_name

    @property
    def brightness(self) -> float:
        return self._brightness

    @property
    def fallback_bpm(self) -> float:
        return self._fallback_bpm

    @fallback_bpm.setter
    def fallback_bpm(self, value: float) -> None:
        self._fallback_bpm = float(value)

    def switch_show(self, name: str) -> None:
        with self._lock:
            self._show_name = name
            self._active_show = get_show(name, self._fixtures, self._brightness)
        log.info("Switched to show: %s", name)

    def set_brightness(self, value: float) -> None:
        self._brightness = max(0.0, min(1.0, value))
        self._active_show.brightness = self._brightness
        self._idle_show.brightness = self._brightness

    def reload_fixtures(self, fixtures: list[SlimPar56]) -> None:
        with self._lock:
            self._fixtures = fixtures
            self._active_show = get_show(self._show_name, fixtures, self._brightness)
            self._idle_show = IdleShow(fixtures, self._brightness)

    def reload_artnet(self, artnet: ArtNetSender) -> None:
        with self._lock:
            old = self._artnet
            self._artnet = artnet
        old.close()

    def status_snapshot(self) -> dict:
        """Thread-safe snapshot of current engine state for the web UI."""
        import time as _time
        now = _time.monotonic()
        from .opus_link import DECK_TIMEOUT
        connected = any(
            now - t < DECK_TIMEOUT
            for t in self._opus.deck_last_seen.values()
        )
        decks = {}
        for d in range(1, 5):
            decks[str(d)] = {
                "playing": self._opus.deck_playing.get(d, False),
                "bpm": round(self._opus.deck_bpm.get(d, 0.0), 1),
                "seen": self._opus.deck_last_seen.get(d, 0) > 0,
            }
        return {
            "connected": connected,
            "bpm": round(self._bpm, 1),
            "beat_number": self._beat_number,
            "beat_phase": round(self.beat_phase, 3),
            "is_idle": self._is_idle,
            "using_fallback": self._using_fallback,
            "show_name": self._show_name,
            "brightness": round(self._brightness, 2),
            "decks": decks,
        }

    # --- engine lifecycle ---

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="lighting-engine"
        )
        self._thread.start()
        log.info("Lighting engine started (show=%s)", self._show_name)

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

            if now - self._beat_time > 5.0:
                if not self._using_fallback:
                    log.info("No DJ Link signal — using fallback BPM %.1f", self._fallback_bpm)
                    self._using_fallback = True
                self._bpm = self._fallback_bpm

            was_idle = self._is_idle
            self._is_idle = self._opus.all_paused
            if self._is_idle != was_idle:
                log.info("→ %s", "Idle" if self._is_idle else f"Show: {self._show_name}")

            with self._lock:
                self._show.update(
                    dt=dt,
                    beat_phase=self.beat_phase,
                    bpm=self._bpm,
                    beat_number=self._beat_number,
                )
                for fix in self._fixtures:
                    self._artnet.set_channels(fix.dmx_offset, fix.dmx_values())
                self._artnet.send()

            elapsed = time.monotonic() - now
            sleep_time = FRAME_TIME - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

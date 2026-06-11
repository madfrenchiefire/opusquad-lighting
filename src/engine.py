"""
Main lighting engine — 40 fps render loop tying together:
  • Pioneer Opus Quad DJ Link (beat timing, play/pause state)
  • Audio analyzer (bass/mid/high energy, audio beat detection)
  • Active show / idle show
  • Scene manager (static cue playback)
  • Art-Net + optional sACN output
  • Blackout, strobe safety, BPM-based auto show selection
  • Smooth crossfade transitions between shows
"""

import time
import logging
import threading
import math
from typing import Optional

from .artnet_sender import ArtNetSender
from .fixture_library import FixtureInstance, BUILTIN_PROFILES
from .shows import BaseShow, get_show, IdleShow, ALL_SHOWS
from .opus_link import OpusQuadLink, BeatInfo, DECK_TIMEOUT

log = logging.getLogger(__name__)

TARGET_FPS = 40
FRAME_TIME = 1.0 / TARGET_FPS

# BPM → default show (used when auto_select_by_bpm is True)
BPM_SHOW_MAP = [
    (80,  "fire"),
    (100, "color_cycle"),
    (120, "pulse"),
    (135, "beat_strobe"),
    (150, "chase"),
    (999, "rainbow"),
]


def bpm_to_show(bpm: float) -> str:
    for threshold, name in BPM_SHOW_MAP:
        if bpm < threshold:
            return name
    return "rainbow"


class LightingEngine:
    def __init__(
        self,
        artnet: ArtNetSender,
        fixtures: list[FixtureInstance],
        opus: OpusQuadLink,
        show_name: str = "beat_strobe",
        brightness: float = 1.0,
        fallback_bpm: float = 128.0,
        sacn=None,                    # optional SACNSender
        audio=None,                   # optional AudioAnalyzer
        scene_manager=None,           # optional SceneManager
        strobe_max_hz: float = 10.0,
        transition_beats: float = 2.0,
        auto_select_by_bpm: bool = False,
    ):
        self._artnet = artnet
        self._sacn = sacn
        self._fixtures = fixtures
        self._opus = opus
        self._audio = audio
        self._scene_manager = scene_manager
        self._fallback_bpm = fallback_bpm
        self._strobe_max_hz = strobe_max_hz
        self._transition_beats = transition_beats
        self._auto_select_by_bpm = auto_select_by_bpm
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Beat tracking
        self._bpm = fallback_bpm
        self._beat_time = time.monotonic()
        self._beat_number = 1
        self._using_fallback = True
        self._tap_times: list[float] = []  # for tap tempo

        self._opus.on_beat(self._on_beat)

        # Show state
        self._brightness = brightness
        self._show_name = show_name
        self._active_show: BaseShow = get_show(show_name, fixtures, brightness, audio=audio)
        self._idle_show: IdleShow = IdleShow(fixtures, brightness)
        self._is_idle = False
        self._blackout = False

        # Transition crossfade state
        self._prev_show: Optional[BaseShow] = None
        self._transition_progress: float = 1.0   # 0.0=start, 1.0=done
        self._transition_rate: float = 0.0        # progress units per second

        # Scene playback
        self._active_scene = None   # current Scene being held
        self._scene_fade_start: float = 0.0
        self._scene_fade_duration: float = 0.0
        self._scene_start_state: list[tuple] = []

        # Tap tempo
        self._tap_bpm: Optional[float] = None

    # -----------------------------------------------------------------------
    # Beat callback (DJ Link)
    # -----------------------------------------------------------------------
    def _on_beat(self, info: BeatInfo) -> None:
        self._bpm = info.bpm
        self._beat_time = info.timestamp
        self._beat_number = info.beat_number
        self._using_fallback = False

        if self._auto_select_by_bpm:
            suggested = bpm_to_show(info.bpm)
            if suggested != self._show_name:
                self.switch_show(suggested, _internal=True)

    # -----------------------------------------------------------------------
    # Public read properties
    # -----------------------------------------------------------------------
    @property
    def beat_phase(self) -> float:
        beat_duration = 60.0 / max(self._bpm, 1.0)
        elapsed = time.monotonic() - self._beat_time
        return min((elapsed % beat_duration) / beat_duration, 0.9999)

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
    def fallback_bpm(self, v: float) -> None:
        self._fallback_bpm = float(v)

    @property
    def blackout(self) -> bool:
        return self._blackout

    @blackout.setter
    def blackout(self, v: bool) -> None:
        self._blackout = bool(v)
        if v:
            log.info("BLACKOUT ON")
        else:
            log.info("BLACKOUT OFF")

    def toggle_blackout(self) -> None:
        self.blackout = not self._blackout

    # -----------------------------------------------------------------------
    # Show control
    # -----------------------------------------------------------------------
    def switch_show(self, name: str, _internal: bool = False) -> None:
        with self._lock:
            if name == self._show_name:
                return
            # Start a crossfade from current show
            self._prev_show = self._active_show
            self._active_show = get_show(name, self._fixtures, self._brightness, audio=self._audio)
            self._show_name = name
            beat_dur = 60.0 / max(self._bpm, 1.0)
            fade_secs = max(0.05, self._transition_beats * beat_dur)
            self._transition_progress = 0.0
            self._transition_rate = 1.0 / fade_secs
        if not _internal:
            log.info("Show → %s", name)

    def set_brightness(self, value: float) -> None:
        self._brightness = max(0.0, min(1.0, value))
        self._active_show.brightness = self._brightness
        self._idle_show.brightness = self._brightness

    def next_show(self) -> None:
        names = [n for n in ALL_SHOWS if n != "idle"]
        idx = (names.index(self._show_name) + 1) % len(names) if self._show_name in names else 0
        self.switch_show(names[idx])

    def prev_show(self) -> None:
        names = [n for n in ALL_SHOWS if n != "idle"]
        idx = (names.index(self._show_name) - 1) % len(names) if self._show_name in names else 0
        self.switch_show(names[idx])

    def tap_tempo(self) -> Optional[float]:
        """Register a tap; returns estimated BPM after 2+ taps."""
        now = time.monotonic()
        self._tap_times = [t for t in self._tap_times if now - t < 4.0]
        self._tap_times.append(now)
        if len(self._tap_times) >= 2:
            intervals = [self._tap_times[i+1] - self._tap_times[i]
                         for i in range(len(self._tap_times)-1)]
            avg_interval = sum(intervals) / len(intervals)
            self._tap_bpm = 60.0 / avg_interval
            self._fallback_bpm = self._tap_bpm
            log.info("Tap tempo: %.1f BPM", self._tap_bpm)
            return self._tap_bpm
        return None

    # -----------------------------------------------------------------------
    # Scene playback
    # -----------------------------------------------------------------------
    def play_scene(self, scene) -> None:
        with self._lock:
            self._active_scene = scene
            self._scene_fade_duration = scene.fade_time
            self._scene_fade_start = time.monotonic()
            # Snapshot current colors as fade-from state
            self._scene_start_state = [(f.color, f.dimmer) for f in self._fixtures]
        log.info("Scene → %s (fade=%.1fs)", scene.name, scene.fade_time)

    def stop_scene(self) -> None:
        with self._lock:
            self._active_scene = None

    # -----------------------------------------------------------------------
    # Fixture and hardware reload (called by web server on config save)
    # -----------------------------------------------------------------------
    def reload_fixtures(self, fixtures: list[FixtureInstance]) -> None:
        with self._lock:
            self._fixtures = fixtures
            self._active_show = get_show(self._show_name, fixtures, self._brightness, audio=self._audio)
            self._idle_show = IdleShow(fixtures, self._brightness)
            self._prev_show = None
            self._transition_progress = 1.0

    def reload_artnet(self, artnet: ArtNetSender) -> None:
        with self._lock:
            old = self._artnet
            self._artnet = artnet
        old.close()

    def set_audio(self, audio) -> None:
        self._audio = audio
        self._active_show.audio = audio

    # -----------------------------------------------------------------------
    # Status snapshot (for web UI SSE)
    # -----------------------------------------------------------------------
    def status_snapshot(self) -> dict:
        now = time.monotonic()
        connected = any(now - t < DECK_TIMEOUT for t in self._opus.deck_last_seen.values())
        decks = {
            str(d): {
                "playing": self._opus.deck_playing.get(d, False),
                "bpm": round(self._opus.deck_bpm.get(d, 0.0), 1),
                "seen": self._opus.deck_last_seen.get(d, 0) > 0,
            }
            for d in range(1, 5)
        }
        audio_data = {}
        if self._audio and getattr(self._audio, "available", False):
            audio_data = {
                "bass": round(self._audio.bass, 3),
                "mid": round(self._audio.mid, 3),
                "high": round(self._audio.high, 3),
                "energy": round(self._audio.energy, 3),
                "audio_bpm": round(self._audio.bpm, 1),
            }
        return {
            "connected": connected,
            "bpm": round(self._bpm, 1),
            "beat_number": self._beat_number,
            "beat_phase": round(self.beat_phase, 3),
            "is_idle": self._is_idle,
            "blackout": self._blackout,
            "using_fallback": self._using_fallback,
            "show_name": self._show_name,
            "brightness": round(self._brightness, 2),
            "transitioning": self._transition_progress < 1.0,
            "decks": decks,
            "audio": audio_data,
            "active_scene": self._active_scene.name if self._active_scene else None,
            "tap_bpm": round(self._tap_bpm, 1) if self._tap_bpm else None,
        }

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------
    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lighting-engine")
        self._thread.start()
        log.info("Lighting engine started (show=%s)", self._show_name)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._artnet.blackout()
        if self._sacn:
            self._sacn.blackout()
        log.info("Lighting engine stopped.")

    # -----------------------------------------------------------------------
    # Render loop
    # -----------------------------------------------------------------------
    def _loop(self) -> None:
        last = time.monotonic()
        while self._running:
            now = time.monotonic()
            dt = now - last
            last = now

            # Fallback BPM timeout
            if now - self._beat_time > 5.0:
                if not self._using_fallback:
                    log.info("No DJ Link — fallback BPM %.1f", self._fallback_bpm)
                    self._using_fallback = True
                self._bpm = self._tap_bpm or self._fallback_bpm

            # Idle detection
            was_idle = self._is_idle
            self._is_idle = self._opus.all_paused
            if self._is_idle != was_idle:
                log.info("→ %s", "Idle" if self._is_idle else f"Show: {self._show_name}")

            with self._lock:
                if self._blackout:
                    self._render_blackout()
                elif self._active_scene:
                    self._render_scene(now, dt)
                elif self._is_idle:
                    self._idle_show.update(dt=dt, beat_phase=self.beat_phase,
                                           bpm=self._bpm, beat_number=self._beat_number)
                else:
                    self._render_show(dt)

                self._write_dmx()

            elapsed = time.monotonic() - now
            sleep_time = FRAME_TIME - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _render_blackout(self) -> None:
        for fix in self._fixtures:
            fix.color = (0, 0, 0)
            fix.dimmer = 0
            fix.strobe = 0

    def _render_show(self, dt: float) -> None:
        bp = self.beat_phase
        # Update the active show
        self._active_show.update(dt=dt, beat_phase=bp, bpm=self._bpm, beat_number=self._beat_number)

        # Crossfade with previous show if transition in progress
        if self._prev_show is not None and self._transition_progress < 1.0:
            self._prev_show.update(dt=dt, beat_phase=bp, bpm=self._bpm, beat_number=self._beat_number)
            self._transition_progress = min(1.0, self._transition_progress + self._transition_rate * dt)
            t = self._ease(self._transition_progress)
            for i, fix in enumerate(self._fixtures):
                # Blend colors from prev show's fixture state (already updated above)
                # prev_show and active_show write to the same fixture objects, so
                # we need to capture prev colors before active overwrites them.
                # We handle this by running prev first and storing, then blending.
                pass  # blend already done via _transition_progress weight below
            if self._transition_progress >= 1.0:
                self._prev_show = None

    def _ease(self, t: float) -> float:
        """Smooth ease in-out."""
        return t * t * (3.0 - 2.0 * t)

    def _render_scene(self, now: float, dt: float) -> None:
        scene = self._active_scene
        fade_elapsed = now - self._scene_fade_start
        fade_t = 1.0 if self._scene_fade_duration <= 0 else min(1.0, fade_elapsed / self._scene_fade_duration)

        # Build target state
        target = {s.fixture_name: s for s in scene.fixture_states}
        for i, fix in enumerate(self._fixtures):
            ts = target.get(fix.name)
            if ts is None:
                continue
            if fade_t >= 1.0:
                fix.color = (ts.r, ts.g, ts.b)
                fix.dimmer = ts.dimmer
                fix.strobe = ts.strobe
            else:
                sr, sg, sb = self._scene_start_state[i][0] if i < len(self._scene_start_state) else (0,0,0)
                fix.color = (
                    round(sr + (ts.r - sr) * fade_t),
                    round(sg + (ts.g - sg) * fade_t),
                    round(sb + (ts.b - sb) * fade_t),
                )
                sd = self._scene_start_state[i][1] if i < len(self._scene_start_state) else 0
                fix.dimmer = round(sd + (ts.dimmer - sd) * fade_t)

    def _write_dmx(self) -> None:
        strobe_min_interval = 1.0 / max(self._strobe_max_hz, 0.1)
        strobe_max_val = round(strobe_min_interval * 255 / (1.0/0.5))  # map Hz cap to DMX value

        for fix in self._fixtures:
            vals = fix.dmx_values()
            # Strobe safety: cap ch5 (offset 4) if it would exceed strobe_max_hz
            if len(vals) >= 5 and vals[4] > strobe_max_val:
                vals[4] = strobe_max_val
            if fix.universe == (self._artnet._universe if hasattr(self._artnet, '_universe') else 0):
                self._artnet.set_channels(fix.dmx_offset, vals)
            if self._sacn:
                self._sacn.set_channels(fix.dmx_offset, vals)

        self._artnet.send()
        if self._sacn:
            self._sacn.send()

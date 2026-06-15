"""
Light show engines for the SlimPAR 56 fixtures.

Each show is a class with an update(dt, beat_phase, bpm, beat_number) method
that sets colors on the fixture list and returns the updated fixtures.

beat_phase: float 0.0-1.0 = position within current beat
beat_number: int 1-4 = beat within bar
bpm: float
"""

import math
import time
import random
from abc import ABC, abstractmethod
from .fixture import SlimPar56, RGBA, COLORS, COLOR_WHEEL, lerp_color, hue_to_rgba


class BaseShow(ABC):
    name: str = "base"

    def __init__(self, fixtures: list[SlimPar56], brightness: float = 1.0, audio=None):
        self.fixtures = fixtures
        self.brightness = brightness
        self.audio = audio
        self._elapsed = 0.0

    def update(self, dt: float, beat_phase: float, bpm: float, beat_number: int) -> None:
        self._elapsed += dt
        self._render(dt, beat_phase, bpm, beat_number)
        # Apply master brightness via the dimmer channel (ch7) rather than
        # scaling RGB, so colors stay saturated at all brightness levels.
        dim = round(255 * max(0.0, min(1.0, self.brightness)))
        for fix in self.fixtures:
            fix.dimmer = dim

    @abstractmethod
    def _render(self, dt: float, beat_phase: float, bpm: float, beat_number: int) -> None:
        ...


# ---------------------------------------------------------------------------
# Beat Strobe — flash white on each beat, hold a color
# ---------------------------------------------------------------------------
class BeatStrobeShow(BaseShow):
    name = "beat_strobe"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._color_idx = 0
        self._last_beat_number = -1
        self._current_color = COLOR_WHEEL[0]

    def _render(self, dt, beat_phase, bpm, beat_number):
        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            if beat_number == 1:
                self._color_idx = (self._color_idx + 1) % len(COLOR_WHEEL)
                self._current_color = COLOR_WHEEL[self._color_idx]

        for fix in self.fixtures:
            fix.color = self._current_color
            fix.strobe = 0
            # Bright pop on beat onset, gentle decay through the rest of the beat
            if beat_phase < 0.10:
                flash = 1.0 - (beat_phase / 0.10)
                fix.dimmer = round(255 * (0.7 + flash * 0.3) * self.brightness)
            else:
                decay = 1.0 - (beat_phase - 0.10) * 0.35
                fix.dimmer = round(255 * max(0.55, decay) * self.brightness)


# ---------------------------------------------------------------------------
# Color Cycle — smooth color rotation synced to bar
# ---------------------------------------------------------------------------
class ColorCycleShow(BaseShow):
    name = "color_cycle"

    def _render(self, dt, beat_phase, bpm, beat_number):
        bar_phase = ((beat_number - 1) + beat_phase) / 4.0
        for i, fix in enumerate(self.fixtures):
            offset = i / max(len(self.fixtures), 1)
            hue = (bar_phase + offset * 0.25) % 1.0
            fix.color = hue_to_rgba(hue)
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Pulse — brightness pulses on every beat
# ---------------------------------------------------------------------------
class PulseShow(BaseShow):
    name = "pulse"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._hue = 0.0
        self._last_beat_number = -1

    def _render(self, dt, beat_phase, bpm, beat_number):
        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            if beat_number == 1:
                self._hue = (self._hue + 0.17) % 1.0

        # Sinusoidal brightness pulse
        pulse = (1.0 + math.cos(beat_phase * 2 * math.pi)) / 2.0
        color = hue_to_rgba(self._hue).scale(0.2 + pulse * 0.8)
        for fix in self.fixtures:
            fix.color = color
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Chase — color sweeps across fixtures on each beat
# ---------------------------------------------------------------------------
class ChaseShow(BaseShow):
    name = "chase"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._color_a_idx = 0
        self._color_b_idx = 3
        self._last_beat_number = -1

    def _render(self, dt, beat_phase, bpm, beat_number):
        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            self._color_a_idx = (self._color_a_idx + 1) % len(COLOR_WHEEL)
            self._color_b_idx = (self._color_b_idx + 1) % len(COLOR_WHEEL)

        n = len(self.fixtures)
        for i, fix in enumerate(self.fixtures):
            # Chase position: each fixture is offset in the beat
            local_phase = (beat_phase + i / n) % 1.0
            t = (1.0 + math.sin(local_phase * 2 * math.pi)) / 2.0
            fix.color = lerp_color(COLOR_WHEEL[self._color_a_idx], COLOR_WHEEL[self._color_b_idx], t)
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Fire — warm flicker effect
# ---------------------------------------------------------------------------
class FireShow(BaseShow):
    name = "fire"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._flicker = [random.random() for _ in fixtures]
        self._flicker_speed = [0.5 + random.random() * 2.0 for _ in fixtures]

    def _render(self, dt, beat_phase, bpm, beat_number):
        for i, fix in enumerate(self.fixtures):
            # Perlin-ish flicker using multiple sine waves
            t = self._elapsed * self._flicker_speed[i]
            flicker = (
                0.5
                + 0.3 * math.sin(t * 3.7)
                + 0.15 * math.sin(t * 7.1 + 1.2)
                + 0.05 * math.sin(t * 13.0 + 2.4)
            )
            flicker = max(0.3, min(1.0, flicker))
            # Beat punch — boost dimmer briefly on each beat
            punch = max(0, 1.0 - beat_phase * 3) * 0.3 if beat_phase < 0.33 else 0
            intensity = min(1.0, flicker + punch)
            # Warm orange-red color at full saturation; dimmer carries intensity
            fix.color = RGBA(r=255, g=round(80 * flicker), b=0)
            fix.dimmer = round(255 * intensity * self.brightness)
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Rainbow — each fixture gets its own color, shifting through spectrum
# ---------------------------------------------------------------------------
class RainbowShow(BaseShow):
    name = "rainbow"

    def _render(self, dt, beat_phase, bpm, beat_number):
        n = max(len(self.fixtures), 1)
        # Full rainbow cycle every 4 bars (16 beats)
        beats_per_cycle = 16.0
        bps = bpm / 60.0
        cycle_phase = (self._elapsed * bps / beats_per_cycle) % 1.0
        for i, fix in enumerate(self.fixtures):
            hue = (cycle_phase + i / n) % 1.0
            fix.color = hue_to_rgba(hue)
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Thunderstorm — slow color base with random white lightning on beat
# ---------------------------------------------------------------------------
class ThunderstormShow(BaseShow):
    name = "thunderstorm"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._lightning_fixtures: set[int] = set()
        self._lightning_decay = 0.0

    def _render(self, dt, beat_phase, bpm, beat_number):
        # Base: deep blue/purple
        base_pulse = (1.0 + math.sin(self._elapsed * 0.5)) / 2.0
        base = lerp_color(COLORS["blue"], COLORS["purple"], base_pulse)

        # Lightning on beat 1 randomly
        if beat_phase < 0.05 and beat_number == 1 and random.random() < 0.4:
            self._lightning_fixtures = set(random.sample(
                range(len(self.fixtures)),
                k=random.randint(1, len(self.fixtures))
            ))
            self._lightning_decay = 1.0

        self._lightning_decay = max(0.0, self._lightning_decay - dt * 8)

        for i, fix in enumerate(self.fixtures):
            if i in self._lightning_fixtures and self._lightning_decay > 0:
                fix.color = lerp_color(base, COLORS["white"], self._lightning_decay)
            else:
                fix.color = base
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Idle Blue — dim blue wash shown automatically when all decks are paused.
# Breathes slowly so the lights don't look dead; snaps back to the active
# show the moment any deck starts playing again.
# ---------------------------------------------------------------------------
class IdleShow(BaseShow):
    name = "idle"

    # How dim the idle wash sits (fraction of master brightness)
    IDLE_LEVEL = 0.25

    def _render(self, dt, beat_phase, bpm, beat_number):
        # Slow breath: one inhale/exhale every ~4 seconds
        breath = (1.0 + math.sin(self._elapsed * (2 * math.pi / 4.0))) / 2.0
        level = self.IDLE_LEVEL + breath * 0.10   # 0.25 -> 0.35 range
        for fix in self.fixtures:
            fix.color = COLORS["blue"]
            fix.strobe = 0
            fix.dimmer = round(255 * level * self.brightness)


# ---------------------------------------------------------------------------
# Bass Reactive — punchy kick-driven brightness with slow hue shift
# ---------------------------------------------------------------------------
class BassReactiveShow(BaseShow):
    name = "bass_reactive"

    # How many frames to hold the strobe on after a bass spike
    _STROBE_FRAMES = 5

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._strobe_frames_left = 0

    def _render(self, dt, beat_phase, bpm, beat_number):
        audio = self.audio

        # Hue rotates once every 32 bars.  Each bar = 4 beats.
        # seconds_per_bar = 4 * 60 / bpm
        # seconds_per_32_bars = 32 * seconds_per_bar
        bps = bpm / 60.0
        cycle_seconds = 32.0 * 4.0 / bps if bps > 0 else 128.0
        hue = (self._elapsed / cycle_seconds) % 1.0
        base_color = hue_to_rgba(hue)

        # Brightness: directly from bass (instant, no smoothing)
        if audio is not None:
            bass = max(0.0, min(1.0, float(audio.bass)))
        else:
            # Fallback: beat_phase cosine pulse mimicking a kick shape
            bass = max(0.0, (1.0 + math.cos(beat_phase * 2 * math.pi)) / 2.0)

        # Strobe on sharp bass spike
        if audio is not None and audio.bass > 0.85:
            self._strobe_frames_left = self._STROBE_FRAMES

        strobe_active = self._strobe_frames_left > 0
        if strobe_active:
            self._strobe_frames_left -= 1

        for fix in self.fixtures:
            fix.color = base_color
            fix.strobe = 255 if strobe_active else 0
            fix.dimmer = round(bass * 255 * self.brightness)


# ---------------------------------------------------------------------------
# Spectrum — 4 fixtures mapped to frequency bands
# ---------------------------------------------------------------------------
class SpectrumShow(BaseShow):
    name = "spectrum"

    def _render(self, dt, beat_phase, bpm, beat_number):
        audio = self.audio

        if audio is None:
            # Fallback: rainbow
            n = max(len(self.fixtures), 1)
            bps = bpm / 60.0
            cycle_phase = (self._elapsed * bps / 16.0) % 1.0
            for i, fix in enumerate(self.fixtures):
                hue = (cycle_phase + i / n) % 1.0
                fix.color = hue_to_rgba(hue)
                fix.strobe = 0
            return

        bass   = max(0.0, min(1.0, float(audio.bass)))
        mid    = max(0.0, min(1.0, float(audio.mid)))
        # Approximate low-mid / high-mid split from mid
        low_mid  = max(0.0, min(1.0, mid * 1.2))          # tilt toward low-mid
        high_mid = max(0.0, min(1.0, mid * 0.8))          # tilt toward high-mid
        high   = max(0.0, min(1.0, float(audio.high)))

        # Band -> color mapping
        band_colors = [
            (RGBA(r=255, g=0,   b=0),   bass),      # 0: deep red  <- bass
            (RGBA(r=255, g=140, b=0),   low_mid),   # 1: orange    <- low-mid
            (RGBA(r=0,   g=200, b=0),   high_mid),  # 2: green     <- high-mid
            (RGBA(r=0,   g=100, b=255), high),      # 3: blue/white <- highs
        ]

        for i, fix in enumerate(self.fixtures):
            if i < len(band_colors):
                color, level = band_colors[i]
                fix.color = color
                fix.dimmer = round(level * 255 * self.brightness)
            else:
                # Extra fixtures: mirror last band
                color, level = band_colors[-1]
                fix.color = color
                fix.dimmer = round(level * 255 * self.brightness)
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Energy Pulse — audio.energy-driven pulse, smoothed
# ---------------------------------------------------------------------------
class EnergyPulseShow(BaseShow):
    name = "energy_pulse"

    _SMOOTH_ALPHA = 0.4   # exponential smoothing: higher = more responsive

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._hue = 0.0
        self._last_beat_number = -1
        self._smoothed_energy = 0.0

    def _render(self, dt, beat_phase, bpm, beat_number):
        # Color cycles every 8 bars
        bps = bpm / 60.0
        cycle_seconds = 8.0 * 4.0 / bps if bps > 0 else 32.0
        hue = (self._elapsed / cycle_seconds) % 1.0
        color = hue_to_rgba(hue)

        # Energy source
        if self.audio is not None:
            raw_energy = max(0.0, min(1.0, float(self.audio.energy)))
        else:
            # Fallback: beat_phase sine wave (same as PulseShow)
            raw_energy = (1.0 + math.cos(beat_phase * 2 * math.pi)) / 2.0

        # Exponential smoothing
        alpha = self._SMOOTH_ALPHA
        self._smoothed_energy = alpha * raw_energy + (1.0 - alpha) * self._smoothed_energy

        dim = round(self._smoothed_energy * 255 * self.brightness)
        for fix in self.fixtures:
            fix.color = color
            fix.strobe = 0
            fix.dimmer = dim


# ---------------------------------------------------------------------------
# Single Spot Chase — one fixture lit at a time, marching left to right
# ---------------------------------------------------------------------------
class SingleSpotChaseShow(BaseShow):
    name = "spot_chase"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._color_idx = 0
        self._last_beat_number = -1
        self._active_idx = 0

    def _render(self, dt, beat_phase, bpm, beat_number):
        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            self._active_idx = (self._active_idx + 1) % max(len(self.fixtures), 1)
            if beat_number == 1:
                self._color_idx = (self._color_idx + 1) % len(COLOR_WHEEL)

        color = COLOR_WHEEL[self._color_idx]
        tail = 0.3   # fraction of beat over which the trailing glow fades
        for i, fix in enumerate(self.fixtures):
            if i == self._active_idx:
                # Bright on-beat pop with short decay
                t = beat_phase / 0.15 if beat_phase < 0.15 else 1.0
                intensity = 1.0 - t * 0.4
                fix.color = color
                fix.dimmer = round(255 * intensity * self.brightness)
            else:
                fix.color = (0, 0, 0)
                fix.dimmer = 0
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Theater Chase — alternating fixtures in a marquee pattern
# ---------------------------------------------------------------------------
class TheaterChaseShow(BaseShow):
    name = "theater_chase"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._offset = 0
        self._color_idx = 0
        self._last_beat_number = -1

    def _render(self, dt, beat_phase, bpm, beat_number):
        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            self._offset = (self._offset + 1) % 3
            if beat_number == 1:
                self._color_idx = (self._color_idx + 1) % len(COLOR_WHEEL)

        color = COLOR_WHEEL[self._color_idx]
        for i, fix in enumerate(self.fixtures):
            if i % 3 == self._offset:
                fix.color = color
                fix.dimmer = round(255 * self.brightness)
            else:
                fix.color = (0, 0, 0)
                fix.dimmer = 0
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Bounce Chase — single spot marching L→R→L
# ---------------------------------------------------------------------------
class BounceChaseShow(BaseShow):
    name = "bounce_chase"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._pos = 0
        self._direction = 1
        self._color_idx = 0
        self._last_beat_number = -1

    def _render(self, dt, beat_phase, bpm, beat_number):
        n = max(len(self.fixtures), 1)
        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            self._pos += self._direction
            if self._pos >= n:
                self._pos = n - 2
                self._direction = -1
            elif self._pos < 0:
                self._pos = 1
                self._direction = 1
            if beat_number == 1:
                self._color_idx = (self._color_idx + 1) % len(COLOR_WHEEL)

        color = COLOR_WHEEL[self._color_idx]
        for i, fix in enumerate(self.fixtures):
            if i == self._pos:
                fix.color = color
                fix.dimmer = round(255 * self.brightness)
            else:
                # Soft bleed: adjacent fixtures at low level
                dist = abs(i - self._pos)
                if dist == 1:
                    fix.color = color
                    fix.dimmer = round(60 * self.brightness)
                else:
                    fix.color = (0, 0, 0)
                    fix.dimmer = 0
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Color Wipe — fills fixtures one by one, then wipes them off
# ---------------------------------------------------------------------------
class ColorWipeShow(BaseShow):
    name = "color_wipe"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._beat_count = 0
        self._color_idx = 0
        self._last_beat_number = -1
        self._lit = set()

    def _render(self, dt, beat_phase, bpm, beat_number):
        n = max(len(self.fixtures), 1)
        total_steps = n * 2   # fill then wipe

        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            step = self._beat_count % total_steps
            if step < n:
                self._lit.add(step)
            else:
                wipe_idx = step - n
                self._lit.discard(wipe_idx)
            self._beat_count += 1
            if self._beat_count % total_steps == 0:
                self._color_idx = (self._color_idx + 1) % len(COLOR_WHEEL)

        color = COLOR_WHEEL[self._color_idx]
        for i, fix in enumerate(self.fixtures):
            if i in self._lit:
                fix.color = color
                fix.dimmer = round(255 * self.brightness)
            else:
                fix.color = (0, 0, 0)
                fix.dimmer = 0
            fix.strobe = 0


# ---------------------------------------------------------------------------
# Strobe Chase — each fixture strobes in sequence
# ---------------------------------------------------------------------------
class StrobeChaseShow(BaseShow):
    name = "strobe_chase"

    def __init__(self, fixtures, brightness=1.0, audio=None):
        super().__init__(fixtures, brightness, audio)
        self._active_idx = 0
        self._color_idx = 0
        self._last_beat_number = -1

    def _render(self, dt, beat_phase, bpm, beat_number):
        if beat_number != self._last_beat_number:
            self._last_beat_number = beat_number
            self._active_idx = (self._active_idx + 1) % max(len(self.fixtures), 1)
            if beat_number == 1:
                self._color_idx = (self._color_idx + 1) % len(COLOR_WHEEL)

        color = COLOR_WHEEL[self._color_idx]
        for i, fix in enumerate(self.fixtures):
            if i == self._active_idx:
                fix.color = color
                fix.dimmer = round(255 * self.brightness)
                # Strobe only on the active fixture (hardware strobe channel)
                fix.strobe = 180
            else:
                fix.color = (0, 0, 0)
                fix.dimmer = 0
                fix.strobe = 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
ALL_SHOWS: dict[str, type[BaseShow]] = {
    cls.name: cls
    for cls in [
        BeatStrobeShow, ColorCycleShow, PulseShow,
        ChaseShow, FireShow, RainbowShow, ThunderstormShow,
        IdleShow,
        BassReactiveShow, SpectrumShow, EnergyPulseShow,
        SingleSpotChaseShow, TheaterChaseShow, BounceChaseShow,
        ColorWipeShow, StrobeChaseShow,
    ]
}


def get_show(
    name: str,
    fixtures: list[SlimPar56],
    brightness: float = 1.0,
    **kwargs,
) -> BaseShow:
    cls = ALL_SHOWS.get(name)
    if cls is None:
        raise ValueError(f"Unknown show '{name}'. Available: {list(ALL_SHOWS)}")
    return cls(fixtures, brightness, **kwargs)

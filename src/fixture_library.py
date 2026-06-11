"""
Fixture type system supporting multiple DMX profiles.

FixtureProfile describes a fixture type (manufacturer, channel layout, tags).
FixtureInstance holds runtime state for one physical unit (address, color,
dimmer, strobe, and any extra channel values).

BUILTIN_PROFILES ships common profiles so shows can reference fixtures without
importing OFL files.  The chauvet_slimpar56_7ch profile mirrors the channel
layout in fixture.py exactly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Channel / profile definitions
# ---------------------------------------------------------------------------

VALID_CHANNEL_TYPES = frozenset({
    "intensity", "red", "green", "blue", "amber", "white",
    "strobe", "pan", "tilt", "zoom", "gobo", "color_wheel",
    "program", "speed", "other",
})


@dataclass
class ChannelDef:
    """Definition of a single DMX channel within a fixture profile."""

    name: str   # Human-readable label, e.g. "Red", "Dimmer", "Strobe"
    type: str   # One of VALID_CHANNEL_TYPES
    default: int = 0

    def __post_init__(self) -> None:
        if self.type not in VALID_CHANNEL_TYPES:
            log.warning("Unknown channel type %r for channel %r; using 'other'", self.type, self.name)
            self.type = "other"
        if not (0 <= self.default <= 255):
            raise ValueError(f"Channel default {self.default!r} for {self.name!r} must be 0-255")


@dataclass
class FixtureProfile:
    """Describes a fixture model and its DMX channel layout for one mode."""

    manufacturer: str
    name: str
    channels: list[ChannelDef]
    tags: list[str]   # e.g. ["par", "led", "moving_head"]

    @property
    def channel_count(self) -> int:
        return len(self.channels)

    def channel_index(self, type: str) -> int | None:
        """Return the 0-based index of the first channel of *type*, or None."""
        for i, ch in enumerate(self.channels):
            if ch.type == type:
                return i
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<FixtureProfile {self.manufacturer!r} {self.name!r} "
            f"{self.channel_count}ch tags={self.tags}>"
        )


# ---------------------------------------------------------------------------
# Runtime instance
# ---------------------------------------------------------------------------

@dataclass
class FixtureInstance:
    """
    Runtime state for a single physical DMX fixture.

    *color*  holds the current R,G,B intent (0-255 each).
    *dimmer* is the master intensity channel value (0-255).
    *strobe* is the strobe channel value (0=off).
    *extra_channels* maps channel offset (0-based within the fixture's footprint)
    to raw DMX value for channels that are not red/green/blue/dimmer/strobe.
    """

    name: str
    profile: FixtureProfile
    dmx_start: int                          # 1-based, as set on the fixture
    group: str = "default"
    universe: int = 0
    color: tuple[int, int, int] = (0, 0, 0)
    dimmer: int = 255
    strobe: int = 0
    extra_channels: dict[int, int] = field(default_factory=dict)  # offset → value

    @property
    def dmx_offset(self) -> int:
        """0-based start index in the DMX universe (dmx_start - 1)."""
        return self.dmx_start - 1

    def dmx_values(self) -> list[int]:
        """
        Build a DMX frame for this fixture's full channel footprint.

        Channel values are resolved in priority order:
          1. Caller-supplied override in extra_channels (by offset).
          2. Profile channel type mapped from color / dimmer / strobe fields.
          3. Channel default from the profile definition.
        """
        # Accept both (r,g,b) tuples and RGBA objects from shows
        c = self.color
        if hasattr(c, 'r'):
            r, g, b = c.r, c.g, c.b
        else:
            r, g, b = c[0], c[1], c[2]
        type_map: dict[str, int] = {
            "red":       r,
            "green":     g,
            "blue":      b,
            "intensity": self.dimmer,
            "strobe":    self.strobe,
        }

        values: list[int] = []
        for offset, ch in enumerate(self.profile.channels):
            if offset in self.extra_channels:
                raw = self.extra_channels[offset]
            elif ch.type in type_map:
                raw = type_map[ch.type]
            else:
                raw = ch.default
            values.append(max(0, min(255, raw)))

        log.debug(
            "FixtureInstance %r universe=%d start=%d values=%s",
            self.name, self.universe, self.dmx_start, values,
        )
        return values

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<FixtureInstance {self.name!r} profile={self.profile.name!r} "
            f"u{self.universe}@{self.dmx_start}>"
        )


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

def _profile(
    manufacturer: str,
    name: str,
    channels: list[tuple[str, str, int]],
    tags: list[str],
) -> FixtureProfile:
    """Helper: build a FixtureProfile from (name, type, default) triples."""
    return FixtureProfile(
        manufacturer=manufacturer,
        name=name,
        channels=[ChannelDef(n, t, d) for n, t, d in channels],
        tags=tags,
    )


BUILTIN_PROFILES: dict[str, FixtureProfile] = {
    # ------------------------------------------------------------------
    # Chauvet SlimPAR 56 — 7-channel mode
    # Matches the channel layout in fixture.py exactly:
    #   Ch1 Red, Ch2 Green, Ch3 Blue, Ch4 unused, Ch5 Strobe,
    #   Ch6 unused, Ch7 Dimmer
    # ------------------------------------------------------------------
    "chauvet_slimpar56_7ch": _profile(
        manufacturer="Chauvet",
        name="SlimPAR 56 (7ch)",
        channels=[
            ("Red",    "red",       0),
            ("Green",  "green",     0),
            ("Blue",   "blue",      0),
            ("Unused", "other",     0),
            ("Strobe", "strobe",    0),
            ("Unused", "other",     0),
            ("Dimmer", "intensity", 255),
        ],
        tags=["par", "led", "color_changer"],
    ),

    # ------------------------------------------------------------------
    # Chauvet SlimPAR 56 — 3-channel mode
    # ------------------------------------------------------------------
    "chauvet_slimpar56_3ch": _profile(
        manufacturer="Chauvet",
        name="SlimPAR 56 (3ch)",
        channels=[
            ("Red",   "red",   0),
            ("Green", "green", 0),
            ("Blue",  "blue",  0),
        ],
        tags=["par", "led", "color_changer"],
    ),

    # ------------------------------------------------------------------
    # Generic single-channel dimmer
    # ------------------------------------------------------------------
    "generic_dimmer_1ch": _profile(
        manufacturer="Generic",
        name="Dimmer (1ch)",
        channels=[
            ("Dimmer", "intensity", 0),
        ],
        tags=["dimmer"],
    ),

    # ------------------------------------------------------------------
    # Generic 3-channel RGB
    # ------------------------------------------------------------------
    "generic_rgb_3ch": _profile(
        manufacturer="Generic",
        name="RGB (3ch)",
        channels=[
            ("Red",   "red",   0),
            ("Green", "green", 0),
            ("Blue",  "blue",  0),
        ],
        tags=["led", "color_changer"],
    ),

    # ------------------------------------------------------------------
    # Generic 4-channel RGBA
    # ------------------------------------------------------------------
    "generic_rgba_4ch": _profile(
        manufacturer="Generic",
        name="RGBA (4ch)",
        channels=[
            ("Red",   "red",   0),
            ("Green", "green", 0),
            ("Blue",  "blue",  0),
            ("Amber", "amber", 0),
        ],
        tags=["led", "color_changer"],
    ),
}

log.debug("fixture_library loaded with %d built-in profiles", len(BUILTIN_PROFILES))

"""
Chauvet SlimPAR 56 fixture model.

7-channel DMX mode (as set on the fixture's dip switches):
  Ch 1 (offset 0): Red       0-255
  Ch 2 (offset 1): Green     0-255
  Ch 3 (offset 2): Blue      0-255
  Ch 4 (offset 3): unused    (send 0)
  Ch 5 (offset 4): Strobe    0=off, 1-255=strobe (faster as value increases)
  Ch 6 (offset 5): unused    (send 0)
  Ch 7 (offset 6): Dimmer    0=off, 255=full

The dmx_start address is 1-based as you set on the fixture itself.
Art-Net uses 0-based indices internally, so we subtract 1.

With the dimmer channel available, shows set RGB to full saturation and
control intensity via dimmer — this gives smoother fades than scaling RGB.
"""

from dataclasses import dataclass
import math


@dataclass
class RGB:
    r: int = 0
    g: int = 0
    b: int = 0

    def __iter__(self):
        return iter((self.r, self.g, self.b))


# Keep RGBA as an alias so shows don't need a rename — amber is just ignored.
@dataclass
class RGBA:
    r: int = 0
    g: int = 0
    b: int = 0
    a: int = 0  # ignored in 7-ch mode; kept so shows compile unchanged

    def scale(self, factor: float) -> "RGBA":
        """Kept for API compatibility — prefer fixture.dimmer for brightness."""
        f = max(0.0, min(1.0, factor))
        return RGBA(
            r=round(self.r * f),
            g=round(self.g * f),
            b=round(self.b * f),
            a=round(self.a * f),
        )

    def __iter__(self):
        return iter((self.r, self.g, self.b, self.a))


# Palette of vivid colors for light shows (amber field unused in 7-ch mode)
COLORS = {
    "red":     RGBA(255, 0,   0,   0),
    "orange":  RGBA(255, 80,  0,   0),
    "yellow":  RGBA(255, 200, 0,   0),
    "green":   RGBA(0,   255, 0,   0),
    "cyan":    RGBA(0,   255, 255, 0),
    "blue":    RGBA(0,   0,   255, 0),
    "purple":  RGBA(180, 0,   255, 0),
    "magenta": RGBA(255, 0,   180, 0),
    "white":   RGBA(255, 255, 255, 0),
    "warm":    RGBA(255, 80,  0,   0),
    "off":     RGBA(0,   0,   0,   0),
}

COLOR_WHEEL = [
    COLORS["red"], COLORS["orange"], COLORS["yellow"], COLORS["green"],
    COLORS["cyan"], COLORS["blue"], COLORS["purple"], COLORS["magenta"],
]


def lerp_color(a: RGBA, b: RGBA, t: float) -> RGBA:
    t = max(0.0, min(1.0, t))
    return RGBA(
        r=round(a.r + (b.r - a.r) * t),
        g=round(a.g + (b.g - a.g) * t),
        b=round(a.b + (b.b - a.b) * t),
        a=round(a.a + (b.a - a.a) * t),
    )


def hue_to_rgba(hue: float) -> RGBA:
    """hue in [0, 1) → RGBA (no amber)"""
    h = hue * 6.0
    i = int(h) % 6
    f = h - int(h)
    p = 0
    q = round(255 * (1 - f))
    t = round(255 * f)
    v = 255
    table = [
        (v, t, p), (q, v, p), (p, v, t),
        (p, q, v), (t, p, v), (v, p, q),
    ]
    r, g, b = table[i]
    return RGBA(r, g, b, 0)


@dataclass
class SlimPar56:
    name: str
    dmx_start: int       # 1-based address as set on fixture dip switches
    color: RGBA = None
    strobe: int = 0      # ch5: 0=off, 1-255=strobe speed
    dimmer: int = 255    # ch7: 0=off, 255=full brightness

    def __post_init__(self):
        if self.color is None:
            self.color = RGBA()

    @property
    def dmx_offset(self) -> int:
        """0-based start index in the DMX universe."""
        return self.dmx_start - 1

    def dmx_values(self) -> list[int]:
        """Returns 7 DMX bytes matching the fixture's 7-channel mode."""
        r, g, b, _ = self.color   # amber field ignored
        return [
            max(0, min(255, r)),       # ch1 Red
            max(0, min(255, g)),       # ch2 Green
            max(0, min(255, b)),       # ch3 Blue
            0,                         # ch4 unused
            max(0, min(255, self.strobe)),  # ch5 Strobe
            0,                         # ch6 unused
            max(0, min(255, self.dimmer)),  # ch7 Dimmer
        ]

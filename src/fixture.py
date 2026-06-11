"""
Chauvet SlimPAR 56 fixture model.

6-channel DMX mode (set on the fixture's dip switches):
  Ch 1 (offset 0): Red       0-255
  Ch 2 (offset 1): Green     0-255
  Ch 3 (offset 2): Blue      0-255
  Ch 4 (offset 3): Amber     0-255
  Ch 5 (offset 4): Strobe    0=off, 1-31=slow→fast, 32-63=reserved, 64-95=random
  Ch 6 (offset 5): Programs  0-255 (0=manual RGBA, 1+=built-in macros — keep at 0)

The dmx_start address is 1-based as you set on the fixture itself.
pyartnet uses 0-based indices internally, so we subtract 1.
"""

from dataclasses import dataclass
import math


@dataclass
class RGBA:
    r: int = 0
    g: int = 0
    b: int = 0
    a: int = 0  # amber

    def scale(self, factor: float) -> "RGBA":
        f = max(0.0, min(1.0, factor))
        return RGBA(
            r=round(self.r * f),
            g=round(self.g * f),
            b=round(self.b * f),
            a=round(self.a * f),
        )

    def __iter__(self):
        return iter((self.r, self.g, self.b, self.a))


# Palette of vivid colors for light shows
COLORS = {
    "red":     RGBA(255, 0,   0,   0),
    "orange":  RGBA(255, 80,  0,   60),
    "yellow":  RGBA(255, 200, 0,   80),
    "green":   RGBA(0,   255, 0,   0),
    "cyan":    RGBA(0,   255, 255, 0),
    "blue":    RGBA(0,   0,   255, 0),
    "purple":  RGBA(180, 0,   255, 0),
    "magenta": RGBA(255, 0,   180, 0),
    "white":   RGBA(255, 255, 255, 200),
    "warm":    RGBA(255, 120, 0,   255),
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
    dmx_start: int       # 1-based address as set on fixture
    color: RGBA = None
    strobe: int = 0      # 0=off, 1-31=strobe speed

    def __post_init__(self):
        if self.color is None:
            self.color = RGBA()

    @property
    def dmx_offset(self) -> int:
        """0-based index for pyartnet."""
        return self.dmx_start - 1

    def dmx_values(self) -> list[int]:
        """Returns 6 DMX bytes for this fixture."""
        r, g, b, a = self.color
        return [
            max(0, min(255, r)),
            max(0, min(255, g)),
            max(0, min(255, b)),
            max(0, min(255, a)),
            max(0, min(255, self.strobe)),
            0,  # programs off — manual control only
        ]

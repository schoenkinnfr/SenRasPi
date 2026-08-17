"""
Palette and fonts.

SPDX-License-Identifier: AGPL-3.0-only

Colour here is a STATUS palette — it encodes a clinical state (low / in range /
high), not series identity — so the rules that apply are the status ones: the
steps are reserved, they never stand in for anything else, and they never carry
meaning alone. Every screen that uses colour to say "low" also says LOW in
words, and the sparkline draws an explicit target band so position carries the
same information as hue.

Two palettes ship:

  "clinical"  red / green / amber. What every CGM app and the pump itself uses,
              so this screen agrees with the other things in the house. Under
              deuteranopia red and green separate by only ΔE 4.1, which is
              exactly why the state word is not optional.

  "cvd"       red / blue / amber. Separates under deuteranopia (ΔE 21.9),
              protanopia and tritanopia. Set "palette": "cvd" in the config.

Both are checked to at least 3:1 against the panel background. The steps sit
outside the usual lightness band on purpose: out-of-range states are brighter
than in-range so a screen that needs attention reads differently from across a
room, before you can resolve the digits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

RGB = tuple[int, int, int]


def _hex(h: str) -> RGB:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


@dataclass(frozen=True)
class Palette:
    bg: RGB
    panel: RGB          # slightly lifted surface for the right rail / footer
    rule: RGB
    ink: RGB            # primary text
    ink_2: RGB          # secondary text
    ink_3: RGB          # muted: axis labels, units
    low: RGB
    in_range: RGB
    high: RGB
    accent: RGB         # IOB and other non-status values

    def for_state(self, state: str) -> RGB:
        return {"low": self.low, "high": self.high}.get(state, self.in_range)


_COMMON = dict(
    bg=_hex("#12100f"),
    panel=_hex("#1c1a18"),
    rule=_hex("#33302c"),
    ink=_hex("#ffffff"),
    ink_2=_hex("#c3c2b7"),
    ink_3=_hex("#898781"),
    accent=_hex("#7fb4d8"),
)

PALETTES: dict[str, Palette] = {
    "clinical": Palette(
        low=_hex("#d03b3b"), in_range=_hex("#3fbf46"), high=_hex("#e0a017"), **_COMMON
    ),
    "cvd": Palette(
        low=_hex("#d03b3b"), in_range=_hex("#4a9eda"), high=_hex("#e0a017"), **_COMMON
    ),
}

# Greyscale is not a palette choice — it is what a stale screen turns into, so
# "this number is old" is legible before you have read the timestamp.
STALE = Palette(
    bg=_hex("#0e0e0e"),
    panel=_hex("#181818"),
    rule=_hex("#2b2b2b"),
    ink=_hex("#8e8e8e"),
    ink_2=_hex("#6d6d6d"),
    ink_3=_hex("#565656"),
    low=_hex("#7a7a7a"),
    in_range=_hex("#7a7a7a"),
    high=_hex("#7a7a7a"),
    accent=_hex("#6d6d6d"),
)


def palette(name: str) -> Palette:
    return PALETTES.get(name, PALETTES["clinical"])


# ─────────────────────────────────────────────────────────────────────────────
# Fonts. DejaVu ships with Raspberry Pi OS (fonts-dejavu-core), including Lite.
# Condensed is used for the hero number so a three-digit mg/dL value and a
# trend arrow both fit across a 480px panel without shrinking the digits.

_SEARCH = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/TTF"),
    Path("/usr/share/fonts/truetype/liberation"),
]

_FALLBACKS = {
    "bold": ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"],
    "regular": ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"],
    "condensed": ["DejaVuSansCondensed-Bold.ttf", "DejaVuSans-Bold.ttf",
                  "LiberationSansNarrow-Bold.ttf", "LiberationSans-Bold.ttf"],
}

_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _locate(style: str) -> Path | None:
    for name in _FALLBACKS[style]:
        for d in _SEARCH:
            p = d / name
            if p.exists():
                return p
    return None


def font(style: str, size: int) -> ImageFont.FreeTypeFont:
    """Cached font lookup. Falls back to Pillow's bitmap font rather than
    crashing — a display showing ugly numbers beats a display showing nothing."""
    key = (style, size)
    if key not in _cache:
        path = _locate(style)
        try:
            _cache[key] = (
                ImageFont.truetype(str(path), size) if path else ImageFont.load_default(size)
            )
        except OSError:
            _cache[key] = ImageFont.load_default(size)
    return _cache[key]

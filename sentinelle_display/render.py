"""
Draws the dashboard.

SPDX-License-Identifier: AGPL-3.0-only

One function matters: render(cfg, snapshot, now) -> PIL.Image. Everything is
laid out against a 480x320 reference and multiplied by a single scale factor,
so the same code fills a 320x240 panel or an 800x480 one without a second
layout. Nothing here touches hardware or the network, which is what makes it
testable: `sentinelle-display preview` renders PNGs from canned data on any
machine.

Layout at 480x320 (a 3.5" panel is ~165 ppi, so 1px ~= 0.15mm):

    +--------------------------------------------------+
    |  142 ^          |  INSULIN ON BOARD              |
    |  IN RANGE  +6   |  2.4 u                         |
    |                 |  Reservoir  118 u  Batt  72%   |
    +--------------------------------------------------+
    |  [ 3h trend, target band shaded ]                |
    +--------------------------------------------------+
    |  2 min ago   [ TIR bar ]  78% in range     21:47 |
    +--------------------------------------------------+

Design decisions that are easy to undo by accident, and shouldn't be:

  - Gaps break the trend line. Any interval longer than 20 minutes starts a
    new segment instead of drawing straight through. A smooth line across a
    two-hour sensor outage implies readings that were never received.
  - Stale data is greyed, not hidden. Removing the number makes you tap the
    screen wondering if it's broken; desaturating it makes "this is old"
    legible from across a room.
  - The state is always written in words next to the number. Under the default
    clinical palette, low and in-range are nearly indistinguishable to a
    red-green colourblind reader — colour is the fast channel here, never the
    only one.
  - An unknown trend string renders nothing, not a guessed arrow.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw

from .client import Snapshot
from .theme import STALE, Palette, font, palette

REF_W, REF_H = 480, 320
MMOL_FACTOR = 18.016
GAP_MINUTES = 20  # break the sparkline across intervals longer than this

# CareLink's trend vocabulary, mapped to (degrees above horizontal, how many
# arrows). Anything not in here draws NOTHING — an unrecognised trend string
# must never become a guessed arrow, because a guessed arrow pointing the
# wrong way is worse than no arrow at all.
#
# Drawn as geometry rather than as ↗ ⇈ glyphs on purpose: the double-arrow
# characters are missing from several fonts that ship on a Lite image, and a
# missing glyph renders as a tofu box that looks like a warning symbol.
TRENDS: dict[str, tuple[int, int]] = {
    "UP_TRIPLE": (90, 3), "UP_DOUBLE": (90, 2), "UP": (90, 1),
    "UP_FORTY_FIVE": (45, 1), "FLAT": (0, 1), "NONE": (0, 1),
    "DOWN_FORTY_FIVE": (-45, 1), "DOWN": (-90, 1),
    "DOWN_DOUBLE": (-90, 2), "DOWN_TRIPLE": (-90, 3),
}


def _state(mgdl: float, low: int, high: int) -> str:
    if mgdl < low:
        return "low"
    if mgdl > high:
        return "high"
    return "in_range"


STATE_WORD = {"low": "LOW", "high": "HIGH", "in_range": "IN RANGE"}


class Layout:
    """Scaled geometry. Every number in render() goes through this.

    `w`/`h` are the LOGICAL canvas — what the layout is drawn on. When the
    panel is mounted rotated 90 or 270 degrees the logical canvas is the
    panel's dimensions swapped, and render() rotates the finished image back
    to the panel's real orientation on the way out. Getting this wrong is the
    classic small-panel bug: a dashboard that renders beautifully and then
    appears cropped to a third of the screen.
    """

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.s = min(w / REF_W, h / REF_H)

    def px(self, v: float) -> int:
        return max(1, round(v * self.s))

    def pt(self, v: float) -> int:
        return max(6, round(v * self.s))


def _fmt_value(mgdl: float | None, units: str) -> str:
    if mgdl is None:
        return "--"
    if units == "mmol":
        return f"{mgdl / MMOL_FACTOR:.1f}"
    return str(int(round(mgdl)))


def _fmt_delta(delta: float | None, units: str) -> str:
    if delta is None:
        return ""
    if units == "mmol":
        v = delta / MMOL_FACTOR
        return f"{v:+.1f}"
    return f"{int(round(delta)):+d}"


def _fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "no data"
    m = int(round(minutes))
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    h, rem = divmod(m, 60)
    return f"{h}h {rem}m ago" if rem else f"{h}h ago"


def _fmt_sensor(minutes: Any) -> str | None:
    """CareLink reports sensor life remaining in minutes."""
    if minutes in (None, ""):
        return None
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return "expired"
    d, rem = divmod(m, 1440)
    h = rem // 60
    return f"{d}d {h}h" if d else f"{h}h"


def _text(draw: ImageDraw.ImageDraw, xy, s: str, f, fill, anchor="la") -> None:
    draw.text(xy, s, font=f, fill=fill, anchor=anchor)


def _width(draw: ImageDraw.ImageDraw, s: str, f) -> int:
    return int(draw.textlength(s, font=f))


# ─────────────────────────────────────────────────────────────────────────────


VIEWS = ("full", "minimal")


def render(
    cfg,
    snap: Snapshot,
    now: float | None = None,
    view: str = "full",
    state: dict | None = None,
    bar: bool = False,
    hint: bool = False,
    force_day: bool = False,
    pollen=None,
) -> Image.Image:
    """The whole screen. Never raises — a renderer that throws leaves a Pi
    showing a frozen frame with no clue why, so failures are drawn instead.

    `view` selects between the full dashboard and the across-the-room one.
    Night mode overrides both: a bright dashboard in a bedroom at 3am is not
    what anyone wants, and that stays true whichever view is selected --
    unless `force_day`, which is what a tap during the night window sets, so
    walking past at 3am and touching the glass shows you the real screen for
    a few seconds before it fades back.

    `state` is the live, runtime-mutable view/night_mode/units the control bar
    reflects and changes. It is separate from cfg because the buttons change
    what is on screen now without rewriting the config file on disk.

    `bar` draws the control bar; `hint` draws the small dotted chip that says
    the bar exists. Both are decided by the caller, because only the caller
    knows whether this output can actually be clicked.
    """
    now = now or time.time()
    lay = Layout(*_logical_size(cfg))

    d = snap.data or {}
    minutes_ago = d.get("minutes_ago")
    stale = (
        cfg.stale_minutes > 0
        and minutes_ago is not None
        and minutes_ago > cfg.stale_minutes
    )
    if not snap.data:
        return _render_empty(cfg, lay, snap)

    pal = STALE if stale else palette(getattr(cfg, "palette", "clinical"))

    state = state or {}
    if is_night(cfg, now, state.get("night_mode")) and not force_day:
        # The bar has to survive the wash. Without this, setting NIGHT to "On"
        # would hide the very control needed to set it back -- a one-way door
        # you could only escape over SSH.
        return _render_night(cfg, lay, pal, d, stale, cfg_state=state,
                             bar=bar, hint=hint, view=view)

    img = Image.new("RGB", (lay.w, lay.h), pal.bg)
    draw = ImageDraw.Draw(img)

    # Reserve the bar's strip so the dashboard is drawn into the space that
    # is actually left, rather than under a bar that then covers it.
    content = Layout(lay.w, lay.h - (lay.px(BAR_H) if bar else 0))
    content.w, content.h = lay.w, lay.h - (lay.px(BAR_H) if bar else 0)

    if view == "minimal":
        _draw_minimal(draw, content, pal, cfg, d, stale, now)
    else:
        _draw_hero(draw, content, pal, cfg, d, stale)
        _draw_rail(draw, content, pal, cfg, d)
        if pollen is not None:
            # Trend gets 75% of the row, the allergen panel the rest.
            _draw_trend(draw, content, pal, cfg, d, now, width_frac=0.75)
            _draw_pollen(draw, content, pal, pollen,
                         x0=content.px(16) + int((content.w - content.px(24)) * 0.75)
                            + content.px(6))
        else:
            _draw_trend(draw, content, pal, cfg, d, now)
        _draw_footer(draw, content, pal, cfg, d, snap, now)

    # Drawn LAST so nothing covers the controls.
    if bar:
        _draw_bar(draw, lay, pal, button_layout(cfg, {**state, "view": view}, lay.w, lay.h))
    elif hint:
        _draw_bar_hint(draw, lay, pal)

    if snap.offline:
        _draw_offline_banner(draw, lay, pal, snap)

    return _orient(img, cfg)


def _draw_view_button(draw, lay: Layout, pal: Palette, view: str,
                      can_hide: bool = True) -> None:
    """Two chips in the bottom-right corner naming both gestures.

    Deliberately understated: this is an ambient display, and a bright button
    competing with the glucose number would be the wrong thing to notice from
    across a room. But a control nobody can discover is not a control, and
    "hold to hide" is not a gesture anyone guesses.

    The chips are labels, not hit targets -- a tap or hold ANYWHERE on the
    glass works. See touch.py for why there is no hit-region.
    """
    f = font("regular", lay.pt(11))
    chips = [f"tap · {'more' if view == 'minimal' else 'less'}"]
    if can_hide:
        chips.append("hold · hide")

    x1 = lay.w - lay.px(6)
    y1, h = lay.h - lay.px(5), lay.px(20)
    y0 = y1 - h
    for label in reversed(chips):
        w = int(draw.textlength(label, font=f)) + lay.px(14)
        x0 = x1 - w
        draw.rounded_rectangle([x0, y0, x1, y1], radius=lay.px(5),
                               fill=pal.panel, outline=pal.rule, width=lay.px(1))
        _text(draw, ((x0 + x1) // 2, (y0 + y1) // 2), label, f, pal.ink_3, anchor="mm")
        x1 = x0 - lay.px(5)


def _logical_size(cfg) -> tuple[int, int]:
    return (cfg.height, cfg.width) if cfg.rotate in (90, 270) else (cfg.width, cfg.height)


def _orient(img: Image.Image, cfg) -> Image.Image:
    """Rotates the logical canvas into the panel's physical orientation."""
    if not cfg.rotate:
        return img
    return img.rotate(-cfg.rotate, expand=True)



# ─────────────────────────────────────────────────────────────────────────────
# The control bar.
#
# Geometry lives in ONE function, button_layout(), which both the renderer and
# the click handler call. Drawing buttons in one place and hit-testing them in
# another is how you get a control that visibly moves but still responds where
# it used to be -- and on a touchscreen that is indistinguishable from broken
# hardware.
#
# The bar is not permanent. An always-on display should mostly be the reading,
# so it appears on a tap and hides itself again a few seconds later. A small
# dotted chip in the corner is the standing hint that it exists.


BAR_H = 44          # reference-space height
N_BUTTONS = 4


@dataclass(frozen=True)
class Button:
    key: str        # "view" | "night" | "units" | "minimize"
    caption: str    # small label above
    value: str      # current state, or the action for minimize
    x0: int
    y0: int
    x1: int
    y1: int

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


def button_layout(cfg, state: dict, w: int, h: int) -> list[Button]:
    """The bar's buttons with their real pixel rects, for the CURRENT state.

    `state` carries the live values the buttons reflect: view, night_mode and
    units. They are passed in rather than read off cfg because the buttons
    change things at runtime without rewriting the config file.
    """
    lay = Layout(w, h)
    bar_h = lay.px(BAR_H)
    y0, y1 = h - bar_h, h
    step = w / N_BUTTONS

    night = {"auto": "Auto", "on": "On", "off": "Off"}.get(state.get("night_mode", "auto"), "Auto")
    specs = [
        ("view", "VIEW", "Minimal" if state.get("view") == "minimal" else "Full"),
        ("night", "NIGHT", night),
        ("units", "UNITS", "mmol/L" if state.get("units") == "mmol" else "mg/dL"),
        ("minimize", "", "Minimize"),
    ]
    return [
        Button(key, cap, val, round(i * step), y0, round((i + 1) * step) - 1, y1)
        for i, (key, cap, val) in enumerate(specs)
    ]


def hit_test(buttons: list[Button], x: float, y: float) -> str | None:
    for b in buttons:
        if b.contains(x, y):
            return b.key
    return None


def _draw_bar(draw, lay: Layout, pal: Palette, buttons: list[Button]) -> None:
    if not buttons:
        return
    y0 = buttons[0].y0
    draw.rectangle([0, y0, lay.w, lay.h], fill=pal.panel)
    draw.line([(0, y0), (lay.w, y0)], fill=pal.rule, width=lay.px(1))

    f_cap = font("regular", lay.pt(9))
    f_val = font("bold", lay.pt(13))
    for i, b in enumerate(buttons):
        if i:                                   # hairline between buttons
            draw.line([(b.x0, y0 + lay.px(6)), (b.x0, lay.h - lay.px(6))],
                      fill=pal.rule, width=lay.px(1))
        cx = (b.x0 + b.x1) // 2
        if b.caption:
            _text(draw, (cx, y0 + lay.px(9)), b.caption, f_cap, pal.ink_3, anchor="mm")
            _text(draw, (cx, y0 + lay.px(28)), b.value, f_val, pal.ink, anchor="mm")
        else:
            _text(draw, (cx, (y0 + lay.h) // 2), b.value, f_val, pal.ink_2, anchor="mm")


def _draw_bar_hint(draw, lay: Layout, pal: Palette) -> None:
    """The standing affordance while the bar is hidden. Deliberately tiny --
    an ambient screen should mostly be the number."""
    w, h = lay.px(30), lay.px(16)
    x1, y1 = lay.w - lay.px(6), lay.h - lay.px(5)
    x0, y0 = x1 - w, y1 - h
    draw.rounded_rectangle([x0, y0, x1, y1], radius=lay.px(4),
                           fill=pal.panel, outline=pal.rule, width=lay.px(1))
    _text(draw, ((x0 + x1) // 2, (y0 + y1) // 2 - lay.px(1)), "•••",
          font("bold", lay.pt(10)), pal.ink_3, anchor="mm")


# ─────────────────────────────────────────────────────────────────────────────
# Hero: the number, the arrow, the state word, the delta.


RAIL_X = 288  # reference-space left edge of the right-hand rail


def _draw_hero(draw, lay: Layout, pal: Palette, cfg, d: dict, stale: bool) -> None:
    mgdl = d.get("mgdl")
    state = _state(mgdl, cfg.low, cfg.high) if mgdl is not None else "in_range"
    colour = pal.for_state(state)

    value = _fmt_value(mgdl, cfg.units)
    trend = TRENDS.get(str(d.get("trend") or "").upper())
    x, y = lay.px(16), lay.px(6)

    # Fit the number and its arrow to the space left of the rail rather than
    # trusting a fixed point size. "24.4" in mmol/L is four glyphs wide, a
    # triple arrow is three arrows wide, and the available font may not be the
    # condensed one — any of those overflows a hardcoded 124pt into the rail.
    arrow_budget = lay.px(20 + 15 * (trend[1] - 1) + 46) if trend else 0
    available = lay.px(RAIL_X) - lay.px(10) - x - arrow_budget
    f_hero = font("condensed", lay.pt(124))
    for size in (124, 116, 108, 100, 92, 84, 76):
        f_hero = font("condensed", lay.pt(size))
        if _width(draw, value, f_hero) <= available:
            break

    _text(draw, (x, y), value, f_hero, colour)
    # Real ink extents, not the font's nominal box: digits have no descender,
    # so the nominal box leaves a band of dead space the state row can use.
    bbox = draw.textbbox((x, y), value, font=f_hero)
    vw, v_bottom = bbox[2] - bbox[0], bbox[3]

    # Arrow on the digits' vertical midpoint, so it reads as part of the
    # number rather than as a separate widget floating beside it.
    if trend:
        angle, count = trend
        _draw_arrows(draw, lay, x + vw + arrow_budget // 2, (bbox[1] + bbox[3]) // 2,
                     angle, count, colour)

    # State in words + delta. This line is why the palette choice is not a
    # safety question: the screen says LOW whether or not you can see red.
    row_y = v_bottom + lay.px(8)
    f_state = font("bold", lay.pt(19))
    word = "STALE" if stale else STATE_WORD[state]
    _text(draw, (x + lay.px(3), row_y), word, f_state, colour)

    delta = _fmt_delta(d.get("delta"), cfg.units)
    if delta:
        f_delta = font("regular", lay.pt(19))
        wx = x + lay.px(3) + _width(draw, word, f_state) + lay.px(12)
        _text(draw, (wx, row_y), delta, f_delta, pal.ink_2)

    f_unit = font("regular", lay.pt(15))
    _text(draw, (x + lay.px(3), row_y + lay.px(25)),
          "mmol/L" if cfg.units == "mmol" else "mg/dL", f_unit, pal.ink_3)


# ─────────────────────────────────────────────────────────────────────────────
# Minimal view: the number, as large as the panel allows.


def _draw_minimal(draw, lay: Layout, pal: Palette, cfg, d: dict, stale: bool,
                  now: float) -> None:
    """Glucose, trend, and how old it is. Nothing else.

    The full dashboard is for standing in front of the screen. This is for
    glancing at it from the other side of the room, where a 40px IOB figure
    is unreadable anyway and the only question is "what is it doing".
    """
    mgdl = d.get("mgdl")
    state = _state(mgdl, cfg.low, cfg.high) if mgdl is not None else "in_range"
    colour = pal.for_state(state)
    value = _fmt_value(mgdl, cfg.units)
    trend = TRENDS.get(str(d.get("trend") or "").upper())

    # Fit to the panel rather than trusting a point size: "24.4" in mmol/L is
    # four glyphs, a triple arrow is three arrows wide, and the condensed face
    # may not be installed.
    arrow_budget = lay.px(30 + 22 * (trend[1] - 1) + 70) if trend else 0
    available = lay.w - lay.px(36) - arrow_budget
    for size in (210, 190, 170, 150, 130, 110):
        f_hero = font("condensed", lay.pt(size))
        if _width(draw, value, f_hero) <= available:
            break

    vw = _width(draw, value, f_hero)
    total = vw + arrow_budget
    x = (lay.w - total) // 2
    y = lay.px(18)
    _text(draw, (x, y), value, f_hero, colour)
    bbox = draw.textbbox((x, y), value, font=f_hero)

    if trend:
        angle, count = trend
        _draw_arrows_scaled(draw, lay, x + vw + arrow_budget // 2,
                            (bbox[1] + bbox[3]) // 2, angle, count, colour, scale=1.6)

    # One line underneath carrying the two facts a glance needs: what state
    # this is (in words, never colour alone) and whether the number is current.
    minutes = d.get("minutes_ago")
    parts = ["STALE" if stale else STATE_WORD[state], _fmt_age(minutes)]
    delta = _fmt_delta(d.get("delta"), cfg.units)
    if delta:
        parts.insert(1, delta)
    line = "   ·   ".join(parts)
    f_sub = font("bold", lay.pt(20))
    _text(draw, (lay.w // 2, lay.h - lay.px(38)), line, f_sub,
          pal.high if stale else pal.ink_2, anchor="mm")

    _text(draw, (lay.px(10), lay.h - lay.px(8)),
          "mmol/L" if cfg.units == "mmol" else "mg/dL",
          font("regular", lay.pt(11)), pal.ink_3, anchor="ls")

    _text(draw, (lay.w // 2, lay.h - lay.px(8)),
          datetime.fromtimestamp(now).strftime("%H:%M"),
          font("regular", lay.pt(11)), pal.ink_3, anchor="ms")


# ─────────────────────────────────────────────────────────────────────────────
# Right rail: IOB, then pump vitals.


def _draw_rail(draw, lay: Layout, pal: Palette, cfg, d: dict) -> None:
    x0 = lay.px(RAIL_X)
    draw.rectangle(
        [x0, lay.px(6), lay.w - lay.px(8), lay.px(170)],
        fill=pal.panel,
    )
    x = x0 + lay.px(12)
    f_label = font("regular", lay.pt(12))
    f_big = font("bold", lay.pt(40))
    f_row = font("regular", lay.pt(15))
    f_row_v = font("bold", lay.pt(15))

    iob = (d.get("iob") or {}).get("total")
    _text(draw, (x, lay.px(16)), "INSULIN ON BOARD", f_label, pal.ink_3)
    _text(draw, (x, lay.px(30)), "--" if iob is None else f"{float(iob):.1f}", f_big, pal.accent)
    if iob is not None:
        w = _width(draw, f"{float(iob):.1f}", f_big)
        _text(draw, (x + w + lay.px(6), lay.px(60)), "u", f_row, pal.ink_3)

    # A pen or hand-entered dose is insulin CareLink cannot see. If any is in
    # the total, say so — otherwise this number silently disagrees with the
    # pump's own display and there is no way to tell which is wrong.
    if (d.get("iob") or {}).get("has_non_pump"):
        _text(draw, (x, lay.px(82)), "incl. pen doses", f_label, pal.ink_3)

    draw.line(
        [(x, lay.px(100)), (lay.w - lay.px(20), lay.px(100))],
        fill=pal.rule, width=lay.px(1),
    )

    pump = d.get("pump") or {}
    rows: list[tuple[str, str]] = []
    res = pump.get("reservoir_units")
    if res is not None:
        rows.append(("Reservoir", f"{float(res):.0f} u"))
    batt = pump.get("battery_percent")
    if batt is not None:
        rows.append(("Pump batt", f"{int(batt)}%"))
    sensor = _fmt_sensor(pump.get("sensor_minutes_left"))
    if sensor:
        rows.append(("Sensor", sensor))
    if pump.get("suspended"):
        rows.append(("Delivery", "SUSPENDED"))

    ry = lay.px(110)
    for label, value in rows[:3]:
        _text(draw, (x, ry), label, f_row, pal.ink_3)
        _text(draw, (lay.w - lay.px(20), ry), value, f_row_v,
              pal.high if value == "SUSPENDED" else pal.ink_2, anchor="ra")
        ry += lay.px(19)



# ─────────────────────────────────────────────────────────────────────────────
# Allergen panel.
#
# Colour here is a SEQUENTIAL ramp in one hue, not the glucose status palette.
# Red/green/amber are reserved: a red pollen row next to a red glucose number
# would read as the same kind of alarm, and it is not. Magnitude is carried by
# a bar whose length encodes the band, and the band is ALWAYS written in words,
# so the hue is support rather than the message.

POLLEN_HUE = {
    "Low":       (0x4c, 0x46, 0x6b),
    "Moderate":  (0x6f, 0x63, 0xa2),
    "High":      (0x93, 0x80, 0xd2),
    "Very high": (0xb8, 0xa2, 0xff),
}


def _draw_pollen(draw, lay: Layout, pal: Palette, reading, x0: int) -> None:
    """The right-hand column. `reading` is a pollen.PollenReading."""
    x1 = lay.w - lay.px(8)
    y0, y1 = lay.px(180), lay.px(268)
    draw.rectangle([x0, y0, x1, y1], fill=pal.panel)

    pad = lay.px(7)
    tx = x0 + pad
    f_cap = font("regular", lay.pt(9))
    f_band = font("bold", lay.pt(15))
    f_row = font("regular", lay.pt(11))

    # The location rides in the header rather than on its own line at the
    # bottom: at 120px wide the panel has room for a header, a band, a bar and
    # THREE species rows, and a separate footer line pushed the third row off
    # the bottom edge.
    label = (reading.label if reading else "") or ""
    age = reading.age_minutes if reading else None
    header = f"POLLEN · {label.upper()}" if label else "POLLEN"
    if age is not None and age > 90:
        header = f"{header} · {int(age // 60)}H"
    _text(draw, (tx, y0 + lay.px(5)), header[:20], f_cap, pal.ink_3)

    rows = reading.detected if reading else []
    if not rows:
        # Three distinct situations, three different words. "none detected" is
        # a measurement; "out of season" is the API declining to measure; "no
        # data" is us failing to reach it. Collapsing them would turn a
        # failed fetch into a reassuring all-clear.
        if reading is None or not reading.ok:
            why = "no data"
        elif reading.species:
            why = "none detected"
        else:
            why = "out of season"
        _text(draw, ((x0 + x1) // 2, (y0 + y1) // 2), why,
              f_row, pal.ink_3, anchor="mm")
        return

    worst_name, worst_value, worst_band = rows[0]
    _text(draw, (tx, y0 + lay.px(16)), worst_band, f_band, pal.ink)

    # The magnitude bar. Four steps, so it reads at a glance from the length
    # even before the word resolves.
    steps = ["Low", "Moderate", "High", "Very high"].index(worst_band) + 1
    bw = (x1 - pad - tx)
    bh = lay.px(4)
    by = y0 + lay.px(36)
    draw.rectangle([tx, by, tx + bw, by + bh], fill=pal.rule)
    draw.rectangle([tx, by, tx + int(bw * steps / 4), by + bh],
                   fill=POLLEN_HUE.get(worst_band, pal.ink_3))

    # Up to three species, worst first, each with its raw grains/m3 so the
    # number behind our banding is never hidden.
    ry = y0 + lay.px(46)
    for name, value, _band in rows[:3]:
        _text(draw, (tx, ry), name.capitalize()[:8], f_row, pal.ink_2)
        _text(draw, (x1 - pad, ry), f"{value:.0f}", f_row, pal.ink_2, anchor="ra")
        ry += lay.px(13)


# ─────────────────────────────────────────────────────────────────────────────
# Trend. Two pure functions first, because they carry the decisions worth
# testing and testing them through rendered pixels is both brittle and a poor
# description of what is supposed to happen.

Point = tuple[float, float]  # (epoch milliseconds, mg/dL)


def trend_window(
    points: list[Point], now_ms: float, hours: float
) -> tuple[float, float, list[Point]]:
    """Returns (t_start, t_end, points inside that window).

    The window ends at the NEWEST READING, not at wall-clock now. If the
    server is running an hour behind, anchoring to now would slide the whole
    curve off the left of the plot and leave an empty panel sitting next to a
    glucose number that is visibly present — which reads as a rendering bug
    rather than as late data. The "N min ago" in the footer is what reports
    the lag.

    The floor stops that from going too far the other way: if the newest
    reading is ancient, the window still covers the recent past rather than
    drifting off to wherever the data stops, so an empty plot means "nothing
    recently" rather than "here is a detailed view of last Tuesday".

    Points outside the window are DROPPED, not clamped. Clamping stacks every
    out-of-window reading into one vertical smear at the left edge that looks
    exactly like a real excursion.
    """
    span_ms = hours * 3600 * 1000
    t_end = max((t for t, _ in points), default=now_ms)
    t_end = max(t_end, now_ms - span_ms / 2)
    t_start = t_end - span_ms
    return t_start, t_end, [(t, v) for t, v in points if t_start <= t <= t_end]


def split_on_gaps(points: list[Point], gap_minutes: float = GAP_MINUTES) -> list[list[Point]]:
    """Splits a series wherever readings stop for longer than `gap_minutes`.

    Drawing one continuous line through a two-hour sensor outage implies
    readings that were never received — the eye reads a straight segment as
    measurement, not as absence. Each run of contiguous readings becomes its
    own polyline instead.

    Returns [] for an empty input rather than [[]], so callers can iterate
    without a special case.
    """
    if not points:
        return []
    gap_ms = gap_minutes * 60_000
    segments: list[list[Point]] = [[points[0]]]
    for prev, cur in zip(points, points[1:]):
        if cur[0] - prev[0] > gap_ms:
            segments.append([])
        segments[-1].append(cur)
    return segments


# ─────────────────────────────────────────────────────────────────────────────
# Trend. Target band first, then the line on top of it.


def _draw_trend(draw, lay: Layout, pal: Palette, cfg, d: dict, now: float,
                width_frac: float = 1.0) -> None:
    x0, x1 = lay.px(16), lay.w - lay.px(8)
    if width_frac < 1.0:
        # Shrink from the RIGHT so the left edge (oldest reading) stays put.
        # Moving both edges would make the curve appear to slide sideways the
        # moment the pollen panel appeared or vanished.
        x1 = x0 + int((x1 - x0) * width_frac)
    y0, y1 = lay.px(180), lay.px(268)
    draw.rectangle([x0, y0, x1, y1], fill=pal.panel)

    series = d.get("series") or []
    hours = d.get("hours") or cfg.hours

    # Right-hand gutter for the threshold labels. Without it they sit under the
    # newest reading's marker, which is always pinned to the right edge.
    gutter = lay.px(30)
    xp1 = x1 - gutter

    all_pts = [(float(p["t"]), float(p["sg"])) for p in series if p.get("sg") is not None]
    t_start, t_end, pts = trend_window(all_pts, now * 1000, hours)

    # A mostly fixed y-window keeps the shape of the curve comparable between
    # glances. An autoscaled axis makes a flat night look like a rollercoaster.
    # It only ever widens, never narrows, so an excursion is never cropped.
    lo_axis, hi_axis = 40.0, 300.0
    if pts:
        lo_axis = min(lo_axis, min(v for _, v in pts) - 10)
        hi_axis = max(hi_axis, max(v for _, v in pts) + 10)

    def sx(t: float) -> float:
        span = max(1.0, t_end - t_start)
        return x0 + (xp1 - x0) * max(0.0, min(1.0, (t - t_start) / span))

    def sy(v: float) -> float:
        return y1 - (y1 - y0) * max(0.0, min(1.0, (v - lo_axis) / (hi_axis - lo_axis)))

    # Target band — the second channel that carries "in range" without colour.
    band_top, band_bot = sy(cfg.high), sy(cfg.low)
    draw.rectangle([x0, band_top, xp1, band_bot], fill=_mix(pal.panel, pal.in_range, 0.14))
    f_axis = font("regular", lay.pt(11))
    for yy, label in ((band_top, cfg.high), (band_bot, cfg.low)):
        _dashed_h(draw, x0, xp1, yy, pal.rule, lay.px(1))
        _text(draw, (xp1 + lay.px(4), yy), _fmt_value(label, cfg.units), f_axis,
              pal.ink_3, anchor="lm")

    if not pts:
        _text(draw, ((x0 + xp1) // 2, (y0 + y1) // 2), "no readings in this window",
              font("regular", lay.pt(13)), pal.ink_3, anchor="mm")
        return

    segments = [[(sx(t), sy(v)) for t, v in seg] for seg in split_on_gaps(pts)]

    for seg in segments:
        if len(seg) >= 2:
            draw.line(seg, fill=pal.ink_2, width=lay.px(2), joint="curve")
        elif seg:
            r = lay.px(2)
            cx, cy = seg[0]
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=pal.ink_2)

    # Only the newest point gets a marker. A dot on every reading turns a
    # 3-hour window into 36 dots and hides the shape.
    lt, lv = pts[-1]
    cx, cy = sx(lt), sy(lv)
    r = lay.px(4)
    draw.ellipse([cx - r - lay.px(2), cy - r - lay.px(2), cx + r + lay.px(2), cy + r + lay.px(2)],
                 fill=pal.panel)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 fill=pal.for_state(_state(lv, cfg.low, cfg.high)))

    _text(draw, (x0 + lay.px(6), y0 + lay.px(4)), f"{int(hours)}h",
          font("regular", lay.pt(11)), pal.ink_3)


# ─────────────────────────────────────────────────────────────────────────────
# Footer: how old the data is, 24h time in range, the clock.


def _draw_footer(draw, lay: Layout, pal: Palette, cfg, d: dict, snap: Snapshot, now: float) -> None:
    y = lay.px(276)
    f = font("regular", lay.pt(13))
    f_b = font("bold", lay.pt(13))

    age = _fmt_age(d.get("minutes_ago"))
    stale = cfg.stale_minutes > 0 and (d.get("minutes_ago") or 0) > cfg.stale_minutes
    _text(draw, (lay.px(16), y), age, f_b if stale else f, pal.high if stale else pal.ink_2)

    clock = datetime.fromtimestamp(now).strftime("%H:%M")
    _text(draw, (lay.w - lay.px(8), y), clock, f, pal.ink_3, anchor="ra")
    clock_left = lay.w - lay.px(8) - _width(draw, clock, f) - lay.px(12)

    # TIR as a stacked bar. Segments are separated by a background gap so the
    # boundary is a shape and not only a colour change — the same reason the
    # hero number is labelled in words.
    tir = d.get("tir") or {}
    pl, pi, ph = tir.get("pct_low"), tir.get("pct_in"), tir.get("pct_high")
    if None not in (pl, pi, ph):
        label = f"{pi}% in range"
        label_w = _width(draw, label, f)
        _text(draw, (clock_left, y), label, f, pal.ink_2, anchor="ra")
        bx0 = lay.px(150)
        bx1 = clock_left - label_w - lay.px(12)
        by, bh = y + lay.px(3), lay.px(11)
        total = max(1, pl + pi + ph)
        cursor = float(bx0)
        if bx1 > bx0:
            for pct, colour in ((pl, pal.low), (pi, pal.in_range), (ph, pal.high)):
                width = (bx1 - bx0) * pct / total
                if width > 0:
                    draw.rectangle(
                        [cursor, by, cursor + max(1, width - lay.px(2)), by + bh], fill=colour
                    )
                cursor += width
    elif tir.get("avg_sg"):
        _text(draw, (lay.px(150), y), f"24h avg {_fmt_value(tir['avg_sg'], cfg.units)}",
              f, pal.ink_2)


def _draw_offline_banner(draw, lay: Layout, pal: Palette, snap: Snapshot) -> None:
    """Two failed fetches in a row. The number stays — it was real — but it
    must not sit there looking current."""
    h = lay.px(22)
    draw.rectangle([0, 0, lay.w, h], fill=pal.high)
    _text(draw, (lay.w // 2, h // 2), (snap.last_error or "offline")[:56],
          font("bold", lay.pt(12)), (20, 16, 8), anchor="mm")


# ─────────────────────────────────────────────────────────────────────────────
# Night mode and the empty state.


def is_night(cfg, now: float, mode: str | None = None) -> bool:
    """Is the ambient wash showing?

    `mode` is the runtime override from the NIGHT button:
        "on"    force the wash regardless of the clock
        "off"   never wash
        "auto"  follow the configured schedule (the default)

    Three-way rather than a toggle on purpose. A plain on/off switch replaces
    the schedule with something you have to remember to flip twice a day, and
    the failure mode is a bedroom screen at full brightness all night because
    you forgot.
    """
    mode = mode or getattr(cfg, "night_mode", "auto")
    if mode == "on":
        return True
    if mode == "off":
        return False
    win = cfg.night_window
    if not win:
        return False
    a, b = win
    hour = datetime.fromtimestamp(now).hour
    return (a <= hour or hour < b) if a > b else (a <= hour < b)


def _render_night(cfg, lay: Layout, pal: Palette, d: dict, stale: bool,
                  cfg_state: dict | None = None, bar: bool = False,
                  hint: bool = False, view: str = "full") -> Image.Image:
    """A dim wash plus a small number. A full dashboard at 3am is not what
    anyone wants, and a bright panel in a bedroom is worse than no panel."""
    mgdl = d.get("mgdl")
    state = _state(mgdl, cfg.low, cfg.high) if mgdl is not None else "in_range"
    wash = _mix((0, 0, 0), pal.for_state(state), 0.10 if state == "in_range" else 0.22)
    img = Image.new("RGB", (lay.w, lay.h), wash)
    draw = ImageDraw.Draw(img)
    # Lift the number clear of whatever occupies the bottom edge. A reading
    # half-covered by its own control bar is worse than no control bar.
    inset = lay.px(BAR_H) if bar else (lay.px(24) if hint else 0)

    value = _fmt_value(mgdl, cfg.units)
    _text(draw, (lay.w - lay.px(18), lay.h - lay.px(14) - inset), value,
          font("condensed", lay.pt(46)),
          _mix(wash, pal.ink, 0.55), anchor="rs")
    if stale:
        _text(draw, (lay.px(18), lay.h - lay.px(16) - inset), "stale",
              font("regular", lay.pt(14)), _mix(wash, pal.ink, 0.35), anchor="ls")

    # The bar has to survive the wash. Without it, setting NIGHT to "On" would
    # hide the only control that can set it back — a one-way door you could
    # escape from only over SSH.
    if bar:
        _draw_bar(draw, lay, pal,
                  button_layout(cfg, {**(cfg_state or {}), "view": view}, lay.w, lay.h))
    elif hint:
        _draw_bar_hint(draw, lay, pal)

    return _orient(img, cfg)


def _render_empty(cfg, lay: Layout, snap: Snapshot) -> Image.Image:
    """No data at all yet — first boot, or the server has never had a reading.
    Says what it is waiting for, because a blank screen on a device with no
    keyboard is indistinguishable from broken hardware."""
    pal = palette(getattr(cfg, "palette", "clinical"))
    img = Image.new("RGB", (lay.w, lay.h), pal.bg)
    draw = ImageDraw.Draw(img)
    _text(draw, (lay.w // 2, lay.h // 2 - lay.px(18)), "Sentinelle",
          font("condensed", lay.pt(40)), pal.ink_2, anchor="mm")
    msg = snap.last_error or "waiting for the first reading"
    _text(draw, (lay.w // 2, lay.h // 2 + lay.px(18)), msg[:52],
          font("regular", lay.pt(14)), pal.ink_3, anchor="mm")
    _text(draw, (lay.w // 2, lay.h - lay.px(16)), cfg.base_url.replace("https://", "")[:48],
          font("regular", lay.pt(11)), pal.rule, anchor="ms")
    return _orient(img, cfg)


# ─────────────────────────────────────────────────────────────────────────────


def _draw_arrows(draw, lay: Layout, cx: int, cy: int, angle: int, count: int, colour) -> None:
    """One to three arrows at `angle` degrees, stacked across the direction of
    travel so a double arrow reads as 'fast', not as two separate readings."""
    _draw_arrows_scaled(draw, lay, cx, cy, angle, count, colour, scale=1.0)


def _draw_arrows_scaled(draw, lay: Layout, cx: int, cy: int, angle: int, count: int,
                        colour, scale: float = 1.0) -> None:
    length = lay.px(42 * scale)
    head = lay.px(13 * scale)
    shaft = lay.px(7 * scale)
    rad = math.radians(angle)
    dx, dy = math.cos(rad), -math.sin(rad)
    # Perpendicular offset, so stacked arrows sit side by side.
    px_, py = -dy, dx
    spacing = lay.px(15 * scale)
    start = -(count - 1) / 2

    for i in range(count):
        off = (start + i) * spacing
        ox, oy = cx + px_ * off, cy + py * off
        tail = (ox - dx * length / 2, oy - dy * length / 2)
        tip = (ox + dx * length / 2, oy + dy * length / 2)
        draw.line([tail, tip], fill=colour, width=shaft)
        draw.polygon(
            [
                tip,
                (tip[0] - dx * head + px_ * head * 0.62, tip[1] - dy * head + py * head * 0.62),
                (tip[0] - dx * head - px_ * head * 0.62, tip[1] - dy * head - py * head * 0.62),
            ],
            fill=colour,
        )


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _dashed_h(draw, x0: int, x1: int, y: float, colour, width: int, dash: int = 5) -> None:
    x = x0
    while x < x1:
        draw.line([(x, y), (min(x + dash, x1), y)], fill=colour, width=width)
        x += dash * 2


def demo_snapshot(kind: str = "in_range", now: float | None = None) -> Snapshot:
    """Canned data for `preview` and for the tests. Shaped exactly like
    /kiosk/data so a preview that looks right predicts a Pi that looks right.

    `now` anchors the fake series. It MUST be the same value the caller then
    passes to render(): the trend window is computed from wall-clock now, so
    a preview that renders at a pretend 14:47 against a series built from the
    real clock produces "no readings in this window" — a correct answer to
    the wrong question, and one that looks exactly like a broken program.
    """
    now_ms = (now if now is not None else time.time()) * 1000
    base = {"in_range": 142, "low": 58, "high": 244, "stale": 131}[kind]
    series = []
    for i in range(36):
        t = now_ms - (36 - i) * 5 * 60_000
        # A deliberate hole in the middle so the gap-breaking behaviour is
        # visible in every preview rather than only in the field.
        if kind != "low" and 14 <= i <= 20:
            continue
        sg = base + 34 * math.sin(i / 5.5) + (i - 18) * 1.4
        series.append({"t": t, "sg": max(45, round(sg))})
    data: dict[str, Any] = {
        "ts": None,
        "mgdl": base,
        "mmol": round(base / MMOL_FACTOR, 1),
        "delta": {"in_range": 6, "low": -9, "high": 3, "stale": 0}[kind],
        "trend": {"in_range": "FLAT", "low": "DOWN", "high": "UP_FORTY_FIVE",
                  "stale": "FLAT"}[kind],
        "minutes_ago": 47 if kind == "stale" else 2,
        "hours": 3,
        "series": series,
        "iob": {"total": 2.4, "pump": 1.9, "pen": 0.5, "manual": 0.0, "has_non_pump": True},
        "pump": {"reservoir_units": 118, "reservoir_percent": 61, "battery_percent": 72,
                 "suspended": False, "auto_mode_state": "AUTO", "sensor_state": "OK",
                 "sensor_minutes_left": 6300},
        "tir": {"readings": 271, "avg_sg": 141, "pct_low": 4, "pct_in": 78, "pct_high": 18},
    }
    return Snapshot(ok=True, data=data, fetched_at=time.time())

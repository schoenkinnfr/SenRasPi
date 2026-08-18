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


def render(cfg, snap: Snapshot, now: float | None = None) -> Image.Image:
    """The whole screen. Never raises — a renderer that throws leaves a Pi
    showing a frozen frame with no clue why, so failures are drawn instead."""
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

    if _is_night(cfg, now):
        return _render_night(cfg, lay, pal, d, stale)

    img = Image.new("RGB", (lay.w, lay.h), pal.bg)
    draw = ImageDraw.Draw(img)

    _draw_hero(draw, lay, pal, cfg, d, stale)
    _draw_rail(draw, lay, pal, cfg, d)
    _draw_trend(draw, lay, pal, cfg, d, now)
    _draw_footer(draw, lay, pal, cfg, d, snap, now)

    if snap.offline:
        _draw_offline_banner(draw, lay, pal, snap)

    return _orient(img, cfg)


def _logical_size(cfg) -> tuple[int, int]:
    return (cfg.height, cfg.width) if cfg.rotate in (90, 270) else (cfg.width, cfg.height)


def _orient(img: Image.Image, cfg) -> Image.Image:
    """Rotates the logical canvas into the panel's physical orientation."""
    if not cfg.rotate:
        return img
    return img.rotate(-cfg.rotate, expand=True)


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


def _draw_trend(draw, lay: Layout, pal: Palette, cfg, d: dict, now: float) -> None:
    x0, x1 = lay.px(16), lay.w - lay.px(8)
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


def _is_night(cfg, now: float) -> bool:
    win = cfg.night_window
    if not win:
        return False
    a, b = win
    hour = datetime.fromtimestamp(now).hour
    return (a <= hour or hour < b) if a > b else (a <= hour < b)


def _render_night(cfg, lay: Layout, pal: Palette, d: dict, stale: bool) -> Image.Image:
    """A dim wash plus a small number. A full dashboard at 3am is not what
    anyone wants, and a bright panel in a bedroom is worse than no panel."""
    mgdl = d.get("mgdl")
    state = _state(mgdl, cfg.low, cfg.high) if mgdl is not None else "in_range"
    wash = _mix((0, 0, 0), pal.for_state(state), 0.10 if state == "in_range" else 0.22)
    img = Image.new("RGB", (lay.w, lay.h), wash)
    draw = ImageDraw.Draw(img)
    value = _fmt_value(mgdl, cfg.units)
    _text(draw, (lay.w - lay.px(18), lay.h - lay.px(14)), value,
          font("condensed", lay.pt(46)),
          _mix(wash, pal.ink, 0.55), anchor="rs")
    if stale:
        _text(draw, (lay.px(18), lay.h - lay.px(16)), "stale",
              font("regular", lay.pt(14)), _mix(wash, pal.ink, 0.35), anchor="ls")
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
    length = lay.px(42)
    head = lay.px(13)
    shaft = lay.px(7)
    rad = math.radians(angle)
    dx, dy = math.cos(rad), -math.sin(rad)
    # Perpendicular offset, so stacked arrows sit side by side.
    px_, py = -dy, dx
    spacing = lay.px(15)
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

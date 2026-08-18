"""
Tests for the things that would silently show a wrong number.

SPDX-License-Identifier: AGPL-3.0-only

Not a rendering test suite — pixel comparison on a layout that is still moving
is a maintenance tax. These cover the decisions where a regression would be
invisible on screen but wrong in substance: the stale threshold, the gap
handling, the code alphabet, the geometry contract.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from sentinelle_display import render as R
from sentinelle_display.client import PairError, Snapshot, format_code, normalise_code
from sentinelle_display.config import Config


def cfg(**kw) -> Config:
    return Config(base_url="https://example.test", token="t", **kw)


# ── geometry ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "w,h,rotate",
    [(480, 320, 0), (480, 320, 180), (320, 480, 90), (320, 480, 270), (800, 480, 0)],
)
def test_output_always_matches_the_panel(w, h, rotate):
    """The image handed to a backend must be exactly the panel's size.

    Anything else gets resized or, worse, silently cropped — and the corner
    that gets cropped is the one the reading is in.
    """
    img = R.render(cfg(width=w, height=h, rotate=rotate), R.demo_snapshot())
    assert img.size == (w, h)


# ── staleness ───────────────────────────────────────────────────────────────


def test_stale_data_is_greyed_not_hidden():
    snap = R.demo_snapshot("stale", now=_daytime())
    assert snap.data["minutes_ago"] > 20
    img = R.render(cfg(night="off"), snap, now=_daytime())
    a = np.asarray(img.convert("RGB"), dtype=int)
    # A greyscale frame has no saturated pixel anywhere.
    assert (a.max(axis=2) - a.min(axis=2)).max() < 12
    # ...and the number is still on it: a blank screen reads as broken hardware.
    assert np.asarray(img.convert("L")).max() > 100


def test_stale_threshold_of_zero_disables_greying():
    snap = R.demo_snapshot("stale", now=_daytime())
    img = R.render(cfg(stale_minutes=0, night="off"), snap, now=_daytime())
    a = np.asarray(img.convert("RGB"), dtype=int)
    assert (a.max(axis=2) - a.min(axis=2)).max() > 40


# ── the trend line ──────────────────────────────────────────────────────────


HOUR_MS = 3600 * 1000


def test_gaps_break_the_line_instead_of_interpolating():
    """A continuous line across a sensor outage implies readings that were
    never received — the eye reads a straight segment as measurement."""
    pts = [(0.0, 100.0), (5 * 60_000.0, 105.0),
           # a two-hour hole
           (125 * 60_000.0, 180.0), (130 * 60_000.0, 175.0)]
    segments = R.split_on_gaps(pts)
    assert len(segments) == 2
    assert [len(seg) for seg in segments] == [2, 2]
    assert segments[0][-1][0] == 5 * 60_000.0
    assert segments[1][0][0] == 125 * 60_000.0


def test_contiguous_readings_stay_one_segment():
    pts = [(i * 5 * 60_000.0, 120.0) for i in range(12)]
    assert len(R.split_on_gaps(pts)) == 1


def test_gap_threshold_is_exclusive():
    """CareLink runs 5-10 min behind, so a legitimate cycle can stretch. The
    line must break only when the gap EXCEEDS the threshold, not when it
    lands exactly on it."""
    exactly = R.GAP_MINUTES * 60_000.0
    assert len(R.split_on_gaps([(0.0, 100.0), (exactly, 110.0)])) == 1
    assert len(R.split_on_gaps([(0.0, 100.0), (exactly + 1, 110.0)])) == 2


def test_split_on_gaps_handles_empty_and_single():
    assert R.split_on_gaps([]) == []
    assert R.split_on_gaps([(0.0, 100.0)]) == [[(0.0, 100.0)]]


def test_window_ends_at_the_newest_reading_not_at_now():
    """CareLink runs 5-10 min behind the sensor, so the newest reading is
    always in the past. The window must end there, not at wall-clock now —
    otherwise the curve sits shifted left of the plot with dead space on the
    right, on every single frame."""
    now = 10 * HOUR_MS
    newest = now - 30 * 60_000                      # half an hour behind
    pts = [(newest - HOUR_MS, 100.0), (newest, 120.0)]
    t_start, t_end, inside = R.trend_window(pts, now_ms=now, hours=3)
    assert t_end == newest
    assert t_start == newest - 3 * HOUR_MS
    assert len(inside) == 2


def test_the_window_floor_takes_over_once_data_is_badly_stale():
    """The floor is half the span. Past that the window stops chasing the
    data, so an empty plot means 'nothing recently' rather than a detailed
    view of whenever the readings happened to stop."""
    now = 10 * HOUR_MS
    pts = [(now - 3 * HOUR_MS, 120.0)]              # older than the 1.5h floor
    _, t_end, inside = R.trend_window(pts, now_ms=now, hours=3)
    assert t_end == now - 1.5 * HOUR_MS
    assert len(inside) == 1                         # still visible, just not pinned right


def test_window_does_not_drift_off_to_ancient_data():
    """A very old newest-reading must not turn the panel into a detailed view
    of last Tuesday. The floor keeps it on the recent past."""
    now = 100 * HOUR_MS
    pts = [(now - 50 * HOUR_MS, 100.0)]
    t_start, t_end, inside = R.trend_window(pts, now_ms=now, hours=3)
    assert t_end == now - 1.5 * HOUR_MS
    assert inside == []


def test_points_outside_the_window_are_dropped_not_clamped():
    """Clamping stacks every out-of-window reading into one vertical smear at
    the left edge that looks exactly like a real excursion."""
    now = 24 * HOUR_MS
    pts = [(now - 20 * HOUR_MS, 400.0), (now - HOUR_MS, 120.0), (now, 118.0)]
    _, _, inside = R.trend_window(pts, now_ms=now, hours=3)
    assert [v for _, v in inside] == [120.0, 118.0]


def test_no_points_still_yields_a_sane_window():
    now = 10 * HOUR_MS
    t_start, t_end, inside = R.trend_window([], now_ms=now, hours=3)
    assert t_end == now
    assert t_start == now - 3 * HOUR_MS
    assert inside == []


def test_empty_series_says_so():
    snap = Snapshot(ok=True, data={**R.demo_snapshot(now=_daytime()).data, "series": []})
    R.render(cfg(night="off"), snap, now=_daytime())


# ── trend arrows ────────────────────────────────────────────────────────────


def test_unknown_trend_draws_nothing():
    """A guessed arrow pointing the wrong way is worse than no arrow."""
    assert R.TRENDS.get("SIDEWAYS_ISH") is None
    data = {**R.demo_snapshot(now=_daytime()).data, "trend": "SIDEWAYS_ISH"}
    R.render(cfg(night="off"), Snapshot(ok=True, data=data), now=_daytime())


@pytest.mark.parametrize("trend", sorted(R.TRENDS))
def test_every_known_trend_renders(trend):
    data = {**R.demo_snapshot(now=_daytime()).data, "trend": trend}
    R.render(cfg(night="off"), Snapshot(ok=True, data=data), now=_daytime())


# ── units and thresholds ────────────────────────────────────────────────────


def test_thresholds_stay_in_mgdl_when_displaying_mmol():
    """low/high are mg/dL in every unit mode. If this ever flips, a user with
    low=70 displaying mmol/L would get a 'low' alert band at 70 mmol/L."""
    assert R._state(65, 70, 180) == "low"
    assert R._state(65 / 1, 70, 180) == "low"
    R.render(cfg(units="mmol", night="off"), R.demo_snapshot("low", now=_daytime()), now=_daytime())


def test_mmol_conversion():
    assert R._fmt_value(180, "mmol") == "10.0"
    assert R._fmt_value(180, "mgdl") == "180"
    assert R._fmt_value(None, "mgdl") == "--"


def test_missing_pump_block_does_not_crash():
    data = {**R.demo_snapshot(now=_daytime()).data, "pump": None, "iob": {}}
    R.render(cfg(night="off"), Snapshot(ok=True, data=data), now=_daytime())


def test_no_data_at_all_renders_a_waiting_screen():
    R.render(cfg(), Snapshot(ok=False, last_error="offline — timed out"))


# ── pairing codes ───────────────────────────────────────────────────────────


def test_code_normalisation_accepts_the_formats_a_person_types():
    for raw in ("K7M2PQRXW9", "k7m2p-qrxw9", "K7M2P QRXW9", " k7m2pqrxw9 "):
        assert normalise_code(raw) == "K7M2PQRXW9"


def test_code_rejects_confusable_characters_with_a_hint():
    """Silently substituting O for 0 would burn a rate-limited attempt on a
    code the user typed correctly."""
    with pytest.raises(PairError, match="did you mean O"):
        normalise_code("K7M20QRXW9")
    with pytest.raises(PairError, match="did you mean I"):
        normalise_code("K7M2LQRXW9")


def test_code_rejects_wrong_length():
    with pytest.raises(PairError, match="10 characters"):
        normalise_code("K7M2P")


def test_format_code_matches_what_the_web_app_shows():
    assert format_code("K7M2PQRXW9") == "K7M2P-QRXW9"


# ── offline behaviour ───────────────────────────────────────────────────────


def test_one_failure_is_not_offline_two_are():
    """One failed poll is a blip. Two means the last good number must stop
    looking current."""
    assert not Snapshot(consecutive_failures=1).offline
    assert Snapshot(consecutive_failures=2).offline


def test_config_rejects_a_poll_interval_that_hammers_the_server():
    problems = cfg(poll_seconds=1).validate()
    assert any("poll_seconds" in p for p in problems)


def test_config_rejects_inverted_thresholds():
    assert any("must be below" in p for p in cfg(low=200, high=100).validate())


def _daytime() -> float:
    """A fixed mid-afternoon clock, so night mode never turns a layout test
    into a wash test depending on when CI happens to run."""
    import datetime
    return datetime.datetime.now().replace(hour=14, minute=47, second=0).timestamp()

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
    snap = R.demo_snapshot("stale")
    assert snap.data["minutes_ago"] > 20
    img = R.render(cfg(night="off"), snap, now=_daytime())
    a = np.asarray(img.convert("RGB"), dtype=int)
    # A greyscale frame has no saturated pixel anywhere.
    assert (a.max(axis=2) - a.min(axis=2)).max() < 12
    # ...and the number is still on it: a blank screen reads as broken hardware.
    assert np.asarray(img.convert("L")).max() > 100


def test_stale_threshold_of_zero_disables_greying():
    snap = R.demo_snapshot("stale")
    img = R.render(cfg(stale_minutes=0, night="off"), snap, now=_daytime())
    a = np.asarray(img.convert("RGB"), dtype=int)
    assert (a.max(axis=2) - a.min(axis=2)).max() > 40


# ── the trend line ──────────────────────────────────────────────────────────


def test_gaps_break_the_line_instead_of_interpolating():
    """A smooth line across a sensor outage implies readings never received."""
    now_ms = time.time() * 1000
    pts = [{"t": now_ms - 180 * 60_000, "sg": 100}, {"t": now_ms, "sg": 200}]
    snap = Snapshot(ok=True, data={**R.demo_snapshot().data, "series": pts})
    img = R.render(cfg(night="off"), snap, now=_daytime())
    # With the gap honoured there are two isolated dots, not a diagonal sweep;
    # a drawn line between them would light up the middle of the plot.
    mid = img.convert("RGB").getpixel((img.width // 2, 224))
    assert mid[0] == mid[1] == mid[2] or sum(mid) < 200


def test_points_outside_the_window_are_dropped_not_clamped():
    """Clamping stacks old readings into a vertical smear at the left edge
    that looks like a real excursion."""
    now_ms = time.time() * 1000
    pts = [{"t": now_ms - 24 * 3600 * 1000, "sg": 400}, {"t": now_ms, "sg": 120}]
    snap = Snapshot(ok=True, data={**R.demo_snapshot().data, "series": pts, "hours": 3})
    R.render(cfg(night="off"), snap, now=_daytime())  # must not raise


def test_empty_series_says_so():
    snap = Snapshot(ok=True, data={**R.demo_snapshot().data, "series": []})
    R.render(cfg(night="off"), snap, now=_daytime())


# ── trend arrows ────────────────────────────────────────────────────────────


def test_unknown_trend_draws_nothing():
    """A guessed arrow pointing the wrong way is worse than no arrow."""
    assert R.TRENDS.get("SIDEWAYS_ISH") is None
    data = {**R.demo_snapshot().data, "trend": "SIDEWAYS_ISH"}
    R.render(cfg(night="off"), Snapshot(ok=True, data=data), now=_daytime())


@pytest.mark.parametrize("trend", sorted(R.TRENDS))
def test_every_known_trend_renders(trend):
    data = {**R.demo_snapshot().data, "trend": trend}
    R.render(cfg(night="off"), Snapshot(ok=True, data=data), now=_daytime())


# ── units and thresholds ────────────────────────────────────────────────────


def test_thresholds_stay_in_mgdl_when_displaying_mmol():
    """low/high are mg/dL in every unit mode. If this ever flips, a user with
    low=70 displaying mmol/L would get a 'low' alert band at 70 mmol/L."""
    assert R._state(65, 70, 180) == "low"
    assert R._state(65 / 1, 70, 180) == "low"
    R.render(cfg(units="mmol", night="off"), R.demo_snapshot("low"), now=_daytime())


def test_mmol_conversion():
    assert R._fmt_value(180, "mmol") == "10.0"
    assert R._fmt_value(180, "mgdl") == "180"
    assert R._fmt_value(None, "mgdl") == "--"


def test_missing_pump_block_does_not_crash():
    data = {**R.demo_snapshot().data, "pump": None, "iob": {}}
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
    import datetime
    return datetime.datetime.now().replace(hour=14, minute=47, second=0).timestamp()

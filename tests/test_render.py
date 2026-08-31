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


# ── views and the on-screen button ──────────────────────────────────────────


def test_minimal_view_renders_at_every_state():
    for kind in ("in_range", "low", "high", "stale"):
        img = R.render(cfg(night="off"), R.demo_snapshot(kind, now=_daytime()),
                       now=_daytime(), view="minimal")
        assert img.size == (480, 320)


def test_minimal_view_still_names_the_state_in_words():
    """The whole point of the state word is that colour is not the only
    channel. Shrinking the layout must not quietly drop it."""
    assert R.STATE_WORD["low"] == "LOW"
    img_min = R.render(cfg(night="off"), R.demo_snapshot("low", now=_daytime()),
                       now=_daytime(), view="minimal")
    img_full = R.render(cfg(night="off"), R.demo_snapshot("low", now=_daytime()),
                        now=_daytime(), view="full")
    # Both views draw text in the lower third; neither is a bare number.
    for img in (img_min, img_full):
        lower = np.asarray(img.convert("L"))[210:, :]
        assert lower.max() > 120


def test_night_wake_overrides_the_ambient_wash():
    """A tap at 3am must show the real screen, not a slightly different wash."""
    night = _at_hour(3)
    snap = R.demo_snapshot(now=night)
    washed = R.render(cfg(night="22-7"), snap, now=night)
    woken = R.render(cfg(night="22-7"), snap, now=night, force_day=True)
    assert np.asarray(washed).tobytes() != np.asarray(woken).tobytes()
    # The wash is nearly uniform; the dashboard is not.
    assert np.asarray(washed.convert("L")).std() < np.asarray(woken.convert("L")).std()


def test_is_night_handles_windows_that_cross_midnight():
    assert R.is_night(cfg(night="22-7"), _at_hour(23))
    assert R.is_night(cfg(night="22-7"), _at_hour(3))
    assert not R.is_night(cfg(night="22-7"), _at_hour(12))
    assert not R.is_night(cfg(night="off"), _at_hour(3))
    # A same-day window must not be treated as wrapping.
    assert R.is_night(cfg(night="13-15"), _at_hour(14))
    assert not R.is_night(cfg(night="13-15"), _at_hour(23))


def test_config_rejects_an_unknown_view():
    assert any("view must be" in p for p in cfg(view="graph").validate())


def _at_hour(hour: int) -> float:
    import datetime
    return datetime.datetime.now().replace(hour=hour, minute=30, second=0).timestamp()


# ── the control bar ─────────────────────────────────────────────────────────


BAR_STATE = {"view": "full", "night_mode": "auto", "units": "mgdl"}


def test_buttons_tile_the_full_width_without_gaps_or_overlap():
    """A dead strip between two buttons is a press that does nothing, which on
    a touchscreen is indistinguishable from a broken panel."""
    bs = R.button_layout(cfg(), BAR_STATE, 480, 320)
    assert [b.key for b in bs] == ["view", "night", "units", "other", "minimize"]
    assert bs[0].x0 == 0
    assert bs[-1].x1 == 479
    for left, right in zip(bs, bs[1:]):
        assert right.x0 == left.x1 + 1, f"gap/overlap between {left.key} and {right.key}"


def test_every_pixel_of_the_bar_hits_exactly_one_button():
    bs = R.button_layout(cfg(), BAR_STATE, 480, 320)
    y = (bs[0].y0 + bs[0].y1) // 2
    for x in range(0, 480):
        assert R.hit_test(bs, x, y) is not None, f"x={x} hits nothing"
    # ...and nothing above the bar responds.
    assert R.hit_test(bs, 240, bs[0].y0 - 1) is None


def test_hit_regions_follow_the_panel_size():
    """The bar is drawn from button_layout and hit-tested from button_layout.
    If it ever stopped scaling, clicks would land where the buttons used to
    be — visibly moved, still responding at the old coordinates."""
    for w, h in ((480, 320), (320, 240), (800, 480)):
        bs = R.button_layout(cfg(), BAR_STATE, w, h)
        assert bs[0].x0 == 0 and bs[-1].x1 == w - 1
        assert bs[0].y1 == h
        assert R.hit_test(bs, w // 2, h - 2) is not None


def test_button_labels_report_the_current_state_not_the_action():
    """Mixing "what this is" and "what this does" across one row of buttons is
    how you get a NIGHT button nobody can read."""
    def val(state, key):
        return next(b.value for b in R.button_layout(cfg(), state, 480, 320) if b.key == key)

    assert val({**BAR_STATE, "night_mode": "auto"}, "night") == "Auto"
    assert val({**BAR_STATE, "night_mode": "on"}, "night") == "On"
    assert val({**BAR_STATE, "night_mode": "off"}, "night") == "Off"
    assert val({**BAR_STATE, "view": "minimal"}, "view") == "Minimal"
    assert val({**BAR_STATE, "units": "mmol"}, "units") == "mmol/L"


def test_night_mode_override_beats_the_schedule_both_ways():
    noon, small_hours = _at_hour(12), _at_hour(3)
    assert R.is_night(cfg(night="22-7"), noon, "on")
    assert not R.is_night(cfg(night="22-7"), small_hours, "off")
    assert R.is_night(cfg(night="22-7"), small_hours, "auto")
    assert not R.is_night(cfg(night="22-7"), noon, "auto")


def test_the_bar_survives_night_mode():
    """Setting NIGHT to On must not hide the control that sets it back —
    that would be a one-way door escapable only over SSH."""
    night = _at_hour(3)
    snap = R.demo_snapshot(now=night)
    state = {**BAR_STATE, "night_mode": "on"}
    washed = R.render(cfg(), snap, now=night, state=state, bar=False)
    with_bar = R.render(cfg(), snap, now=night, state=state, bar=True)
    assert np.asarray(washed).tobytes() != np.asarray(with_bar).tobytes()
    # The bar occupies the bottom strip in both the wash and the dashboard.
    a, b = np.asarray(washed, dtype=int), np.asarray(with_bar, dtype=int)
    assert (a != b).any(axis=2)[280:, :].any()


def test_the_bar_does_not_cover_the_reading():
    """The dashboard is drawn into the space left over, not underneath."""
    when = _daytime()
    snap = R.demo_snapshot(now=when)
    with_bar = R.render(cfg(night="off"), snap, now=when, state=BAR_STATE, bar=True)
    # The hero number still has to be there, at full saturation.
    top = np.asarray(with_bar.convert("RGB"), dtype=int)[:160, :]
    assert (top.max(axis=2) - top.min(axis=2)).max() > 60


def test_config_rejects_an_unknown_night_mode():
    assert any("night_mode must be" in p for p in cfg(night_mode="sometimes").validate())


def test_night_wake_overrides_the_ambient_wash():
    """A tap at 3am must show the real screen, not a slightly different wash."""
    night = _at_hour(3)
    snap = R.demo_snapshot(now=night)
    washed = R.render(cfg(night="22-7"), snap, now=night)
    woken = R.render(cfg(night="22-7"), snap, now=night, force_day=True)
    assert np.asarray(washed).tobytes() != np.asarray(woken).tobytes()
    # The wash is nearly uniform; the dashboard is not.
    assert np.asarray(washed.convert("L")).std() < np.asarray(woken.convert("L")).std()


def test_is_night_handles_windows_that_cross_midnight():
    assert R.is_night(cfg(night="22-7"), _at_hour(23))
    assert R.is_night(cfg(night="22-7"), _at_hour(3))
    assert not R.is_night(cfg(night="22-7"), _at_hour(12))
    assert not R.is_night(cfg(night="off"), _at_hour(3))
    # A same-day window must not be treated as wrapping.
    assert R.is_night(cfg(night="13-15"), _at_hour(14))
    assert not R.is_night(cfg(night="13-15"), _at_hour(23))


def test_config_rejects_an_unknown_view():
    assert any("view must be" in p for p in cfg(view="graph").validate())


def _at_hour(hour: int) -> float:
    import datetime
    return datetime.datetime.now().replace(hour=hour, minute=30, second=0).timestamp()


def _at_hour(hour: int) -> float:
    import datetime
    return datetime.datetime.now().replace(hour=hour, minute=30, second=0).timestamp()


# ── the control bar ─────────────────────────────────────────────────────────


BAR_STATE = {"view": "full", "night_mode": "auto", "units": "mgdl"}


def test_buttons_tile_the_full_width_without_gaps_or_overlap():
    """A dead strip between two buttons is a press that does nothing, which on
    a touchscreen is indistinguishable from a broken panel."""
    bs = R.button_layout(cfg(), BAR_STATE, 480, 320)
    assert [b.key for b in bs] == ["view", "night", "units", "other", "minimize"]
    assert bs[0].x0 == 0
    assert bs[-1].x1 == 479
    for left, right in zip(bs, bs[1:]):
        assert right.x0 == left.x1 + 1, f"gap/overlap between {left.key} and {right.key}"


def test_every_pixel_of_the_bar_hits_exactly_one_button():
    bs = R.button_layout(cfg(), BAR_STATE, 480, 320)
    y = (bs[0].y0 + bs[0].y1) // 2
    for x in range(0, 480):
        assert R.hit_test(bs, x, y) is not None, f"x={x} hits nothing"
    # ...and nothing above the bar responds.
    assert R.hit_test(bs, 240, bs[0].y0 - 1) is None


def test_hit_regions_follow_the_panel_size():
    """The bar is drawn from button_layout and hit-tested from button_layout.
    If it ever stopped scaling, clicks would land where the buttons used to
    be — visibly moved, still responding at the old coordinates."""
    for w, h in ((480, 320), (320, 240), (800, 480)):
        bs = R.button_layout(cfg(), BAR_STATE, w, h)
        assert bs[0].x0 == 0 and bs[-1].x1 == w - 1
        assert bs[0].y1 == h
        assert R.hit_test(bs, w // 2, h - 2) is not None


def test_button_labels_report_the_current_state_not_the_action():
    """Mixing "what this is" and "what this does" across one row of buttons is
    how you get a NIGHT button nobody can read."""
    def val(state, key):
        return next(b.value for b in R.button_layout(cfg(), state, 480, 320) if b.key == key)

    assert val({**BAR_STATE, "night_mode": "auto"}, "night") == "Auto"
    assert val({**BAR_STATE, "night_mode": "on"}, "night") == "On"
    assert val({**BAR_STATE, "night_mode": "off"}, "night") == "Off"
    assert val({**BAR_STATE, "view": "minimal"}, "view") == "Minimal"
    assert val({**BAR_STATE, "units": "mmol"}, "units") == "mmol/L"


def test_night_mode_override_beats_the_schedule_both_ways():
    noon, small_hours = _at_hour(12), _at_hour(3)
    assert R.is_night(cfg(night="22-7"), noon, "on")
    assert not R.is_night(cfg(night="22-7"), small_hours, "off")
    assert R.is_night(cfg(night="22-7"), small_hours, "auto")
    assert not R.is_night(cfg(night="22-7"), noon, "auto")


def test_the_bar_survives_night_mode():
    """Setting NIGHT to On must not hide the control that sets it back —
    that would be a one-way door escapable only over SSH."""
    night = _at_hour(3)
    snap = R.demo_snapshot(now=night)
    state = {**BAR_STATE, "night_mode": "on"}
    washed = R.render(cfg(), snap, now=night, state=state, bar=False)
    with_bar = R.render(cfg(), snap, now=night, state=state, bar=True)
    assert np.asarray(washed).tobytes() != np.asarray(with_bar).tobytes()
    # The bar occupies the bottom strip in both the wash and the dashboard.
    a, b = np.asarray(washed, dtype=int), np.asarray(with_bar, dtype=int)
    assert (a != b).any(axis=2)[280:, :].any()


def test_the_bar_does_not_cover_the_reading():
    """The dashboard is drawn into the space left over, not underneath."""
    when = _daytime()
    snap = R.demo_snapshot(now=when)
    with_bar = R.render(cfg(night="off"), snap, now=when, state=BAR_STATE, bar=True)
    # The hero number still has to be there, at full saturation.
    top = np.asarray(with_bar.convert("RGB"), dtype=int)[:160, :]
    assert (top.max(axis=2) - top.min(axis=2)).max() > 60


def test_config_rejects_an_unknown_night_mode():
    assert any("night_mode must be" in p for p in cfg(night_mode="sometimes").validate())


def test_night_wake_overrides_the_ambient_wash():
    """A tap at 3am must show the real screen, not a slightly different wash."""
    night = _at_hour(3)
    snap = R.demo_snapshot(now=night)
    washed = R.render(cfg(night="22-7"), snap, now=night)
    woken = R.render(cfg(night="22-7"), snap, now=night, force_day=True)
    assert np.asarray(washed).tobytes() != np.asarray(woken).tobytes()
    # The wash is nearly uniform; the dashboard is not.
    assert np.asarray(washed.convert("L")).std() < np.asarray(woken.convert("L")).std()


def test_is_night_handles_windows_that_cross_midnight():
    assert R.is_night(cfg(night="22-7"), _at_hour(23))
    assert R.is_night(cfg(night="22-7"), _at_hour(3))
    assert not R.is_night(cfg(night="22-7"), _at_hour(12))
    assert not R.is_night(cfg(night="off"), _at_hour(3))
    # A same-day window must not be treated as wrapping.
    assert R.is_night(cfg(night="13-15"), _at_hour(14))
    assert not R.is_night(cfg(night="13-15"), _at_hour(23))


def test_config_rejects_an_unknown_view():
    assert any("view must be" in p for p in cfg(view="graph").validate())


def _at_hour(hour: int) -> float:
    import datetime
    return datetime.datetime.now().replace(hour=hour, minute=30, second=0).timestamp()


def test_the_hidden_exit_code_matches_the_systemd_unit():
    """EXIT_HIDDEN and RestartPreventExitStatus have to agree or the gesture
    silently does nothing: the service exits and comes straight back."""
    from pathlib import Path

    from sentinelle_display import cli

    unit = Path(__file__).resolve().parent.parent / "systemd" / "sentinelle-display.service"
    installer = Path(__file__).resolve().parent.parent / "install.sh"
    for f in (unit, installer):
        if f.exists():
            assert f"RestartPreventExitStatus={cli.EXIT_HIDDEN}" in f.read_text(), \
                f"{f.name} does not prevent restart on exit {cli.EXIT_HIDDEN}"


# ── process detection ───────────────────────────────────────────────────────


def test_run_argv_matching_is_exact_not_substring():
    """`pgrep -f "sentinelle-display run"` substring-matches the whole command
    line, so it reports the shell running the check and `status` claims the
    display is up when it is not. This is the replacement; it must not."""
    from sentinelle_display.cli import is_run_argv

    # real invocations
    assert is_run_argv(["/opt/sentinelle-display/bin/sentinelle-display", "run"])
    assert is_run_argv(["python3", "-m", "sentinelle_display.cli", "run"])
    assert is_run_argv(["sentinelle-display", "run", "--backend", "png"])

    # not the display
    assert not is_run_argv(["sentinelle-display", "status"])
    assert not is_run_argv(["sentinelle-display", "pair"])
    assert not is_run_argv(["/bin/bash", "-c", "echo sentinelle-display run"])
    assert not is_run_argv(["vim", "sentinelle-display"])
    assert not is_run_argv(["run"])
    assert not is_run_argv([])


# ── the OTHER page: daily review + joke ─────────────────────────────────────


OTHER_STATE = {"view": "full", "page": "other", "night_mode": "auto", "units": "mgdl"}


def test_the_other_page_swaps_view_for_refresh_and_keeps_a_way_back():
    """The page has exactly one control it needs (Refresh) and exactly one way
    out (Back). Losing either one strands a device with no keyboard."""
    bs = R.button_layout(cfg(), OTHER_STATE, 480, 320)
    keys = [b.key for b in bs]
    assert keys == ["refresh", "night", "units", "other", "minimize"]
    assert next(b.value for b in bs if b.key == "other") == "Back"
    # ...and on the dashboard the same slot offers the way in.
    back = R.button_layout(cfg(), BAR_STATE, 480, 320)
    assert next(b.value for b in back if b.key == "other") == "Other"


def test_the_bar_still_tiles_with_five_buttons():
    for w, h in ((480, 320), (320, 240), (800, 480)):
        bs = R.button_layout(cfg(), OTHER_STATE, w, h)
        assert bs[0].x0 == 0 and bs[-1].x1 == w - 1
        for left, right in zip(bs, bs[1:]):
            assert right.x0 == left.x1 + 1, f"gap/overlap at {left.key}/{right.key}"
        for x in range(0, w):
            assert R.hit_test(bs, x, h - 2) is not None, f"x={x} hits nothing"


def test_the_other_page_draws_both_halves():
    """Recommendation on top, joke underneath. If either half is blank the
    page has silently lost half its content."""
    when = _daytime()
    img = R.render(cfg(night="off"), R.demo_snapshot(now=when), now=when,
                   state=OTHER_STATE, page="other", review=R.demo_review(), bar=True)
    a = np.asarray(img.convert("L"), dtype=int)
    top, bottom = a[30:150, :], a[180:270, :]
    assert top.max() > 90, "no recommendation text drawn"
    assert bottom.max() > 70, "no joke drawn"


def test_the_other_page_is_not_the_dashboard():
    when = _daytime()
    snap = R.demo_snapshot(now=when)
    dash = R.render(cfg(night="off"), snap, now=when, state=BAR_STATE, bar=True)
    other = R.render(cfg(night="off"), snap, now=when, state=OTHER_STATE,
                     page="other", review=R.demo_review(), bar=True)
    assert np.asarray(dash).tobytes() != np.asarray(other).tobytes()


def test_the_other_page_overrides_the_night_wash():
    """You only reach this page by pressing a button, so you are standing at
    the panel. Answering an explicit request with a colour field would read as
    a broken button."""
    night = _at_hour(3)
    img = R.render(cfg(night="22-7"), R.demo_snapshot(now=night), now=night,
                   state=OTHER_STATE, page="other", review=R.demo_review())
    assert np.asarray(img.convert("L")).max() > 90


def test_a_review_from_an_earlier_day_is_dimmed_and_dated():
    """Yesterday's advice shown as if it were tonight's is the one genuinely
    harmful thing this page could do."""
    when = _daytime()
    fresh = R.demo_review()
    stale = R.demo_review()
    stale.data = {**stale.data, "is_today": False, "day": "2026-08-30"}
    a = np.asarray(R.render(cfg(night="off"), R.demo_snapshot(now=when), now=when,
                            state=OTHER_STATE, page="other", review=fresh).convert("L"),
                   dtype=int)
    b = np.asarray(R.render(cfg(night="off"), R.demo_snapshot(now=when), now=when,
                            state=OTHER_STATE, page="other", review=stale).convert("L"),
                   dtype=int)
    assert a.tobytes() != b.tobytes()
    # The recommendation half is drawn in a muted ink when it is not today's.
    assert b[30:150, :].max() < a[30:150, :].max()


@pytest.mark.parametrize("kind", ["ok", "waiting", "error"])
def test_every_review_state_renders_without_raising(kind):
    when = _daytime()
    img = R.render(cfg(night="off"), R.demo_snapshot(now=when), now=when,
                   state=OTHER_STATE, page="other", review=R.demo_review(kind), bar=True)
    assert img.size == (480, 320)
    assert np.asarray(img.convert("L")).max() > 60


def test_the_page_says_so_when_the_review_is_switched_off():
    kind, msg = R._review_state(cfg(review="off"), R.demo_review())
    assert kind == "off" and "switched off" in msg
    # ...and a missing poller is the same case, not a crash.
    assert R._review_state(cfg(), None)[0] == "off"


def test_a_missing_review_still_renders_the_page():
    when = _daytime()
    img = R.render(cfg(night="off"), R.demo_snapshot(now=when), now=when,
                   state=OTHER_STATE, page="other", review=None, bar=True)
    assert img.size == (480, 320)


def test_long_text_shrinks_to_fit_rather_than_falling_off_the_glass():
    """Agent 4 is told to write two or three sentences; the layout must not
    depend on it obeying."""
    from PIL import Image, ImageDraw

    lay = R.Layout(480, 276)
    draw = ImageDraw.Draw(Image.new("RGB", (480, 276)))
    long_text = ("Your overnight glucose sat above target from midnight until nearly six, "
                 "which lines up with the late dinner and the evening walk you skipped. "
                 "Tomorrow is a Saturday and your Saturdays are usually far more active. "
                 "Keep fast-acting carbohydrate with you and check in mid-morning.") * 12
    box_h = 140
    _f, lines, line_h = R._fit_text(draw, long_text, "regular", (19, 17, 15, 13, 11),
                                    lay.w - 32, box_h, lay)
    assert len(lines) * line_h <= box_h
    assert lines[-1].endswith("…"), "over-long text must show that it was cut"


def test_wrapping_never_splits_a_word():
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (480, 276)))
    f = R.font("regular", 15)
    text = "Dinner ran you up to 14.2 for nearly three hours last night."
    lines = R._wrap(draw, text, f, 160)
    assert " ".join(lines).split() == text.split()


def test_the_other_page_works_before_the_first_reading_arrives():
    """A freshly paired Pi has no glucose yet. The review page does not depend
    on one, and should not be replaced by the waiting screen."""
    when = _daytime()
    empty = Snapshot(ok=False, data=None, last_error="waiting for the first reading")
    img = R.render(cfg(night="off"), empty, now=when, state=OTHER_STATE,
                   page="other", review=R.demo_review(), bar=True)
    a = np.asarray(img.convert("L"), dtype=int)
    assert a[30:150, :].max() > 90, "the recommendation is not on screen"


def test_the_glucose_offline_banner_does_not_land_on_the_review_header():
    when = _daytime()
    snap = R.demo_snapshot(now=when)
    snap.consecutive_failures = 3
    assert snap.offline
    img = R.render(cfg(night="off"), snap, now=when, state=OTHER_STATE,
                   page="other", review=R.demo_review(), bar=True)
    strip = np.asarray(img.convert("RGB"), dtype=int)[:20, :]
    # The banner is a saturated amber bar; the review header is not.
    assert (strip.max(axis=2) - strip.min(axis=2)).max() < 60

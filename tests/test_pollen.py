"""
Pollen parsing and banding.

SPDX-License-Identifier: AGPL-3.0-only

The thing worth guarding here is not arithmetic — it is the difference between
"not measured" and "none detected". Open-Meteo returns null for a species that
is out of season, and turning that into 0 would put a reassuring "Low" on the
screen for a measurement nobody took.
"""

from __future__ import annotations

from sentinelle_display import pollen as P
from sentinelle_display.config import Config


def payload(**kw):
    """An Open-Meteo air-quality response with every species defaulting to null,
    which is what out-of-season actually looks like."""
    current = {f"{s}_pollen": None for s in P.SPECIES}
    current.update({f"{k}_pollen": v for k, v in kw.items()})
    return {"current": current}


# ── null is not zero ────────────────────────────────────────────────────────


def test_out_of_season_reports_nothing_not_low():
    """A null is an absent measurement. Rendering it as 'Low' would invent a
    reassuring reading that was never taken."""
    r = P.parse(payload(), "Edgware")
    assert r.ok is True          # the fetch succeeded...
    assert r.species == []       # ...and correctly found nothing to report
    assert r.worst is None


def test_a_real_zero_is_kept():
    """Zero IS a measurement: the model looked and found none. It must survive
    where a null does not."""
    r = P.parse(payload(alder=0.0), "Edgware")
    assert [(n, v, b) for n, v, b in r.species] == [("alder", 0.0, "Low")]


def test_nulls_are_dropped_alongside_real_values():
    r = P.parse(payload(grass=41.0), "Edgware")
    assert [n for n, _, _ in r.species] == ["grass"]


# ── banding ─────────────────────────────────────────────────────────────────


def test_grass_uses_the_uk_met_office_bands():
    """<30 Low, 30-49 Moderate, 50-149 High, 150+ Very high — the scale behind
    the headline number in a UK pollen forecast."""
    assert P.band_for("grass", 29.9) == "Low"
    assert P.band_for("grass", 30) == "Moderate"
    assert P.band_for("grass", 49.9) == "Moderate"
    assert P.band_for("grass", 50) == "High"
    assert P.band_for("grass", 149) == "High"
    assert P.band_for("grass", 150) == "Very high"


def test_species_are_banded_separately_not_on_one_scale():
    """Absolute counts differ by an order of magnitude between species. A
    ragweed count of 60 is severe where a birch count of 60 is unremarkable —
    one shared scale would badly misreport both."""
    assert P.band_for("ragweed", 60) == "Very high"
    assert P.band_for("birch", 60) == "High"
    assert P.band_for("grass", 60) == "High"
    assert P.band_for("birch", 8) == "Low"
    assert P.band_for("ragweed", 8) == "Moderate"


def test_band_for_null_is_none_not_low():
    assert P.band_for("grass", None) is None


# ── ordering ────────────────────────────────────────────────────────────────


def test_worst_species_leads_by_band_not_by_raw_number():
    """The panel leads with what matters. 8 grains of ragweed is Moderate
    while 9 of birch is still Low, so the smaller number must lead."""
    r = P.parse(payload(ragweed=8.0, birch=9.0), "Edgware")
    assert r.worst[0] == "ragweed"
    assert r.worst[2] == "Moderate"


def test_ties_inside_a_band_break_on_each_species_own_scale():
    """Two species in the same band must not be ordered by raw grains/m3 —
    that is the apples-to-oranges comparison the per-species thresholds exist
    to prevent. 30 grains of ragweed (6x its moderate line) outranks 30 of
    birch (3x its own), despite being the identical raw number."""
    assert P.severity("ragweed", 30) > P.severity("birch", 30)
    r = P.parse(payload(ragweed=30.0, birch=30.0), "Edgware")
    assert [n for n, _, _ in r.species] == ["ragweed", "birch"]
    # ragweed 30 is past its high line of 20; birch 30 is only past its
    # moderate line of 10. Same raw number, genuinely different events.
    assert dict((n, b) for n, _, b in r.species) == {
        "ragweed": "High",
        "birch": "Moderate",
    }


def test_ordering_is_stable_between_identical_frames():
    """Ties must not shuffle, or the panel visibly reshuffles every redraw."""
    a = P.parse(payload(grass=5.0, birch=5.0, alder=5.0), "Edgware")
    b = P.parse(payload(grass=5.0, birch=5.0, alder=5.0), "Edgware")
    assert [n for n, _, _ in a.species] == [n for n, _, _ in b.species]


# ── the URL it builds ───────────────────────────────────────────────────────


def test_url_requests_every_species_at_the_configured_location():
    cfg = Config(pollen="on", pollen_lat=51.6136, pollen_lon=-0.275)
    url = P.PollenPoller(cfg)._url()
    assert "latitude=51.6136" in url
    assert "longitude=-0.275" in url
    for species in P.SPECIES:
        assert f"{species}_pollen" in url


def test_allergy_url_overrides_the_endpoint_entirely():
    """So this can be repointed at an OpenClaw endpoint serving the same shape
    without touching any code."""
    cfg = Config(pollen="on", allergy_url="https://openclaw.example/pollen.json")
    assert P.PollenPoller(cfg)._url() == "https://openclaw.example/pollen.json"


# ── failure handling ────────────────────────────────────────────────────────


def test_a_fetch_failure_keeps_the_last_good_reading():
    """A pollen outage must not blank a panel that had real data a minute ago,
    and must never disturb the glucose reading beside it."""
    cfg = Config(pollen="on", allergy_url="http://127.0.0.1:1/nope")
    pp = P.PollenPoller(cfg)
    with pp._lock:
        pp.reading = P.parse(payload(grass=44.0), "Edgware")
    pp._fetch_once()
    after = pp.get()
    assert [n for n, _, _ in after.species] == ["grass"]
    assert after.last_error


def test_a_garbage_payload_does_not_raise():
    for junk in ({}, {"current": None}, {"current": {"grass_pollen": "n/a"}}):
        P.parse(junk if isinstance(junk, dict) else {}, "Edgware")


# ── zeros are measurements, but not worth a row ─────────────────────────────


def test_zero_counts_are_kept_as_data_but_not_shown():
    """Checked against the live API: out of season it returns 0.0, NOT null —
    the docs' "only available during pollen season" reads like null but isn't.
    So the raw list is mostly zeros, and a panel row saying 'Alder 0' spends
    one of three scarce rows saying nothing."""
    r = P.parse(payload(alder=0.0, birch=0.0, grass=5.5,
                        mugwort=2.8, olive=0.0, ragweed=0.0), "Edgware")
    assert len(r.species) == 6                       # all six were measured
    assert [n for n, _, _ in r.detected] == ["mugwort", "grass"]


def test_all_zero_is_none_detected_not_out_of_season():
    """Three different situations that must not collapse into one message:
    all-zero is a measurement, all-null is the API declining to measure, and
    a failed fetch is us not knowing either way."""
    measured = P.parse(payload(**{s: 0.0 for s in P.SPECIES}), "Edgware")
    assert measured.species and not measured.detected   # -> "none detected"

    unmeasured = P.parse(payload(), "Edgware")
    assert not unmeasured.species                        # -> "out of season"


def test_worst_ignores_zeros_when_anything_is_present():
    r = P.parse(payload(alder=0.0, grass=5.5), "Edgware")
    assert r.worst[0] == "grass"

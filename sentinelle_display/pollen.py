"""
Pollen for the allergen panel.

SPDX-License-Identifier: AGPL-3.0-only

Source: Open-Meteo's Air Quality API, which serves the Copernicus CAMS
European air-quality model at ~11 km resolution. Six species — alder, birch,
grass, mugwort, olive, ragweed — in grains/m3. No API key.

Two things about that source you have to design around:

  * EUROPE ONLY. CAMS's pollen fields do not cover North America, which is
    also why this could not simply reuse the OpenClaw briefing's pollen: that
    pipeline reads pollen.com by US ZIP code and has no London coverage at all.
  * SEASONAL, and it signals that with ZEROS. The docs say pollen is "only
    available during pollen season", which reads like it returns null out of
    season. Checked against the live API in August: it returns 0.0 for every
    dormant species, not null. Both are handled — null is still treated as
    "not measured" if it ever appears — but zero is the case that actually
    happens, and it is why the panel filters zero-count species out of its
    list. A row reading "Alder 0" spends one of three scarce rows saying
    nothing.

`allergy_url` in the config overrides the endpoint. Point it at an OpenClaw
endpoint serving the same shape and this module needs no changes.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

ENDPOINT = "https://air-quality-api.open-meteo.com/v1/air-quality"
USER_AGENT = "sentinelle-pi-display/1.0"
TIMEOUT = 15

SPECIES = ("alder", "birch", "grass", "mugwort", "olive", "ragweed")

# Band boundaries in grains/m3: (moderate_at, high_at, very_high_at).
#
# GRASS carries the UK Met Office's published bands (<30 low, 30-49 moderate,
# 50-149 high, 150+ very high) — that is the scale behind the headline "pollen
# count" in a UK forecast, and grass is the species most people react to here.
#
# The tree and weed rows are NOT as standardised. Absolute counts differ by
# an order of magnitude between species (a birch count of 60 is unremarkable
# where a ragweed count of 60 is severe), so applying the grass bands to all
# six would badly misreport. These follow the commonly published per-species
# scales. Treat them as indicative: the panel always shows the raw
# grains/m3 next to the word, so the underlying number is never hidden behind
# our banding.
THRESHOLDS: dict[str, tuple[float, float, float]] = {
    "grass": (30, 50, 150),
    "birch": (10, 50, 500),
    "alder": (10, 50, 500),
    "mugwort": (10, 50, 200),
    "olive": (20, 50, 200),
    "ragweed": (5, 20, 50),
}

BANDS = ("Low", "Moderate", "High", "Very high")


def band_for(species: str, value: float | None) -> str | None:
    """Band name, or None when there is no measurement.

    None is not "Low". A species out of season reports nothing, and saying
    "Low" would be inventing a reassuring measurement that was never taken.
    """
    if value is None:
        return None
    mod, high, very = THRESHOLDS.get(species, THRESHOLDS["grass"])
    if value >= very:
        return "Very high"
    if value >= high:
        return "High"
    if value >= mod:
        return "Moderate"
    return "Low"


def band_rank(band: str | None) -> int:
    return BANDS.index(band) if band in BANDS else -1


def severity(species: str, value: float) -> float:
    """How elevated a count is ON ITS OWN SPECIES' SCALE.

    1.0 means "exactly at this species' moderate threshold". This exists to
    order two species inside the same band. Sorting on raw grains/m3 there
    would reintroduce precisely the apples-to-oranges comparison the
    per-species thresholds are for: 20 grains of birch and 20 of ragweed are
    not the same event, and whichever happened to be the larger number would
    lead the panel.
    """
    mod, _high, _very = THRESHOLDS.get(species, THRESHOLDS["grass"])
    return value / mod if mod else value


@dataclass
class PollenReading:
    """What the panel draws."""

    ok: bool = False
    label: str = ""
    fetched_at: float = 0.0
    # [(species, grains/m3, band)] sorted worst-first
    species: list[tuple[str, float, str]] = field(default_factory=list)
    last_error: str | None = None

    @property
    def detected(self) -> list[tuple[str, float, str]]:
        """Only species actually present. This is what the panel lists.

        Out of season the API reports 0.0 for every dormant species, so the
        raw list is mostly zeros. Showing them would fill the panel with rows
        that carry no information and push a real reading off the bottom.
        """
        return [r for r in self.species if r[1] > 0]

    @property
    def worst(self) -> tuple[str, float, str] | None:
        rows = self.detected or self.species
        return rows[0] if rows else None

    @property
    def age_minutes(self) -> float | None:
        if not self.fetched_at:
            return None
        return (time.time() - self.fetched_at) / 60


def parse(payload: dict[str, Any], label: str) -> PollenReading:
    """Normalises an Open-Meteo air-quality response.

    Split out from the fetching so it can be tested against canned payloads,
    including the out-of-season all-nulls case that is easy to get wrong.
    """
    current = payload.get("current") or {}
    rows: list[tuple[str, float, str]] = []
    for name in SPECIES:
        raw = current.get(f"{name}_pollen")
        if raw is None:
            continue                       # not measured — not zero
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        band = band_for(name, value)
        if band is None:
            continue
        rows.append((name, value, band))

    # Worst first, so the panel leads with what actually matters today.
    # Band first, then severity relative to that species' own scale, then name
    # so equals keep a stable order instead of reshuffling every redraw.
    rows.sort(key=lambda r: (-band_rank(r[2]), -severity(r[0], r[1]), r[0]))
    return PollenReading(ok=True, label=label, fetched_at=time.time(), species=rows)


class PollenPoller(threading.Thread):
    """Fetches pollen on a slow interval. Never raises into the render loop."""

    daemon = True

    def __init__(self, cfg):
        super().__init__(name="sentinelle-pollen")
        self.cfg = cfg
        self.reading = PollenReading(label=cfg.pollen_label)
        # NOT self._stop: Thread already has a private _stop() that join()
        # calls, and shadowing it breaks join() from inside the stdlib.
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stopping.set()

    def get(self) -> PollenReading:
        with self._lock:
            return self.reading

    # ------------------------------------------------------------------
    def _url(self) -> str:
        if self.cfg.allergy_url:
            return self.cfg.allergy_url
        q = urllib.parse.urlencode({
            "latitude": self.cfg.pollen_lat,
            "longitude": self.cfg.pollen_lon,
            "current": ",".join(f"{s}_pollen" for s in SPECIES),
            "timezone": "auto",
        })
        return f"{ENDPOINT}?{q}"

    def _fetch_once(self) -> None:
        req = urllib.request.Request(
            self._url(), headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode())
            reading = parse(payload, self.cfg.pollen_label)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError, ValueError) as e:
            with self._lock:
                prev = self.reading
                self.reading = PollenReading(
                    ok=prev.ok, label=prev.label, fetched_at=prev.fetched_at,
                    species=prev.species, last_error=str(e),
                )
            return
        with self._lock:
            self.reading = reading

    def run(self) -> None:
        while not self._stopping.is_set():
            self._fetch_once()
            # CAMS publishes hourly at best, so polling faster buys nothing and
            # spends someone else's free API. The glucose poller runs at 60s;
            # this deliberately does not.
            self._stopping.wait(max(self.cfg.pollen_minutes, 10) * 60)

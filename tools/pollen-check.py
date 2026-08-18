#!/usr/bin/env python3
"""
Check the pollen feed from any machine, before touching the Pi.

SPDX-License-Identifier: AGPL-3.0-only

Runs on macOS system Python with NOTHING installed: this script and the
sentinelle_display.pollen module it exercises are stdlib-only (no Pillow, no
numpy). That is the point — you can confirm the feed works from your laptop
before deploying anything.

It imports the REAL parsing code rather than reimplementing it, so a pass here
means the code that will run on the Pi handles today's actual response.

    cd ~/Documents/Apps/SentinelleT1D/pi-display
    python3 tools/pollen-check.py                 # Edgware, the default
    python3 tools/pollen-check.py --raw           # ...and dump the raw JSON
    python3 tools/pollen-check.py --lat 51.5072 --lon -0.1276 --label London
    python3 tools/pollen-check.py --url https://your-host/pollen.json

Exit status: 0 got a usable reading, 1 reachable but nothing reported
(out of season), 2 could not reach the source at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Import the real modules from the repo this script lives in.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from sentinelle_display import pollen as P
    from sentinelle_display.config import Config
except ImportError as e:  # pragma: no cover - only when run outside the repo
    print(f"Could not import sentinelle_display: {e}", file=sys.stderr)
    print("Run this from inside the repo:  python3 tools/pollen-check.py",
          file=sys.stderr)
    raise SystemExit(2)


BAR = {"Low": "▁", "Moderate": "▃", "High": "▆", "Very high": "█"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--lat", type=float, default=51.6136, help="default: Edgware")
    ap.add_argument("--lon", type=float, default=-0.2750)
    ap.add_argument("--label", default="Edgware")
    ap.add_argument("--url", default="", help="override the endpoint entirely")
    ap.add_argument("--raw", action="store_true", help="also dump the raw JSON")
    args = ap.parse_args(argv)

    cfg = Config(pollen="on", pollen_lat=args.lat, pollen_lon=args.lon,
                 pollen_label=args.label, allergy_url=args.url)
    url = P.PollenPoller(cfg)._url()

    print(f"Location : {args.label}  ({args.lat}, {args.lon})")
    print(f"Source   : {'custom' if args.url else 'Open-Meteo air quality (CAMS)'}")
    print(f"URL      : {url}")
    print()

    req = urllib.request.Request(
        url, headers={"User-Agent": P.USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=P.TIMEOUT) as resp:
            status = resp.status
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} from the server.", file=sys.stderr)
        print(e.read().decode()[:500], file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"Could not reach it: {e}", file=sys.stderr)
        print("\nIf you are behind a proxy or VPN, that is the usual cause.",
              file=sys.stderr)
        return 2

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"HTTP {status} but the body is not JSON: {e}", file=sys.stderr)
        print(body[:500], file=sys.stderr)
        return 2

    if args.raw:
        print("--- raw response ---")
        print(json.dumps(payload, indent=2)[:4000])
        print("--- end raw ---\n")

    # The fields the panel actually reads. Printed explicitly because a null
    # here is meaningful (out of season) and easy to confuse with a missing
    # field (wrong response shape) — they need different fixes.
    current = payload.get("current") or {}
    missing = [f"{s}_pollen" for s in P.SPECIES if f"{s}_pollen" not in current]
    print("Fields returned:")
    for species in P.SPECIES:
        key = f"{species}_pollen"
        if key not in current:
            print(f"  {key:<16} MISSING FROM RESPONSE")
        elif current[key] is None:
            print(f"  {key:<16} null   (not measured — out of season)")
        else:
            print(f"  {key:<16} {current[key]}")
    print()

    # Missing and null are different failures needing different fixes, and
    # collapsing them is the mistake this script exists to prevent. A null is
    # the API correctly saying "not this time of year". A MISSING field means
    # the response shape is not what the parser expects, and no amount of
    # waiting for spring will fix it.
    if missing:
        print(f"{len(missing)} expected field(s) are absent, not null.",
              file=sys.stderr)
        print("The response shape does not match what the panel parses — this",
              file=sys.stderr)
        print("is NOT an out-of-season result. Re-run with --raw and send me",
              file=sys.stderr)
        print("the output; the parser needs adjusting.", file=sys.stderr)
        return 2

    reading = P.parse(payload, args.label)
    if not reading.detected:
        if reading.species:
            print("Every species reads 0.0 — measured, and none present.")
            print("The panel will show \"none detected\".")
        else:
            print("Every species is null — not measured at all.")
            print("The panel will show \"out of season\".")
        print()
        print("If you expected numbers: CAMS covers EUROPE ONLY, so this")
        print("returns nothing useful for North American coordinates.")
        return 1

    print("What the panel will show:")
    worst = reading.detected[0]
    print(f"  {worst[2].upper()}")
    print()
    print(f"  {'species':<10}{'grains/m3':>11}   {'band':<11}  on its own scale")
    for name, value, band in reading.detected:
        sev = P.severity(name, value)
        print(f"  {name:<10}{value:>11.1f}   {band:<11}  {BAR.get(band, '?')} {sev:.1f}x moderate")

    zeros = [n for n, v, _ in reading.species if v == 0]
    if zeros:
        print()
        print(f"  Reading 0.0, so not listed on the panel: {', '.join(zeros)}")

    print()
    print(f"  Rows on screen: {', '.join(n for n, _, _ in reading.detected[:3])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Configuration and credential storage for the Sentinelle Pi display.

SPDX-License-Identifier: AGPL-3.0-only

Everything the display needs to run lives in one JSON file, written 0600:

    ~/.config/sentinelle/display.json

    {
      "base_url": "https://sentinelle.example.com",
      "token":    "<dashboard-scoped kiosk token>",
      "units":    "mgdl",
      "low": 70, "high": 180,
      "hours": 3,
      "stale_minutes": 20,
      "night": "22-7",
      "rotate": 0,
      "backend": "auto"
    }

The token is a long-lived read-only bearer credential. It reaches exactly two
endpoints on the server (/kiosk/data and /night/data), can write nothing, and
carries no name, email or account identifier — but anyone who reads this file
can watch the owner's glucose until the token is revoked from the web app's
Settings page. Hence 0600, and hence the deliberate refusal to log it.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("SENTINELLE_CONFIG_DIR", Path.home() / ".config" / "sentinelle"))
CONFIG_PATH = CONFIG_DIR / "display.json"


@dataclass
class Config:
    # --- set by `sentinelle-display pair` -------------------------------------
    base_url: str = ""
    token: str = ""
    label: str = ""

    # --- display preferences --------------------------------------------------
    units: str = "mgdl"          # "mgdl" | "mmol"
    low: int = 70                # mg/dL, always — mmol display converts on the fly
    high: int = 180              # mg/dL
    hours: int = 3               # hours of trend on the sparkline (1-12)
    stale_minutes: int = 20      # grey the screen out after this long with no reading
    night: str = "22-7"          # dim ambient wash during these hours; "off" disables
    poll_seconds: int = 60       # how often to ask the server for new data
    palette: str = "clinical"    # "clinical" (red/green/amber) | "cvd" (red/blue/amber)

    # --- hardware -------------------------------------------------------------
    backend: str = "auto"        # "auto" | "fb" | "spi" | "png"
    width: int = 480
    height: int = 320
    rotate: int = 0              # 0 | 90 | 180 | 270, applied in software
    panel: str = "ili9486"       # direct-SPI backend only
    spi_bus: int = 0
    spi_device: int = 0
    spi_hz: int = 32_000_000
    pin_dc: int = 24             # BCM numbering
    pin_reset: int = 25
    pin_backlight: int = 18      # -1 if the panel has no controllable backlight
    touch: str = "off"           # "off" | "xpt2046"
    touch_device: int = 1        # SPI CE for the XPT2046

    # --- runtime-only, never persisted ---------------------------------------
    _transient: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------------
    @property
    def kiosk_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/kiosk/data"

    @property
    def night_window(self) -> tuple[int, int] | None:
        """(from_hour, to_hour) or None when night mode is disabled."""
        if not self.night or self.night.lower() == "off":
            return None
        try:
            a, b = self.night.split("-", 1)
            return int(a) % 24, int(b) % 24
        except (ValueError, AttributeError):
            return None

    def validate(self) -> list[str]:
        """Returns a list of human-readable problems; empty means good to go."""
        problems: list[str] = []
        if not self.base_url:
            problems.append("no server URL — run `sentinelle-display pair` first")
        elif not self.base_url.startswith(("http://", "https://")):
            problems.append(f"base_url must start with http:// or https:// (got {self.base_url!r})")
        if not self.token:
            problems.append("no access token — run `sentinelle-display pair` first")
        if self.units not in ("mgdl", "mmol"):
            problems.append(f"units must be mgdl or mmol (got {self.units!r})")
        if not 1 <= self.hours <= 12:
            problems.append(f"hours must be between 1 and 12 (got {self.hours})")
        if self.rotate not in (0, 90, 180, 270):
            problems.append(f"rotate must be 0, 90, 180 or 270 (got {self.rotate})")
        if self.low >= self.high:
            problems.append(f"low ({self.low}) must be below high ({self.high})")
        if self.poll_seconds < 15:
            problems.append("poll_seconds below 15 hammers a server whose data only "
                            "changes every ~5 minutes")
        return problems


def _persistable(cfg: Config) -> dict[str, Any]:
    return {k: v for k, v in asdict(cfg).items() if not k.startswith("_")}


def load(path: Path = CONFIG_PATH) -> Config:
    """Loads config, tolerating a missing file (returns defaults) and unknown keys."""
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"{path} is not readable JSON: {e}")
    known = {f.name for f in fields(Config) if not f.name.startswith("_")}
    # Ignore unknown keys rather than crashing: a config written by a newer
    # version should not stop an older one from putting a number on the screen.
    return Config(**{k: v for k, v in raw.items() if k in known})


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    """Writes config 0600, creating the directory if needed.

    Written to a temp file and renamed so a power cut mid-write leaves the
    previous config intact rather than a truncated one — on a device with no
    keyboard, an unparseable config is a trip to find a monitor.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(_persistable(cfg), fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)

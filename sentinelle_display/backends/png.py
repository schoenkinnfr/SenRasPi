"""
PNG backend — writes frames to disk instead of to a panel.

SPDX-License-Identifier: AGPL-3.0-only

For developing the layout on a laptop, and for one specific diagnostic on the
Pi: if `--backend png` produces a correct-looking image but the panel is blank
or scrambled, the problem is wiring or the driver, not this program. That
split saves an evening.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

DEFAULT_PATH = Path(os.environ.get("SENTINELLE_PNG_OUT", "/tmp/sentinelle-display.png"))


class PngBackend:
    name = "png"

    def __init__(self, cfg, path: Path | None = None):
        self.width, self.height = cfg.width, cfg.height
        self.path = Path(path or DEFAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.frames = 0

    def show(self, image: Image.Image) -> None:
        # Written to a temp file and renamed, so anything watching the path
        # (`feh --reload`, an scp loop) never catches a half-written PNG.
        tmp = self.path.with_suffix(".tmp.png")
        image.save(tmp)
        os.replace(tmp, self.path)
        self.frames += 1

    def set_backlight(self, on: bool) -> None:
        pass

    def close(self) -> None:
        pass

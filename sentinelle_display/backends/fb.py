"""
Linux framebuffer backend.

SPDX-License-Identifier: AGPL-3.0-only

Used when a kernel driver has already claimed the panel and exposed it as
/dev/fbN. Writes raw pixels through an mmap — no X, no Wayland, no compositor,
no SDL. On a 1GB Pi that difference is the whole point: this process holds
about 35MB resident where a Chromium kiosk holds 400-600MB and restarts its
renderer when the panel is small and memory is tight.

Two pixel formats are handled, which covers every small panel worth owning:
16bpp RGB565 (nearly all SPI TFTs) and 32bpp BGRA (what the DRM fbdev
emulation layer usually presents).
"""

from __future__ import annotations

import mmap
import os
from pathlib import Path

import numpy as np
from PIL import Image

from . import BackendError

SYS_FB = Path("/sys/class/graphics")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def enumerate_framebuffers() -> list[dict]:
    """Every fbN with its geometry. Used by `probe` and by auto-detection."""
    out = []
    for dev in sorted(SYS_FB.glob("fb*")):
        size = _read(dev / "virtual_size")
        bpp = _read(dev / "bits_per_pixel")
        stride = _read(dev / "stride")
        if not size or not bpp:
            continue
        try:
            w, h = (int(v) for v in size.split(","))
        except ValueError:
            continue
        out.append({
            "device": f"/dev/{dev.name}",
            "name": _read(dev / "name") or "?",
            "width": w,
            "height": h,
            "bpp": int(bpp),
            "stride": int(stride) if stride else w * (int(bpp) // 8),
        })
    return out


def find_framebuffer(max_pixels: int = 1_400_000) -> dict | None:
    """Picks the most plausible small panel.

    Prefers the SMALLEST framebuffer, because on a Pi with anything plugged
    into HDMI the small panel is fb1 and HDMI is fb0 — grabbing fb0 would paint
    the dashboard onto a monitor that is not there. Anything larger than about
    1.4 megapixels is a desktop display, not a 3.5" panel, and is skipped.
    """
    candidates = [fb for fb in enumerate_framebuffers()
                  if fb["width"] * fb["height"] <= max_pixels]
    if not candidates:
        return None
    return min(candidates, key=lambda fb: fb["width"] * fb["height"])


class FramebufferBackend:
    name = "fb"

    def __init__(self, cfg):
        fb = find_framebuffer()
        if cfg.backend == "fb" and not fb:
            raise BackendError(
                "no framebuffer found under /sys/class/graphics.\n"
                "Either no kernel driver has claimed the panel (try "
                "backend \"spi\"), or you are running on a machine with no "
                "display at all. `sentinelle-display probe` shows what is there."
            )
        assert fb is not None
        self.info = fb
        self.width, self.height = fb["width"], fb["height"]
        self.bpp = fb["bpp"]
        self.stride = fb["stride"]

        if self.bpp not in (16, 32):
            raise BackendError(
                f"{fb['device']} is {self.bpp}bpp; only 16 (RGB565) and 32 "
                "(BGRA) are supported"
            )

        try:
            self._fd = os.open(fb["device"], os.O_RDWR)
        except PermissionError as e:
            raise BackendError(
                f"cannot open {fb['device']} — add your user to the 'video' "
                "group (`sudo usermod -aG video $USER`) and log out and back "
                "in. A group change does not apply to an existing session."
            ) from e
        except OSError as e:
            raise BackendError(f"cannot open {fb['device']}: {e}") from e

        self._map = mmap.mmap(self._fd, self.stride * self.height,
                              mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        self._backlight = _Backlight()

    # ------------------------------------------------------------------
    def show(self, image: Image.Image) -> None:
        if image.size != (self.width, self.height):
            # Never crop: a resized frame is legible, a cropped one silently
            # loses the corner the reading is in.
            image = image.resize((self.width, self.height), Image.LANCZOS)
        arr = np.asarray(image.convert("RGB"), dtype=np.uint16)

        if self.bpp == 16:
            packed = (
                ((arr[:, :, 0] & 0xF8) << 8)
                | ((arr[:, :, 1] & 0xFC) << 3)
                | (arr[:, :, 2] >> 3)
            ).astype("<u2")
            row_bytes = self.width * 2
        else:
            rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
            packed = np.dstack([
                rgb[:, :, 2], rgb[:, :, 1], rgb[:, :, 0],
                np.full(rgb.shape[:2], 255, dtype=np.uint8),
            ])
            row_bytes = self.width * 4

        raw = packed.tobytes()
        if self.stride == row_bytes:
            self._map.seek(0)
            self._map.write(raw)
        else:
            # Padded stride: copy row by row rather than assuming a tight
            # buffer. Getting this wrong produces the classic diagonal-shear
            # image that looks like a broken cable.
            for y in range(self.height):
                self._map.seek(y * self.stride)
                self._map.write(raw[y * row_bytes:(y + 1) * row_bytes])

    def set_backlight(self, on: bool) -> None:
        self._backlight.set(on)

    def close(self) -> None:
        try:
            self._map.close()
        finally:
            os.close(self._fd)


class _Backlight:
    """Best-effort brightness control via /sys/class/backlight.

    Absent on plenty of panels, so every failure here is swallowed: a display
    that refuses to start because it cannot dim itself is worse than a display
    that stays bright.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self.max = 255
        for dev in sorted(Path("/sys/class/backlight").glob("*")):
            if (dev / "brightness").exists():
                self.path = dev / "brightness"
                try:
                    self.max = int((dev / "max_brightness").read_text().strip())
                except (OSError, ValueError):
                    pass
                break

    def set(self, on: bool) -> None:
        if not self.path:
            return
        try:
            self.path.write_text(str(self.max if on else 0))
        except OSError:
            pass

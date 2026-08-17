"""
Display backends — the only code here that touches hardware.

SPDX-License-Identifier: AGPL-3.0-only

The reason this package exists is that SPI panel support on a Pi 5 is not one
thing. Depending on the panel, the kernel version and which vendor script last
touched /boot/firmware/config.txt, the same physical screen shows up as any of:

  fb    a real framebuffer at /dev/fb0, because a kernel driver claimed the
        panel (fbtft on older kernels, panel-mipi-dbi-spi / tinydrm on newer
        ones). Fastest and simplest when it works.

  spi   nothing in the kernel claimed the panel, so we drive the controller
        ourselves over /dev/spidev*. Slower, but it depends on nothing except
        spidev and two GPIO pins — which means it keeps working across kernel
        upgrades and vendor overlay churn. On a Pi 5 this is frequently the
        only thing that works at all, because the vendor LCD-show scripts and
        the legacy fbtft overlays predate the RP1 southbridge.

  png   writes frames to disk. For development on a laptop, and for proving a
        layout problem is a layout problem rather than a wiring problem.

`open_backend(cfg)` picks one. With backend="auto" it prefers fb (if a
framebuffer of a plausible size exists) and falls back to spi.
"""

from __future__ import annotations

from typing import Protocol

from PIL import Image


class Backend(Protocol):
    name: str
    width: int
    height: int

    def show(self, image: Image.Image) -> None: ...
    def set_backlight(self, on: bool) -> None: ...
    def close(self) -> None: ...


class BackendError(RuntimeError):
    """Raised with a message meant to be read by a person over SSH."""


def open_backend(cfg):
    choice = (cfg.backend or "auto").lower()

    if choice == "png":
        from .png import PngBackend
        return PngBackend(cfg)

    if choice == "fb":
        from .fb import FramebufferBackend
        return FramebufferBackend(cfg)

    if choice == "spi":
        from .spi import SpiBackend
        return SpiBackend(cfg)

    if choice == "auto":
        from .fb import FramebufferBackend, find_framebuffer
        if find_framebuffer():
            return FramebufferBackend(cfg)
        from .spi import SpiBackend
        return SpiBackend(cfg)

    raise BackendError(
        f"unknown backend {cfg.backend!r} — expected auto, fb, spi or png"
    )

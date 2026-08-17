"""
Direct SPI backend — drives the panel controller ourselves.

SPDX-License-Identifier: AGPL-3.0-only

Why this exists: on a Raspberry Pi 5 the usual route for a 3.5" SPI TFT is
frequently a dead end. The vendor "LCD-show" scripts and the legacy fbtft
overlays were written for the BCM2835/2711 GPIO block and predate the Pi 5's
RP1 southbridge; several of them edit the wrong config file, load a module
that no longer exists, or leave you with a backlight and no picture. Rather
than fight that, this backend talks to the controller over /dev/spidev and two
GPIO lines and asks nothing of the kernel beyond SPI.

It is slower than a framebuffer — a full 480x320 refresh at 32 MHz is roughly
70 ms, plus about 15 ms to pack the pixels. For a screen whose data changes
once every five minutes that is irrelevant, and only the rows that changed are
sent anyway.

Supported controllers: ILI9486, ILI9488, ST7735S, ST7789 and ST7796S. These
cover the overwhelming majority of 2.4"-4" panels sold for the Pi. If yours is
a Waveshare-branded board, read the note in the README first — some of those
put a shift register in front of the controller and expect each byte doubled.

Wiring assumed (BCM numbering, all configurable):

    SPI0 MOSI  GPIO10 (pin 19)     DC     GPIO24 (pin 18)
    SPI0 SCLK  GPIO11 (pin 23)     RESET  GPIO25 (pin 22)
    SPI0 CE0   GPIO8  (pin 24)     BL     GPIO18 (pin 12)
"""

from __future__ import annotations

import time

import numpy as np
from PIL import Image

from . import BackendError

# Controller command bytes shared across every chip here.
SWRESET, SLPOUT, INVOFF, INVON = 0x01, 0x11, 0x20, 0x21
DISPOFF, DISPON = 0x28, 0x29
CASET, RASET, RAMWR = 0x2A, 0x2B, 0x2C
MADCTL, COLMOD = 0x36, 0x3A

# MADCTL bits. MY/MX/MV are the row/column/exchange flips that decide which
# corner is the origin; BGR says the panel wires blue first, which most of
# these do — get it wrong and the display works perfectly with red and blue
# swapped, which is a genuinely confusing failure to debug.
MY, MX, MV, BGR = 0x80, 0x40, 0x20, 0x08

ROTATIONS = {
    0: MX | BGR,
    90: MV | BGR,
    180: MY | BGR,
    270: MX | MY | MV | BGR,
}


class Panel:
    """Controller-specific init. Everything after init is the same."""

    def __init__(self, name: str, native: tuple[int, int], init: list[tuple[int, list[int]]],
                 invert: bool = False):
        self.name, self.native, self.init, self.invert = name, native, init, invert


_COMMON_TAIL = [(COLMOD, [0x55])]  # 16 bits per pixel, RGB565

PANELS: dict[str, Panel] = {
    "ili9486": Panel("ILI9486", (320, 480), [
        (0xB0, [0x00]),               # interface mode: SDO not used
        (0xC0, [0x0E, 0x0E]),         # power control 1
        (0xC1, [0x41, 0x00]),         # power control 2
        (0xC5, [0x00, 0x22]),         # VCOM
        (0xB4, [0x02]),               # inversion control: 2-dot
        (0xB6, [0x02, 0x22]),         # display function control
        (0xE0, [0x0F, 0x1F, 0x1C, 0x0C, 0x0F, 0x08, 0x48, 0x98,
                0x37, 0x0A, 0x13, 0x04, 0x11, 0x0D, 0x00]),
        (0xE1, [0x0F, 0x32, 0x2E, 0x0B, 0x0D, 0x05, 0x47, 0x75,
                0x37, 0x06, 0x10, 0x03, 0x24, 0x20, 0x00]),
    ] + _COMMON_TAIL),
    "ili9488": Panel("ILI9488", (320, 480), [
        (0xC0, [0x17, 0x15]),
        (0xC1, [0x41]),
        (0xC5, [0x00, 0x12, 0x80]),
        (0xB0, [0x00]),
        (0xB1, [0xA0]),
        (0xB4, [0x02]),
        (0xB6, [0x02, 0x02]),
        (0xE9, [0x00]),
        (0xF7, [0xA9, 0x51, 0x2C, 0x82]),
    ] + _COMMON_TAIL),
    "st7796": Panel("ST7796S", (320, 480), [
        (0xF0, [0xC3]),               # command set control: unlock
        (0xF0, [0x96]),
        (0xB4, [0x01]),
        (0xB6, [0x80, 0x02, 0x3B]),
        (0xC1, [0x06]),
        (0xC2, [0xA7]),
        (0xC5, [0x18]),
        (0xF0, [0x3C]),               # relock
        (0xF0, [0x69]),
    ] + _COMMON_TAIL),
    "st7789": Panel("ST7789", (240, 320), [
        (0xB2, [0x0C, 0x0C, 0x00, 0x33, 0x33]),
        (0xB7, [0x35]),
        (0xBB, [0x1A]),
        (0xC0, [0x2C]),
        (0xC2, [0x01]),
        (0xC3, [0x0B]),
        (0xC4, [0x20]),
        (0xC6, [0x0F]),
        (0xD0, [0xA4, 0xA1]),
    ] + _COMMON_TAIL, invert=True),
    "st7735": Panel("ST7735S", (128, 160), [
        (0xB1, [0x01, 0x2C, 0x2D]),
        (0xB4, [0x07]),
        (0xC0, [0xA2, 0x02, 0x84]),
        (0xC5, [0x0A, 0x00]),
    ] + _COMMON_TAIL),
}


class SpiBackend:
    name = "spi"

    def __init__(self, cfg):
        try:
            import spidev  # noqa: PLC0415
        except ImportError as e:
            raise BackendError(
                "python3-spidev is not installed. `sudo apt install "
                "python3-spidev`, or re-run install.sh."
            ) from e

        panel = PANELS.get(cfg.panel.lower())
        if panel is None:
            raise BackendError(
                f"unknown panel {cfg.panel!r}. Known: {', '.join(sorted(PANELS))}"
            )
        self.panel = panel

        # cfg.width/height describe the picture we want on the glass; the
        # controller's own frame is `native` and MADCTL rotates between them.
        self.width, self.height = cfg.width, cfg.height
        self._madctl = ROTATIONS.get(cfg.rotate, ROTATIONS[0])
        self._offset = (0, 0)
        self._last: np.ndarray | None = None

        self._gpio = _Gpio(cfg.pin_dc, cfg.pin_reset, cfg.pin_backlight)

        self._spi = spidev.SpiDev()
        try:
            self._spi.open(cfg.spi_bus, cfg.spi_device)
        except FileNotFoundError as e:
            raise BackendError(
                f"/dev/spidev{cfg.spi_bus}.{cfg.spi_device} does not exist. "
                "Enable SPI: add `dtparam=spi=on` to /boot/firmware/config.txt "
                "and reboot (or run `sudo raspi-config nonint do_spi 0`)."
            ) from e
        except PermissionError as e:
            raise BackendError(
                "cannot open the SPI device — add your user to the 'spi' group "
                "(`sudo usermod -aG spi $USER`) and log out and back in."
            ) from e
        self._spi.max_speed_hz = cfg.spi_hz
        self._spi.mode = 0

        self._reset()
        self._init_panel()

    # ------------------------------------------------------------------
    def _cmd(self, cmd: int, data: list[int] | bytes | None = None) -> None:
        self._gpio.dc(0)
        self._spi.writebytes([cmd])
        if data:
            self._gpio.dc(1)
            self._write(bytes(data))

    def _write(self, buf: bytes) -> None:
        # spidev's per-transfer limit defaults to 4096 bytes. A full frame is
        # 307200, so chunk it — and chunk on the smaller of the kernel's limit
        # and our own, because a too-large writebytes2 fails with a bare EMSGSIZE
        # that gives no hint what went wrong.
        step = 4096
        for i in range(0, len(buf), step):
            self._spi.writebytes2(buf[i:i + step])

    def _reset(self) -> None:
        self._gpio.reset(1); time.sleep(0.02)
        self._gpio.reset(0); time.sleep(0.02)
        self._gpio.reset(1); time.sleep(0.15)

    def _init_panel(self) -> None:
        self._cmd(SWRESET); time.sleep(0.15)
        self._cmd(SLPOUT); time.sleep(0.12)
        for cmd, data in self.panel.init:
            self._cmd(cmd, data)
        self._cmd(MADCTL, [self._madctl])
        self._cmd(INVON if self.panel.invert else INVOFF)
        self._cmd(DISPON); time.sleep(0.05)
        self._gpio.backlight(1)

    def _window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        ox, oy = self._offset
        self._cmd(CASET, [(x0 + ox) >> 8, (x0 + ox) & 0xFF,
                          (x1 + ox) >> 8, (x1 + ox) & 0xFF])
        self._cmd(RASET, [(y0 + oy) >> 8, (y0 + oy) & 0xFF,
                          (y1 + oy) >> 8, (y1 + oy) & 0xFF])
        self._cmd(RAMWR)

    # ------------------------------------------------------------------
    def show(self, image: Image.Image) -> None:
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height), Image.LANCZOS)
        arr = np.asarray(image.convert("RGB"), dtype=np.uint16)
        # RGB565, big-endian on the wire — these controllers clock the high
        # byte first.
        packed = (
            ((arr[:, :, 0] & 0xF8) << 8)
            | ((arr[:, :, 1] & 0xFC) << 3)
            | (arr[:, :, 2] >> 3)
        ).astype(">u2")

        # Only push rows that changed. On this display most frames differ in
        # the clock and one digit, so a partial update is typically a tenth of
        # a full one — which is the difference between a screen that redraws
        # invisibly and one that visibly wipes every minute.
        y0, y1 = 0, self.height - 1
        if self._last is not None and self._last.shape == packed.shape:
            changed = np.flatnonzero((self._last != packed).any(axis=1))
            if changed.size == 0:
                return
            y0, y1 = int(changed[0]), int(changed[-1])

        self._window(0, y0, self.width - 1, y1)
        self._write(packed[y0:y1 + 1].tobytes())
        self._last = packed

    def set_backlight(self, on: bool) -> None:
        self._gpio.backlight(1 if on else 0)

    def close(self) -> None:
        try:
            self._cmd(DISPOFF)
            self._gpio.backlight(0)
        except Exception:
            pass
        finally:
            self._gpio.close()
            self._spi.close()


class _Gpio:
    """DC / RESET / backlight.

    Uses gpiozero, whose default pin factory on current Raspberry Pi OS is
    lgpio. RPi.GPIO is deliberately not used: it drives the BCM GPIO registers
    directly and does not work on a Pi 5 at all, which is one of the most
    common reasons a display tutorial written before 2024 fails on this board.
    """

    def __init__(self, dc: int, reset: int, backlight: int):
        try:
            from gpiozero import DigitalOutputDevice  # noqa: PLC0415
        except ImportError as e:
            raise BackendError(
                "gpiozero is not installed. `sudo apt install python3-gpiozero "
                "python3-lgpio`, or re-run install.sh."
            ) from e
        try:
            self._dc = DigitalOutputDevice(dc)
            self._reset = DigitalOutputDevice(reset)
            self._bl = DigitalOutputDevice(backlight) if backlight >= 0 else None
        except Exception as e:
            raise BackendError(
                f"cannot claim GPIO {dc}/{reset}/{backlight}: {e}\n"
                "Another process may hold them — a leftover kernel overlay for "
                "this panel will. Comment out any LCD dtoverlay in "
                "/boot/firmware/config.txt and reboot."
            ) from e

    def dc(self, v: int) -> None:
        self._dc.value = v

    def reset(self, v: int) -> None:
        self._reset.value = v

    def backlight(self, v: int) -> None:
        if self._bl:
            self._bl.value = v

    def close(self) -> None:
        for d in (self._dc, self._reset, self._bl):
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass

"""
Touchscreen input.

SPDX-License-Identifier: AGPL-3.0-only

Reads taps from a Linux input device and nothing else. No calibration, no
coordinate mapping, no dependency on python3-evdev — just the 24-byte
`struct input_event` records the kernel writes to /dev/input/eventN, parsed
with `struct`.

**A tap anywhere on the glass counts.** That is a deliberate choice, not
laziness. Mapping a tap to a specific on-screen button needs the panel's raw
ADC range, which varies between boards and drifts with temperature, so a
hit-region either needs a calibration step or silently stops working at the
edges. On a screen with exactly one control, "anywhere" is both more robust
and easier to hit in the dark. The on-screen button is drawn as an
affordance — so you know the control exists — rather than as the only target.

Resistive panels (ADS7846/XPT2046, the usual companion to a 3.5" SPI TFT)
report a press as EV_KEY/BTN_TOUCH; some capacitive ones only move
ABS_PRESSURE. Both are handled, and a release is required between taps so
holding a finger down does not fire repeatedly.

Two gestures, dispatched on RELEASE so the two can be told apart:

    tap         switch between the full and minimal views
    long press  hide the display and hand the panel back

Dispatching on release costs a few milliseconds of latency on a tap and buys
the ability to distinguish them at all without firing the wrong one first.
"""

from __future__ import annotations

import os
import re
import struct
import threading
import time
from pathlib import Path

# struct input_event: struct timeval (2 x long) + __u16 type + __u16 code
# + __s32 value. Native sizes, so this is 24 bytes on a 64-bit kernel and 16
# on a 32-bit one without any special-casing here.
EVENT = struct.Struct("llHHi")

EV_KEY, EV_ABS = 0x01, 0x03
BTN_TOUCH = 0x14A
ABS_PRESSURE = 0x18

# Names of touch controllers seen on Pi panels. Matched case-insensitively as
# substrings, so "ADS7846 Touchscreen" and "generic ft5x06 (79)" both hit.
TOUCH_HINTS = (
    "touch", "ads7846", "xpt2046", "ft5406", "ft5x06", "edt-ft5x06",
    "goodix", "stmpe", "ili210", "raspberrypi-ts",
)

DEBOUNCE_SECONDS = 0.35
LONG_PRESS_SECONDS = 1.5
# If a release never arrives -- a resistive panel losing contact mid-press can
# do this -- a held finger would otherwise wedge the state machine forever.
STUCK_PRESS_SECONDS = 8.0


def list_input_devices() -> list[dict]:
    """Every input device the kernel knows about, from /proc/bus/input/devices.

    Parsed rather than probed with ioctls so this stays stdlib-only and safe
    to call from `probe` without opening anything.
    """
    try:
        blocks = Path("/proc/bus/input/devices").read_text().split("\n\n")
    except OSError:
        return []

    out = []
    for block in blocks:
        if not block.strip():
            continue
        name = re.search(r'N: Name="([^"]*)"', block)
        handlers = re.search(r"H: Handlers=(.*)", block)
        if not name or not handlers:
            continue
        events = [h for h in handlers.group(1).split() if h.startswith("event")]
        if not events:
            continue
        label = name.group(1)
        out.append({
            "name": label,
            "device": f"/dev/input/{events[0]}",
            "looks_like_touch": any(h in label.lower() for h in TOUCH_HINTS),
        })
    return out


def find_touch_device() -> str | None:
    """The most likely touchscreen, or None."""
    for dev in list_input_devices():
        if dev["looks_like_touch"] and os.path.exists(dev["device"]):
            return dev["device"]
    return None


class TouchWatcher(threading.Thread):
    """Calls `on_tap()` once per finger-down. Never raises into the caller.

    A touchscreen that stops working must degrade to "the screen no longer
    responds to taps", never to "the glucose display exited".
    """

    daemon = True

    def __init__(self, path: str | None, on_tap, on_long_press=None):
        super().__init__(name="sentinelle-touch")
        self.path = path or find_touch_device()
        self.on_tap = on_tap
        self.on_long_press = on_long_press
        self.error: str | None = None
        # NOT self._stop: threading.Thread already has a private _stop()
        # method that join() calls internally, and shadowing it with an Event
        # makes join() raise "'Event' object is not callable" -- from inside
        # the standard library, on a line you did not write.
        self._stopping = threading.Event()
        self._down = False
        self._down_at = 0.0
        self._last_tap = 0.0

    def stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------------
    def _press(self) -> None:
        if not self._down:
            self._down = True
            self._down_at = time.monotonic()

    def _release(self) -> None:
        if not self._down:
            return
        held = time.monotonic() - self._down_at
        self._down = False
        # A press that never released and then did, hours later, is not a
        # gesture -- it is a panel glitch. Drop it rather than hiding the
        # display because of electrical noise.
        if held > STUCK_PRESS_SECONDS:
            return
        long_press = held >= LONG_PRESS_SECONDS and self.on_long_press is not None
        self._fire(self.on_long_press if long_press else self.on_tap,
                   debounce=not long_press)

    def _fire(self, handler, debounce: bool = True) -> None:
        now = time.monotonic()
        if debounce and now - self._last_tap < DEBOUNCE_SECONDS:
            return
        self._last_tap = now
        try:
            handler()
        except Exception as e:            # a bad handler must not kill input
            self.error = f"tap handler failed: {e}"

    def _handle(self, ev_type: int, code: int, value: int) -> None:
        if ev_type == EV_KEY and code == BTN_TOUCH:
            self._press() if value == 1 else self._release()
        elif ev_type == EV_ABS and code == ABS_PRESSURE:
            # Panels that report pressure instead of a button. Release is
            # pressure returning to zero.
            self._press() if value > 0 else self._release()

    def run(self) -> None:
        if not self.path:
            self.error = "no touchscreen found in /proc/bus/input/devices"
            return
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except PermissionError:
            self.error = (
                f"cannot read {self.path} — add your user to the 'input' group "
                "(`sudo usermod -aG input $USER`) and reboot"
            )
            return
        except OSError as e:
            self.error = f"cannot open {self.path}: {e}"
            return

        try:
            import select                                    # noqa: PLC0415
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            buf = b""
            while not self._stopping.is_set():
                if not poll.poll(500):                       # ms; lets stop() land
                    continue
                try:
                    chunk = os.read(fd, EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError as e:
                    self.error = f"read failed on {self.path}: {e}"
                    return
                if not chunk:
                    continue
                buf += chunk
                # The kernel writes whole events, but a short read can still
                # split one across two reads; carry the remainder.
                while len(buf) >= EVENT.size:
                    record, buf = buf[: EVENT.size], buf[EVENT.size :]
                    _, _, ev_type, code, value = EVENT.unpack(record)
                    self._handle(ev_type, code, value)
        finally:
            os.close(fd)

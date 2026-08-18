"""
Window-backend tests that need a real display server and a real pointer.

SPDX-License-Identifier: AGPL-3.0-only

These exist because of one specific bug that unit tests cannot catch.

The backend originally bound <Button-1> on BOTH the toplevel and the image
label. A real click on the label fires the label's binding and then propagates
to the toplevel's, so every press arrived twice. Every control on the bar
toggles, so a doubled press cancelled itself out: UNITS went
mg/dL -> mmol/L -> mg/dL and looked completely dead, and NIGHT silently
skipped a step.

The reason it shipped: `event_generate("<Button-1>")` on the root delivers
straight to the root and fires ONCE. A synthetic-event test passes with the
bug present. Only a real pointer reproduces it — hence xdotool.

Skipped automatically where there is no display, no tkinter, or no xdotool,
so `pytest` on a headless box still runs the rest of the suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="needs a display server",
)

tk = pytest.importorskip("tkinter", reason="needs python3-tk")
pytest.importorskip("PIL.ImageTk", reason="needs python3-pil.imagetk")

if not shutil.which("xdotool"):
    pytest.skip("needs xdotool to synthesise a real pointer click",
                allow_module_level=True)


def _click(x: int, y: int) -> None:
    subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], check=False)


def _prime(be) -> None:
    """Draw one frame and let the window map.

    Without this the label has no image, the window may not be mapped yet, and
    xdotool's click lands on the root window instead — the test then reports
    zero handler calls and looks like the very bug it is guarding against.
    """
    from PIL import Image
    be.show(Image.new("RGB", (be.width, be.height), (0, 0, 0)))
    be._root.update()
    be._root.update_idletasks()
    time.sleep(0.4)


def test_one_real_click_fires_the_handler_exactly_once():
    """The regression guard. If someone re-adds a second binding, this fails."""
    from sentinelle_display.backends.window import WindowBackend
    from sentinelle_display.config import Config

    calls: list[tuple[float, float]] = []
    be = WindowBackend(Config(width=480, height=320),
                       on_click=lambda x, y: calls.append((x, y)))
    try:
        _prime(be)
        be._root.after(300, lambda: _click(240, 300))
        be._root.after(1600, be._root.quit)
        be._root.mainloop()
    finally:
        be.close()

    assert len(calls) == 1, (
        f"one physical click produced {len(calls)} handler calls — a doubled "
        "press cancels out every toggle on the control bar"
    )


def test_a_click_lands_on_the_button_under_it():
    """Drawing the bar from button_layout and hit-testing it from button_layout
    is what keeps these aligned. This proves it end to end, with a real
    pointer rather than a coordinate we made up."""
    from sentinelle_display import render as R
    from sentinelle_display.backends.window import WindowBackend
    from sentinelle_display.config import Config

    cfg = Config(width=480, height=320)
    state = {"view": "full", "night_mode": "auto", "units": "mgdl"}
    hits: list[str | None] = []

    def on_click(x, y):
        hits.append(R.hit_test(R.button_layout(cfg, state, be.width, be.height), x, y))

    be = WindowBackend(cfg, on_click=on_click)
    try:
        _prime(be)
        buttons = R.button_layout(cfg, state, be.width, be.height)
        delay = 300
        for b in buttons:
            cx, cy = (b.x0 + b.x1) // 2, (b.y0 + b.y1) // 2
            # 400ms apart: comfortably clear of the backend's 120ms
            # duplicate-delivery guard.
            be._root.after(delay, lambda x=cx, y=cy: _click(x, y))
            delay += 400
        be._root.after(delay + 500, be._root.quit)
        be._root.mainloop()
    finally:
        be.close()

    assert hits == [b.key for b in buttons], (
        f"clicks landed on {hits}, expected {[b.key for b in buttons]}"
    )

"""
Desktop window backend.

SPDX-License-Identifier: AGPL-3.0-only

Why this exists, and why it is the right default on Raspberry Pi OS Desktop:

The `fb` backend writes pixels straight into /dev/fb0. That is correct on a
Lite image where nothing else touches the framebuffer. On a Desktop image it
is not: the desktop owns that framebuffer too, and the two fight. The symptom
is specific and confusing -- the dashboard looks fine until you touch the
screen, at which point the pointer event makes the desktop repaint the damaged
region and the dashboard is visibly eaten away, then partly restored on the
next redraw. Nothing is broken; two programs are drawing the same pixels.

A real window fixes that by not competing. It also buys three things the raw
framebuffer cannot give:

  * Real click coordinates, so the control bar can have actual hit regions
    instead of whole-screen gestures. No touchscreen calibration needed --
    the display server has already done it.
  * A real minimise, to the taskbar, rather than exiting the process.
  * Launching from the menu like any other application.

The cost is roughly 40MB over the framebuffer path. On a board already running
a desktop that is noise; on Lite, use `fb` and keep the 35MB footprint.

Tkinter is the toolkit because it ships with Python (python3-tk on Debian),
draws a PIL image without a second rendering stack, and does not drag in Qt or
GTK. It is not pretty, but nothing here uses a widget: the whole window is one
label containing an image this program drew itself.
"""

from __future__ import annotations

from PIL import Image

from . import BackendError


class WindowBackend:
    """A fullscreen (or fixed-size) window showing rendered frames.

    Unlike the other backends this one OWNS THE EVENT LOOP: Tk insists on
    running its mainloop on the main thread, so the caller hands control over
    via run_loop() instead of driving its own while loop. `owns_event_loop`
    is how cli.py knows which shape to use.
    """

    name = "window"
    owns_event_loop = True

    def __init__(self, cfg, on_click=None):
        try:
            import tkinter as tk                                # noqa: PLC0415
            from PIL import ImageTk                              # noqa: PLC0415
        except ImportError as e:
            raise BackendError(
                "the window backend needs Tkinter and Pillow's ImageTk.\n"
                "  sudo apt install python3-tk python3-pil.imagetk\n"
                "...or re-run install.sh. On a machine with no desktop, use "
                "backend \"fb\" instead."
            ) from e

        self._tk = tk
        self._ImageTk = ImageTk
        self.on_click = on_click
        self._cfg = cfg

        try:
            self._root = tk.Tk()
        except tk.TclError as e:
            # The usual cause is running under systemd, where DISPLAY and
            # WAYLAND_DISPLAY are not set. Say that, rather than leaving a
            # bare "no display name and no $DISPLAY environment variable".
            raise BackendError(
                f"cannot open a window: {e}\n"
                "This backend needs a desktop session. Under systemd there is "
                "no DISPLAY, which is why install.sh starts it from the "
                "desktop's autostart instead of as a system service.\n"
                "For a headless panel use backend \"fb\" or \"spi\"."
            ) from e

        self._root.title("Sentinelle Glucose Display")
        self._root.configure(bg="black")
        # No cursor: this is a touchscreen, and a mouse pointer parked in the
        # middle of a glucose reading is just a smudge you cannot wipe off.
        try:
            self._root.config(cursor="none")
        except Exception:
            pass

        self.width, self.height = self._go_fullscreen(cfg)

        self._label = tk.Label(self._root, bd=0, highlightthickness=0, bg="black")
        self._label.pack(fill="both", expand=True)
        self._photo = None            # a live reference; Tk will not keep one
        self._last_click = 0.0

        # Bound on the TOPLEVEL ONLY, and this is load bearing.
        #
        # Binding both the root and the label looks harmless and is not: a real
        # click on the label fires the label's binding AND then propagates to
        # the toplevel's, so every press is delivered twice. Each button here
        # toggles, so a double delivery cancels itself out -- UNITS went
        # mg/dL -> mmol/L -> mg/dL and appeared completely dead, while NIGHT
        # silently skipped a step.
        #
        # Synthetic `event_generate` on the root does NOT reproduce this: it
        # delivers straight to the root and fires once. It only shows up under
        # a real pointer, which is why this shipped.
        self._root.bind("<Button-1>", self._clicked)
        # Escape and q are an escape hatch for when the window manager has no
        # decorations and a fullscreen app would otherwise be inescapable
        # without SSH.
        self._root.bind("<Escape>", lambda _e: self.minimize())
        self._root.bind("<q>", lambda _e: self._root.quit())
        self._root.protocol("WM_DELETE_WINDOW", self._root.quit)

    # ------------------------------------------------------------------
    def _go_fullscreen(self, cfg) -> tuple[int, int]:
        """Fullscreen if the screen is panel-sized, otherwise a fixed window.

        On the 3.5" panel fullscreen is what you want. On a developer's laptop
        it emphatically is not -- a fullscreen glucose display you then have
        to fight to close is a bad first impression, so there we open a window
        the size of the real panel instead.
        """
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        if sw <= 1024 and sh <= 768:
            self._root.attributes("-fullscreen", True)
            self._root.update_idletasks()
            return sw, sh
        w, h = cfg.width, cfg.height
        self._root.geometry(f"{w}x{h}")
        self._root.resizable(False, False)
        return w, h

    def _clicked(self, event) -> None:
        # Belt and braces against a window manager or toolkit delivering the
        # same press twice. 120ms is far below a deliberate double-press on a
        # touchscreen and far above any duplicate-delivery interval.
        import time as _time
        now = _time.monotonic()
        if now - self._last_click < 0.12:
            return
        self._last_click = now
        if self.on_click:
            try:
                self.on_click(event.x, event.y)
            except Exception:
                pass          # a bad handler must not kill the window

    # ------------------------------------------------------------------
    def show(self, image: Image.Image) -> None:
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height), Image.LANCZOS)
        # The PhotoImage must be kept alive on the instance. If it is only a
        # local, Python collects it, Tk is left holding a freed handle and the
        # window goes blank or grey -- the single most common Tkinter+PIL bug.
        self._photo = self._ImageTk.PhotoImage(image)
        self._label.configure(image=self._photo)

    def minimize(self) -> None:
        """To the taskbar, not out of existence."""
        try:
            self._root.attributes("-fullscreen", False)
            self._root.iconify()
        except Exception:
            pass

    def restore(self) -> None:
        try:
            self._root.deiconify()
            if self._root.winfo_screenwidth() <= 1024:
                self._root.attributes("-fullscreen", True)
        except Exception:
            pass

    def set_backlight(self, on: bool) -> None:
        pass

    def close(self) -> None:
        try:
            self._root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def run_loop(self, tick, interval_ms: int = 1000) -> None:
        """Runs Tk's mainloop, calling `tick()` every interval_ms.

        `tick` does whatever the caller's while-loop body did: look at the
        newest snapshot, render, and call show(). Exceptions from it are
        reported and swallowed -- one bad frame must not take the window down,
        for the same reason a bad frame does not take the framebuffer loop
        down.
        """
        def pump():
            try:
                tick()
            except Exception as e:
                print(f"render/display error: {e}", flush=True)
            self._root.after(interval_ms, pump)

        self._root.after(1, pump)
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass

    def wake(self) -> None:
        """Ask for a redraw before the next tick — used after a click, so the
        button you pressed changes immediately instead of up to a second
        later, which on a touchscreen reads as a missed press."""
        try:
            self._root.after_idle(lambda: None)
        except Exception:
            pass


def display_available() -> bool:
    """True when there is a desktop session to open a window on."""
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

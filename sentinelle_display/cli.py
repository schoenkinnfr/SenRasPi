"""
Command line: pair, run, probe, preview, config.

SPDX-License-Identifier: AGPL-3.0-only

    sentinelle-display pair          type the 10-character code from Settings
    sentinelle-display run           the display loop (what systemd starts)
    sentinelle-display probe         what hardware is actually here
    sentinelle-display preview       render sample screens to PNG
    sentinelle-display config        show / change settings

`pair` is the only interactive command and the only one that ever writes a
credential. Everything else works from the config file it produces.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from typing import Any

from pathlib import Path

from . import config as configmod
from .client import PairError, Poller, format_code, normalise_code, redeem_code

EXIT_CONFIG = 2
EXIT_HARDWARE = 3
# Long-press-to-hide exits with this. The systemd unit lists it under
# RestartPreventExitStatus, so it is the one exit code that does NOT get
# restarted five seconds later -- which is what makes "hide" mean hidden
# rather than "flicker and come back".
EXIT_HIDDEN = 64


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ─────────────────────────────────────────────────────────────────────────────


def cmd_pair(args) -> int:
    cfg = configmod.load()

    base = args.server or cfg.base_url
    if not base:
        _say("Which Sentinelle server? (e.g. https://sentinelle.example.com)")
        base = input("  Server URL: ").strip()
    base = base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    _say()
    _say(f"Server: {base}")
    _say("In the web app, open Settings and generate a device code under")
    _say("\"Raspberry Pi display\". It looks like ABCDE-FGHJK and expires")
    _say("15 minutes after you create it.")
    _say()

    code = args.code
    attempts = 0
    while True:
        if not code:
            try:
                code = input("  Device code: ").strip()
            except (EOFError, KeyboardInterrupt):
                _say()
                return 1
        try:
            normalise_code(code)
            break
        except PairError as e:
            _err(f"  {e}")
            attempts += 1
            code = None
            if attempts >= 5 and not args.code:
                _err("  Giving up. Run `sentinelle-display pair` again when ready.")
                return 1
            if args.code:
                return 1

    label = args.label or _default_label()
    _say(f"  Pairing as \"{label}\"…")
    try:
        result = redeem_code(base, code, label)
    except PairError as e:
        _err(f"  Pairing failed: {e}")
        _err("  Codes are single-use and expire after 15 minutes — generate a")
        _err("  fresh one in Settings and try again.")
        return 1

    cfg.base_url = result.get("base_url") or base
    cfg.token = result["token"]
    cfg.label = result.get("label") or label
    configmod.save(cfg)

    _say()
    _say(f"  Paired. Credentials written to {configmod.CONFIG_PATH} (mode 600).")
    _say("  This display now reads glucose data and nothing else. It cannot")
    _say("  change anything on your account, and you can revoke it from the")
    _say("  same Settings page at any time.")
    _say()
    _say("  Start it now:   sudo systemctl start sentinelle-display")
    _say("  Watch the logs: journalctl -u sentinelle-display -f")
    return 0


def _default_label() -> str:
    import socket
    try:
        return f"Pi — {socket.gethostname()}"
    except OSError:
        return "Raspberry Pi display"


# ─────────────────────────────────────────────────────────────────────────────


def cmd_run(args) -> int:
    from PIL import Image  # noqa: F401  (import here so `probe` works without it)

    from . import render as renderer
    from .backends import BackendError, open_backend

    cfg = configmod.load()
    _apply_overrides(cfg, args)

    problems = cfg.validate()
    if problems:
        for p in problems:
            _err(f"config: {p}")
        return EXIT_CONFIG

    # Live, runtime-mutable state. Separate from cfg because the control bar
    # changes what is on screen NOW; `config --set` is what changes the file.
    state = {
        "view": cfg.view if cfg.view in renderer.VIEWS else "full",
        # Always start on the glucose dashboard. The review page is somewhere
        # you go on purpose; a display that reboots at 3am and comes back
        # showing yesterday's advice instead of the number would be a bug.
        "page": "dashboard",
        "night_mode": cfg.night_mode,
        "units": cfg.units,
    }
    ui = {"bar_until": 0.0, "wake_until": 0.0, "quit": False,
          "hidden": False, "backend": None, "other_until": 0.0}
    BAR_SECONDS = 8.0

    def on_click(x: float, y: float) -> None:
        """A real coordinate from a real display server. No calibration."""
        now = time.time()
        backend = ui["backend"]
        bar_showing = now < ui["bar_until"]

        # First touch summons the bar rather than pressing whatever happens to
        # be under your finger. Otherwise reaching out to see the screen would
        # fire a button you never meant to press.
        if not bar_showing:
            ui["bar_until"] = now + BAR_SECONDS
            if renderer.is_night(cfg, now, state["night_mode"]):
                ui["wake_until"] = now + max(cfg.night_wake_seconds, BAR_SECONDS)
            _redraw()
            return

        buttons = renderer.button_layout(cfg, state, backend.width, backend.height)
        key = renderer.hit_test(buttons, x, y)
        if key == "other":
            state["page"] = "dashboard" if state["page"] == "other" else "other"
            ui["other_until"] = (
                now + cfg.other_seconds
                if state["page"] == "other" and cfg.other_seconds else 0.0
            )
            # The review page overrides the night wash, so the night wake has
            # to be extended too -- otherwise the page appears and the wash
            # comes back over it thirty seconds later.
            if state["page"] == "other" and renderer.is_night(cfg, now, state["night_mode"]):
                ui["wake_until"] = now + max(cfg.other_seconds or 180, cfg.night_wake_seconds)
            elif state["page"] == "dashboard":
                ui["wake_until"] = 0.0
        elif key == "refresh":
            if review_poller is not None:
                review_poller.refresh_now()
                # Four Opus calls take a minute or two. The page says
                # "rewriting…" and picks the answer up on a later poll.
                ui["other_until"] = now + max(cfg.other_seconds, 180) if cfg.other_seconds else 0.0
            else:
                _err("refresh ignored — the daily review is off (review=off)")
        elif key == "view":
            state["view"] = "minimal" if state["view"] == "full" else "full"
        elif key == "night":
            order = ["auto", "on", "off"]
            state["night_mode"] = order[(order.index(state["night_mode"]) + 1) % 3]
            ui["wake_until"] = 0.0          # an explicit choice ends any wake
        elif key == "units":
            state["units"] = "mmol" if state["units"] == "mgdl" else "mgdl"
            cfg.units = state["units"]
        elif key == "minimize":
            if hasattr(backend, "minimize"):
                backend.minimize()          # a real window: to the taskbar
                ui["bar_until"] = 0.0
            else:
                # No window manager (fb/spi on a headless panel), so the only
                # way to hand the screen back is to exit. It must exit with
                # EXIT_HIDDEN: the unit lists that under
                # RestartPreventExitStatus, and any other code would have
                # systemd restart it five seconds later, making Minimize look
                # like a flicker.
                ui["hidden"] = True
                ui["quit"] = True
            _persist()
            return
        else:
            # A press in the dashboard area while the bar is up just keeps it up.
            ui["bar_until"] = now + BAR_SECONDS
            _redraw()
            return

        ui["bar_until"] = now + BAR_SECONDS
        _persist()
        _redraw()

    def _persist() -> None:
        """Remember the choices, so a restart does not undo them. Best effort:
        a read-only home must not stop the display working."""
        cfg.view, cfg.night_mode, cfg.units = state["view"], state["night_mode"], state["units"]
        try:
            configmod.save(cfg)
        except OSError as e:
            _err(f"could not save settings: {e}")

    def _redraw() -> None:
        backend = ui["backend"]
        if backend is None:
            return
        now = time.time()
        try:
            backend.show(renderer.render(
                cfg, poller.get(), now=now, view=state["view"], state=state,
                page=state["page"],
                review=review_poller.get() if review_poller else None,
                bar=now < ui["bar_until"],
                hint=clickable and now >= ui["bar_until"],
                force_day=now < ui["wake_until"],
                pollen=pollen_poller.get() if pollen_poller else None,
            ))
        except Exception as e:
            _err(f"render/display error: {e}")

    try:
        backend = open_backend(cfg, on_click=on_click)
    except BackendError as e:
        _err(str(e))
        return EXIT_HARDWARE
    ui["backend"] = backend

    # The backend knows the real geometry; trust it over the config so a wrong
    # width in a hand-edited file cannot letterbox the dashboard.
    if backend.name in ("fb", "window"):
        cfg.width, cfg.height = backend.width, backend.height

    clickable = backend.name == "window"
    _say(f"backend={backend.name} {backend.width}x{backend.height} "
         f"rotate={cfg.rotate} server={cfg.base_url}")

    poller = Poller(cfg)
    watcher = None
    last_error: str | None = None

    # Pollen is optional and entirely separate from glucose: it has its own
    # thread, its own much slower interval, and its own failure mode. A pollen
    # outage must never affect the glucose reading, so nothing here is allowed
    # to raise into the render loop.
    # The daily review, same isolation rule as pollen: its own thread, its own
    # much slower interval, and no path by which a failure can reach the
    # glucose render loop.
    review_poller = None
    if cfg.review == "on":
        from .client import ReviewPoller                          # noqa: PLC0415
        review_poller = ReviewPoller(cfg)
        review_poller.start()
        _say(f"review=on (every {cfg.review_minutes} min, OTHER button)")

    pollen_poller = None
    if cfg.pollen == "on":
        from .pollen import PollenPoller                        # noqa: PLC0415
        pollen_poller = PollenPoller(cfg)
        pollen_poller.start()
        _say(f"pollen={cfg.pollen_label} ({cfg.pollen_lat}, {cfg.pollen_lon})"
             f"{' via ' + cfg.allergy_url if cfg.allergy_url else ''}")

    # A touchscreen read from /dev/input is only needed when there is no
    # display server to deliver clicks. Under `window` the toolkit already
    # has them, correctly mapped, and opening the raw device as well would
    # double every press.
    if not clickable and cfg.touch != "off":
        from .touch import TouchWatcher                          # noqa: PLC0415

        watcher = TouchWatcher(
            None if cfg.touch == "auto" else cfg.touch,
            lambda: on_click(-1, -1),                            # summon the bar
            lambda: on_click(-1, -1),
        )
        watcher.start()
        time.sleep(0.2)
        if watcher.error:
            _err(f"touch: {watcher.error}")
        else:
            _say(f"touch={watcher.path}")

    if clickable:
        ui["bar_until"] = time.time() + BAR_SECONDS   # show the controls once at startup

    def tick() -> None:
        nonlocal last_error
        # An always-on glucose display should not sit on a page of prose. Hand
        # the screen back after a while unless other_seconds is 0.
        if state["page"] == "other" and ui["other_until"] and time.time() > ui["other_until"]:
            state["page"] = "dashboard"
            ui["other_until"] = 0.0
            ui["wake_until"] = 0.0
        snap = poller.get()
        if snap.last_error and snap.last_error != last_error:
            _err(snap.last_error)            # journald gets one line per change,
            last_error = snap.last_error     # not one per poll
        elif snap.ok and last_error:
            _say("recovered")
            last_error = None
        _redraw()

    poller.start()
    try:
        if getattr(backend, "owns_event_loop", False):
            # Tk insists on its mainloop being on the main thread, so it drives
            # us. One-second ticks: the bar's timeout and the "N min ago"
            # counter both need finer resolution than the 60s poll.
            backend.run_loop(tick, interval_ms=1000)
        else:
            wake = threading.Event()

            def handle(_signum, _frame):
                ui["quit"] = True
                poller.stop()
                wake.set()

            signal.signal(signal.SIGTERM, handle)
            signal.signal(signal.SIGINT, handle)
            while not ui["quit"]:
                tick()
                wake.wait(1)
                wake.clear()
    finally:
        poller.stop()
        if review_poller:
            review_poller.stop()
        if pollen_poller:
            pollen_poller.stop()
        if watcher:
            watcher.stop()
        try:
            backend.close()
        except Exception:
            pass
    if ui["hidden"]:
        _say("hidden — bring it back with: sentinelle-display show")
        return EXIT_HIDDEN
    return 0


def _apply_overrides(cfg, args) -> None:
    for name in ("units", "low", "high", "hours", "rotate", "backend",
                 "width", "height", "panel", "night", "palette", "view", "touch",
                 "review"):
        v = getattr(args, name, None)
        if v is not None:
            setattr(cfg, name, v)


# ─────────────────────────────────────────────────────────────────────────────


def cmd_probe(_args) -> int:
    """What is actually attached. The first thing to run when nothing works."""
    import shutil
    from pathlib import Path

    _say("Sentinelle Pi display — hardware probe")
    _say("=" * 44)

    model = Path("/proc/device-tree/model")
    _say(f"board          {model.read_text().strip(chr(0)) if model.exists() else 'unknown'}")

    try:
        import platform
        _say(f"kernel         {platform.release()}")
    except Exception:
        pass

    _say()
    _say("SPI devices")
    spidevs = sorted(Path("/dev").glob("spidev*"))
    if spidevs:
        for d in spidevs:
            _say(f"  {d}")
    else:
        _say("  none — add `dtparam=spi=on` to /boot/firmware/config.txt and reboot")

    _say()
    _say("Framebuffers")
    try:
        from .backends.fb import enumerate_framebuffers, find_framebuffer
        fbs = enumerate_framebuffers()
        if fbs:
            chosen = find_framebuffer()
            for fb in fbs:
                mark = "  <- would use" if chosen and fb["device"] == chosen["device"] else ""
                _say(f"  {fb['device']}  {fb['width']}x{fb['height']}  "
                     f"{fb['bpp']}bpp  {fb['name']}{mark}")
        else:
            _say("  none — no kernel driver has claimed a panel.")
            _say("  That is normal on a Pi 5 with an SPI TFT. Use backend \"spi\".")
    except Exception as e:
        _say(f"  could not enumerate: {e}")

    _say()
    _say("Desktop session")
    import os as _os
    disp = _os.environ.get("DISPLAY") or _os.environ.get("WAYLAND_DISPLAY")
    if disp:
        _say(f"  present ({disp}) — the window backend will be used")
        _say("  clicks come from the display server, already calibrated")
    else:
        _say("  none in this shell.")
        _say("  Note: probe over SSH has no DISPLAY even when the Pi has a")
        _say("  desktop. What matters is the session the display runs in.")
    try:
        import tkinter                                        # noqa: F401
        from PIL import ImageTk                               # noqa: F401
        _say("  tkinter + ImageTk  ok")
    except ImportError:
        _say("  tkinter + ImageTk  MISSING (sudo apt install python3-tk python3-pil.imagetk)")

    _say()
    _say("Touchscreen")
    try:
        from .touch import find_touch_device, list_input_devices
        devs = list_input_devices()
        if devs:
            chosen = find_touch_device()
            for d in devs:
                mark = "  <- would use" if d["device"] == chosen else ""
                _say(f"  {d['device']:<18} {d['name']}{mark}")
            if not chosen:
                _say("  no device looks like a touchscreen; set touch=/dev/input/eventN")
        else:
            _say("  no input devices at all")
    except Exception as e:
        _say(f"  could not enumerate: {e}")

    _say()
    _say("Backlight")
    bls = sorted(Path("/sys/class/backlight").glob("*"))
    _say("  " + (", ".join(p.name for p in bls) if bls else
                 "none (panel backlight is probably on a GPIO pin)"))

    _say()
    _say("Python modules")
    for mod in ("PIL", "numpy", "spidev", "gpiozero", "lgpio"):
        try:
            __import__(mod)
            _say(f"  {mod:<10} ok")
        except ImportError:
            _say(f"  {mod:<10} MISSING")

    _say()
    cfg = configmod.load()
    _say("Config")
    _say(f"  file         {configmod.CONFIG_PATH} "
         f"({'present' if configmod.CONFIG_PATH.exists() else 'not created yet'})")
    _say(f"  server       {cfg.base_url or '(unpaired)'}")
    _say(f"  token        {'set' if cfg.token else 'MISSING — run pair'}")
    _say(f"  backend      {cfg.backend}")
    _say(f"  geometry     {cfg.width}x{cfg.height} rotate {cfg.rotate}")
    _say(f"  touch        {cfg.touch}")
    _say(f"  view         {cfg.view}")
    _say(f"  review       {cfg.review}"
         + (f" (every {cfg.review_minutes} min)" if cfg.review == "on" else ""))
    _say(f"  night        {cfg.night} (mode: {cfg.night_mode})")

    if cfg.pollen == "on":
        _say()
        _say("Pollen")
        _say(f"  location     {cfg.pollen_label} ({cfg.pollen_lat}, {cfg.pollen_lon})")
        _say(f"  source       {cfg.allergy_url or 'open-meteo air quality (CAMS)'}")
        try:
            from .pollen import PollenPoller
            pp = PollenPoller(cfg)
            pp._fetch_once()
            r = pp.get()
            if r.species:
                for name, value, band in r.species[:3]:
                    _say(f"  {name:<12} {value:>7.1f} grains/m3   {band}")
            elif r.last_error:
                _say(f"  {r.last_error}")
            else:
                _say("  nothing reported — out of season, or outside Europe "
                     "(CAMS has no pollen over North America)")
        except Exception as e:
            _say(f"  could not fetch: {e}")

    if cfg.token:
        _say()
        _say("Server reachability")
        poller = Poller(cfg)
        poller._fetch_once()
        snap = poller.get()
        _say(f"  {'ok — got a reading' if snap.ok else snap.last_error}")

    if shutil.which("raspi-config") is None:
        _say()
        _say("note: raspi-config not found — this may not be Raspberry Pi OS")
    return 0


# ─────────────────────────────────────────────────────────────────────────────


def cmd_preview(args) -> int:
    from pathlib import Path

    from . import render as renderer

    cfg = configmod.load()
    _apply_overrides(cfg, args)
    cfg.base_url = cfg.base_url or "https://sentinelle.example.com"

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Fixed mid-afternoon timestamp so night mode does not surprise whoever
    # runs this at 11pm and thinks the layout broke. The SAME value goes to
    # demo_snapshot, so the fake readings land inside the window this fake
    # clock implies -- otherwise every preview run before 14:47 local draws
    # "no readings in this window".
    import datetime
    when = datetime.datetime.now().replace(hour=14, minute=47, second=0).timestamp()

    for kind in ("in_range", "low", "high", "stale"):
        img = renderer.render(cfg, renderer.demo_snapshot(kind, now=when), now=when)
        path = out / f"{kind}.png"
        img.save(path)
        _say(f"  {path}  {img.size[0]}x{img.size[1]}")

    night_cfg = configmod.load()
    _apply_overrides(night_cfg, args)
    night_cfg.night = "0-23"
    img = renderer.render(night_cfg, renderer.demo_snapshot("in_range", now=when), now=when)
    img.save(out / "night.png")
    _say(f"  {out / 'night.png'}  night mode")

    # The OTHER page, with the bar up -- that is how you actually see it, and
    # the bar is what proves REFRESH and Back are reachable at this panel size.
    other_state = {"view": "full", "page": "other", "night_mode": "auto", "units": cfg.units}
    img = renderer.render(
        cfg, renderer.demo_snapshot("in_range", now=when), now=when,
        state=other_state, page="other", review=renderer.demo_review(), bar=True,
    )
    img.save(out / "other.png")
    _say(f"  {out / 'other.png'}  the OTHER page (daily review + joke)")
    return 0


def cmd_config(args) -> int:
    cfg = configmod.load()
    if args.set:
        import dataclasses
        valid = {f.name for f in dataclasses.fields(cfg) if not f.name.startswith("_")}
        for pair in args.set:
            if "=" not in pair:
                _err(f"expected key=value, got {pair!r}")
                return EXIT_CONFIG
            key, value = pair.split("=", 1)
            key = key.strip()
            if key not in valid:
                _err(f"unknown setting {key!r}. Known: {', '.join(sorted(valid))}")
                return EXIT_CONFIG
            if key == "token":
                _err("refusing to set the token by hand — use `pair`")
                return EXIT_CONFIG
            current = getattr(cfg, key)
            try:
                setattr(cfg, key, type(current)(value) if not isinstance(current, bool)
                        else value.lower() in ("1", "true", "yes"))
            except ValueError:
                _err(f"{key} expects a {type(current).__name__}, got {value!r}")
                return EXIT_CONFIG
        problems = cfg.validate()
        # A missing token is expected before pairing; other problems are not.
        problems = [p for p in problems if "pair" not in p]
        if problems:
            for p in problems:
                _err(f"config: {p}")
            return EXIT_CONFIG
        configmod.save(cfg)
        _say(f"Saved to {configmod.CONFIG_PATH}")

    import dataclasses
    _say()
    for f in dataclasses.fields(cfg):
        if f.name.startswith("_"):
            continue
        v = getattr(cfg, f.name)
        # The token is the one thing this program will not print. Anyone who
        # can read it can watch the owner's glucose from anywhere.
        if f.name == "token":
            v = f"set ({len(v)} chars)" if v else "not set"
        _say(f"  {f.name:<16} {v}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Service control. Thin wrappers so neither you nor a desktop launcher has to
# remember systemctl syntax, and so the .desktop entry has one stable command
# to call.


SERVICE = "sentinelle-display"


# There are two ways this program gets started, and `show`/`hide`/`status`
# have to be right in both:
#
#   systemd     headless panels (Lite, framebuffer or direct SPI). A system
#               unit starts it at boot.
#   session     Raspberry Pi OS Desktop. A ~/.config/autostart entry starts it
#               with the desktop, because a systemd unit has no DISPLAY and
#               the window backend could not open a window from one.
#
# Reporting only the systemd unit would mean `status` saying "inactive" while
# the dashboard is visibly on the screen in front of you.


def is_run_argv(argv: list[str]) -> bool:
    """Does this argv belong to a `sentinelle-display run`?

    Split out so it can be tested without spawning processes. The rule: find
    the token naming this program, then require "run" somewhere after it.
    NOT `argv[-1] == "run"` -- flags follow the subcommand.
    """
    argv = [a for a in argv if a]
    prog = next((i for i, a in enumerate(argv)
                 if a.endswith("sentinelle-display") or a == "sentinelle_display.cli"),
                None)
    return prog is not None and "run" in argv[prog + 1:]


def _running_pids() -> list[int]:
    """PIDs of any live `sentinelle-display run`.

    Reads /proc directly and matches argv TOKENS. `pgrep -f
    "sentinelle-display run"` looks like the obvious way to do this and is
    wrong: it substring-matches the whole command line, so it happily reports
    the shell that is running the check, and `status` claims the display is up
    when it is not.
    """
    me, parent = os.getpid(), os.getppid()
    found: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in (me, parent):
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue                       # the process exited mid-scan
        if is_run_argv(argv):
            found.append(pid)
    return sorted(found)


def _unit_state() -> tuple[str, str]:
    """(is-active, is-enabled) for the systemd unit, or ("", "") if absent."""
    import shutil
    import subprocess
    if not shutil.which("systemctl"):
        return "", ""
    def q(verb: str) -> str:
        try:
            return subprocess.run(["systemctl", verb, SERVICE],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    active, enabled = q("is-active"), q("is-enabled")
    # "not-found" means no such unit; report that as "no systemd unit here"
    # rather than as a unit in a strange state.
    if enabled in ("", "not-found"):
        return "", ""
    return active, enabled


def _systemctl(action: str) -> int:
    import subprocess
    cmd = ["systemctl", action, SERVICE]
    if os.geteuid() != 0:
        # install.sh drops a sudoers rule permitting exactly start/stop/restart
        # of this one unit without a password.
        cmd = ["sudo", "-n", *cmd]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        _err(f"could not {action} {SERVICE}: {detail or 'unknown error'}")
        return 1
    return 0


def cmd_show(_args) -> int:
    """Start the display, whichever way this machine starts it."""
    if _running_pids():
        _say("already running")
        return 0

    active, enabled = _unit_state()
    if enabled in ("enabled", "static"):
        rc = _systemctl("start")
        if rc == 0:
            _say("display started (systemd)")
        return rc

    # Desktop: launch it detached from this shell, so closing the terminal or
    # the SSH session does not take the dashboard down with it.
    import subprocess
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        _err("No DISPLAY in this shell — over SSH there is none even when the")
        _err("Pi has a desktop. Start it from the Pi's own screen, from the")
        _err("Accessories menu, or just log out and back in: it is in the")
        _err("desktop's autostart.")
        return 1
    subprocess.Popen([sys.executable, "-m", "sentinelle_display.cli", "run"],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _say("display started")
    return 0


def cmd_hide(_args) -> int:
    pids = _running_pids()
    _, enabled = _unit_state()
    if enabled in ("enabled", "static"):
        rc = _systemctl("stop")
        if rc == 0:
            _say("display stopped")
        return rc
    if not pids:
        _say("not running")
        return 0
    import signal as _signal
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError as e:
            _err(f"could not stop pid {pid}: {e}")
            return 1
    _say(f"display stopped ({len(pids)} process{'es' if len(pids) > 1 else ''})")
    return 0


def cmd_status(_args) -> int:
    pids = _running_pids()
    active, enabled = _unit_state()

    if pids:
        _say(f"running (pid {', '.join(str(p) for p in pids)})")
    else:
        _say("not running")

    if enabled:
        _say(f"systemd unit: {active or 'unknown'}, {enabled}")
        if enabled == "disabled" and pids:
            _say("  (started by the desktop session, which is correct on a "
                 "Desktop image)")
    else:
        _say("systemd unit: not installed")

    autostart = Path.home() / ".config" / "autostart" / "sentinelle-display.desktop"
    _say(f"desktop autostart: {'present' if autostart.exists() else 'not installed'}")
    return 0 if pids else 1


# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sentinelle-display",
        description="Always-on glucose display for a Raspberry Pi panel. "
                    "Not an alarm — keep your pump/CGM and phone alerts on.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="exchange a 10-character device code for access")
    pair.add_argument("code", nargs="?", help="the code from Settings (prompted if omitted)")
    pair.add_argument("--server", help="https://your-sentinelle-host")
    pair.add_argument("--label", help="how this display appears in Settings")
    pair.set_defaults(func=cmd_pair)

    run = sub.add_parser("run", help="run the display loop")
    _add_display_args(run)
    run.set_defaults(func=cmd_run)

    probe = sub.add_parser("probe", help="report what hardware and config is present")
    probe.set_defaults(func=cmd_probe)

    prev = sub.add_parser("preview", help="render sample screens to PNG")
    prev.add_argument("--out", default="./preview", help="output directory")
    _add_display_args(prev)
    prev.set_defaults(func=cmd_preview)

    sub.add_parser("show", help="start the display (undo a long-press hide)"
                   ).set_defaults(func=cmd_show)
    sub.add_parser("hide", help="stop the display and hand the panel back"
                   ).set_defaults(func=cmd_hide)
    sub.add_parser("status", help="is the display running?"
                   ).set_defaults(func=cmd_status)

    conf = sub.add_parser("config", help="show or change settings")
    conf.add_argument("--set", action="append", metavar="KEY=VALUE")
    conf.set_defaults(func=cmd_config)
    return p


def _add_display_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--units", choices=["mgdl", "mmol"])
    sp.add_argument("--low", type=int, help="low threshold, mg/dL")
    sp.add_argument("--high", type=int, help="high threshold, mg/dL")
    sp.add_argument("--hours", type=int, help="hours of trend to draw (1-12)")
    sp.add_argument("--rotate", type=int, choices=[0, 90, 180, 270])
    sp.add_argument("--backend", choices=["auto", "window", "fb", "spi", "png"])
    sp.add_argument("--width", type=int)
    sp.add_argument("--height", type=int)
    sp.add_argument("--panel", help="ili9486 | ili9488 | st7796 | st7789 | st7735")
    sp.add_argument("--night", help='dim hours, e.g. "22-7", or "off"')
    sp.add_argument("--palette", choices=["clinical", "cvd"])
    sp.add_argument("--view", choices=["full", "minimal"])
    sp.add_argument("--review", choices=["on", "off"],
                    help="the OTHER page: the server's daily review and joke")
    sp.add_argument("--touch", help='"auto", "off", or /dev/input/eventN')


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `sentinelle-display config | head` closes the pipe while we are still
        # writing. Every Unix tool has to handle this; Python's default is to
        # raise, print a traceback, and then raise AGAIN from the interpreter's
        # final flush of stdout — three screens of noise for a command that
        # actually worked.
        #
        # Pointing stdout at /dev/null is the documented fix: it gives that
        # final flush somewhere harmless to go. 141 is the conventional status
        # for death by SIGPIPE (128 + 13), which is what a C program would
        # exit with here.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141


if __name__ == "__main__":
    raise SystemExit(main())

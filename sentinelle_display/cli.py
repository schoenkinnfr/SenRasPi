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

    try:
        backend = open_backend(cfg)
    except BackendError as e:
        _err(str(e))
        return EXIT_HARDWARE

    # The backend knows the panel's real geometry; trust it over the config so
    # a wrong width in a hand-edited file cannot letterbox the dashboard.
    if backend.name == "fb":
        cfg.width, cfg.height = backend.width, backend.height

    _say(f"backend={backend.name} {backend.width}x{backend.height} "
         f"rotate={cfg.rotate} server={cfg.base_url}")

    poller = Poller(cfg)
    stopping = False

    # `wake` doubles as the frame clock and the "something happened, redraw
    # now" signal. Without it a tap would wait out the sleep below before
    # anything changed on screen, which reads as an unresponsive screen.
    wake = threading.Event()
    view = cfg.view if cfg.view in renderer.VIEWS else "full"
    wake_until = 0.0
    hidden = False

    def handle(signum, _frame):
        nonlocal stopping
        stopping = True
        poller.stop()
        wake.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    # ---- touchscreen -------------------------------------------------------
    watcher = None
    if cfg.touch != "off":
        from .touch import TouchWatcher                       # noqa: PLC0415

        def on_long_press() -> None:
            nonlocal stopping, hidden
            hidden = True
            stopping = True
            poller.stop()
            wake.set()

        def on_tap() -> None:
            nonlocal view, wake_until
            # During the night window a tap does NOT toggle the view -- it
            # wakes the screen. Someone walking past at 3am wants to see the
            # number, not to discover they have silently changed a setting
            # they will not notice until morning.
            if renderer.is_night(cfg, time.time()) and cfg.night_wake_seconds > 0:
                wake_until = time.time() + cfg.night_wake_seconds
            else:
                view = "minimal" if view == "full" else "full"
            wake.set()

        watcher = TouchWatcher(None if cfg.touch == "auto" else cfg.touch,
                               on_tap, on_long_press)
        watcher.start()
        # Give the thread a moment to open the device so the first frame
        # already knows whether to draw the button.
        time.sleep(0.2)
        if watcher.error:
            _err(f"touch: {watcher.error}")
        else:
            _say(f"touch={watcher.path} — tap to switch view, hold to hide")

    show_button = bool(watcher and watcher.path and not watcher.error)
    # Only advertise "hold to hide" when hiding actually works. Under systemd
    # the unit's RestartPreventExitStatus honours EXIT_HIDDEN; run by hand from
    # a shell there is no supervisor, so the process simply exits -- still
    # correct, so the chip is shown either way.
    can_hide = show_button

    poller.start()
    last_error: str | None = None
    try:
        while not stopping:
            snap = poller.get()
            if snap.last_error and snap.last_error != last_error:
                _err(snap.last_error)          # journald gets one line per change,
                last_error = snap.last_error   # not one per poll
            elif snap.ok and last_error:
                _say("recovered")
                last_error = None

            awake = time.time() < wake_until
            try:
                backend.show(renderer.render(
                    cfg, snap, view=view, show_button=show_button,
                    force_day=awake, can_hide=can_hide,
                ))
            except Exception as e:  # a bad frame must not end the process
                _err(f"render/display error: {e}")

            # Redraw on a 10s cadence even though data arrives every 60s: the
            # clock and the "N min ago" counter both have to stay honest. Cut
            # that short while a night wake is counting down so the screen
            # fades back promptly rather than up to 10s late.
            wake.wait(1 if awake else 10)
            wake.clear()
    finally:
        poller.stop()
        if watcher:
            watcher.stop()
        if hidden:
            # Blank the panel on the way out. Leaving the last dashboard frame
            # sitting there would show a number that is no longer being
            # refreshed -- the exact thing this program refuses to do
            # everywhere else.
            try:
                from PIL import Image as _Image
                backend.show(_Image.new("RGB", (backend.width, backend.height), (0, 0, 0)))
            except Exception:
                pass
        try:
            backend.close()
        except Exception:
            pass
    if hidden:
        _say("hidden — bring it back with: sentinelle-display show")
        return EXIT_HIDDEN
    return 0


def _apply_overrides(cfg, args) -> None:
    for name in ("units", "low", "high", "hours", "rotate", "backend",
                 "width", "height", "panel", "night", "palette", "view", "touch"):
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


def _systemctl(action: str) -> int:
    import shutil
    import subprocess

    if not shutil.which("systemctl"):
        _err("systemctl not found — this machine does not use systemd.")
        _err("Start the display directly instead:  sentinelle-display run")
        return EXIT_CONFIG

    cmd = ["systemctl", action, SERVICE]
    if os.geteuid() != 0:
        # install.sh drops a sudoers rule permitting exactly start/stop/restart
        # of this one unit without a password, so the desktop launcher works
        # with a single click. If that rule is absent sudo prompts, which on a
        # launcher click means nothing visibly happens -- hence the hint.
        cmd = ["sudo", "-n", *cmd]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        _err(f"could not {action} {SERVICE}: {detail or 'unknown error'}")
        if "password" in detail.lower() or "sudo" in detail.lower():
            _err("Re-run ./install.sh to install the sudoers rule, or use:")
            _err(f"  sudo systemctl {action} {SERVICE}")
        return 1
    return 0


def cmd_show(_args) -> int:
    """Bring the display back after a long-press hide."""
    rc = _systemctl("start")
    if rc == 0:
        _say("display started")
    return rc


def cmd_hide(_args) -> int:
    """Same as holding a finger on the screen."""
    rc = _systemctl("stop")
    if rc == 0:
        _say("display hidden — bring it back with: sentinelle-display show")
    return rc


def cmd_status(_args) -> int:
    import subprocess
    proc = subprocess.run(["systemctl", "is-active", SERVICE],
                          capture_output=True, text=True)
    state = proc.stdout.strip() or "unknown"
    _say(f"{SERVICE}: {state}")
    return 0 if state == "active" else 1


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
    sp.add_argument("--backend", choices=["auto", "fb", "spi", "png"])
    sp.add_argument("--width", type=int)
    sp.add_argument("--height", type=int)
    sp.add_argument("--panel", help="ili9486 | ili9488 | st7796 | st7789 | st7735")
    sp.add_argument("--night", help='dim hours, e.g. "22-7", or "off"')
    sp.add_argument("--palette", choices=["clinical", "cvd"])
    sp.add_argument("--view", choices=["full", "minimal"])
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

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
import time
from typing import Any

from . import config as configmod
from .client import PairError, Poller, format_code, normalise_code, redeem_code

EXIT_CONFIG = 2
EXIT_HARDWARE = 3


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

    def handle(signum, _frame):
        nonlocal stopping
        stopping = True
        poller.stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

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
            try:
                backend.show(renderer.render(cfg, snap))
            except Exception as e:  # a bad frame must not end the process
                _err(f"render/display error: {e}")
            # Redraw on a 10s cadence even though data arrives every 60s: the
            # clock and the "N min ago" counter both need to stay honest.
            for _ in range(10):
                if stopping:
                    break
                time.sleep(1)
    finally:
        poller.stop()
        try:
            backend.close()
        except Exception:
            pass
    return 0


def _apply_overrides(cfg, args) -> None:
    for name in ("units", "low", "high", "hours", "rotate", "backend",
                 "width", "height", "panel", "night", "palette"):
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

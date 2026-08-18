# Sentinelle Pi Display

An always-on glucose screen for a Raspberry Pi with a small SPI TFT panel.
Current reading with a trend arrow, a 3-hour graph, insulin on board, pump
reservoir and battery, sensor age and 24-hour time in range — on a 3.5" panel,
readable from across a room.

It pairs by typing a 10-character code. No browser on the Pi, no sign-in, no
token pasted over SSH.

> **This is not an alarm.** It is a screen on a shelf, with no guaranteed wake,
> no guaranteed network and no ability to make noise. Keep your pump/CGM alarms
> and phone alerts as the actual safety net. The screen always shows how old its
> data is and greys out entirely when readings go stale, precisely so it can
> never quietly present an old number as a current one.

---

## What makes this different from the `/kiosk` page

Sentinelle already serves a browser dashboard at `/kiosk`. On a 7" panel with a
2GB Pi, point Chromium at it and stop reading here.

This program exists for the case that does not work: a **1GB Pi 5** driving a
**small SPI panel**. Two reasons.

**Memory.** Chromium on a 1GB board holds 400–600MB and restarts its renderer
under pressure — on a bedside screen that means going blank at unpredictable
moments. This program holds about 35MB, because it is Pillow drawing into a
buffer and a socket. It runs on Raspberry Pi OS **Lite**: no X, no Wayland, no
compositor, no desktop.

**Drivers.** Getting a browser onto an SPI panel needs a working framebuffer,
and on a Pi 5 that is genuinely hard — see [Panel setup](#4-panel-setup). This
program can drive the panel controller directly over `/dev/spidev`, which needs
nothing from the kernel except SPI itself.

---

## 1. What you need

| | |
|---|---|
| Raspberry Pi 5 (1GB is fine here) | 2GB+ also fine, but not required |
| 3.5" SPI TFT, 480×320, ILI9486 / ILI9488 / ST7796 | the common "red board" that sits on the 40-pin header |
| microSD card, **A2-rated** | a cheap card is the usual cause of a Pi that dies after three months |
| Official 27W USB-C supply | undervoltage causes random reboots that look like software faults |

You do **not** need a keyboard, mouse, monitor or HDMI cable. Everything below
happens over SSH.

---

## 2. Flash the card

Use Raspberry Pi Imager and choose **Raspberry Pi OS Lite (64-bit)**. Lite, not
Desktop — this program does not use a desktop, and the desktop is the thing
that would eat the RAM you are trying to save.

Before writing, open the gear icon and set:

- Hostname: `sentinelle-pi`
- Enable SSH, with your public key
- Wi-Fi SSID, password and country
- Locale and timezone — **set this correctly.** The clock and the night-dimming
  window both come from it.

Boot the Pi, wait a couple of minutes, then:

```bash
ssh <user>@sentinelle-pi.local
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

---

## 3. Install

```bash
git clone https://github.com/schoenkinnfr/SenRasPi.git
cd SenRasPi
./install.sh
```

Run it as your normal user, not with `sudo` — it asks for sudo where it needs
it. It is safe to re-run; every step checks before changing anything.

It installs the Debian packages (`python3-pil`, `python3-numpy`,
`python3-spidev`, `python3-gpiozero`, `python3-lgpio`), creates a venv at
`/opt/sentinelle-display`, enables SPI, adds you to the `spi`/`gpio`/`video`/`input`
groups, and installs a systemd unit.

**Deliberately apt, not pip,** for numpy and Pillow: from PyPI those build from
source on a Pi and take most of an hour on a 1GB board.

Then reboot — group membership and SPI both apply at boot, not to the session
you are in:

```bash
sudo reboot
```

---

## 4. Panel setup

This is the part that goes wrong, so do it before pairing.

```bash
sentinelle-display probe
```

That prints the board, the kernel, which `/dev/spidev*` devices exist, which
framebuffers exist, and which Python modules are importable. Two outcomes:

### A framebuffer appeared

```
Framebuffers
  /dev/fb1  480x320  16bpp  fb_ili9486   <- would use
```

A kernel driver has claimed the panel. Nothing more to do — the default
`backend: auto` will use it.

### No framebuffer

```
Framebuffers
  none — no kernel driver has claimed a panel.
```

**This is the normal result on a Pi 5, and it is fine.** The vendor `LCD-show`
scripts and the legacy `fbtft` overlays were written for the BCM2835/2711 GPIO
block; the Pi 5 puts its GPIO behind the RP1 southbridge, so those scripts
variously edit the wrong config file, load a module that no longer exists, or
leave you with a lit backlight and no picture. Do not go down that road.

Use the direct-SPI backend instead:

```bash
sentinelle-display config --set backend=spi --set panel=ili9486
```

Panel names: `ili9486` (most 3.5" 480×320 red boards), `ili9488`, `st7796`,
`st7789`, `st7735`. If you do not know which you have, the silkscreen on the
back of the board usually says, and `ili9486` is the right first guess for a
3.5" 480×320.

Wiring the direct-SPI backend assumes (BCM numbering — all configurable):

| Signal | GPIO | Header pin |
|---|---|---|
| MOSI | 10 | 19 |
| SCLK | 11 | 23 |
| CE0 | 8 | 24 |
| DC | 24 | 18 |
| RESET | 25 | 22 |
| Backlight | 18 | 12 |

Change any of them: `sentinelle-display config --set pin_dc=23`.

> **If a kernel overlay for this panel is still loaded**, it holds the GPIO
> lines and the direct-SPI backend cannot claim them. Comment out any LCD
> `dtoverlay=` line in `/boot/firmware/config.txt` and reboot.

---

## 5. Pair it

In the web app, open **Settings → Raspberry Pi display** and click **Generate
device code**. You get something like `K7M2P-QRXW9`, good for 15 minutes.

On the Pi:

```bash
sentinelle-display pair
```

```
Which Sentinelle server? (e.g. https://sentinelle.example.com)
  Server URL: sentinelle.example.com

Server: https://sentinelle.example.com
In the web app, open Settings and generate a device code under
"Raspberry Pi display". It looks like ABCDE-FGHJK and expires
15 minutes after you create it.

  Device code: k7m2p-qrxw9
  Pairing as "Pi — sentinelle-pi"…

  Paired. Credentials written to /home/pi/.config/sentinelle/display.json (mode 600).
```

Case and the dash do not matter. The digits `0` and `1` never appear in a code
— anything that looks like one is the letter `O` or `I`, and the tool tells you
so rather than guessing.

The Settings card flips to "paired" on its own within a few seconds.

Then:

```bash
sudo systemctl start sentinelle-display
journalctl -u sentinelle-display -f
```

It is already enabled, so it comes back on every boot.

---

## 6. The controls

Touch anywhere to bring up the control bar. It hides itself after about eight
seconds; the small `•••` chip in the corner is the standing reminder that it
exists.

| Button | Does |
|---|---|
| **VIEW** | Full dashboard ⇄ Minimal — the number at twice the size, for reading across a room |
| **NIGHT** | Auto / On / Off — see below |
| **UNITS** | mg/dL ⇄ mmol/L |
| **Minimize** | Minimises the window to the taskbar. Click it there to bring it back. |

The first touch only summons the bar; it never presses whatever happened to be
under your finger. Reaching out to look at the screen should not change a
setting.

**NIGHT is three-way, not a toggle.** `Auto` follows the configured schedule
(`22-7` by default). `On` and `Off` force the ambient wash either way. A plain
on/off switch would replace the schedule with something you have to remember to
flip twice a day, and the failure mode is a bedroom screen at full brightness
all night because you forgot.

The bar stays visible during the night wash. Otherwise setting `NIGHT: On`
would hide the only control that can set it back — a one-way door escapable
only over SSH.

Changes are written to the config file, so they survive a restart.

### Why a window and not the framebuffer

On **Raspberry Pi OS Desktop** the display runs as a real fullscreen window and
the desktop compositor owns the screen. That matters for a specific reason: the
`fb` backend writes straight into `/dev/fb0`, and on a Desktop image the
desktop owns that framebuffer too. The two fight. The symptom is exact and
baffling — the dashboard looks right until you touch it, then the pointer event
makes the desktop repaint the damaged region and the dashboard is visibly eaten
away, partly returning on the next redraw. Nothing is broken; two programs are
drawing the same pixels.

A window also means real click coordinates, so the buttons above are actual hit
regions rather than whole-screen gestures — the display server has already done
the touchscreen calibration. And Minimize can genuinely minimise rather than
killing the process.

It costs about 40MB over the framebuffer path. On a board already running a
desktop that is noise. On **Lite** there is no desktop to fight with, so use
`--set backend=fb`, keep the 35MB footprint, and the controls fall back to
whole-screen gestures (tap to change view, hold to exit) read from
`/dev/input`.

On a Desktop image the display is started from the **desktop session's
autostart**, not from systemd — a systemd unit has no `DISPLAY` or
`WAYLAND_DISPLAY`, so a window backend launched that way cannot open a window
at all. `install.sh` detects which case you are in and wires up the right one.

`sentinelle-display probe` reports which path applies, and
`sentinelle-display status` reports whether it is running and which mechanism
started it.

Because an autostarted app has no console, the session launcher writes to
`~/.local/state/sentinelle-display.log` (one previous run kept as `.log.1`).
That is the first place to look if the screen stays on the wallpaper after
logging in.

---

## 7. Settings

```bash
sentinelle-display config                       # show everything
sentinelle-display config --set units=mmol
sentinelle-display config --set low=80 --set high=160
sudo systemctl restart sentinelle-display       # settings apply on restart
```

| Setting | Default | Meaning |
|---|---|---|
| `units` | `mgdl` | `mgdl` or `mmol` |
| `low` / `high` | `70` / `180` | Thresholds. **Always in mg/dL**, even when displaying mmol/L. |
| `hours` | `3` | Hours of trend on the graph (1–12) |
| `stale_minutes` | `20` | Grey the screen out after this long with no reading. `0` disables. |
| `night` | `22-7` | Dim to an ambient wash during these hours. `off` disables. |
| `poll_seconds` | `60` | How often to ask the server |
| `palette` | `clinical` | `clinical` (red/green/amber) or `cvd` — see below |
| `rotate` | `0` | `0`/`90`/`180`/`270`. See the note below. |
| `backend` | `auto` | `auto`, `window`, `fb`, `spi`, `png` |
| `night_mode` | `auto` | `auto` follows the schedule; `on`/`off` force the wash |
| `touch` | `auto` | `auto`, `off`, or a literal `/dev/input/eventN` |
| `view` | `full` | Which view to start in: `full` or `minimal` |
| `night_wake_seconds` | `30` | How long a night-time tap shows the real screen. `0` disables |
| `width` / `height` | `480` / `320` | The panel's **native** resolution |

**Rotation.** `width`/`height` are the panel's native framebuffer size;
`rotate` is how far the layout turns to match how the panel is physically
mounted. A 480×320 landscape panel mounted normally is `480/320` + `rotate=0`.
A natively-portrait 320×480 panel that you want to read in landscape is
`320/480` + `rotate=90`.

**The 20-minute stale default is deliberate.** CareLink itself runs 5–10 minutes
behind the sensor, so one slow upload cycle legitimately reaches 16–18 minutes
with nothing wrong. Tighten it and you get false greyouts.

**Colour.** Under the default `clinical` palette, low (red) and in-range (green)
separate by ΔE 4.1 under deuteranopia — a red-green colourblind reader cannot
tell them apart by hue. That is why every screen also writes the state in
words, and why the graph shades an explicit target band rather than relying on
point colour. If you want hue to work too, `--set palette=cvd` swaps in a
red/blue/amber set that separates under all three common types.

---

## 8. When it does not work

Run `sentinelle-display probe` first. It answers most of these.

| Symptom | Cause |
|---|---|
| Screen stays black, backlight on | Wrong `panel`, or DC/RESET on different pins than configured. Try `--panel ili9488`. |
| Screen stays black, backlight off too | `pin_backlight` wrong, or the panel takes its backlight from a jumper rather than a GPIO. Set `pin_backlight=-1` and check the jumper. |
| Red and blue swapped | The panel wires RGB, not BGR. Open an issue with the silkscreen text — this is a one-bit fix in `spi.py`. |
| Image sheared diagonally | `width`/`height` do not match the real panel. |
| `no framebuffer found` | Expected on a Pi 5. Use `backend=spi`. |
| `cannot claim GPIO` | A kernel overlay for the panel is loaded and holding the pins. Comment out the `dtoverlay=` line and reboot. |
| `/dev/spidev0.0 does not exist` | SPI is not enabled. `sudo raspi-config nonint do_spi 0`, reboot. |
| `Permission denied` on SPI or fb | Group change needs a **reboot**, not just a new shell. |
| `access revoked or wrong scope` | The token was revoked in Settings, or the code was `ambient`-scoped. Generate a `Full dashboard` code and re-pair. |
| It hides itself and comes straight back | `RestartPreventExitStatus=64` is missing from the unit. Re-run `./install.sh`. |
| The menu entry does nothing when clicked | The sudoers rule did not install, so `sudo` is silently prompting where nothing can answer. Run `sentinelle-display show` in a terminal to see the real error. |
| The dashboard gets eaten away when you touch it | You are on the `fb` backend with a desktop running; both write to `/dev/fb0`. Use `--set backend=window`, or boot to console. |
| Nothing starts after login | `cat ~/.local/state/sentinelle-display.log`. If the file does not exist, the autostart entry is missing — re-run `./install.sh`. |
| `cannot open a window` | No `DISPLAY`/`WAYLAND_DISPLAY`. On a desktop it is started from the session's autostart, not systemd, precisely because units have neither. |
| Tapping does nothing | `probe` says whether a touchscreen was found. If it was, you are probably not in the `input` group yet — that needs a **reboot**, not a new shell. |
| No **more**/**less** chip | No touchscreen detected, so the button is deliberately not drawn. Advertising a control that does not exist is worse than omitting it. |
| A plain colour wash with one small number | That is night mode, working. Check `timedatectl` — a Pi still on UTC thinks it is 4-5 hours later than you do. `config --set night=off` disables it. |
| Screen fine, then dead after weeks | Almost always the microSD card. Use an A2-rated one. |
| Random reboots | Underpowered supply. Use the official 27W. |

**The single most useful diagnostic:**

```bash
sudo systemctl stop sentinelle-display
sentinelle-display run --backend png
# in another shell:  ls -l /tmp/sentinelle-display.png
```

If that PNG looks correct, the program and the network are fine and the problem
is the panel, the wiring or the driver. If it looks wrong, it is the program.
That split saves an evening.

---

## 9. What this can and cannot see

Pairing produces a **kiosk token**, stored in `~/.config/sentinelle/display.json`
mode 600. It:

- reaches exactly two endpoints, `/kiosk/data` and `/night/data`
- can write nothing, and cannot reach `/api` at all
- carries no name, email or account identifier
- is stored server-side only as a SHA-256 hash

What that does **not** bound: anyone with physical access to the Pi can read
that file and watch your glucose from anywhere until you revoke it in Settings.
If the screen lives somewhere semi-public, generate a **Current number only**
(`ambient`) code instead.

The pairing code itself is single-use, expires in 15 minutes, is stored only as
a hash, and the redemption endpoint is rate-limited per IP and globally, with
one identical rejection message for every failure mode so it cannot be used to
enumerate valid codes.

---

## 10. Development

The renderer touches no hardware and no network, so you can work on it from a
laptop:

```bash
pip install -e '.[dev]'
sentinelle-display preview --out ./preview
sentinelle-display preview --out ./preview --units mmol --rotate 90
pytest
```

That writes `in_range.png`, `low.png`, `high.png`, `stale.png` and `night.png`
at whatever geometry you ask for.

Layout lives in `render.py`, colour in `theme.py`, hardware in `backends/`.
Adding a controller means one entry in `PANELS` in `backends/spi.py`.

---

## Licence

AGPL-3.0-only, matching the Sentinelle T1D server. See `LICENSE`.

Informational only. Not a medical device, not medical advice, and not an alarm.

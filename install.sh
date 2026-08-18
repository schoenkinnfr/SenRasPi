#!/usr/bin/env bash
#
# Sentinelle Pi display — installer.
# SPDX-License-Identifier: AGPL-3.0-only
#
# Run as your normal user (NOT with sudo — it will ask for sudo where it needs
# it). Safe to re-run: every step checks before it changes anything.
#
#   ./install.sh                    install and enable
#   ./install.sh --no-spi           skip enabling the SPI bus
#   ./install.sh --user pi          install the service for a different user
#
set -euo pipefail

RUN_USER="${SUDO_USER:-$USER}"
ENABLE_SPI=1
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="/opt/sentinelle-display"
SERVICE=/etc/systemd/system/sentinelle-display.service

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-spi) ENABLE_SPI=0; shift ;;
    --user)   RUN_USER="$2"; shift 2 ;;
    -h|--help) sed -n '3,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 && -z "${SUDO_USER:-}" ]] && die "run this as your normal user, not as root"

# ─────────────────────────────────────────────────────────────────────────────
say "Checking the machine"

if [[ -r /proc/device-tree/model ]]; then
  note "$(tr -d '\0' < /proc/device-tree/model)"
else
  note "not a Raspberry Pi (no /proc/device-tree/model) — continuing anyway"
fi
command -v apt-get >/dev/null || die "this installer expects a Debian-based OS (Raspberry Pi OS)"

# ─────────────────────────────────────────────────────────────────────────────
say "Installing system packages"

# Deliberately apt, not pip. numpy and Pillow from PyPI build from source on a
# Pi and take the better part of an hour on a 1GB board; the Debian packages
# are prebuilt and land in seconds. python3-lgpio is what gpiozero needs to
# talk to a Pi 5's GPIO at all — RPi.GPIO does not work on this board.
PKGS=(python3 python3-venv python3-pip python3-pil python3-numpy
      python3-spidev python3-gpiozero python3-lgpio fonts-dejavu-core)
# Only needed for the window backend, and only meaningful where a desktop
# exists. Skipped on Lite so a headless panel does not pull in Tcl/Tk.
if [[ -d /usr/share/xsessions || -d /usr/share/wayland-sessions ]] \
   || dpkg -s raspberrypi-ui-mods >/dev/null 2>&1; then
  PKGS+=(python3-tk python3-pil.imagetk)
  HAS_DESKTOP=1
else
  HAS_DESKTOP=0
fi

MISSING=()
for p in "${PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  note "installing: ${MISSING[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${MISSING[@]}"
else
  note "already present"
fi

# ─────────────────────────────────────────────────────────────────────────────
say "Creating the virtualenv at $VENV"

# --system-site-packages so numpy/Pillow/spidev/gpiozero come from apt rather
# than being rebuilt. The venv exists only to hold this one package.
if [[ ! -x "$VENV/bin/python" ]]; then
  sudo python3 -m venv --system-site-packages "$VENV"
fi
sudo "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
sudo "$VENV/bin/pip" install --quiet --no-deps --upgrade "$REPO_DIR"
note "$("$VENV/bin/python" -c 'import sentinelle_display as s; print("sentinelle-display", s.__version__)')"

sudo ln -sf "$VENV/bin/sentinelle-display" /usr/local/bin/sentinelle-display
note "sentinelle-display is on your PATH"

# ─────────────────────────────────────────────────────────────────────────────
say "Enabling hardware access"

# Raspberry Pi OS moved config.txt to /boot/firmware in Bookworm. Older images
# keep it at /boot. Editing the wrong one is the single most common reason a
# display tutorial silently does nothing.
CONFIG_TXT=/boot/firmware/config.txt
[[ -f $CONFIG_TXT ]] || CONFIG_TXT=/boot/config.txt
NEEDS_REBOOT=0

if [[ $ENABLE_SPI -eq 1 ]]; then
  if [[ -f $CONFIG_TXT ]]; then
    if grep -qE '^\s*dtparam=spi=on' "$CONFIG_TXT"; then
      note "SPI already enabled in $CONFIG_TXT"
    else
      note "enabling SPI in $CONFIG_TXT"
      echo 'dtparam=spi=on' | sudo tee -a "$CONFIG_TXT" >/dev/null
      NEEDS_REBOOT=1
    fi
  else
    note "no config.txt found — enable SPI yourself if the panel needs it"
  fi
fi

for grp in spi gpio video input; do
  if getent group "$grp" >/dev/null; then
    if id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$grp"; then
      note "$RUN_USER already in group $grp"
    else
      sudo usermod -aG "$grp" "$RUN_USER"
      note "added $RUN_USER to group $grp"
      NEEDS_REBOOT=1
    fi
  fi
done

# ─────────────────────────────────────────────────────────────────────────────
say "Installing the systemd service"

# The delimiter is QUOTED, so the shell expands nothing inside this block.
# That is deliberate: an earlier version used an unquoted heredoc and a
# pair of backticks in a comment turned into command substitution, which
# silently ran an interactive command and hung the installer. The two real
# substitutions happen with sed on the next line.
sudo tee "$SERVICE" >/dev/null <<'UNIT'
[Unit]
Description=Sentinelle T1D glucose display
Documentation=https://github.com/schoenkinnfr/SenRasPi
# Wants= rather than Requires=: the display should come up and show its
# "waiting for the first reading" screen even with no network, not sit dark.
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=__RUN_USER__
SupplementaryGroups=spi gpio video input
ExecStart=__VENV__/bin/sentinelle-display run
Restart=always
RestartSec=5
# Exit code 64 is "the user held a finger on the screen to hide me". Without
# this line Restart=always would bring it straight back five seconds later and
# the gesture would look broken.
RestartPreventExitStatus=64
# A crash loop must not fill the SD card with journal. Cards are the usual
# cause of a Pi that dies after three months; writes are the usual cause of a
# dead card.
StandardOutput=journal
StandardError=journal

# It reads one JSON file and writes to a display. Nothing else is needed.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
# The leading '-' matters. Without it systemd refuses to start the unit at
# all when this directory does not exist yet -- i.e. on every fresh install,
# before the first 'sentinelle-display pair'. The failure is a cryptic
# "Failed to set up mount namespacing ... status=226/NAMESPACE" that never
# reaches the program, so the display crash-loops instead of showing its
# "not paired yet" screen, which is the exact situation that screen exists
# for. install.sh also pre-creates the directory; this makes it survive
# someone deleting it.
# /tmp is not listed: PrivateTmp=yes already gives this unit its own writable
# /tmp, and naming it here would only re-expose the host's.
ReadWritePaths=-/home/__RUN_USER__/.config/sentinelle
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictRealtime=yes
# DeviceAllow is a WHITELIST: naming any device denies everything else, so a
# device missing from this list fails with a bare EACCES that looks like a
# permissions bug in the app. These are all of them.
#   char-input     the touchscreen (/dev/input/eventN), read-only
#   char-spi       the direct-SPI backend
#   char-gpiochip  DC/RESET/backlight lines
DeviceAllow=char-input r
DeviceAllow=char-spi rw
DeviceAllow=char-gpiochip rw
DeviceAllow=/dev/fb0 rw
DeviceAllow=/dev/fb1 rw

[Install]
WantedBy=multi-user.target
UNIT
sudo sed -i "s|__RUN_USER__|${RUN_USER}|g; s|__VENV__|${VENV}|g" "$SERVICE"

sudo systemctl daemon-reload

# On a DESKTOP the display must run inside the desktop session, not as a
# system service: systemd units have no DISPLAY or WAYLAND_DISPLAY, so a
# window backend started that way cannot open a window at all. The desktop's
# own autostart gives it the right environment for free.
if [[ $HAS_DESKTOP -eq 1 ]]; then
  sudo systemctl disable sentinelle-display >/dev/null 2>&1 || true
  note "desktop detected — starting from the desktop session, not systemd"
else
  sudo systemctl enable sentinelle-display >/dev/null
  note "enabled — it will start on boot"
fi

# Create the config directory up front, owned by the user who will run
# `pair`. The service reads its config from here and systemd wants the path
# to exist; creating it now means a fresh Pi shows the "not paired yet"
# screen instead of a crash loop.
CONF_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)/.config/sentinelle"
sudo -u "$RUN_USER" mkdir -p "$CONF_DIR"
sudo -u "$RUN_USER" chmod 700 "$CONF_DIR"
note "config directory: $CONF_DIR"

# ─────────────────────────────────────────────────────────────────────────────
say "Desktop integration"

# Starting a system service normally needs root. Rather than make the menu
# entry prompt for a password -- which on a launcher click just looks like
# nothing happened -- allow exactly three commands on exactly this one unit.
# Validated with visudo before installing: a malformed sudoers file locks you
# out of sudo entirely, so this is never written unchecked.
SUDOERS=/etc/sudoers.d/sentinelle-display
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
TMP_SUDOERS="$(mktemp)"
cat > "$TMP_SUDOERS" <<SUDO
# Lets ${RUN_USER} show/hide the glucose display without a password prompt.
# Three exact commands on one unit -- not a general sudo grant.
${RUN_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} start sentinelle-display, ${SYSTEMCTL} stop sentinelle-display, ${SYSTEMCTL} restart sentinelle-display
SUDO
if sudo visudo -cqf "$TMP_SUDOERS" 2>/dev/null; then
  sudo install -m 0440 -o root -g root "$TMP_SUDOERS" "$SUDOERS"
  note "sudoers rule installed (start/stop/restart only)"
else
  note "sudoers rule FAILED validation — skipped; 'show'/'hide' will prompt for a password"
fi
rm -f "$TMP_SUDOERS"

# A menu entry, but only where a menu exists. On Raspberry Pi OS Lite there is
# no desktop and dropping .desktop files would be litter.
if [[ -d /usr/share/applications ]]; then
  sudo tee /usr/share/applications/sentinelle-display.desktop >/dev/null <<'DESK'
[Desktop Entry]
Type=Application
Version=1.0
Name=Sentinelle Glucose Display
GenericName=Glucose Display
Comment=Show the always-on glucose panel (hold a finger on the screen to hide it)
Exec=sentinelle-display-session
Icon=utilities-system-monitor
Terminal=false
Categories=Utility;Monitor;
Keywords=glucose;diabetes;cgm;sentinelle;
StartupNotify=false
DESK
  sudo chmod 0644 /usr/share/applications/sentinelle-display.desktop
  note "menu entry: Sentinelle Glucose Display (under Accessories)"

  # ...and a shortcut on the desktop itself, if this user has one.
  DESKTOP_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)/Desktop"
  if [[ -d $DESKTOP_DIR ]]; then
    sudo -u "$RUN_USER" cp /usr/share/applications/sentinelle-display.desktop "$DESKTOP_DIR/"
    sudo -u "$RUN_USER" chmod +x "$DESKTOP_DIR/sentinelle-display.desktop"
    note "desktop shortcut: $DESKTOP_DIR"
  fi
  # Start it with the desktop session. This is what replaces the systemd unit
  # on a Desktop image.
  # A launcher script rather than a shell one-liner inside Exec=. The Desktop
  # Entry spec has its own quoting rules for Exec, and a redirect with quotes
  # in it is exactly the kind of thing that parses on one desktop and silently
  # fails to start on another.
  sudo tee /usr/local/bin/sentinelle-display-session >/dev/null <<'LAUNCH'
#!/bin/sh
# Started by the desktop session's autostart. It logs, because an autostarted
# app has no console: without this a failure to start is completely invisible,
# and the only symptom is a screen that stays on the wallpaper.
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/sentinelle-display.log"
# Keep one previous run and start fresh, so the log cannot grow without bound.
# This lives on an SD card, and writes are what kill SD cards.
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"
exec sentinelle-display run >>"$LOG" 2>&1
LAUNCH
  sudo chmod 0755 /usr/local/bin/sentinelle-display-session

  AUTOSTART_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)/.config/autostart"
  sudo -u "$RUN_USER" mkdir -p "$AUTOSTART_DIR"
  sudo -u "$RUN_USER" tee "$AUTOSTART_DIR/sentinelle-display.desktop" >/dev/null <<'AUTO'
[Desktop Entry]
Type=Application
Name=Sentinelle Glucose Display
Exec=sentinelle-display-session
Terminal=false
X-GNOME-Autostart-enabled=true
AUTO
  note "autostart: $AUTOSTART_DIR"
  note "session log: ~/.local/state/sentinelle-display.log"

  command -v update-desktop-database >/dev/null && \
    sudo update-desktop-database /usr/share/applications 2>/dev/null || true
else
  note "no /usr/share/applications — no desktop here, skipping the menu entry"
fi

# ─────────────────────────────────────────────────────────────────────────────
say "Done"

if [[ $NEEDS_REBOOT -eq 1 ]]; then
  cat <<EOF

    A reboot is needed before the display can reach the hardware
    (group membership and SPI both apply at boot, not to this session).

      sudo reboot

    After it comes back:
EOF
else
  echo
  echo "    Next:"
fi

cat <<EOF

      sentinelle-display probe          # confirm the panel is visible
      sentinelle-display pair           # type the code from Settings
      sentinelle-display run            # start it

    On the screen: touch anywhere to bring up the control bar, then
    press VIEW, NIGHT, UNITS or Minimize. The bar hides itself after
    a few seconds; the small ••• chip in the corner is the reminder
    that it is there.

    On a desktop it starts automatically with your session, and there
    is a menu entry under Accessories.

    If the screen stays dark, start with:

      sentinelle-display probe
      sentinelle-display run --backend png   # then look at /tmp/sentinelle-display.png

    That last one separates "the layout is broken" from "the wiring is
    broken", which is most of the debugging.

    This is not an alarm. Keep your pump/CGM and phone alerts on.

EOF

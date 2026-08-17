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

for grp in spi gpio video; do
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

sudo tee "$SERVICE" >/dev/null <<UNIT
[Unit]
Description=Sentinelle T1D glucose display
Documentation=https://github.com/schoenkinnfr/SenRasPi
# Wants= rather than Requires=: the display should come up and show its
# "waiting for the first reading" screen even with no network, not sit dark.
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${RUN_USER}
SupplementaryGroups=spi gpio video
ExecStart=${VENV}/bin/sentinelle-display run
Restart=always
RestartSec=5
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
ReadWritePaths=/home/${RUN_USER}/.config/sentinelle /tmp
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictRealtime=yes
# /dev/fb*, /dev/spidev* and /dev/gpiochip* are the only devices it touches.
DeviceAllow=char-spi rw
DeviceAllow=char-gpiochip rw
DeviceAllow=/dev/fb0 rw
DeviceAllow=/dev/fb1 rw

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable sentinelle-display >/dev/null
note "enabled — it will start on boot"

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
      sudo systemctl start sentinelle-display

    If the screen stays dark, start with:

      sentinelle-display probe
      sentinelle-display run --backend png   # then look at /tmp/sentinelle-display.png

    That last one separates "the layout is broken" from "the wiring is
    broken", which is most of the debugging.

    This is not an alarm. Keep your pump/CGM and phone alerts on.

EOF

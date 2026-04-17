#!/bin/bash
# Install systemd-unit/bt-adapter-wait.{sh,service} host-wide.
#
# This is a ONE-PER-HOST install: the service is global, not per-bot.
# Running it twice (e.g. from both gatekeeper-km and gatekeeper-hoftor
# clones) is a no-op the second time beyond re-copying identical
# files. Safe to re-run after a git pull to pick up script changes.
#
# Usage:
#     sudo ./install-bt-wait-service.sh            # install + enable
#     sudo ./install-bt-wait-service.sh --uninstall
set -e

SCRIPT_SRC="$(dirname "$0")/systemd-unit/bt-adapter-wait.sh"
UNIT_SRC="$(dirname "$0")/systemd-unit/bt-adapter-wait.service"
SCRIPT_DST="/usr/local/sbin/bt-adapter-wait"
UNIT_DST="/etc/systemd/system/bt-adapter-wait.service"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--uninstall|-h|--help]

Installs or removes the system-wide bt-adapter-wait service, which
runs at boot to recover USB Bluetooth adapters that fail their
initial firmware download (a known Realtek RTL8761 issue on Pi USB
ports).

  (no args)      Install the script + unit, enable for next boot.
  --uninstall    Disable + remove the installed files.
  -h, --help     Show this help.
EOF
}

if [ "$EUID" -ne 0 ]; then
    echo "Must run as root (writes to /usr/local/sbin and /etc/systemd/system):" >&2
    echo "    sudo $0" >&2
    exit 5
fi

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --uninstall)
        if systemctl is-enabled --quiet bt-adapter-wait.service 2>/dev/null; then
            systemctl disable bt-adapter-wait.service
        fi
        rm -f "$UNIT_DST" "$SCRIPT_DST"
        systemctl daemon-reload
        echo "bt-adapter-wait removed."
        exit 0
        ;;
    "") ;; # install path
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac

if [ ! -f "$SCRIPT_SRC" ] || [ ! -f "$UNIT_SRC" ]; then
    echo "ERROR: source files not found at:" >&2
    echo "    $SCRIPT_SRC" >&2
    echo "    $UNIT_SRC" >&2
    echo "Run this from the repo root (where systemd-unit/ lives)." >&2
    exit 1
fi

install -D -m 755 "$SCRIPT_SRC" "$SCRIPT_DST"
install -D -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable bt-adapter-wait.service

echo "Installed:"
echo "    $SCRIPT_DST"
echo "    $UNIT_DST"
echo "Enabled for next boot. Verify after reboot with:"
echo "    journalctl -u bt-adapter-wait -b"

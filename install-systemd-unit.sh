#!/bin/bash
# Install a systemd unit for this gatekeeper-bot instance.
#
# Run from a bot's own directory (where .env lives). Reads .env to
# derive BOT_NAME and DOOR_NAME, fills them into
# systemd-unit/deltabot.service.template, writes the result to
# /etc/systemd/system/deltabot-<BOT_NAME>.service, runs daemon-reload,
# applies setcap on bluepy-helper, and offers to enable+start the
# service.
#
# Typical use for a single bot:
#
#     cd /home/pi/gatekeeper-bot
#     $EDITOR .env                                 # set BOT_NAME, DOOR_NAME, etc.
#     sudo ./install-systemd-unit.sh               # installs, prompts to start
#
# For the two-bot deployment:
#
#     cd /home/pi/gatekeeper-km    && sudo ./install-systemd-unit.sh -y
#     cd /home/pi/gatekeeper-hoftor && sudo ./install-systemd-unit.sh -y
#
# The script is idempotent: re-running against an up-to-date target
# is a no-op beyond reapplying setcap.
set -e

usage() {
    cat <<EOF
Usage: $(basename "$0") [-y|--yes] [--skip-setcap] [--dry-run] [-h|--help]

  -y, --yes        Non-interactive: overwrite without prompting, and
                   enable+start the service without asking.
  --skip-setcap    Skip the setcap step on bluepy-helper (e.g. you
                   already applied it manually or the venv doesn't
                   contain bluepy).
  --dry-run        Print what the generated unit would look like and
                   exit; do not touch /etc/systemd/system/ or caps.
  -h, --help       Show this help.
EOF
}

ASSUME_YES=0
SKIP_SETCAP=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)       ASSUME_YES=1; shift ;;
        --skip-setcap)  SKIP_SETCAP=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# /etc/systemd/system/ + setcap both need root. Fail fast with a
# clear message (same pattern as pair-lock.sh).
if [ "$EUID" -ne 0 ] && [ $DRY_RUN -eq 0 ]; then
    cat >&2 <<EOF
This script writes to /etc/systemd/system/ and applies file
capabilities, both of which need root. Re-run as:

    sudo $0 $*
EOF
    exit 5
fi

cd "$(dirname "$0")"
BOT_DIR="$PWD"

if [ ! -f .env ]; then
    echo "ERROR: no .env in $BOT_DIR. Create one first (see .env.example)." >&2
    exit 1
fi

# Source .env so BOT_NAME / DOOR_NAME become shell variables.
# set -a auto-exports; we don't strictly need the export here but it
# matches how start-gatekeeper-bot.sh and lib/common.sh source it.
set -a
# shellcheck disable=SC1091
source ./.env
set +a

BOT_NAME="${BOT_NAME:-gatekeeper}"
DESCRIPTION="${DOOR_NAME:-$BOT_NAME}"

TEMPLATE="$BOT_DIR/systemd-unit/deltabot.service.template"
if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: template not found: $TEMPLATE" >&2
    exit 1
fi

TARGET="/etc/systemd/system/deltabot-${BOT_NAME}.service"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# Use | as sed delimiter so directory paths don't need escaping.
# DESCRIPTION is unlikely to contain |; if it does the admin can
# rename DOOR_NAME or hand-edit the template.
sed \
    -e "s|@WORKING_DIR@|$BOT_DIR|g" \
    -e "s|@DESCRIPTION@|$DESCRIPTION|g" \
    -e "s|@BOT_NAME@|$BOT_NAME|g" \
    "$TEMPLATE" > "$TMP"

if [ $DRY_RUN -eq 1 ]; then
    echo "--- would install to $TARGET ---"
    cat "$TMP"
    echo "--- end ---"
    exit 0
fi

# If a unit is already installed, show the diff and confirm before
# overwriting -- users sometimes hand-edit the live unit and we
# don't want to stomp that silently.
changed=0
if [ -f "$TARGET" ]; then
    if diff -q "$TARGET" "$TMP" >/dev/null 2>&1; then
        echo "Unit already up-to-date: $TARGET"
    else
        echo "Unit at $TARGET differs from the generated version:"
        diff -u "$TARGET" "$TMP" || true
        echo
        if [ $ASSUME_YES -eq 0 ]; then
            read -rp "Overwrite $TARGET? [y/N] " ans
            if [[ ! "$ans" =~ ^[Yy] ]]; then
                echo "aborted; target untouched."
                exit 1
            fi
        fi
        install -m 644 "$TMP" "$TARGET"
        changed=1
        echo "updated: $TARGET"
    fi
else
    install -m 644 "$TMP" "$TARGET"
    changed=1
    echo "installed: $TARGET"
fi

# setcap on bluepy-helper so the bot can open raw HCI sockets as
# the unprivileged service user. Capability is file-bound; lost on
# any pip reinstall of bluepy, so it's worth re-running this
# installer after a venv rebuild.
if [ $SKIP_SETCAP -eq 0 ]; then
    BH=""
    # Most common path first, fall back to whatever python version the
    # venv actually uses.
    for cand in \
        "$BOT_DIR"/venv/lib/python*/site-packages/bluepy/bluepy-helper
    do
        if [ -x "$cand" ]; then
            BH="$cand"; break
        fi
    done
    if [ -n "$BH" ]; then
        setcap cap_net_raw,cap_net_admin+eip "$BH"
        echo "setcap cap_net_raw,cap_net_admin+eip applied to $BH"
    else
        echo "WARNING: no bluepy-helper under $BOT_DIR/venv; skipping setcap." >&2
        echo "         BLE calls will fail under the unprivileged service user" >&2
        echo "         until you run: sudo setcap cap_net_raw,cap_net_admin+eip <path>" >&2
    fi
fi

systemctl daemon-reload

# Enable + start unless already running. Prompt in interactive mode.
if systemctl is-active --quiet "deltabot-${BOT_NAME}.service"; then
    if [ $changed -eq 1 ]; then
        systemctl restart "deltabot-${BOT_NAME}.service"
        echo "restarted: deltabot-${BOT_NAME}.service"
    else
        echo "already running: deltabot-${BOT_NAME}.service"
    fi
else
    if [ $ASSUME_YES -eq 1 ]; then
        ans=y
    else
        read -rp "enable + start deltabot-${BOT_NAME}.service now? [y/N] " ans
    fi
    if [[ "$ans" =~ ^[Yy] ]]; then
        systemctl enable --now "deltabot-${BOT_NAME}.service"
        sleep 2
        if systemctl is-active --quiet "deltabot-${BOT_NAME}.service"; then
            echo "started: deltabot-${BOT_NAME}.service"
        else
            echo "WARNING: service failed to become active; check journalctl -u deltabot-${BOT_NAME}" >&2
            exit 1
        fi
    else
        echo "deltabot-${BOT_NAME}.service installed but not started."
        echo "Start it later with: sudo systemctl enable --now deltabot-${BOT_NAME}.service"
    fi
fi

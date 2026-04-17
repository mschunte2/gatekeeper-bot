#!/bin/bash
# Interactive script to (re-)establish the BLE bond with the lock.
# Run this when send-command.sh reports "Bond may be lost".
#
# Prerequisites:
#   - Must run as root (or under sudo). This script drives bluetoothctl
#     pair / trust / remove, restarts the bluetooth service, and
#     kills stale bluepy-helper processes -- all of which need root.
#   - The lock must be put in pairing mode BEFORE running this script
#     (press and hold the "open" button for 3 seconds until the LED
#     turns orange). The pairing window lasts about 30 seconds.
#
# Running bot: this script stops the matching deltabot-<BOT_NAME>.service
# around the bluetoothctl + pairing steps so there is no contention
# with scheduled BLE commands, then restarts it on exit (even on
# failure / interrupt, via an EXIT trap). If the unit isn't active
# or doesn't exist, the stop/start dance is skipped.

# Fail fast if not root -- the bot runs as `pi` now, so accidental
# invocation without sudo is more likely than it used to be. Without
# this check, the script limps along but prompts for the sudo
# password several times during the bluetoothctl session, which
# breaks the 30-second pairing window more often than not.
if [ "$EUID" -ne 0 ]; then
    cat >&2 <<EOF
This script must be run with root privileges -- it drives the BlueZ
bond (bluetoothctl pair / trust), restarts the bluetooth service,
and resets the HCI adapter, all of which require CAP_NET_ADMIN.

Re-run as:

    sudo $0

(the bot service runs as an unprivileged user, but the *one-off*
bonding step does need root.)
EOF
    exit 5
fi

cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./lib/common.sh

load_env

# If the matching bot service is currently running, stop it for the
# duration of the pairing so we don't race it on the BLE adapter, and
# make sure it comes back up no matter how this script exits (clean
# exit, pairing failure, Ctrl-C). The unit name follows the new
# deltabot-<BOT_NAME>.service convention produced by
# install-systemd-unit.sh.
SERVICE_NAME="deltabot-${BOT_NAME:-gatekeeper}"
BOT_WAS_RUNNING=0
_restart_bot_on_exit() {
    if [ "$BOT_WAS_RUNNING" -eq 1 ]; then
        echo ""
        echo "Restarting $SERVICE_NAME..."
        if ! systemctl start "$SERVICE_NAME.service"; then
            echo "WARNING: failed to restart $SERVICE_NAME -- start it manually" >&2
        fi
    fi
}
if systemctl is-active --quiet "$SERVICE_NAME.service" 2>/dev/null; then
    echo "Stopping $SERVICE_NAME for the duration of pairing..."
    # Install the trap BEFORE the stop so that if stop fails and we
    # exit, we don't try to restart a service we never successfully
    # stopped (BOT_WAS_RUNNING stays 0 in that case).
    trap _restart_bot_on_exit EXIT
    if ! systemctl stop "$SERVICE_NAME.service"; then
        echo "ERROR: failed to stop $SERVICE_NAME; aborting pairing." >&2
        exit 1
    fi
    BOT_WAS_RUNNING=1
fi

resolve_adapter
acquire_ble_lock

if [ -z "$ADAPTER_BD" ]; then
    echo "✗ Could not read BD address from hci${HCI_IFACE}."
    exit 4
fi

echo "Lock MAC:    $LOCK_MAC"
echo "Adapter:     $ADAPTER_LABEL"
echo "Adapter BD:  $ADAPTER_BD"
echo ""
echo "Put the lock in pairing mode (3-second button press, orange LED)."
echo "Press Enter when ready..."
read -r

echo ""
echo "Scanning for KEY-BLE on hci${HCI_IFACE} (20 seconds)..."

# Kill any stale BLE processes first. Uses sudo explicitly because
# pair-lock is an interactive admin flow that may be invoked by a
# non-root user; cleanup_ble's auto-detection wouldn't help here.
sudo killall -9 bluepy-helper 2>/dev/null || true
sudo bluetoothctl scan off >/dev/null 2>&1 || true
sudo systemctl restart bluetooth
sleep 2

# Run scan + pair + trust in one bluetoothctl session.
RESULT=$(
(
echo "select $ADAPTER_BD"
echo "power on"
echo "remove $LOCK_MAC"
sleep 2
echo "scan le"
sleep 20
echo "scan off"
sleep 2
echo "pair $LOCK_MAC"
sleep 12
echo "trust $LOCK_MAC"
sleep 2
echo "info $LOCK_MAC"
sleep 1
echo "quit"
) | sudo bluetoothctl 2>&1
)

echo "$RESULT" | grep -E 'NEW.*KEY-BLE|Attempt|Paired|Bonded|Trusted|fail|error|success' -i

# Check if pairing succeeded.
if echo "$RESULT" | grep -q "Paired: yes"; then
    echo ""
    echo "Verifying with send-command.sh status..."
    # send-command.sh takes the same flock, so release ours first.
    release_ble_lock
    ./send-command.sh status
    rc=$?
    if [ $rc -eq 0 ]; then
        echo ""
        echo "✓ Bond established and lock is reachable."
    else
        echo ""
        echo "✗ Bond looks OK but lock command failed (rc=$rc)."
        echo "  The lock may have exited pairing mode. Try again."
    fi
else
    echo ""
    echo "✗ Pairing failed. Check that:"
    echo "  - The lock's LED was orange (pairing mode) during the scan."
    echo "  - The Pi is within ~5 metres of the lock."
    echo "  - No phone has the Eqiva app connected to the lock."
    echo "  Then run this script again."
fi

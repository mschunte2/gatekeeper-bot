#!/bin/bash
# Interactive script to (re-)establish the BLE bond with the lock.
# Run this when send-command.sh reports "Bond may be lost".
#
# Prerequisites:
#   - The lock must be put in pairing mode BEFORE running this script
#     (press and hold the "open" button for 3 seconds until the LED
#     turns orange). The pairing window lasts about 30 seconds.
#   - The bot must not be operating the adapter. This script takes
#     the same BLE flock as send-command.sh and exits 3 on collision.

cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./lib/common.sh

load_env
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

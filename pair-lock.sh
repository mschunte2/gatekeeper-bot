#!/bin/bash
# Interactive script to (re-)establish the BLE bond with the lock.
# Run this when send-command.sh reports "Bond may be lost".
#
# Prerequisites:
#   - The lock must be put in pairing mode BEFORE running this script
#     (press and hold the "open" button for 3 seconds until the LED
#     turns orange). The pairing window lasts about 30 seconds.

cd "$(dirname "$0")"
if [ ! -f .env ]; then
    echo "Please create a .env configuration file first (see .env.example)."
    exit 1
fi
set -a; source ./.env; set +a

# --- adapter resolution (same logic as send-command.sh) --------------------

if [ -n "$ADAPTER_MAC" ]; then
    HCI_IFACE=$(hciconfig -a 2>/dev/null \
        | grep -B1 "$ADAPTER_MAC" \
        | grep -oP 'hci\K\d+' \
        | head -1)
    if [ -z "$HCI_IFACE" ]; then
        echo "✗ BLE adapter $ADAPTER_MAC not found. Is the dongle plugged in?"
        exit 4
    fi
    ADAPTER_LABEL="$ADAPTER_MAC (hci${HCI_IFACE})"
elif [ -n "$HCI_IFACE" ]; then
    ADAPTER_LABEL="hci${HCI_IFACE}"
else
    HCI_IFACE=$(hciconfig -a 2>/dev/null \
        | grep -B1 "Bus: UART" \
        | grep -oP 'hci\K\d+' \
        | head -1)
    if [ -z "$HCI_IFACE" ]; then
        echo "✗ No built-in BLE adapter found."
        exit 4
    fi
    ADAPTER_LABEL="built-in UART (hci${HCI_IFACE})"
fi

# Resolve the adapter's BD address for bluetoothctl select.
ADAPTER_BD=$(hciconfig "hci${HCI_IFACE}" 2>/dev/null \
    | grep -oE '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}' \
    | head -1)
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

# Kill any stale BLE processes first.
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

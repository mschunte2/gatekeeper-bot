#!/bin/bash
# Issue a single command (status|lock|unlock|open) to the lock.
# All knobs come from .env; see .env.example.
#
# Robustness:
#   - Adapter resolution: ADAPTER_MAC -> hciN at runtime (stable across
#     reboots even if HCI numbering shifts). Falls back to the built-in
#     UART adapter if ADAPTER_MAC is unset.
#   - flock: only one BLE operation at a time per adapter (non-blocking).
#     Concurrent callers get "BLE adapter busy" immediately. Safe for
#     two bots sharing the same adapter.
#   - Pre-flight cleanup: kill stale bluepy-helper, stop any active
#     bluetoothd scan, reset the HCI adapter.
#   - Hard timeout: shell-level timeout wraps keyblepy so a stuck
#     process can't hang forever.
#   - Automatic retry: if the first attempt fails (e.g. bond evicted),
#     retry once with SEC_LEVEL=low (unbonded fallback, ~45s).
#
# Exit codes:
#   0  success
#   2  both attempts failed (lock unreachable or command rejected)
#   3  BLE adapter busy (another operation in progress)
#   4  BLE adapter not found

cd "$(dirname "$0")"
if [ ! -f .env ]; then
    echo "Please create a .env configuration file first (see .env.example)." >&2
    exit 1
fi
set -a; source ./.env; set +a
source ./venv/bin/activate

COMMAND="$1"
HARD_TIMEOUT=$(( ${TIMEOUT:-90} + 10 ))

# --- adapter resolution ----------------------------------------------------

resolve_adapter() {
    if [ -n "$ADAPTER_MAC" ]; then
        HCI_IFACE=$(hciconfig -a 2>/dev/null \
            | grep -B1 "$ADAPTER_MAC" \
            | grep -oP 'hci\K\d+' \
            | head -1)
        if [ -z "$HCI_IFACE" ]; then
            echo "BLE adapter $ADAPTER_MAC not found. Is the dongle plugged in?" >&2
            exit 4
        fi
    elif [ -z "$HCI_IFACE" ]; then
        # Default: built-in (UART) adapter.
        HCI_IFACE=$(hciconfig -a 2>/dev/null \
            | grep -B1 "Bus: UART" \
            | grep -oP 'hci\K\d+' \
            | head -1)
        if [ -z "$HCI_IFACE" ]; then
            echo "No built-in BLE adapter found." >&2
            exit 4
        fi
    fi
}

resolve_adapter

# --- serialize via flock ---------------------------------------------------

LOCK_FILE="/tmp/ble-hci${HCI_IFACE}.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "BLE adapter busy (another operation in progress)" >&2
    exit 3
fi

# --- helpers ---------------------------------------------------------------

cleanup_ble() {
    killall -9 bluepy-helper 2>/dev/null || true
    bluetoothctl scan off >/dev/null 2>&1 || true
    hciconfig "hci${HCI_IFACE}" reset >/dev/null 2>&1 || true
    sleep 1
}

run_keyble() {
    local sec="$1"
    timeout "$HARD_TIMEOUT" python3 -u ./keyblepy/keyble.py \
        --device "$LOCK_MAC" --user-id "$USER_ID" --user-key "$USER_KEY" \
        --iface "$HCI_IFACE" --sec-level "$sec" \
        --connect-timeout "$CONNECT_TIMEOUT" --timeout "$TIMEOUT" \
        --"$COMMAND"
}

# --- attempt 1: configured SEC_LEVEL (fast bonded path) --------------------

cleanup_ble
if run_keyble "${SEC_LEVEL:-medium}"; then
    exit 0
fi

# --- attempt 2: SEC_LEVEL=low (unbonded fallback) --------------------------

echo "⚠ Bond may be lost — retrying without pairing (slow). Run pair-lock.sh to re-establish." >&2
cleanup_ble
if run_keyble low; then
    exit 0
fi

echo "Both attempts failed." >&2
exit 2

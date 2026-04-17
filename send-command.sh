#!/bin/bash
# Issue a single command (status|lock|unlock|open) to the lock.
# All knobs come from .env; see .env.example.
#
# Robustness (most logic lives in lib/common.sh):
#   - Adapter resolution: ADAPTER_MAC -> hciN at runtime (stable
#     across reboots even if HCI numbering shifts). Falls back to the
#     built-in UART adapter if ADAPTER_MAC is unset.
#   - flock: only one BLE operation at a time per adapter
#     (non-blocking). Concurrent callers get "BLE adapter busy"
#     immediately. Lock file is world-writable so any user that can
#     invoke this script can acquire it.
#   - Pre-flight cleanup (root only): kill stale bluepy-helper, stop
#     any active bluetoothd scan, reset the HCI adapter. Silent
#     no-op when run as non-root; rerun with sudo if the adapter
#     wedges.
#   - Hard timeout: shell-level timeout wraps keyblepy so a stuck
#     process can't hang forever.
#   - Automatic retry: if the first attempt fails (e.g. bond
#     evicted), retry once with SEC_LEVEL=low (unbonded fallback,
#     ~45s).
#
# Exit codes:
#   0  success
#   2  both attempts failed (lock unreachable or command rejected)
#   3  BLE adapter busy (another operation in progress)
#   4  BLE adapter not found

cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./lib/common.sh

load_env
activate_venv
resolve_adapter
acquire_ble_lock

COMMAND="$1"
HARD_TIMEOUT=$(( ${TIMEOUT:-90} + 10 ))

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

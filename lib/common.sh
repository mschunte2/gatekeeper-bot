#!/bin/bash
# Shared setup for gatekeeper-bot shell scripts. Sourced (not
# executed) by send-command.sh, pair-lock.sh, register-user.sh.
#
# Contract: the caller must `cd "$(dirname "$0")"` before sourcing
# this file, so `./.env` and `./venv/bin/activate` resolve correctly.
#
# All functions are side-effect-free at source time and safe to call
# independently. Typical order:
#     load_env
#     activate_venv        # scripts that need Python
#     resolve_adapter
#     acquire_ble_lock
#     ... do BLE work ...
#     (optional) release_ble_lock   # before invoking another script
#                                   # that takes the same flock

is_root() { [ "$EUID" -eq 0 ]; }

# --- env + venv ------------------------------------------------------------

load_env() {
    if [ ! -f .env ]; then
        echo "Please create a .env configuration file first (see .env.example)." >&2
        exit 1
    fi
    set -a
    # shellcheck disable=SC1091
    source ./.env
    set +a
}

activate_venv() {
    # shellcheck disable=SC1091
    source ./venv/bin/activate
}

# --- adapter resolution ----------------------------------------------------
#
# Sets: HCI_IFACE, ADAPTER_LABEL, ADAPTER_BD, BLE_LOCK_FILE.
# Preference order:
#   1. $ADAPTER_MAC from .env (stable across reboots)
#   2. Built-in UART adapter (fallback when ADAPTER_MAC is unset)
# Exits 4 if no usable adapter is found.

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
        ADAPTER_LABEL="$ADAPTER_MAC (hci${HCI_IFACE})"
    else
        HCI_IFACE=$(hciconfig -a 2>/dev/null \
            | grep -B1 "Bus: UART" \
            | grep -oP 'hci\K\d+' \
            | head -1)
        if [ -z "$HCI_IFACE" ]; then
            echo "No built-in BLE adapter found." >&2
            exit 4
        fi
        ADAPTER_LABEL="built-in UART (hci${HCI_IFACE})"
    fi

    # BD address of the adapter -- bluetoothctl's `select` needs it.
    # Empty result is not fatal here; pair-lock.sh checks it explicitly.
    ADAPTER_BD=$(hciconfig "hci${HCI_IFACE}" 2>/dev/null \
        | grep -oE '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}' \
        | head -1)

    BLE_LOCK_FILE="/tmp/ble-hci${HCI_IFACE}.lock"
    export HCI_IFACE ADAPTER_LABEL ADAPTER_BD BLE_LOCK_FILE
}

# --- flock serialization ---------------------------------------------------
#
# The lock file is a rendezvous for flock(2), shared across users
# (e.g. bot running as `pi` + manual `sudo send-command.sh status`
# for recovery). Created 0666 so any invoker can open it for write;
# a stale 0644 file left by an older version is upgraded when we own
# it. If a root-owned 0644 file remains and a non-root call hits it,
# remove it once with `sudo rm /tmp/ble-hci*.lock` and rerun.

_ensure_lock_file() {
    if [ ! -e "$BLE_LOCK_FILE" ]; then
        (umask 0; : > "$BLE_LOCK_FILE")
    fi
    chmod 0666 "$BLE_LOCK_FILE" 2>/dev/null || true
}

# Acquire the flock on fd 9 (non-blocking). Exit 3 on collision.
acquire_ble_lock() {
    _ensure_lock_file
    exec 9>"$BLE_LOCK_FILE"
    if ! flock -n 9; then
        echo "BLE adapter busy (likely the bot is running)." >&2
        echo "Stop the bot (e.g. sudo systemctl stop deltabot-hoftor)" >&2
        echo "or wait for the current operation to finish, then retry." >&2
        exit 3
    fi
}

# Drop the flock (close fd 9). Needed before invoking another script
# that itself acquires the same lock -- e.g. pair-lock.sh running
# ./send-command.sh for verification.
release_ble_lock() {
    exec 9>&-
}

# --- BLE state cleanup -----------------------------------------------------
#
# Runs between attempts to un-wedge the adapter. Needs root (kills
# root-owned bluepy-helper processes, resets the HCI device). Silent
# no-op when run as non-root, so a non-root caller doesn't spam
# failing syscalls; if the adapter actually wedges in that mode, the
# user runs the script once as root to recover.

cleanup_ble() {
    if ! is_root; then
        return 0
    fi
    killall -9 bluepy-helper 2>/dev/null || true
    bluetoothctl scan off >/dev/null 2>&1 || true
    hciconfig "hci${HCI_IFACE}" reset >/dev/null 2>&1 || true
    sleep 1
}

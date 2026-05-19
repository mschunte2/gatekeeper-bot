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
    # Sourcing .env will overwrite any ADAPTER_MAC the caller passed
    # in (because plain assignments in .env beat exported env vars).
    # That broke the link-quality probe and ad-hoc pair-lock runs
    # that wanted to target the built-in adapter. Honour an explicit
    # ADAPTER_MAC_FORCE override (set even to empty == "use UART"),
    # restored after the source. Production callers don't set this,
    # so .env continues to win for them.
    local _force_set=0 _force_val=""
    if [ "${ADAPTER_MAC_FORCE+x}" = "x" ]; then
        _force_set=1
        _force_val="$ADAPTER_MAC_FORCE"
    fi
    set -a
    # shellcheck disable=SC1091
    source ./.env
    set +a
    if [ "$_force_set" = "1" ]; then
        ADAPTER_MAC="$_force_val"
    fi
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
    if ! command -v hciconfig >/dev/null 2>&1; then
        echo "hciconfig not found (package bluez-tools); cannot resolve BLE adapter." >&2
        exit 4
    fi
    if [ -n "$ADAPTER_MAC" ]; then
        HCI_IFACE=$(hciconfig -a 2>/dev/null \
            | grep -B1 "$ADAPTER_MAC" \
            | grep -oP 'hci\K\d+' \
            | head -1)
        if ! [[ "$HCI_IFACE" =~ ^[0-9]+$ ]]; then
            echo "BLE adapter $ADAPTER_MAC not found. Is the dongle plugged in?" >&2
            exit 4
        fi
        ADAPTER_LABEL="$ADAPTER_MAC (hci${HCI_IFACE})"
    else
        HCI_IFACE=$(hciconfig -a 2>/dev/null \
            | grep -B1 "Bus: UART" \
            | grep -oP 'hci\K\d+' \
            | head -1)
        if ! [[ "$HCI_IFACE" =~ ^[0-9]+$ ]]; then
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

    # Put the lock file inside a non-sticky subdirectory of /tmp.
    # Modern Debian ships `fs.protected_regular=2`, which blocks
    # open-for-write on files in sticky world-writable directories
    # unless the opener is the file's owner -- so the bot's
    # pi-owned lock in bare /tmp would shut root (e.g.
    # `sudo pair-lock.sh`) out entirely. A non-sticky subdir lets
    # both users share the rendezvous. The dir is recreated on
    # every boot (tmpfs semantics on /tmp), so no cleanup is needed.
    BLE_LOCK_DIR="/tmp/gatekeeper-ble"
    BLE_LOCK_FILE="$BLE_LOCK_DIR/hci${HCI_IFACE}.lock"
    export HCI_IFACE ADAPTER_LABEL ADAPTER_BD BLE_LOCK_DIR BLE_LOCK_FILE
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
    # Create the per-host rendezvous dir on first use. Mode 0777 (no
    # sticky bit) is deliberate -- see resolve_adapter() for why: the
    # fs.protected_regular kernel check only blocks cross-user writes
    # in sticky world-writable directories.
    if [ ! -d "$BLE_LOCK_DIR" ]; then
        mkdir -p "$BLE_LOCK_DIR" 2>/dev/null || true
    fi
    chmod 0777 "$BLE_LOCK_DIR" 2>/dev/null || true
    if [ ! -e "$BLE_LOCK_FILE" ]; then
        (umask 0; : > "$BLE_LOCK_FILE")
    fi
    chmod 0666 "$BLE_LOCK_FILE" 2>/dev/null || true
}

# Acquire the flock on fd 9 with a 20-second bounded wait. A real
# BLE operation takes ~2-8 s over an existing bond, so 20 s is
# enough to absorb a single concurrent request (the common case in
# the two-bot deployment: both locks receive a tap at nearly the
# same moment). Only genuine contention beyond that window surfaces
# as exit 3 -- callers / apps treat that as a "busy, try again"
# signal rather than a hard error.
#
# BLE_LOCK_WAIT_SECONDS is overridable (useful for interactive admin
# scripts like pair-lock.sh that may want to fail fast).
: "${BLE_LOCK_WAIT_SECONDS:=20}"
acquire_ble_lock() {
    _ensure_lock_file
    exec 9>"$BLE_LOCK_FILE"
    if ! flock -w "$BLE_LOCK_WAIT_SECONDS" 9; then
        echo "BLE adapter busy (waited ${BLE_LOCK_WAIT_SECONDS}s for another operation to finish)." >&2
        echo "Retry in a few seconds." >&2
        exit 3
    fi
}

# Drop the flock (close fd 9). Needed before invoking another script
# that itself acquires the same lock -- e.g. pair-lock.sh running
# ./send-command.sh for verification.
release_ble_lock() {
    exec 9>&-
}

# --- per-lock post-op cooldown --------------------------------------------
#
# Two empirically-observed blackout bands after a successful BLE op:
#
#   1. Rapid-retap (~0-1 s post-success): 5/5 observed retaps within
#      1 s of the prior success failed (rc=2 on both paired and low-sec
#      attempts). Likely cause: the lock's BLE radio has not fully
#      released the prior connection state when a new connect arrives.
#      Floor: _COOLDOWN_RAPID = 5 s.
#
#   2. Post-op blackout (~30-60 s post-success): the Eqiva auto-relock
#      motor fires around T+30 s and the lock cannot complete a fresh
#      encrypt handshake within send-command.sh's 25 s x 2 budget until
#      ~T+60 s. 9/9 in-window attempts failed historically.
#      Floor: _COOLDOWN_LOW = 30 s, ceiling: COOLDOWN_AFTER_SECONDS = 60 s.
#
# In both bands the chat-visible "Bond may be lost" warning surfaces
# despite the bond being intact. cooldown_check sleeps just enough to
# push the next attempt past whichever band applies. Sweet spot is
# 5 s <= elapsed < 30 s (and >= 60 s): proceeds immediately.
#
# Per-LOCK_MAC state and command-agnostic (any successful op refreshes
# the state file; any subsequent op respects it). Set
# COOLDOWN_AFTER_SECONDS=0 in .env to disable the gate entirely.

: "${COOLDOWN_AFTER_SECONDS:=60}"
_COOLDOWN_RAPID=5
_COOLDOWN_LOW=30

cooldown_path() {
    echo "$BLE_LOCK_DIR/lock-${LOCK_MAC}.lastop"
}

cooldown_check() {
    [ "${COOLDOWN_AFTER_SECONDS:-60}" -gt 0 ] || return 0
    local f
    f=$(cooldown_path)
    [ -r "$f" ] || return 0
    local last now elapsed wait
    last=$(cat "$f" 2>/dev/null) || return 0
    [ -n "$last" ] || return 0
    now=$(date +%s)
    elapsed=$(( now - last ))
    if [ "$elapsed" -lt "$_COOLDOWN_RAPID" ]; then
        wait=$(( _COOLDOWN_RAPID - elapsed ))
        echo ">>> COOLDOWN_WAIT secs=$wait"
        echo "⏳ Lock just finished an op (${elapsed}s ago); waiting ${wait}s before next." >&2
        sleep "$wait"
    elif [ "$elapsed" -ge "$_COOLDOWN_LOW" ] \
         && [ "$elapsed" -lt "$COOLDOWN_AFTER_SECONDS" ]; then
        wait=$(( COOLDOWN_AFTER_SECONDS - elapsed ))
        echo ">>> COOLDOWN_WAIT secs=$wait"
        echo "⏳ Lock is in post-op cooldown (${elapsed}s since last op); waiting ${wait}s." >&2
        sleep "$wait"
    fi
}

cooldown_mark_success() {
    [ "${COOLDOWN_AFTER_SECONDS:-60}" -gt 0 ] || return 0
    local f tmp
    f=$(cooldown_path)
    tmp="${f}.tmp.$$"
    (umask 0; date +%s > "$tmp") || return 0
    mv "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
    chmod 0666 "$f" 2>/dev/null || true
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

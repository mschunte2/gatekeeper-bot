#!/bin/bash
# BLE link-quality probe via send-command.sh status. Logs one CSV
# line per (adapter, lock) sample to $LOG_FILE. Designed to run on
# a 30-min systemd timer alongside the gatekeeper-bot services.
#
# Why not passive RSSI scan: Eqiva locks don't advertise when idle
# (battery conservation), so a scan never sees them. Instead we do
# a real read-only BLE connection (the same code path /status uses)
# and record success + duration + raw diagnostics. That measures
# what users actually experience.
#
# Each invocation runs ONE probe = (one bot dir) x (one adapter).
# The wrapping systemd unit calls this script once per (lock x
# adapter) combination. Flock serialization is automatic via
# common.sh inside send-command.sh -- this script never touches
# the flock itself.
#
# Output CSV (newline-terminated, comma-separated, last field
# base64-encoded stderr to keep one record per line):
#   ts_iso,antenna_label,adapter_label,lock_label,rc,duration_ms,stderr_b64
#
# antenna_label is a free-form tag (e.g. "internal", "ext-5dbi") so
# you can swap antennas without editing this script -- just bump
# the ANTENNA env var in the systemd unit.
#
# Usage:
#   ble-rssi-probe.sh <bot-dir> <adapter-label> <adapter-mac-or-empty> <lock-label>
#
# Environment:
#   ANTENNA       free-form tag for the current antenna (default: "unspecified")
#   LOG_FILE      default /home/pi/ble-probe/probe.log
#   DEBUG_DIR     default /home/pi/ble-probe/debug
#
# Examples:
#   ANTENNA=internal ./ble-rssi-probe.sh /home/pi/gatekeeper-hoftor usb 8A:88:4B:C2:9C:B9 hoftor
#   ANTENNA=ext-5dbi ./ble-rssi-probe.sh /home/pi/gatekeeper-km     usb 8A:88:4B:C2:9C:B9 km

set -u

if [ "$#" -ne 4 ]; then
    echo "Usage: $0 <bot-dir> <adapter-label> <adapter-mac-or-empty> <lock-label>" >&2
    exit 64
fi

BOT_DIR="$1"
ADAPTER_LABEL="$2"
ADAPTER_MAC_ARG="$3"
LOCK_LABEL="$4"
ANTENNA="${ANTENNA:-unspecified}"

LOG_FILE="${LOG_FILE:-/home/pi/ble-probe/probe.log}"
DEBUG_DIR="${DEBUG_DIR:-/home/pi/ble-probe/debug}"
mkdir -p "$DEBUG_DIR"

ts=$(date -Iseconds)
ts_safe="${ts//:/}"  # filename-safe
debug_file="${DEBUG_DIR}/${ts_safe}_${ADAPTER_LABEL}_${LOCK_LABEL}.txt"

cd "$BOT_DIR" || {
    echo "${ts},${ANTENNA},${ADAPTER_LABEL},${LOCK_LABEL},127,0,$(printf 'no bot dir: %s' "$BOT_DIR" | base64 -w0)" >> "$LOG_FILE"
    exit 0
}

# Override ADAPTER_MAC for this single invocation. Empty value is
# valid (forces common.sh's UART fallback = built-in adapter).
# Use ADAPTER_MAC_FORCE because plain ADAPTER_MAC gets clobbered
# when send-command.sh's load_env sources .env (assignments in a
# sourced file beat exported env vars).
export ADAPTER_MAC_FORCE="$ADAPTER_MAC_ARG"
# LOG_LEVEL doesn't affect send-command.sh itself (it's a bot env
# var), but we capture full stderr unconditionally below.

t0=$(date +%s%3N)
stderr_capture=$(./send-command.sh status 2>&1 >/dev/null)
rc=$?
t1=$(date +%s%3N)
duration_ms=$((t1 - t0))

# Full stderr to a per-sample file for later inspection (don't bloat
# the CSV; do keep a base64 snippet inline so the log is self-contained).
{
    echo "=== $ts ==="
    echo "antenna=$ANTENNA adapter=$ADAPTER_LABEL ($ADAPTER_MAC_ARG) lock=$LOCK_LABEL bot=$BOT_DIR"
    echo "rc=$rc duration_ms=$duration_ms"
    echo "--- stderr ---"
    echo "$stderr_capture"
} > "$debug_file"

# CSV: one line, base64-encoded stderr keeps it parseable.
stderr_b64=$(printf '%s' "$stderr_capture" | base64 -w0)
echo "${ts},${ANTENNA},${ADAPTER_LABEL},${LOCK_LABEL},${rc},${duration_ms},${stderr_b64}" >> "$LOG_FILE"

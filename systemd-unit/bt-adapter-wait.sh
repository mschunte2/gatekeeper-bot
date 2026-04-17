#!/bin/bash
# Recover USB Bluetooth adapters from firmware-load failure at boot.
#
# Realtek RTL8761 dongles (and similar) occasionally fail their
# initial firmware download with "tx timeout" / "-110" errors -- most
# visible on Raspberry Pi Zero-class USB ports. The adapter presents
# itself to the kernel but hciconfig reports BD Address all-zeros
# and the device stays DOWN. bluetoothd then sees no controller,
# every BLE operation fails, and recovery before this script was a
# physical unplug/replug.
#
# At boot, if any USB-bus hciN reports 00:00:00:00:00:00 within the
# grace window, we reload the btusb kernel module up to MAX_ATTEMPTS
# times (with a short delay between each) to give the firmware
# download another try. If that still doesn't wake up the adapter,
# we log a warning and exit non-zero -- bluetooth.service still
# starts (the unit's SuccessExitStatus covers this), but the
# operator gets a clear journal line and can replug manually.
#
# Exits 0 if:
#   * no USB BT adapter is present (built-in only), or
#   * all present USB BT adapters have a non-zero BD address.
# Exits 1 if a USB BT adapter is still wedged after all retries.
set -u
PATH=/usr/sbin:/sbin:/usr/bin:/bin

MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
SETTLE_SECONDS=${SETTLE_SECONDS:-8}
INITIAL_GRACE_SECONDS=${INITIAL_GRACE_SECONDS:-3}

log() {
    # Keep messages on a single line so journalctl groups them.
    echo "bt-adapter-wait: $*" >&2
}

# Returns 0 if every USB-bus hciN has a non-zero BD address.
# Prints a summary of wedged adapters to stderr. Stable under
# missing tools (grep / hciconfig both expected to be present).
check_usb_hci_ok() {
    local wedged=0
    local hci name bus bd
    while read -r hci; do
        name=${hci%:}
        bus=$(hciconfig "$name" 2>/dev/null \
              | awk -F'Bus: ' '/Bus:/ {print $2; exit}')
        [ "$bus" = "USB" ] || continue
        bd=$(hciconfig "$name" 2>/dev/null \
             | awk '/BD Address/ {print $3; exit}')
        if [ -z "$bd" ] || [ "$bd" = "00:00:00:00:00:00" ]; then
            log "$name wedged (BD=${bd:-none})"
            wedged=1
        fi
    done < <(hciconfig 2>/dev/null | awk '/^hci[0-9]+:/ {print $1}')
    return $wedged
}

# Give the kernel a moment to enumerate USB devices -- service runs
# before bluetooth.service, but USB init on a Pi can lag by a couple
# of seconds on cold boot. Without this, we miss a just-arriving
# adapter entirely and exit 0 ("no USB adapter") erroneously.
sleep "$INITIAL_GRACE_SECONDS"

if check_usb_hci_ok; then
    log "USB BT adapter(s) ready at boot; no recovery needed"
    exit 0
fi

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    log "USB BT adapter wedged; reload attempt $attempt/$MAX_ATTEMPTS"
    modprobe -r btusb 2>/dev/null || true
    sleep 2
    modprobe btusb || log "modprobe btusb failed"
    sleep "$SETTLE_SECONDS"
    if check_usb_hci_ok; then
        log "USB BT adapter recovered after $attempt reload(s)"
        exit 0
    fi
done

log "USB BT adapter still wedged after $MAX_ATTEMPTS attempts -- physical replug required"
exit 1

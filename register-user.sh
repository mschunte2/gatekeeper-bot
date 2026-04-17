#!/bin/bash
# Register a new user on the lock. All knobs come from .env.
# Takes the same BLE flock as send-command.sh and pair-lock.sh, so
# running this while the bot is active exits 3 rather than colliding
# at the BLE layer.
set -e
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./lib/common.sh

load_env
activate_venv
resolve_adapter
acquire_ble_lock

cd keyblepy
./keyble.py --device "$LOCK_MAC" \
    --user-name "$USER_NAME" \
    --qrdata "$QR_DATA" \
    --iface "$HCI_IFACE" \
    --register \
    --user-id "$USER_ID" --user-key "$USER_KEY"

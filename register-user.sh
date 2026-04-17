#!/bin/bash
# Register a new user on the lock.
#
# Generates a fresh random 16-byte user-key locally and asks the lock
# to auto-assign a free slot via the USERID_AUTO_ASSIGN sentinel
# (see keyblepy/messages.py). On success, prints the resulting USER_ID
# and USER_KEY for the admin to copy into .env -- the script does NOT
# touch .env itself.
#
# Why not read USER_ID/USER_KEY from .env: registration *creates*
# credentials; .env *consumes* them (the bot reads .env to authenticate
# every status/lock/unlock call). Conflating the two roles led to the
# "I forgot to rotate USER_KEY before registering, so I just clobbered
# my own working key" foot-gun.
#
# Takes the same BLE flock as send-command.sh and pair-lock.sh, so
# running this while the bot is active exits 3 rather than colliding
# at the BLE layer.
set -e
cd "$(dirname "$0")"
BOT_DIR=$(pwd)
# shellcheck disable=SC1091
source ./lib/common.sh

load_env
activate_venv
resolve_adapter
acquire_ble_lock

NEW_USER_KEY=$(openssl rand -hex 16)
LOG_FILE=$(mktemp -t keyble-register.XXXXXX.log)
echo "Logging full keyblepy output to: $LOG_FILE" >&2

cd keyblepy
# `tee` so verbose output is preserved AND we can grep stdout for the
# structured REGISTRATION_SUCCESS line that ui_pair emits. set +e so
# a non-zero rc from keyble.py doesn't kill us before we can format
# the failure message.
set +e
./keyble.py --device "$LOCK_MAC" \
    --user-name "${USER_NAME:-unnamed}" \
    --qrdata "$QR_DATA" \
    --iface "$HCI_IFACE" \
    --connect-timeout 30 --timeout 90 \
    --register --user-key "$NEW_USER_KEY" --verbose 2>&1 | tee "$LOG_FILE"
rc=${PIPESTATUS[0]}
set -e

# Look for the machine-readable summary line. ui_pair emits exactly:
#     REGISTRATION_SUCCESS user_id=N user_key=HEX(32)
SUCCESS_LINE=$(grep -E '^REGISTRATION_SUCCESS user_id=[0-9]+ user_key=[0-9a-fA-F]{32}$' \
    "$LOG_FILE" | tail -1 || true)

if [[ -z "$SUCCESS_LINE" ]]; then
    echo >&2
    echo "Registration FAILED (keyble.py exit=$rc, no REGISTRATION_SUCCESS line)." >&2
    echo "Full log: $LOG_FILE" >&2
    echo "Common causes:" >&2
    echo "  - Lock not in pairing mode (orange LED must be blinking)" >&2
    echo "  - BLE adapter busy (another keyble.py / bot still running)" >&2
    echo "  - Wrong QR_DATA in .env" >&2
    exit 1
fi

ASSIGNED_ID=$(echo "$SUCCESS_LINE" | sed -E 's/.*user_id=([0-9]+).*/\1/')
ASSIGNED_KEY=$(echo "$SUCCESS_LINE" | sed -E 's/.*user_key=([0-9a-fA-F]+).*/\1/')

cat <<EOF

============================================================
Registration successful.

To activate, set the following two lines in
$BOT_DIR/.env

    USER_ID=$ASSIGNED_ID
    USER_KEY=$ASSIGNED_KEY

Then restart the bot service, e.g.
    sudo systemctl restart deltabot-${BOT_NAME:-<bot>}.service

Full log saved at: $LOG_FILE
============================================================
EOF

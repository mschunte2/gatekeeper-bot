#!/bin/bash
# Issue a single command (status|lock|unlock|open) to the lock.
# All knobs come from .env; see .env.example.
set -e
cd "$(dirname "$0")"
set -a; source ./.env; set +a
source ./venv/bin/activate

python3 -u ./keyblepy/keyble.py \
    --device "$LOCK_MAC" --user-id "$USER_ID" --user-key "$USER_KEY" \
    --iface "$HCI_IFACE" --sec-level "$SEC_LEVEL" \
    --connect-timeout "$CONNECT_TIMEOUT" --timeout "$TIMEOUT" \
    --"$1"
# Append --verbose above for debug logging.

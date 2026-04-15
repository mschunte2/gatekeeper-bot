#!/bin/bash
# Register a new user on the lock. All knobs come from .env.
set -e
cd "$(dirname "$0")"
set -a; source ./.env; set +a
source ./venv/bin/activate

cd keyblepy
./keyble.py --device "$LOCK_MAC" \
    --user-name "$USER_NAME" \
    --qrdata "$QR_DATA" \
    --register \
    --user-id "$USER_ID" --user-key "$USER_KEY"

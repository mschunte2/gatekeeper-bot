#!/bin/bash
# Register a new user on the lock. All knobs come from .env.
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
    echo "Please create a .env configuration file first (see .env.example)."
    exit 1
fi
set -a; source ./.env; set +a
source ./venv/bin/activate

cd keyblepy
./keyble.py --device "$LOCK_MAC" \
    --user-name "$USER_NAME" \
    --qrdata "$QR_DATA" \
    --register \
    --user-id "$USER_ID" --user-key "$USER_KEY"

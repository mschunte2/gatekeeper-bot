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
# No log file is ever written to disk: keyblepy's --verbose stream
# echoes the new --user-key on the command-line, which makes any
# captured log a credential leak. Output is held in shell memory only;
# in -v mode it's also streamed live to the controlling TTY, and on
# failure the captured output is dumped to stderr for debugging.
#
# Takes the same BLE flock as send-command.sh and pair-lock.sh, so
# running this while the bot is active exits 3 rather than colliding
# at the BLE layer.
set -e

# --- arg parsing -----------------------------------------------------------
VERBOSE=0
usage() {
    cat <<EOF
Usage: $(basename "$0") [-v|--verbose] [-h|--help]

  -v, --verbose   Stream keyblepy's debug output live to the terminal
                  while it runs. Without this flag, only the framed
                  success/failure summary is printed; on failure, the
                  captured output is then dumped to stderr.
  -h, --help      Show this help.

No persistent log file is written. The captured output contains the
generated --user-key in the keyblepy invocation line, so use a shell
redirect (e.g. \`./register-user.sh -v 2>&1 | tee mylog\`) only when
you knowingly want a copy and can manage its lifetime.
EOF
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")"
BOT_DIR=$(pwd)
# shellcheck disable=SC1091
source ./lib/common.sh

load_env
activate_venv
resolve_adapter
acquire_ble_lock

NEW_USER_KEY=$(openssl rand -hex 16)

cd keyblepy
# We always pass --verbose to keyble.py so the FULL stream is available
# to the wrapper -- both for grepping the REGISTRATION_SUCCESS line
# and for surfacing diagnostics on failure. Whether the user *sees*
# that stream live depends on -v on the wrapper:
#
#   -v     -> tee to /dev/tty as it happens (and capture for parsing)
#   no -v  -> capture only; on failure, dump captured output to stderr
#
# Either way the stream lives only in shell memory and is gone when
# the script exits. set +e so a non-zero rc from keyble.py doesn't
# kill us before we can format the failure message.
set +e
if [[ $VERBOSE -eq 1 ]]; then
    OUTPUT=$(./keyble.py --device "$LOCK_MAC" \
        --user-name "${USER_NAME:-unnamed}" \
        --qrdata "$QR_DATA" \
        --iface "$HCI_IFACE" \
        --connect-timeout 30 --timeout 90 \
        --register --user-key "$NEW_USER_KEY" --verbose 2>&1 \
        | tee /dev/tty; exit "${PIPESTATUS[0]}")
    rc=$?
else
    OUTPUT=$(./keyble.py --device "$LOCK_MAC" \
        --user-name "${USER_NAME:-unnamed}" \
        --qrdata "$QR_DATA" \
        --iface "$HCI_IFACE" \
        --connect-timeout 30 --timeout 90 \
        --register --user-key "$NEW_USER_KEY" --verbose 2>&1)
    rc=$?
fi
set -e

# Look for the machine-readable summary line. ui_pair emits exactly:
#     REGISTRATION_SUCCESS user_id=N user_key=HEX(32)
SUCCESS_LINE=$(printf '%s\n' "$OUTPUT" \
    | grep -E '^REGISTRATION_SUCCESS user_id=[0-9]+ user_key=[0-9a-fA-F]{32}$' \
    | tail -1 || true)

if [[ -z "$SUCCESS_LINE" ]]; then
    echo >&2
    echo "Registration FAILED (keyble.py exit=$rc, no REGISTRATION_SUCCESS line)." >&2
    if [[ $VERBOSE -eq 0 ]]; then
        echo "--- captured keyblepy output (re-run with -v to see live) ---" >&2
        printf '%s\n' "$OUTPUT" >&2
        echo "-------------------------------------------------------------" >&2
    fi
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
============================================================
EOF

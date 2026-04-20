#!/bin/bash
# Start the deltabot service. Loads .env so child processes inherit
# secrets/config (delta-door-bot.py reads ALLOWED_CHATS from os.environ).
set -e
cd "$(dirname "$0")"
set -a; source ./.env; set +a
source ./venv/bin/activate
exec python3 ./delta-door-bot.py --logging "${LOG_LEVEL:-info}" serve

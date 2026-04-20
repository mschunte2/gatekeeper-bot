# CLAUDE.md — Project context for LLM sessions

## What this project is

Gatekeeper-bot is a Raspberry Pi-hosted bridge that operates an eQ-3
Eqiva Smart Lock from Delta Chat. A Python daemon (`delta-door-bot.py`)
listens on a Delta Chat account and translates text commands and webxdc
app interactions into BLE lock operations via `send-command.sh` →
`keyblepy` → BlueZ → the lock.

## Architecture

```
DeltaChat user
  ├── text commands (/lock, /unlock, /status, /apps, /id)
  └── webxdc app (gatekeeper; quick-lock is built-but-disabled)
        │
        ▼
delta-door-bot.py (deltabot-cli + deltachat2)
        │
        ▼
send-command.sh (flock serialization, adapter resolution, retry + fallback)
        │
        ▼
keyblepy/keyble.py (bluepy BLE → Eqiva lock)
```

### Two control paths, one backend

- **Text path**: user types `/lock` → bot calls `send-command.sh lock`
  → echoes stdout/stderr to chat → pushes state to all app instances.
- **App path**: user taps a webxdc button → bot receives
  `WEBXDC_STATUS_UPDATE` → calls `send-command.sh` → pushes state
  silently to all app instances + shows audit line in chat.

Both paths converge on `send-command.sh`. The bot does NOT retry
internally; all retry/fallback logic lives in the shell script.

## Key design decisions and rationale

### Permission model
`ALLOWED_CHATS` is the single allow-list for both text and app paths.
`/id` is the only exempt command (needed for setup discovery). There is
no separate `APP_ENABLED_CHATS` — we considered it but simplified to
one list.

### Adapter identification
`ADAPTER_MAC` in `.env` identifies the BLE adapter by BD address.
Resolved to `hciN` at runtime so HCI renumbering across reboots is
harmless. Falls back to the built-in UART adapter if unset. We removed
`HCI_IFACE` from `.env` because numeric indices are unstable.

### Retry and fallback in send-command.sh (not the bot)
`send-command.sh` handles all BLE robustness: kill stale
`bluepy-helper`, reset adapter, attempt with configured `SEC_LEVEL`,
retry with `SEC_LEVEL=low` if the first attempt fails (bond may be
evicted). The bot was simplified to a single `subprocess.run` call.
Rationale: keeps the retry logic close to the BLE layer; works
identically whether triggered by the bot or run manually from the
shell; supports the two-bot-one-adapter scenario without the bots
knowing about each other.

### flock serialization
All three BLE shell scripts (`send-command.sh`, `pair-lock.sh`,
`register-user.sh`) acquire the same `flock -n` (non-blocking) on
`/tmp/ble-hci${HCI_IFACE}.lock` via `acquire_ble_lock` from
`lib/common.sh`. Only one BLE operation at a time per adapter.
Concurrent callers get exit code 3 ("busy") immediately with a
message pointing at the likely culprit (the bot). The lock file is
per-adapter (not per-bot) so two bots sharing one adapter serialize
correctly; it is created mode 0666 so any user with shell access to
the script can acquire it (e.g. bot as `pi`, manual `sudo
send-command.sh` for recovery). `pair-lock.sh` calls `release_ble_lock`
before invoking its verification `./send-command.sh status` so the
child can take the same flock.

### Webxdc replay protection
Text commands have a 60 s `msg.timestamp` age check. Webxdc button
presses historically had none (accepted gap), but were found to
replay stale taps after the bot reconnected from an offline period.
Apps now embed `ts: Math.floor(Date.now()/1000)` in the request
payload; `on_webxdc_update` drops commands with age > 45 s before
acking or firing BLE. Apps built before this feature have no `ts`
field -- the bot accepts those with a log line so older installed
instances still work until users run `/apps` to pick up the new
build (soft migration).

### Colour semantics in quick-lock app (disabled but still buildable)
Green = open, red = closed, orange = in-progress. REVERSED from the
original source slider images where green meant "locked"; recoloured
with ImageMagick to match this scheme. quick-lock opens in orange
(pending) and settles on red once the bot confirms the lock -- the
starting visual is an explicit "working on it" signal rather than a
claim about current state.

(quick-unlock was retired for safety -- a stray tap on a phone home
screen shouldn't be enough to open a door. One-tap lock is cheap
failure; one-tap unlock is costly failure.)

### Bot ack protocol
When the bot receives a webxdc command, it pushes `{payload: {ack: cmd}}`
to all app instances BEFORE running `send-command.sh`. Apps that care
(quick-lock) use this to transition from the starting visual to the
orange "in progress" visual at the right moment. Apps that don't care
(gatekeeper) ignore it. The ack is NOT a confirmation that the lock
operated — just that the bot received the command.

### _ready flag in quick-lock
On app open, `setUpdateListener` may deliver queued status updates
from previous sessions. Without guarding, these overwrite the
starting visual before the auto-lock fires. The `_ready` flag blocks
ack/response processing until the auto-command has been sent. Config
updates (door_name) are always allowed through.

### Audit line format
App-driven commands produce `{icon} {DOOR_NAME} {actor_name}` in the
originating chat (e.g. "🟢 Hoftor Matthias"). Icons are 🔒 for lock
and 🟢 for unlock/open -- chosen over 🔒/🔓 because the padlock pair
is visually near-identical at chat-line size on many rendering
stacks; padlock-vs-green-circle is unambiguous at a glance. Status
checks are silent (read-only, auto-requested on app open). Text
commands echo raw subprocess output instead (the user's own message
already identifies them).

Apps also render "Last update HH:MM" (24h local time) below the
status icon, filled from the `ts` field in the response payload.
The bot maintains `_last_state_ts` alongside `_last_known_state`,
updating both in `run_lock_command` and the startup status probe.

### App discovery and /apps as always-resend
`delta-door-bot.py` scans `apps/*.xdc` on each `/apps` call. No
hard-coded list. Dropping a new `<id>.xdc` into `apps/` is enough.

`/apps` **always sends** every discovered app and deletes the
chat's prior tracked msgid for that app via `delete_messages_for_
all` after a successful send. The chat ends up with exactly one
current copy per app. Rationale: Delta Chat doesn't backfill
attachments to new chat members, so "already installed" tracking
would leave late joiners without any app -- always-resend onboards
everyone currently in the chat.

Tracking shape: `chat_id → {app_id → msgid}` in
`~/.config/<BOT_NAME>/app_msgids.json`, used only to know which
prior msgid to delete. Send-first-then-delete so a failed delete
never leaves the chat with no app. There is no `/apps reset` --
the always-send model is self-healing.

**Retract**: any tracked `app_id` whose artifact no longer exists
under `apps/*.xdc` (e.g. moved to `apps-disabled/`, renamed,
removed) gets its old msgid deleted for all chat members and
dropped from tracking on the next `/apps`. This means "unpublish"
through the filesystem cleanly withdraws the app from every chat.

`apps-disabled/` holds apps we currently don't serve. Unlike the
earlier convention (only the built .xdc moved), the source tree
now moves alongside the artifact: e.g. `apps-disabled/quick-lock/`
+ `apps-disabled/quick-lock.xdc`. The source's `vite.config.mjs`
`outDir` is `"../../apps-disabled/"` so rebuilds land in the same
directory. To re-enable an app, move both the source dir and the
.xdc back under `apps/` and adjust `outDir` to `"../"`.

## Protocol: bot ↔ webxdc apps

All apps share one protocol. The `app` field is logged but does not
affect routing.

### App → bot
```json
{"payload": {"request": {"name": "Alice", "text": "lock",
                         "app": "gatekeeper", "ts": 1713600000}}}
```
`text` is whitelisted against `{lock, unlock, open, status}`. `ts`
is Unix seconds; commands older than 45 s are dropped (replay
protection). Missing `ts` is accepted with a log line (soft migration
for old installed apps).

### Bot → app (state)
```json
{"payload": {"response": {"name": "bot", "text": "locked",
                          "battery_low": false, "ts": 1713600000}}}
```
`text` is one of: `locked`, `unlocked`, `unknown`, `error`. `ts` is
the Unix seconds when the bot last confirmed that state. Apps render
it as "Last update HH:MM" (24h local time). `_push_state` broadcasts
to every app instance in every allowed chat, so a single state
change propagates everywhere.

### Bot → app (config)
```json
{"payload": {"config": {"door_name": "Hoftor"}}}
```
Pushed on `/apps`, on startup, and via opportunistic learning.

### Bot → app (ack)
```json
{"payload": {"ack": "lock"}}
```
Pushed immediately when a command is accepted, before the BLE
round-trip.

## File layout

```
delta-door-bot.py          main bot (deltachat2 + deltabot-cli)
send-command.sh            BLE wrapper (flock, adapter resolution, retry)
pair-lock.sh               interactive BLE bond guide
register-user.sh           one-time lock registration
start-gatekeeper-bot.sh    systemd entrypoint (sources .env, activates venv)
lib/common.sh              sourced helper: .env load, venv, adapter,
                           flock, cleanup (shared by the 3 BLE scripts)
.env                       secrets + config (gitignored)
.env.example               template
apps/
  gatekeeper.xdc           built artifact (tracked)
  gatekeeper/              full lock-control app source
apps-disabled/             apps we build but do NOT serve via /apps
  quick-lock.xdc           built artifact (tracked)
  quick-lock/              one-tap lock app source (outDir points here)
                           quick-unlock was retired entirely -- gitignored
                           to prevent accidental re-entry
keyblepy/                  BLE protocol implementation (git submodule)
systemd-unit/              service file
```

## Security model

- `.env` is the only file with secrets; it is gitignored.
- `send-command.sh` receives the command as argv[1] and passes it as
  `--$COMMAND` to keyblepy. The bot whitelists `text` from webxdc
  payloads against `{lock, unlock, open, status}` before calling the
  script. Defence-in-depth: `run_lock_command` also checks the
  whitelist.
- Webxdc payload fields (`name`, `app`) are sanitised via `_sanitize`
  (control chars stripped, length capped) before logging or echoing.
- `_push_state` and startup loops filter by `_is_allowed(chatid)` so
  chats removed from `ALLOWED_CHATS` stop receiving state pushes.
- `app_msgids.json` is written atomically (`*.tmp` + `os.replace`).
- App HTML/JS uses `textContent` (not `innerHTML`) for `door_name`.
- Never use `hcitool` for scanning or debugging — it conflicts with
  bluetoothd. Use `bluetoothctl` exclusively (goes through D-Bus).

## Known issues / accepted trade-offs

- `appdirs` (used for `user_config_dir`) is deprecated upstream in
  favour of `platformdirs`. Pre-existing; no urgency.
- `log_event` hook logs every raw deltachat event at INFO — very
  chatty in journalctl. Pre-existing.
- `bluepy` uses raw HCI sockets which can conflict with bluetoothd.
  `send-command.sh` mitigates this with `bluetoothctl scan off` +
  adapter reset before each operation. Long-term fix: migrate
  keyblepy to `bleak` (D-Bus based).
- The onboard BCM43430A1 adapter on the Pi Zero 2 W returns
  `setsockopt(BT_SECURITY): Invalid argument` when bluepy tries to
  set the security level. Use the USB adapter instead.

## Fixed bugs (history, for context)

- `keyblepy/encrypt.py` `compute_authentication_value` once appended
  an extra `pack('>H', padded_length)` to the final CCM A_0 block,
  growing it past 16 bytes and causing the auth tag to differ from
  the reference (oyooyo/keyble). The lock then rejected every
  `--register` attempt with `AnswerWithoutSecurity 0x81`. Fixed in
  keyblepy commit `bf26987` -- the final block now matches keyble.js
  exactly: `[Flags=1, Nonce(13), Counter=0,0]`. The encrypted command
  path was unaffected in practice (the lock is more lenient on the
  encrypted MAC) which is why Hoftor commands always worked while KM
  registration always failed before the fix.

## Lock signals

- The Eqiva Smart Lock confirms successful user registration with a
  short **beep** plus the orange LED **stops blinking**. The
  `register-user.sh` script also exits 0. Without that confirmation,
  the registration did not take, regardless of script return code.

## Two-bot setup (deployed)

Two gatekeeper-bot instances run on the Pi sharing one BLE adapter:
- `/home/pi/gatekeeper-hoftor/` → `deltabot-gatekeeper-hoftor.service`
  → Hoftor lock (LOCK_MAC `00:1a:22:1b:ae:ac`).
- `/home/pi/gatekeeper-km/` → `deltabot-gatekeeper-km.service` →
  Kulturmetzgerei lock (LOCK_MAC `00:1A:22:1B:AF:3E`).
- Each bot has its own `.env` with distinct `LOCK_MAC`, `BOT_NAME`,
  `USER_ID`, `USER_KEY`, `DOOR_NAME`, `ALLOWED_CHATS`.
- Both set the same `ADAPTER_MAC` → resolve to the same `hciN` →
  share the same flock file `/tmp/ble-hci<N>.lock` → operations
  serialize correctly, even under concurrent taps.
- `killall bluepy-helper` in cleanup is safe because flock guarantees
  no other bot's process is in-flight.
- `BOT_NAME` in `.env` drives `BotCli(BOT_NAME)`, which gives each
  bot its own config dir (e.g. `~/.config/gatekeeper-km/`) and its
  own Delta Chat account.
- Both units are `enabled` with `Restart=always` so they start on
  boot and recover from crashes automatically.

A third clone `/home/pi/gatekeeper-bot-original/` also exists --
kept as a clean reference tree for documentation lookups; has no
systemd unit and no `.env`.

## Build system

Apps are built with vite (**Node.js 22+** required -- newer vite
deps pull in `rolldown`, which imports `node:util.styleText`,
missing on Node 18). Each app has its own `vite.config.mjs` under
its source dir. For live apps (`apps/<id>/`) outDir is `"../"` so
the artifact lands at `apps/<id>.xdc`; for disabled apps
(`apps-disabled/<id>/`) outDir is `"../../apps-disabled/"` for the
same effect one level over. The bot discovers apps by globbing
`apps/*.xdc` — no hard-coded list. After rebuilding, commit the
updated `.xdc` so deploys don't need Node.

## Deployment

The Pi runs each bot instance as its own systemd service under the
unprivileged `pi` user (BLE access via `setcap cap_net_raw,cap_net_admin+eip`
on each venv's `bluepy-helper`; `lib/common.sh:cleanup_ble` silently
no-ops under non-root, so adapter-wedging recovery is a manual
`sudo ./send-command.sh status`). Previously ran under root
(needed for BLE raw-HCI access). Two units are currently active:
`deltabot-gatekeeper-hoftor.service` (from
`/home/pi/gatekeeper-hoftor/`) and `deltabot-gatekeeper-km.service`
(from `/home/pi/gatekeeper-km/`). Both use the same generic
template `systemd-unit/deltabot.service` -- it's copied per-bot
under `/etc/systemd/system/deltabot-gatekeeper-<name>.service`
with the matching `WorkingDirectory` and `Description`. A third
clone `/home/pi/gatekeeper-bot-original/` exists as a clean
reference but has no unit/`.env`.

Each Pi-side clone has `origin` pointing at
`https://github.com/mschunte2/gatekeeper-bot.git` and
`submodule.recurse = true`, so a plain `git pull` as user `pi`
pulls the parent and the `keyblepy` submodule in one shot; a
subsequent `sudo systemctl restart deltabot-gatekeeper-hoftor`
reloads the Python process.

GitHub is the canonical deployment source, not a squashed mirror. It
currently carries the granular dev history (force-pushed from the dev
workstation's `main`). If you decide to return to a squashed-release
model, rewrite both `github/main` and every Pi clone's `main` in
lockstep — a mismatch breaks fast-forward pulls.

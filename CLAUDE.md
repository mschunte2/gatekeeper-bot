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
  └── webxdc apps (gatekeeper, quick-unlock, quick-lock)
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
`send-command.sh` uses `flock -n` (non-blocking) on
`/tmp/ble-hci${HCI_IFACE}.lock`. Only one BLE operation at a time per
adapter. Concurrent callers get exit code 3 ("busy") immediately. The
lock file is per-adapter (not per-bot) so two bots sharing one adapter
serialize correctly.

### Webxdc replay protection gap (accepted)
Text commands have a 30 s `msg.timestamp` age check. Webxdc status
updates do NOT — the user is interactively tapping a button, so replay
is not a realistic threat. This was discussed and accepted during the
initial design review.

### Colour semantics in quick-unlock / quick-lock apps
Green = open (target state for quick-unlock). Red = closed (target
state for quick-lock). This is REVERSED from the source slider images
where green originally meant "locked". The images were recoloured
with ImageMagick to match the user's preferred semantics.

### Bot ack protocol
When the bot receives a webxdc command, it pushes `{payload: {ack: cmd}}`
to all app instances BEFORE running `send-command.sh`. Apps that care
(quick-unlock, quick-lock) use this to transition from the starting
visual to the orange "in progress" visual at the right moment. Apps
that don't care (gatekeeper) ignore it. The ack is NOT a confirmation
that the lock operated — just that the bot received the command.

### _ready flag in quick-unlock / quick-lock
On app open, `setUpdateListener` may deliver queued status updates from
previous sessions. Without guarding, these overwrite the starting
visual (red for quick-unlock, green for quick-lock) before the
auto-open/lock fires. The `_ready` flag blocks ack/response processing
until the auto-command has been sent. Config updates (door_name) are
always allowed through.

### Audit line format
App-driven commands produce `{icon} {DOOR_NAME} {actor_name}` in the
originating chat (e.g. "🔓 Hoftor Matthias"). Status checks are
silent (read-only, auto-requested on app open). Text commands echo
raw subprocess output instead (the user's own message already
identifies them).

### App discovery
`delta-door-bot.py` scans `apps/*.xdc` on each `/apps` call. No
hard-coded list. Dropping a new `<id>.xdc` into `apps/` is enough.

## Protocol: bot ↔ webxdc apps

All apps share one protocol. The `app` field is logged but does not
affect routing.

### App → bot
```json
{"payload": {"request": {"name": "Alice", "text": "lock", "app": "gatekeeper"}}}
```
`text` is whitelisted against `{lock, unlock, open, status}`.

### Bot → app (state)
```json
{"payload": {"response": {"name": "bot", "text": "locked"}}}
```
`text` is one of: `locked`, `unlocked`, `unknown`, `error`.

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
.env                       secrets + config (gitignored)
.env.example               template
apps/
  gatekeeper.xdc           built artifact (tracked)
  quick-unlock.xdc         built artifact (tracked)
  quick-lock.xdc           built artifact (tracked)
  gatekeeper/              full lock-control app source
  quick-unlock/            one-tap unlock app source
  quick-lock/              one-tap lock app source
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

## Two-bot setup (planned)

Two gatekeeper-bot instances can share one BLE adapter:
- Each bot lives in its own directory with its own `.env` (different
  `LOCK_MAC`, `USER_KEY`, `DOOR_NAME`, `ALLOWED_CHATS`).
- Both set the same `ADAPTER_MAC` → resolve to the same `hciN` →
  share the same flock file → operations serialize correctly.
- `killall bluepy-helper` in cleanup is safe because flock guarantees
  no other bot's process is in-flight.
- Timing: worst case both users tap simultaneously → one succeeds
  (~7 s bonded), the other gets "BLE adapter busy" and retries.
- Each bot needs its own Delta Chat account, its own `BotCli("name")`
  (for separate config dirs), and its own systemd unit.

## Build system

Apps are built with vite (Node.js 18+). Each app under `apps/<id>/`
has its own `vite.config.mjs` that outputs `apps/<id>.xdc`. The bot
discovers apps by globbing `apps/*.xdc` — no hard-coded list. After
rebuilding, commit the updated `.xdc` so deploys don't need Node.

## Deployment

The Pi runs each bot instance as its own systemd service under root
(needed for BLE raw-HCI access). As of the two-bot split, the active
instance is `deltabot-hoftor.service`, sourcing its code from
`/home/pi/gatekeeper-hoftor/`. Sibling clones `gatekeeper-km` and
`gatekeeper-bot-original` are configured identically but have no
systemd unit yet.

Each Pi-side clone has `origin` pointing at
`https://github.com/mschunte2/gatekeeper-bot.git` and
`submodule.recurse = true`, so a plain `git pull` as user `pi` pulls
the parent and the `keyblepy` submodule in one shot; a subsequent
`sudo systemctl restart deltabot-hoftor` reloads the Python process.

GitHub is the canonical deployment source, not a squashed mirror. It
currently carries the granular dev history (force-pushed from the dev
workstation's `main`). If you decide to return to a squashed-release
model, rewrite both `github/main` and every Pi clone's `main` in
lockstep — a mismatch breaks fast-forward pulls.

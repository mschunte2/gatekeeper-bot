# gatekeeper-bot

*Based on [missytake/doorbot](https://git.0x90.space/missytake/doorbot) -- thanks to missytake for the Delta Chat bot skeleton.*
*Webxdc app reuses the layout from [deltachat-bot/webxdcbot](https://github.com/deltachat-bot/webxdcbot).*

A Raspberry Pi-hosted bridge that operates an **eQ-3 Eqiva Smart Lock** from
chat. A small Python daemon (`delta-door-bot.py`) listens on a
[Delta Chat](https://delta.chat/) account. Three control surfaces share
one backend:

- **Text commands** -- send `/lock` / `/unlock` / `/status` from an
  allowed chat. Original behaviour, preserved verbatim.
- **Gatekeeper webxdc app** -- full lock control inside the chat:
  closed-lock / open-lock / door buttons, live status icon. Tagged
  internally as `app: "gatekeeper"`.
- **Quick-Unlock webxdc app** -- one-tap door opener: opening the app
  immediately requests `open`. Pin it to your phone's home screen for
  one-click entry. Tagged internally as `app: "quick-unlock"`.
- **Quick-Lock webxdc app** -- one-tap door closer: opening the app
  immediately requests `lock`. No open direction, by design (priority
  is "after touching this app the lock is closed"). Tap to retry if
  the lock didn't engage. Tagged internally as `app: "quick-lock"`.

All three apps speak the same protocol; the bot doesn't behave
differently per app (the `app` tag is only logged for debugging).

All paths converge on `send-command.sh`, which calls
[`keyblepy`](./keyblepy/) over BLE. State pushes flow back from the
bot to every active app instance whenever the lock changes -- whether
the change came from an app, a text command, or another allowed chat.

```
   your phone -- Delta Chat -+--> delta-door-bot.py --> send-command.sh
                             |          |                     |
   webxdc apps (in chat) ----+          |          keyblepy -- BlueZ -- Pi BLE --+
                                        v                                        |
                              systemd (deltabot)                                 v
                                                                          Eqiva Smart Lock
```

All runtime configuration and secrets live in a single `.env` file at the
repo root (see `.env.example`). Scripts source `.env` on startup;
`delta-door-bot.py` reads env vars via `os.environ`. The webxdc apps
are built into `apps/<id>.xdc` (tracked artifacts -- see
[Building the apps](#11-building-the-webxdc-apps)). The bot
auto-discovers them on each `/apps` call.

---

## 1. Prerequisites

- **Hardware** -- a Raspberry Pi (Zero 2 W tested) with working Bluetooth.
  Either the onboard Broadcom radio or a USB BT dongle works. Physical
  proximity to the lock matters: for a reliable BLE link target RSSI >= -85 dBm
  (~5 m line-of-sight or closer).
- **OS** -- Raspberry Pi OS Bookworm or equivalent Debian 12. Python 3.11+.
- **A Delta Chat account** for the bot. Create it once interactively via
  `deltabot-cli init` (see [deltabot-cli](https://github.com/deltachat-bot/deltabot-cli-py)).
  The account's SQLite state ends up under `~/.config/gatekeeper/`.
- **The lock's QR/setup card** (paper insert that came with the Eqiva lock).
  It encodes the MAC, card-key, and serial; you feed the whole string to
  `register-user.sh` as `QR_DATA`.

---

## 2. Installation

### 2.1 System packages

```bash
sudo apt install -y bluez bluez-firmware pi-bluetooth python3-venv git
sudo systemctl enable --now hciuart          # brings up onboard hci0
sudo systemctl enable --now bluetooth
```

### 2.2 Clone the repo

```bash
git clone <this repo's URL> gatekeeper-bot
cd gatekeeper-bot
git submodule update --init --recursive       # pulls keyblepy
```

### 2.3 Python venv and dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install bluepy transitions pycryptodome \
            deltachat2 deltabot-cli deltachat-rpc-server
```

Optional: for the encrypted fast path you want a bluepy build that supports
`connect(timeout=...)`. The PyPI 1.3.0 wheel works too -- keyblepy detects
the missing parameter at runtime and falls back to the 3-arg signature
(logging a warning in `--verbose` mode). If you want `--connect-timeout`
honoured, install bluepy editable from a newer checkout:

```bash
git clone https://github.com/IanHarvey/bluepy.git ../bluepy-src
pip install -e ../bluepy-src
```

---

## 3. Configuration

```bash
cp .env.example .env
$EDITOR .env
```

`.env` fields (see `.env.example` for the template):

| variable          | meaning                                                                                                  |
|-------------------|----------------------------------------------------------------------------------------------------------|
| `LOCK_MAC`        | Lock BLE MAC, e.g. `00:CA:FF:EE:DE:AD`. Obtain via `sudo hcitool lescan`; the lock advertises as `KEY-BLE`. |
| `USER_ID`         | Numeric slot on the lock (1-255). You pick this. After `register-user.sh` succeeds, the Eqiva *mobile app* will show the registered user as e.g. **"User 4"** -- that number is your `USER_ID`. |
| `USER_KEY`        | 32 hex chars (16 bytes) -- your shared secret with the lock. Generate with `openssl rand -hex 16`. Keep private. |
| `QR_DATA`         | The full QR string from the lock's setup card, format `M<12-hex-MAC>K<32-hex-card-key><10-char-serial>`. Only needed for `register-user.sh`. |
| `USER_NAME`       | A label shown in the Eqiva mobile app for this user.                                                    |
| `ADAPTER_MAC`     | BD address of the BLE adapter to use (e.g. `8A:88:4B:C2:9C:B9` for a USB dongle). Resolved to `hciN` at runtime so HCI renumbering across reboots is harmless. Leave blank to default to the built-in UART adapter. Find yours with `hciconfig -a`. |
| `SEC_LEVEL`       | `low` (unencrypted, works on any lock) or `medium` (LE-encrypted; requires a BlueZ bond -- see section 5). If the bond is lost, `send-command.sh` automatically retries with `low` and prints a warning. |
| `CONNECT_TIMEOUT` | Seconds to wait for the BLE connection; `75` is a safe default.                                         |
| `TIMEOUT`         | Overall wall-clock timeout per command. `90` default.                                                   |
| `ALLOWED_CHATS`   | Comma-separated Delta Chat chat-ids that may operate the lock. Gates **both** text commands (`/lock`, `/unlock`, `/status`) **and** the webxdc app -- chats in this list automatically receive the app and may use either path. `/id` is the only command that bypasses this check (so you can discover chat ids during setup). Empty = nobody. See section 6. |
| `DOOR_NAME`       | Display name shown as the heading inside the webxdc app (e.g. `"Front Gate"`). Pushed silently to every active app instance on startup. Default: `Door`. |
| `HELP_MESSAGE`    | Optional override for the bot's help text (multi-line supported). Empty = a sensible English default. Put your contact info / localized aliases / extra commands here. |

---

## 4. Register the Pi as a lock user

The Eqiva lock ships with a factory *card-key* (encoded in the QR card)
that authenticates *administrative* operations. You use it once to inject
your own *user-key* into an empty user slot.

### 4.1 Put the lock into registration mode

**Press and hold the "open" button on the lock for about 3 seconds, until
its LED turns orange.** That's the signal that the lock will accept a new
user registration on the next BLE connection. The registration window lasts
about 30 seconds; if you miss it, repeat the press.

### 4.2 Generate a user-key and register

```bash
# Generate a fresh random user-key and pick an empty slot number.
NEW_USER_KEY=$(openssl rand -hex 16)
echo "$NEW_USER_KEY"
# Pick USER_ID (e.g. 4) -- any number 1..255 that isn't already taken.

# Put USER_KEY and USER_ID into .env.
$EDITOR .env

# With the lock's LED orange, run:
./register-user.sh
```

**How the lock signals successful registration:** the lock emits a
short **beep** and the orange LED **stops blinking**. The
`register-user.sh` script also exits 0, and the Eqiva mobile app (next
time it syncs with the lock) shows the new user as **"User N"** where
`N` is the `USER_ID` you chose.

If neither the beep nor the LED change happens within ~30 s after
running `register-user.sh`, registration failed -- typically because
the lock exited registration mode before the BLE handshake completed,
or the auth tag was rejected. Re-press the button (3 s, orange LED)
and try again.

Confirm:

```bash
./send-command.sh status
# device status = {'lock_status': 'UNLOCKED', ...}
```

### 4.3 Notes

- User slots on Eqiva locks are finite (roughly 8-10). If the slot you chose
  is taken, registration times out; pick another number.
- Revoking a user: do it from the Eqiva mobile app (admin device). Pick the
  user and tap *Delete*. The slot becomes free for re-registration.

---

## 5. (Recommended) BLE bond for the encrypted fast path

Without a BLE-level bond, each lock operation takes ~45 s on the Pi: the
lock asks for SMP pairing post-connect, the bypass stack doesn't respond,
and the lock waits about 40 s before accepting GATT writes. With a bond
stored in BlueZ and `SEC_LEVEL=medium` in `.env`, operations drop to 6-8 s.

1. Put the lock in pairing mode (same 3-second "open" button press,
   wait for the orange LED).
2. Run `sudo bluetoothctl` and execute:

   ```
   select <your-controller-MAC>      # e.g. the MAC shown by `hciconfig hci1`
   power on
   agent NoInputNoOutput
   default-agent
   scan le
   # wait ~5s for "KEY-BLE" to appear
   pair <LOCK_MAC>
   trust <LOCK_MAC>
   info <LOCK_MAC>                    # should print Paired: yes / Bonded: yes
   exit
   ```

3. Confirm `.env` has `SEC_LEVEL=medium`, then time a command:

   ```bash
   time ./send-command.sh status       # ~7 s instead of ~45 s
   ```

If the bond is ever evicted (the lock has a finite bond table; bonding
more peers may displace yours), commands slow to ~45 s again. Recover with
`sudo bluetoothctl -- remove <LOCK_MAC>` and repeat the ceremony.

The bond does *not* block other peers (the Eqiva mobile app, another Pi) --
the lock keeps one bond entry per peer.

---

## 6. Run as a systemd service

The included `systemd-unit/deltabot.service` wraps `start-gatekeeper-bot.sh`
with restart-on-failure semantics.

### 6.1 The unit file

Contents of `systemd-unit/deltabot.service`:

```ini
[Unit]
Description=Deltachat-Bot-Gatekeeper Service
After=network.target

[Service]
Type=simple
User=root
ExecStart=/home/pi/gatekeeper-bot/start-gatekeeper-bot.sh
Restart=always

[Install]
WantedBy=multi-user.target
```

Runs as `root` because BlueZ raw-HCI access typically needs `CAP_NET_RAW`.
If you'd rather run unprivileged, grant that capability to
`bluepy-helper` instead (`sudo setcap cap_net_raw,cap_net_admin+eip
venv/lib/python3.11/site-packages/bluepy/bluepy-helper`) and change `User=`
to `pi`.

### 6.2 Installing the unit

```bash
sudo cp systemd-unit/deltabot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable deltabot        # start at boot
sudo systemctl start deltabot         # start now

sudo systemctl status deltabot        # confirm "active (running)"
sudo journalctl -u deltabot -f        # tail logs
```

### 6.3 After editing `.env` or code

```bash
sudo systemctl restart deltabot
```

### 6.4 Uninstalling

```bash
sudo systemctl disable --now deltabot
sudo rm /etc/systemd/system/deltabot.service
sudo systemctl daemon-reload
```

### 6.5 Creating the unit from scratch (if you don't want to use the bundled one)

```bash
sudo tee /etc/systemd/system/deltabot.service >/dev/null <<'EOF'
[Unit]
Description=Deltachat-Bot-Gatekeeper Service
After=network.target bluetooth.service

[Service]
Type=simple
User=root
WorkingDirectory=/home/pi/gatekeeper-bot
ExecStart=/home/pi/gatekeeper-bot/start-gatekeeper-bot.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now deltabot
```

---

## 7. Operating the lock

### 7.1 From the command line (on the Pi)

```bash
./send-command.sh status    # prints: device status = {'lock_status': 'LOCKED', ...}
./send-command.sh lock      # engages the bolt
./send-command.sh unlock    # retracts the bolt
./send-command.sh open      # fully retracts (mode-dependent)
```

Exit codes:

| code | meaning |
|------|---------|
| 0    | success |
| 2    | both attempts failed (lock unreachable or command rejected) |
| 3    | BLE adapter busy (another operation in progress — try again) |
| 4    | BLE adapter not found (dongle unplugged? wrong ADAPTER_MAC?) |

`send-command.sh` is self-healing: it kills stale `bluepy-helper`
processes, resets the HCI adapter, and stops any active bluetoothd
scan before each attempt. If the first attempt fails (e.g. bond
evicted after a battery swap), it automatically retries with
`SEC_LEVEL=low` and prints a warning to stderr:

```
⚠ Bond may be lost — retrying without pairing (slow). Run pair-lock.sh to re-establish.
```

Concurrent callers are serialized via `flock` (one BLE operation at a
time per adapter). A second caller while the first is in progress
gets exit code 3 immediately.

### 7.1.1 Re-establishing the BLE bond

If `send-command.sh` reports a lost bond, run the interactive pairing
guide:

```bash
./pair-lock.sh
```

It prompts you to put the lock in pairing mode (3-second button
press, orange LED), scans for the lock, pairs and trusts it on the
configured adapter, and verifies with a status probe. See README §5
for background on bonding.

### 7.2 From Delta Chat

1. Make sure the service is running (section 6).
2. From your own Delta Chat, message the bot's account (QR invite after
   `deltabot-cli init`). Send `/id` -- the bot replies with the chat's
   numeric id.
3. Stop the service, add that id to `ALLOWED_CHATS` in `.env`, restart:

   ```bash
   sudo systemctl stop deltabot
   $EDITOR .env           # ALLOWED_CHATS=14,12
   sudo systemctl start deltabot
   ```

4. From the allowed chat:

   | command             | effect                                            |
   |---------------------|---------------------------------------------------|
   | `/status`           | current lock state                                |
   | `/lock` / `/zu`     | engage the bolt                                   |
   | `/unlock` / `/auf`  | retract the bolt                                  |
   | `/apps`             | (re)send all webxdc apps (Gatekeeper + Quick-Unlock) |
   | `/id`               | show this chat's id (always works, no permission) |
   | anything else       | help text                                         |

   Reactions: hourglass on receipt, checkmark on completion, cross if the
   message is older than 30 s (replay-protection).

### 7.3 From the webxdc apps

`/apps` drops two apps in the chat:

#### Gatekeeper (`apps/gatekeeper.xdc`)

Full lock control. Tap the app message; three buttons appear over a
small colourful house drawing:

| button         | sends to bot   | runs                       |
|----------------|----------------|----------------------------|
| closed-lock    | `lock`         | `send-command.sh lock`     |
| open-lock      | `open`         | `send-command.sh open`     |
| door (centre)  | `status`       | `send-command.sh status`   |

While a command is pending the door button glows yellow and the status
text shows `Door: locking…` / `opening…` / `checking…`. The icon
returns to the actual state when the bot's response arrives (or to
`Door: timeout (no response)` after 60 s).

#### Quick-Unlock (`apps/quick-unlock.xdc`)

Designed to pin to your phone's home screen. **Opening the app
immediately sends `open`** to the bot -- one tap, door open. The
slider stays on red ("Closed", starting frame) until the bot acks
the request, then transitions through orange ("Opening…") to green
("Open"). Tap the green slider to lock again (green → orange → red);
tap red to open again. The chat-message preview icon is the green
"Open" image (the target state for this app).

#### Quick-Lock (`apps/quick-lock.xdc`)

Mirror of Quick-Unlock with the priority "after touching this app the
lock is closed". **Opening the app immediately sends `lock`**. Visual
goes from green ("Open") through orange ("Closing…") to red
("Closed"). There is no open direction here -- a tap on a
non-closed slider only retries the lock command. The chat-message
preview icon is the red "Closed" image (the target state).

#### State updates

The status icons / labels in both apps update from silent webxdc
status messages the bot pushes back -- so every member of every allowed
chat sees the same current lock state in real time. The app heading
shows `DOOR_NAME` from `.env`. The bot pushes that name to each app on
delivery; clients see it once they open the app.

#### Audit trail

Every state-changing app command produces a single concise chat line
`{icon} {DOOR_NAME} {actor}` in the originating chat:

```
🔓 Hoftor Matthias       (opened)
🔒 Hoftor Matthias       (locked)
❌ Hoftor Matthias (lock failed)
```

`status` requests don't produce an audit line (read-only; the apps
auto-request it on open / refresh). Text-driven commands keep their
original output (`device locked` etc.) -- the user's own `/lock`
message already identifies them. The originating app id (`gatekeeper`
or `quick-unlock`) is logged at INFO level for debugging but is not
shown to chat members.

#### Automatic retry and fallback

`send-command.sh` handles retries internally: if the first attempt
fails (configured `SEC_LEVEL`), it retries once with `SEC_LEVEL=low`
(unbonded fallback). A warning is printed to stderr if the fallback
is used. The bot relays stderr to the chat for text commands. See
§7.1 for details and exit codes.

---

## 8. Troubleshooting

| symptom                                                     | likely cause / fix                                                                                  |
|-------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| `./send-command.sh status` takes ~45 s                      | No BLE bond (or bond evicted). Run `./pair-lock.sh` to re-establish (see §7.1.1).                   |
| `./send-command.sh` exits with code 2                       | Both attempts failed. Lock out of range, phone app connected, or battery dead. Check and retry.     |
| `./send-command.sh` exits with code 3                       | Another BLE operation in progress. Wait a moment and try again.                                     |
| `./send-command.sh` exits with code 4                       | BLE adapter not found. Check `ADAPTER_MAC` in `.env` and that the dongle is plugged in.             |
| `⚠ Bond may be lost` warning in chat                       | The bonded fast path failed; `send-command.sh` fell back to `SEC_LEVEL=low`. Run `./pair-lock.sh`.  |
| Bot reacts with cross to every command                      | Message older than 30 s. Check network latency or clock skew on the Pi.                             |
| Bot replies "permission denied"                             | Chat id not in `ALLOWED_CHATS`. Use `/id`, edit `.env`, restart the service.                        |
| `MAC mismatch on received frame; dropping` in `--verbose`   | User-key mismatch between `.env` and the lock -- re-register or double-check the hex string.        |
| `Failed to connect to peripheral ... addr type: public`     | Lock-side issue: someone else is connected, or the lock is advertising slowly. Retry.               |
| After reboot, `hci0` is DOWN                                | `sudo systemctl enable hciuart` (section 2.1). Or `sudo hciconfig hci0 up`.                         |
| DeltaChat replies delayed by minutes                        | SMTP rate-limit on the account's provider. Check `journalctl -u deltabot` for `rate-limited until`. |
| App icon stays "Unknown" after restart                      | Bot couldn't reach the lock to seed the state. Check `journalctl -u deltabot` for the startup `status probe` line; tap the door button (sends `status`) to retry on demand. |
| App buttons do nothing                                      | Chat not in `ALLOWED_CHATS`, or the bot can't see your webxdc status updates. Check `journalctl -u deltabot` for `app cmd from chat N` lines. |

---

## 9. Security notes

- `.env` contains your lock credentials and is **gitignored**. Never commit
  it. Back it up to encrypted storage if you care about disaster recovery.
- The `user-key` is a shared secret. If it leaks, revoke the user via the
  Eqiva app, generate a new key, and re-register (section 4).
- The `card-key` embedded in `QR_DATA` is the lock's factory admin secret
  and cannot be rotated without a factory reset. Treat the QR card and
  `register-user.sh` as high-value.
- The DeltaChat account credentials live in `~/.config/gatekeeper/`. That
  directory is protected only by filesystem permissions on the Pi.
- Only chats listed in `ALLOWED_CHATS` can trigger lock operations.
  This single allow-list gates **both** text commands (`/lock`,
  `/unlock`, `/status`, `/app`) and the webxdc app: chats in this list
  automatically receive the app and any chat member can tap its
  buttons. `/id` is the only command that bypasses the check (so you
  can discover chat ids during setup).
- Chat ids are private per your Delta Chat account but treat them as
  low-sensitivity -- anyone who learns an allowed id and can send messages
  to your bot account can operate the lock.
- The bot whitelists the four lock-operation tokens (`lock`, `unlock`,
  `open`, `status`) before passing them to `send-command.sh`. The
  webxdc payload's `text` field is matched against this whitelist;
  anything else is logged and dropped, so a malicious app payload can't
  inject arbitrary shell arguments.
- Webxdc status updates the bot pushes back to apps use empty `info`,
  so the Delta Chat client renders them silently. They are still
  visible to every member of the chat (the app screen reflects them) --
  do not rely on them as a private channel.
- App-instance ids cached in `~/.config/gatekeeper/app_msgids.json`
  reveal which chats currently host the app but contain no
  credentials.

---

## 10. Layout

```
gatekeeper-bot/
|-- README.md                    (this file)
|-- .env                         (your secrets, gitignored)
|-- .env.example                 (template)
|-- delta-door-bot.py            (DeltaChat listener)
|-- start-gatekeeper-bot.sh      (service entrypoint)
|-- send-command.sh              (one-shot CLI wrapper around keyblepy, with retry + flock)
|-- pair-lock.sh                 (interactive BLE bond guide -- run when bond is lost)
|-- register-user.sh             (one-shot registration wrapper)
|-- keyblepy/                    (Python KeyBLE implementation, submodule)
|   \-- README.md                (protocol-level docs, Python API)
|-- apps/                        (webxdc apps -- sources + built artifacts)
|   |-- gatekeeper.xdc           (built artifact, tracked)
|   |-- quick-unlock.xdc         (built artifact, tracked)
|   |-- quick-lock.xdc           (built artifact, tracked)
|   |-- gatekeeper/              (full lock-control app source)
|   |   |-- index.html main.js main.css
|   |   |-- vite.config.mjs package.json
|   |   \-- public/              (icon, manifest)
|   |-- quick-unlock/            (one-tap-unlock app source)
|   |   |-- index.html main.js main.css
|   |   |-- vite.config.mjs package.json
|   |   \-- public/              (slider images, icon, manifest)
|   \-- quick-lock/              (one-tap-lock app source -- inverse of quick-unlock)
|       |-- index.html main.js main.css
|       |-- vite.config.mjs package.json
|       \-- public/              (slider images, icon, manifest)
|-- systemd-unit/
|   \-- deltabot.service         (sudo cp to /etc/systemd/system/)
\-- venv/                        (Python venv, gitignored)
```

Per-deployment state (gitignored, lives outside this tree):

- `~/.config/gatekeeper/`         -- Delta Chat account database (root user when
  the bot runs under the bundled systemd unit)
- `~/.config/gatekeeper/app_msgids.json`  -- chat-id -> [msgid, …] map the
  bot uses to push silent state updates to existing app instances
  (atomic write; safe to delete -- next `/apps` will reseed it).

For a deeper dive into the BLE protocol, encryption layout, and the bugs
that got fixed in keyblepy, see [`keyblepy/README.md`](./keyblepy/README.md).

---

## 11. Building the webxdc apps

You only need to rebuild if you change something under `apps/gatekeeper/`
or `apps/quick-unlock/`. The repo ships pre-built `apps/gatekeeper.xdc`
and `apps/quick-unlock.xdc` so a fresh deploy doesn't require Node.js.

### 11.1 Prerequisites

- Node.js 18+ and `npm` (only on the build machine -- not needed on
  the Pi if the `.xdc` artifacts are already committed).

### 11.2 Build

```bash
cd apps/gatekeeper
npm install      # one-time
npm run build    # writes ../gatekeeper.xdc

cd ../quick-unlock
npm install      # one-time
npm run build    # writes ../quick-unlock.xdc

cd ../quick-lock
npm install      # one-time
npm run build    # writes ../quick-lock.xdc
```

Each `vite.config.mjs` writes the bundled `.xdc` one level up, into
`apps/`, replacing the existing artifact.

### 11.3 What the apps share

- Both send the same protocol to the bot:
  `{request: {name, text: "lock"|"open"|"status", app: "<id>"}}`.
  The bot whitelists `text` against `{lock, unlock, open, status}` and
  uses `app` only for log lines.
- Both listen for two payload types from the bot:
  - `{response: {text: "locked"|"unlocked"|"unknown"|"error"}}`
    -- updates the visual state.
  - `{config: {door_name: "..."}}` -- sets the heading. Pushed when an
    app is sent (`/apps`), opportunistically when the bot first sees
    an unknown msgid, and on bot startup for every known instance.
- All bot-side state updates use empty `info` so they're silent in the
  chat.

After rebuilding any app, commit the updated `.xdc` so deploys can
skip the Node toolchain.

### 11.4 Adding a new app

The bot discovers apps by scanning `apps/*.xdc` -- there is no
hard-coded list. To add a new app:

1. Create `apps/<id>/` with its own `vite.config.mjs` (point
   `outDir: "../"` and `outFileName: "<id>.xdc"`).
2. Implement the same protocol: send `{request: {name, text, app: "<id>"}}`,
   listen for `{response}`, `{config}`, optionally `{ack}`.
3. `npm install && npm run build`. The resulting `apps/<id>.xdc` is
   picked up automatically the next time `/apps` runs (no bot
   restart needed if the file appears between two `/apps` calls;
   for new chats the bot also picks it up at startup).

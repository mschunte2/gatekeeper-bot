# gatekeeper-bot

*Based on [missytake/doorbot](https://git.0x90.space/missytake/doorbot) -- thanks to missytake for the Delta Chat bot skeleton.*

A Raspberry Pi-hosted bridge that operates an **eQ-3 Eqiva Smart Lock** from
chat. A small Python daemon (`delta-door-bot.py`) listens on a
[Delta Chat](https://delta.chat/) account; when an authorised chat sends
`/lock` / `/unlock` / `/status`, the daemon shells out to `send-command.sh`,
which in turn calls [`keyblepy`](./keyblepy/) over BLE.

```
   your phone  -- Delta Chat -->  delta-door-bot.py  -->  send-command.sh
                                         |                       |
                                         |             keyblepy (Python) -- BlueZ -- Pi BLE --+
                                         v                                                    |
                                systemd service                                               |
                                  (deltabot)                                                  v
                                                                                      Eqiva Smart Lock
```

All runtime configuration and secrets live in a single `.env` file at the
repo root (see `.env.example`). Scripts source `.env` on startup;
`delta-door-bot.py` reads env vars via `os.environ`.

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
| `HCI_IFACE`       | HCI index: `0` for onboard, `1` for a USB dongle, etc.                                                  |
| `SEC_LEVEL`       | `low` (unencrypted, works on any lock) or `medium` (LE-encrypted; requires a BlueZ bond -- see section 5). |
| `CONNECT_TIMEOUT` | Seconds to wait for the BLE connection; `75` is a safe default.                                         |
| `TIMEOUT`         | Overall wall-clock timeout per command. `90` default.                                                   |
| `ALLOWED_CHATS`   | Comma-separated Delta Chat chat-ids that may invoke `/lock` / `/unlock`. Empty = nobody. See section 6. |
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

If registration succeeds the LED stops blinking orange, the script exits 0,
and the Eqiva mobile app (next time it syncs with the lock) shows the new
user as **"User N"** where `N` is the `USER_ID` you chose.

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

Exit code: 0 on success, 1 on failure (timeout, MAC mismatch, lock
unreachable). Error goes to stderr.

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

   | command             | effect                             |
   |---------------------|------------------------------------|
   | `/status`           | current lock state                 |
   | `/lock` / `/zu`     | engage the bolt                    |
   | `/unlock` / `/auf`  | retract the bolt                   |
   | `/id`               | show this chat's id (always works) |
   | anything else       | help text                          |

   Reactions: hourglass on receipt, checkmark on completion, cross if the
   message is older than 30 s (replay-protection).

---

## 8. Troubleshooting

| symptom                                                     | likely cause / fix                                                                                  |
|-------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| `./send-command.sh status` takes ~45 s                      | No BLE bond. Complete section 5 for the fast path or accept the latency.                            |
| `./send-command.sh status` takes >90 s, exits 1             | Lock out of range, bond evicted, or phone app currently connected. Move closer, re-bond, or wait.   |
| Bot reacts with cross to every command                      | Message older than 30 s. Check network latency or clock skew on the Pi.                             |
| Bot replies "permission denied"                             | Chat id not in `ALLOWED_CHATS`. Use `/id`, edit `.env`, restart the service.                        |
| `MAC mismatch on received frame; dropping` in `--verbose`   | User-key mismatch between `.env` and the lock -- re-register or double-check the hex string.        |
| `Failed to connect to peripheral ... addr type: public`     | Lock-side issue: someone else is connected, or the lock is advertising slowly. Retry.               |
| After reboot, `hci0` is DOWN                                | `sudo systemctl enable hciuart` (section 2.1). Or `sudo hciconfig hci0 up`.                         |
| DeltaChat replies delayed by minutes                        | SMTP rate-limit on the account's provider. Check `journalctl -u deltabot` for `rate-limited until`. |

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
- Only chats listed in `ALLOWED_CHATS` can trigger `/lock`/`/unlock`. Chat
  ids are private per your Delta Chat account but treat them as
  low-sensitivity -- anyone who learns an allowed id and can send messages
  to your bot account can operate the lock.

---

## 10. Layout

```
gatekeeper-bot/
|-- README.md                    (this file)
|-- .env                         (your secrets, gitignored)
|-- .env.example                 (template)
|-- delta-door-bot.py            (DeltaChat listener)
|-- start-gatekeeper-bot.sh      (service entrypoint)
|-- send-command.sh              (one-shot CLI wrapper around keyblepy)
|-- register-user.sh             (one-shot registration wrapper)
|-- keyblepy/                    (Python KeyBLE implementation, submodule)
|   \-- README.md                (protocol-level docs, Python API)
|-- systemd-unit/
|   \-- deltabot.service         (sudo cp to /etc/systemd/system/)
\-- venv/                        (Python venv, gitignored)
```

For a deeper dive into the BLE protocol, encryption layout, and the bugs
that got fixed in keyblepy, see [`keyblepy/README.md`](./keyblepy/README.md).

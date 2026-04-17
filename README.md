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

### Hardware -- reference deployment

The project is developed and tested on:

- **Host** -- Raspberry Pi Zero 2 W (ARMv8, 512 MB RAM).
- **OS** -- Raspberry Pi OS (Debian 12 Bookworm), 64-bit. Python 3.11+.
- **Bluetooth** -- Realtek RTL8761BU-based USB dongle with an external
  antenna (USB ID `0bda:a729`, advertised as "Bluetooth 5.3 Radio").
  The onboard Broadcom BCM43430A1 radio (hci0) also works and is
  used as a fallback/verification adapter, but its integrated antenna
  only reaches short line-of-sight; the external-antenna dongle is
  the reliable path for locks more than a couple of metres away or
  through walls.
- **Locks** -- Eqiva eQ-3 Smart Lock (this project covers two: a
  gate lock and an indoor lock).

Physical proximity still matters: for reliable GATT operation target
RSSI ≥ -85 dBm. With the external-antenna dongle we see -65 to -75
dBm through two interior walls at ~8 m; both locks reach the bot
consistently. The onboard adapter was sufficient for one of the two
locks at ~3 m line-of-sight but not for the gate lock at ~8 m.

Other configurations should work in principle -- anything Linux,
recent BlueZ (`bluetoothctl` present), Python 3.11+. The BLE layer
is managed by `bluepy`; see section 2 for the version note.

### Known Bluetooth-stack pitfall on this host

Realtek RTL8761-series dongles on Pi-class USB ports sometimes
fail their initial firmware download at boot with `RTL: download
fw command failed (-110)` in dmesg. The adapter presents but has
BD address `00:00:00:00:00:00` and stays DOWN. Without intervention
all BLE operations then fail until a physical replug.

The bundled `bt-adapter-wait.service` (see section 6.2) mitigates
this: at boot, before `bluetooth.service`, it detects a wedged USB
adapter and reloads the `btusb` kernel module up to three times to
coax the firmware through. It's a per-host install (one for the
whole Pi, shared by both bot instances) -- see the installer
`install-bt-wait-service.sh`. If the adapter can't be recovered in
software after all retries, the service logs that fact and
bluetooth.service still starts; the operator then replugs the
dongle physically. In our deployment the automatic recovery
succeeds most of the time.

### Other requirements

- **A Delta Chat account** for the bot. Create it once interactively via
  `deltabot-cli init` (see [deltabot-cli](https://github.com/deltachat-bot/deltabot-cli-py)).
  The account's SQLite state ends up under `~/.config/<BOT_NAME>/`
  (the value you set for `BOT_NAME` in `.env`; defaults to
  `gatekeeper` if unset).
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

**On bluepy versions:** the off-the-shelf `bluepy 1.3.0` wheel from
PyPI works for everything we need -- including registration and the
encrypted fast path -- and is what the Pi deployment uses. We ran
both flows end-to-end against an Eqiva eQ-3 SmartLock with the PyPI
wheel and saw no functional difference vs a self-compiled bluepy
from upstream master. **No source build is required.**

Minor note: PyPI 1.3.0 lacks `connect(timeout=...)` -- keyblepy
detects this and falls back to the 3-arg signature (the connect
still happens; only `--connect-timeout` is silently ignored). If
you specifically need that flag honoured at the BLE layer, you can
install bluepy editable from a newer checkout (`git clone
https://github.com/IanHarvey/bluepy.git ../bluepy-src && pip install
-e ../bluepy-src`) -- but the Pi deployment does not, and has had
no issues.

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
| `USER_ID`         | Numeric slot on the lock (0-254). **Output of `register-user.sh` -- do not set before running it.** The lock auto-assigns this; `register-user.sh` prints the chosen value, which you paste here. The Eqiva *mobile app* shows the registered user as e.g. **"User 4"** -- that number is your `USER_ID`. |
| `USER_KEY`        | 32 hex chars (16 bytes) -- your shared secret with the lock. **Also an output of `register-user.sh`**; the script generates a fresh random value, registers it with the lock, and prints it. Keep private. |
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

### 4.2 Run register-user.sh and copy its output into .env

`register-user.sh` does **all** the credential work for you:

- generates a fresh random 16-byte user-key,
- asks the lock to **auto-assign** a free slot (you don't pick one),
- on success, prints both the assigned `USER_ID` and the new
  `USER_KEY` in a copy-pasteable block.

You do **not** need to set `USER_ID` or `USER_KEY` in `.env` before
running the script -- registration creates them. (`LOCK_MAC`,
`QR_DATA`, `ADAPTER_MAC`, `USER_NAME` do still need to be set.)

```bash
# With the lock's LED orange (3-second "open" press), run:
./register-user.sh                # quiet (summary only)
./register-user.sh -v             # also streams keyblepy's debug log live
```

On success the script prints, e.g.:

```
============================================================
Registration successful.

To activate, set the following two lines in
/home/pi/gatekeeper-km/.env

    USER_ID=2
    USER_KEY=c4360e78beaf524c4e6af66dec48e11d

Then restart the bot service, e.g.
    sudo systemctl restart deltabot-gatekeeper-km.service
============================================================
```

Paste those two lines into `.env` and restart the service. **No
persistent log file is written**: the keyblepy `--verbose` stream
re-echoes the newly-generated `--user-key` on the command line, and
a captured log would be a credential leak. The output is held in
shell memory only for the duration of the run; on failure it is
dumped to stderr so you can still diagnose (without the success
credentials surviving disk).

**How the lock signals successful registration:** the lock emits a
short **beep** and the orange LED **stops blinking**, and the Eqiva
mobile app (next time it syncs) shows the new user as **"User N"**
matching the assigned `USER_ID`.

If neither the beep nor the LED change happens within ~30 s after
running `register-user.sh`, registration failed -- typically because
the lock exited registration mode before the BLE handshake completed,
or the auth tag was rejected. The script exits non-zero and **does
not** print credentials (so you can't paste a half-baked entry into
`.env`). In quiet mode the captured keyblepy output is dumped to
stderr for debugging; in `-v` mode you've already seen it live.
Re-press the button (3 s, orange LED) and try again.

Confirm with the printed credentials in place:

```bash
./send-command.sh status
# device status = {'lock_status': 'UNLOCKED', ...}
```

### 4.3 Notes

- User slots on Eqiva locks are finite (roughly 8-10). If all slots
  are taken, registration is rejected; revoke a user from the Eqiva
  mobile app first to free a slot.
- Revoking a user: do it from the Eqiva mobile app (admin device).
  Pick the user and tap *Delete*. The slot becomes free for
  re-registration.
- If you specifically need to register into a *chosen* slot rather
  than letting the lock pick, call `keyblepy/keyble.py` directly with
  `--user-id N`. `register-user.sh` always uses auto-assign.

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

Use the bundled `install-systemd-unit.sh` script rather than copying
the unit file by hand -- it handles the per-bot parameterisation
(service name, description, working dir) and the required `setcap`
step in one shot.

### 6.1 Installing

```bash
cd /home/pi/gatekeeper-bot       # or wherever this bot's clone lives
$EDITOR .env                     # set BOT_NAME, DOOR_NAME, etc.
sudo ./install-systemd-unit.sh   # interactive: confirms overwrite + start
```

The script:

1. Reads `BOT_NAME` and `DOOR_NAME` from `.env` in the current
   directory.
2. Renders `systemd-unit/deltabot.service.template` into
   `/etc/systemd/system/deltabot-<BOT_NAME>.service`.
3. Runs `setcap cap_net_raw,cap_net_admin+eip` on the venv's
   `bluepy-helper` so the bot can open raw HCI sockets as an
   unprivileged user.
4. `systemctl daemon-reload`, then offers to `enable --now` the
   service.

For a two-bot deployment you'd run the installer from each bot's
directory:

```bash
cd /home/pi/gatekeeper-km     && sudo ./install-systemd-unit.sh -y
cd /home/pi/gatekeeper-hoftor && sudo ./install-systemd-unit.sh -y
```

Useful flags:

- `-y` / `--yes`  -- non-interactive; overwrite existing unit and
  enable+start without prompting.
- `--skip-setcap` -- skip the setcap step (e.g. you applied it
  manually or you're deploying to a host without bluepy).
- `--dry-run`     -- print the rendered unit to stdout and exit;
  does not touch `/etc/systemd/system/` or file capabilities.

The installer is idempotent: re-running against an up-to-date
target is a no-op beyond re-applying setcap. If the live unit has
diverged from what the installer would render, you see a diff and
a confirmation prompt before anything is overwritten.

### 6.2 The unit template

The template at `systemd-unit/deltabot.service.template` is what the
installer fills in:

```ini
[Unit]
Description=Gatekeeper Bot - @DESCRIPTION@
After=network.target bluetooth.service

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=@WORKING_DIR@
ExecStart=@WORKING_DIR@/start-gatekeeper-bot.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Runs as an unprivileged user (`pi`). BLE raw-HCI access needs
`CAP_NET_RAW` + `CAP_NET_ADMIN`; rather than grant the whole bot
those capabilities, `install-systemd-unit.sh` grants them file-bound
to the single helper binary that actually opens the raw socket
(`venv/lib/python*/site-packages/bluepy/bluepy-helper`). The
capability is lost on `pip install --force-reinstall bluepy`, so
re-run the installer after rebuilding the venv.

One known behavioural compromise: the adapter-wedging recovery path
in `lib/common.sh:cleanup_ble` (`killall bluepy-helper`,
`hciconfig reset`) still needs root and silently no-ops under
`pi`. If the adapter actually wedges, the operator recovers
manually with `sudo ./send-command.sh status` once (the same
script self-heals under root). For a home deployment this has
been rare; an industrial setup might prefer to keep `User=root`.

### 6.3 After editing `.env` or code

```bash
sudo systemctl restart deltabot-<BOT_NAME>
```

Or re-run the installer (which detects that the service is active
and restarts it if the unit file changed).

### 6.4 Uninstalling

```bash
sudo systemctl disable --now deltabot-<BOT_NAME>
sudo rm /etc/systemd/system/deltabot-<BOT_NAME>.service
sudo systemctl daemon-reload
```

### 6.5 Boot-time USB Bluetooth recovery (`bt-adapter-wait.service`)

Realtek RTL8761 dongles on Pi-class USB ports sometimes fail their
initial firmware download at boot (`RTL: download fw command failed
(-110)` in dmesg), leaving the adapter with BD address
`00:00:00:00:00:00` and status `DOWN`. Without intervention every
BLE operation then fails until a physical replug. See section 1
("Known Bluetooth-stack pitfall") for background.

The bundled `bt-adapter-wait.service` runs once per boot, before
`bluetooth.service`, and reloads the `btusb` kernel module up to
three times to recover a wedged USB adapter. It's a **host-wide**
install (one service for the whole Pi, not per-bot) so you run the
installer once:

```bash
cd /home/pi/gatekeeper-bot   # from any bot clone on the host
sudo ./install-bt-wait-service.sh
```

The installer copies `systemd-unit/bt-adapter-wait.sh` to
`/usr/local/sbin/bt-adapter-wait` and
`systemd-unit/bt-adapter-wait.service` to
`/etc/systemd/system/bt-adapter-wait.service`, then enables the
unit. Running the installer from a second bot clone is a safe no-op
beyond re-copying identical files.

Verify after a reboot:

```bash
journalctl -u bt-adapter-wait -b
# expect one of:
#   "USB BT adapter(s) ready at boot; no recovery needed"
#   "USB BT adapter recovered after N reload(s)"
#   "USB BT adapter still wedged after 3 attempts -- physical replug required"
```

The third message means this particular boot lost the race; replug
the dongle and the bonds persist under `/var/lib/bluetooth/` so no
re-pairing is needed.

Uninstall:

```bash
sudo ./install-bt-wait-service.sh --uninstall
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
   | `/apps`             | deliver every `apps/*.xdc` to this chat, **idempotently** -- apps already installed are skipped (state is just refreshed). Currently delivers Gatekeeper + Quick-Lock. |
   | `/apps reset`       | wipe the bot's tracking for this chat and send every app fresh. Use when a user deleted the old app message locally and wants a clean copy. |
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

#### Quick-Unlock (`apps-disabled/quick-unlock.xdc` -- currently disabled)

> **Disabled by default.** The .xdc lives in `apps-disabled/` rather
> than `apps/`, so the bot's `apps/*.xdc` glob does not pick it up
> and `/apps` will not deliver it. Reason: opening the app sends
> `open` immediately on launch, with no confirmation -- judged too
> easy to trigger accidentally (pocket-tap, shortcut misfire) for
> day-to-day deployment. To re-enable, move the file back into
> `apps/` (and revert the `outDir` in
> `apps/quick-unlock/vite.config.mjs` so future rebuilds land in
> the right place).

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
- The DeltaChat account credentials live in `~/.config/<BOT_NAME>/`
  (see section 10 for the exact path; keyed by `BOT_NAME` in `.env`
  so two bots on the same host keep separate databases). That
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
- App-instance ids cached in `~/.config/<BOT_NAME>/app_msgids.json`
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
|-- install-systemd-unit.sh      (renders systemd-unit/*.template and
|                                 installs deltabot-<BOT_NAME>.service)
|-- install-bt-wait-service.sh   (host-wide installer for the USB-BT
|                                 firmware-reload service; one-time)
|-- keyblepy/                    (Python KeyBLE implementation, submodule)
|   \-- README.md                (protocol-level docs, Python API)
|-- apps/                        (webxdc apps -- sources + built artifacts)
|   |-- gatekeeper.xdc           (built artifact, tracked)
|   |-- quick-lock.xdc           (built artifact, tracked)
|-- apps-disabled/               (built artifacts NOT served by /apps)
|   \-- quick-unlock.xdc         (one-tap unlock, currently disabled)
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
|   |-- deltabot.service.template    (parameterised; rendered by
|   |                                 install-systemd-unit.sh)
|   |-- bt-adapter-wait.service      (host-wide; boot-time USB BT
|   |                                 firmware-reload service)
|   \-- bt-adapter-wait.sh           (the retry logic called by
|                                     bt-adapter-wait.service)
\-- venv/                        (Python venv, gitignored)
```

### Persistent state

Lives outside this tree, on the root filesystem — survives reboots.
The directory is keyed by `BOT_NAME` (from `.env`), so two bots on
the same host keep their state separate. Under the bundled systemd
unit the bot runs as `pi`, so the directory is
`/home/pi/.config/<BOT_NAME>/`.

- `~/.config/<BOT_NAME>/`                  -- Delta Chat account database (SQLite).
  Protected only by filesystem permissions; back up if you care
  about disaster recovery. See section 9 (Security model) for the
  trust implications.
- `~/.config/<BOT_NAME>/app_msgids.json`   -- `{chat_id: {app_id: msgid}}`
  map that makes `/apps` idempotent (skip apps already in the chat)
  and lets the bot push silent state updates to existing app
  instances. Atomic write (`tmp` + `os.replace`). Safe to delete:
  next `/apps` in each chat reseeds it, and the bot self-migrates
  the older `{chat_id: [msgid, …]}` shape on startup by dropping
  legacy entries (logs `"dropping legacy app_msgids entries for
  chats [..]; run /apps in each chat to re-seed"`) and rewriting
  the file clean.

### Non-persistent (deliberately in-memory, re-derived on boot)

- Last-known lock state (`locked` / `unlocked` / `unknown`) and
  battery-low flag -- re-derived by the startup status probe in
  `_on_start` (`send-command.sh status`).
- keyblepy BLE session state (session nonces, security counters) --
  freshly negotiated per BLE connection; the lock discards them
  too.
- BLE flock file `/tmp/ble-hci<N>.lock` -- wiped on reboot, recreated
  on first use.

For a deeper dive into the BLE protocol, encryption layout, and the bugs
that got fixed in keyblepy, see [`keyblepy/README.md`](./keyblepy/README.md).

---

## 11. Building the webxdc apps

You only need to rebuild if you change something under an `apps/<id>/`
source directory. The repo ships pre-built artifacts
(`apps/gatekeeper.xdc`, `apps/quick-lock.xdc`, and
`apps-disabled/quick-unlock.xdc`) so a fresh deploy doesn't require
Node.js on the Pi.

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
npm run build    # writes ../../apps-disabled/quick-unlock.xdc  # currently disabled

cd ../quick-lock
npm install      # one-time
npm run build    # writes ../quick-lock.xdc
```

Each `vite.config.mjs` sets its own `outDir` in the `buildXDC` plugin
call. Most apps write to `apps/` (served by `/apps`); the
`quick-unlock` config points at `apps-disabled/` instead, because
that app is currently disabled -- see section 7.3.

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

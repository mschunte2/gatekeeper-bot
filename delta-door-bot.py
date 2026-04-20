#!/usr/bin/env python3
"""Gatekeeper bot: text + webxdc app control of an Eqiva Smart Lock.

Text path:
    /lock | /zu       -> send-command.sh lock
    /unlock | /auf    -> send-command.sh unlock
    /status           -> send-command.sh status
    /id               -> reply with this chat's id (always allowed)
    /apps             -> idempotently deliver webxdc apps (skip ones
                         already in this chat; refresh their state)
    /apps reset       -> wipe tracking and send every app fresh
    anything else     -> help text

Webxdc apps (apps/<id>.xdc), all sharing one protocol:
    {request: {name, text: lock|open|status, app: <id>}}
    -> bot whitelists `text` against {lock, unlock, open, status},
       runs send-command.sh, broadcasts state back as
    {response: {name, text: locked|unlocked|unknown|error}}
    The `app` field is logged for debug but does not affect routing.

Permission: ALLOWED_CHATS is the single allow-list for both text and app
operations. /id is the only command exempt (needed for setup).
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from appdirs import user_config_dir
from deltachat2 import EventType, MsgData, events
from deltabot_cli import BotCli

# BOT_NAME controls the BotCli identity (and therefore the Delta Chat
# account storage dir and the app_msgids.json location). Two bots on
# the same Pi MUST use different names so their state doesn't collide.
BOT_NAME = (os.environ.get("BOT_NAME") or "").strip() or "gatekeeper"
cli = BotCli(BOT_NAME)

# ---------------------------------------------------------------- constants

DEFAULT_HELP_MESSAGE = (
    "This bot operates a lock:\n"
    " /lock or /zu - locks gate\n"
    " /unlock or /auf - unlocks gate\n"
    " /status - print current status\n"
    " /apps - deliver webxdc control apps (idempotent; skips apps already in this chat)\n"
    " /apps reset - wipe tracking and send every app fresh\n"
    " /id - get this chat's id\n"
    " - Any other input shows this message.\n"
    " NOTE: lock operations can take up to 90 seconds."
)

HERE = Path(__file__).resolve().parent
APPS_DIR = HERE / "apps"
SEND_COMMAND_SH = str(HERE / "send-command.sh")


def _xdc_paths() -> list[tuple[str, str]]:
    """Discover available webxdc apps as (app_id, full_path) pairs.

    Scans `apps/*.xdc`; the app_id is just the file stem so dropping a
    new <name>.xdc into apps/ is enough -- no code change required.
    Files are returned in sorted order so the sequence of messages
    /apps sends is deterministic.
    """
    return [(p.stem, str(p)) for p in sorted(APPS_DIR.glob("*.xdc"))]

# Whitelist of accepted lock-operation tokens. Used by BOTH text and app
# paths. Anything else is rejected before reaching the subprocess.
VALID_COMMANDS = {"lock", "unlock", "open", "status"}

# Audit line shown in chat when an app button triggers a command.
# (emoji, past-tense verb) -- used as: "{emoji} {DOOR_NAME} {verb} by {name}"
_AUDIT_VERB = {
    "lock": ("🔒", "locked"),
    "unlock": ("🟢", "unlocked"),
    "open": ("🟢", "opened"),
    "status": ("ℹ️", "status checked"),
}

MAX_AGE_SECONDS = 30  # text-message replay-protection window
MAX_APP_AGE_SECONDS = 45  # webxdc button-press replay-protection window

# Strip control chars / newlines from values that arrive over the wire
# and might end up in log lines or chat messages.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(value, fallback: str = "?", max_len: int = 64) -> str:
    if not isinstance(value, str):
        value = str(value) if value is not None else ""
    cleaned = _CTRL_RE.sub(" ", value).strip()
    if not cleaned:
        return fallback
    return cleaned[:max_len]

# ------------------------------------------------------- env-derived config

ALLOWED_CHATS: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_CHATS", "").split(",") if x.strip()
}
DOOR_NAME = (os.environ.get("DOOR_NAME") or "").strip() or "Door"

# ----------------------------------------------------- persistent state map

STATE_DIR = Path(user_config_dir(BOT_NAME))
APP_MSGIDS_PATH = STATE_DIR / "app_msgids.json"


def _load_msgids() -> dict[int, dict[str, int]]:
    """Load chat_id -> {app_id -> msgid} from disk.

    The state layout evolved from a flat list-per-chat (just msgids,
    no app identity) to a dict-per-chat (app_id -> msgid) so /apps
    can be idempotent. Legacy list entries -- which don't record
    which msgid was which app -- are dropped on load with an info
    log; the next /apps in that chat re-seeds cleanly. Legacy
    single-int entries (older still) are treated the same way.
    """
    try:
        raw = json.loads(APP_MSGIDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out: dict[int, dict[str, int]] = {}
    legacy_dropped: list[int] = []
    for k, v in raw.items():
        try:
            chat = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            apps: dict[str, int] = {}
            for app_id, msgid in v.items():
                try:
                    apps[str(app_id)] = int(msgid)
                except (TypeError, ValueError):
                    continue
            if apps:
                out[chat] = apps
        else:
            # Legacy shape (list[int] or single int): we can't recover
            # the per-app identity, so drop it. /apps will re-seed.
            legacy_dropped.append(chat)
    if legacy_dropped:
        # Use print here rather than bot.logger -- called at import time,
        # before the BotCli logger is wired up.
        print(
            f"[gatekeeper-bot] dropping legacy app_msgids entries for chats "
            f"{legacy_dropped}; run /apps in each chat to re-seed",
            flush=True,
        )
        # Rewrite the file so the next startup doesn't re-log the same
        # drop; without this the legacy entries linger on disk forever
        # (they're only read, never replaced, until a /apps call).
        try:
            _save_msgids(out)
        except Exception as ex:
            print(f"[gatekeeper-bot] failed to rewrite app_msgids.json: {ex}",
                  flush=True)
    return out


def _save_msgids(data: dict[int, dict[str, int]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = APP_MSGIDS_PATH.with_suffix(".tmp")
    serialised = {
        str(chat): {str(app_id): int(msgid) for app_id, msgid in apps.items()}
        for chat, apps in data.items()
    }
    tmp.write_text(json.dumps(serialised))
    os.replace(tmp, APP_MSGIDS_PATH)


_msgid_map: dict[int, dict[str, int]] = _load_msgids()
_last_known_state: str = "unknown"
_last_state_ts: int = 0   # Unix seconds; 0 = never set
_last_battery_low: bool = False

# ----------------------------------------------------------- output parser

# 'device locked' / 'device unlocked' / 'device opened' (case-insensitive)
_RESULT_RE = re.compile(r"^\s*device\s+(locked|unlocked|opened)\s*$", re.IGNORECASE)
# 'device status = {'lock_status': 'LOCKED', ...}' -- handles single/double quotes
_STATUS_RE = re.compile(r"['\"]?lock_status['\"]?\s*[:=]\s*['\"]?(\w+)", re.IGNORECASE)
# 'battery_low': True -- parses the battery_low key from the same dict repr.
_BATTERY_RE = re.compile(
    r"['\"]?battery_low['\"]?\s*[:=]\s*(True|False|true|false|1|0)"
)


def parse_state_from_output(text: str) -> str | None:
    """Return 'locked' or 'unlocked' if the output reports a state, else None."""
    for line in text.splitlines():
        m = _RESULT_RE.match(line)
        if m:
            return "locked" if m.group(1).lower() == "locked" else "unlocked"
        m = _STATUS_RE.search(line)
        if m:
            v = m.group(1).upper()
            if v == "LOCKED":
                return "locked"
            if v in {"UNLOCKED", "OPENED"}:
                return "unlocked"
    return None


def parse_battery_low_from_output(text: str) -> bool | None:
    """Return True/False if the output carries a battery_low field, else None.

    None means "not reported" (e.g. lock/unlock commands, which don't emit
    a status dict). Callers should preserve the last-known value in that
    case, not overwrite it with False.
    """
    for line in text.splitlines():
        m = _BATTERY_RE.search(line)
        if m:
            return m.group(1).lower() in ("true", "1")
    return None


# ---------------------------------------------------------------- app push

def _push_state(bot, accid: int, state: str) -> int:
    """Broadcast `state` to every known app instance in an ALLOWED chat.

    Skips msgids in chats that are no longer in ALLOWED_CHATS -- the
    state file may carry leftovers from chats the admin has since
    removed from the allow-list.
    """
    update = {
        "payload": {
            "response": {
                "name": "bot",
                "text": state,
                "battery_low": _last_battery_low,
                "ts": _last_state_ts,
            }
        }
    }
    body = json.dumps(update)
    pushed = 0
    for chatid, apps in list(_msgid_map.items()):
        if not _is_allowed(chatid):
            continue
        for msgid in apps.values():
            try:
                bot.rpc.send_webxdc_status_update(accid, msgid, body, "")
                pushed += 1
            except Exception as ex:
                bot.logger.warning(
                    f"push state to chat {chatid} msgid {msgid} failed: {ex}"
                )
    return pushed


def _push_door_name(bot, accid: int, msgid: int) -> None:
    body = json.dumps({"payload": {"config": {"door_name": DOOR_NAME}}})
    try:
        bot.rpc.send_webxdc_status_update(accid, msgid, body, "")
    except Exception as ex:
        bot.logger.warning(f"push door_name to msgid {msgid} failed: {ex}")


def _push_ack(bot, accid: int, cmd: str) -> int:
    """Tell every active app instance that we received a command and are
    about to run it. Apps may use this to show a transitional 'in
    progress' visual without guessing how long the BLE round-trip
    takes. Apps that don't care just ignore the payload.
    """
    body = json.dumps({"payload": {"ack": cmd}})
    pushed = 0
    for chatid, apps in list(_msgid_map.items()):
        if not _is_allowed(chatid):
            continue
        for msgid in apps.values():
            try:
                bot.rpc.send_webxdc_status_update(accid, msgid, body, "")
                pushed += 1
            except Exception as ex:
                bot.logger.warning(
                    f"ack push to chat {chatid} msgid {msgid} failed: {ex}"
                )
    return pushed


def _send_apps(
    bot, accid: int, chatid: int, *, force: bool = False
) -> tuple[list[str], list[str]]:
    """Idempotently deliver every available .xdc to `chatid`.

    For each app discovered in `apps/*.xdc`:
      * if this chat already has a *live* instance of that app (tracked
        in `_msgid_map` and `bot.rpc.get_message` doesn't raise), skip
        the binary resend -- the end-of-function `_push_state` still
        refreshes the UI;
      * otherwise send the .xdc, store the new msgid under its app_id.

    When `force=True`, the tracked dict for this chat is wiped up front
    and everything is re-sent. Used by `/apps reset`.

    Returns `(sent, refreshed)` -- lists of app_ids in each bucket,
    suitable for assembling a human-readable reply.
    """
    sent: list[str] = []
    refreshed: list[str] = []
    paths = _xdc_paths()
    if not paths:
        bot.logger.warning(f"no .xdc files found in {APPS_DIR}; nothing to send")
        return sent, refreshed

    if force:
        _msgid_map.pop(chatid, None)

    chat_apps = _msgid_map.setdefault(chatid, {})

    for app_id, path in paths:
        existing = chat_apps.get(app_id)
        live = False
        if existing is not None:
            try:
                # get_message raises for an unknown / deleted msgid.
                bot.rpc.get_message(accid, existing)
                live = True
            except Exception as ex:
                bot.logger.info(
                    f"tracked msgid {existing} for app {app_id!r} in chat "
                    f"{chatid} no longer available ({ex}); re-sending"
                )

        if live:
            # Keep the existing instance; refresh its door_name so any
            # env change since last /apps propagates. State refresh
            # happens uniformly via _push_state below.
            _push_door_name(bot, accid, existing)
            refreshed.append(app_id)
            bot.logger.info(
                f"app {app_id!r} already in chat {chatid} msgid={existing}; skipping resend"
            )
            continue

        try:
            msgid = int(bot.rpc.send_msg(accid, chatid, MsgData(file=path)))
        except Exception as ex:
            bot.logger.error(f"send app {app_id!r} to chat {chatid} failed: {ex}")
            continue
        chat_apps[app_id] = msgid
        sent.append(app_id)
        bot.logger.info(f"app {app_id!r} sent to chat {chatid} msgid={msgid}")
        _push_door_name(bot, accid, msgid)

    if sent or refreshed:
        _save_msgids(_msgid_map)
        # State push is cheap and keeps both freshly-sent and
        # already-present instances in sync.
        _push_state(bot, accid, _last_known_state)
    return sent, refreshed


# ----------------------------------------------------- shared command path

def run_lock_command(
    bot,
    accid: int,
    chatid: int,
    source_msgid: int | None,
    command: str,
    actor_name: str | None = None,
) -> None:
    """Execute send-command.sh, echo output to chat, broadcast state.

    `source_msgid` is the user's text-message id for reactions; pass None
    for app-driven invocations.
    `actor_name` is the webxdc selfName for app-driven invocations; pass
    None for text-driven ones (then no audit line is sent -- the user's
    own text message already names them).

    Retries and SEC_LEVEL fallback are handled by send-command.sh.
    """
    global _last_known_state, _last_state_ts, _last_battery_low

    # Defence-in-depth: also check here, even though every caller already
    # checks the whitelist.
    if command not in VALID_COMMANDS:
        bot.logger.warning(f"refusing unknown command {command!r}")
        if source_msgid is not None:
            bot.rpc.send_reaction(accid, source_msgid, ["❌"])
        return

    if source_msgid is not None:
        bot.rpc.send_reaction(accid, source_msgid, ["⌛"])

    proc = subprocess.run(
        [SEND_COMMAND_SH, command],
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Text path: echo raw subprocess output (existing behaviour).
    # App path: stay silent here -- the audit line below speaks for the
    # app, and the raw 'device opened' line would just duplicate it.
    if actor_name is None:
        for line in proc.stdout.splitlines():
            if line.strip():
                bot.rpc.send_msg(accid, chatid, MsgData(text=line))
        for line in proc.stderr.splitlines():
            if line.strip():
                bot.rpc.send_msg(accid, chatid, MsgData(text=line))

    # Audit line for app-triggered commands. Skip 'status' because it is
    # read-only and noisy (the app auto-requests it on open).
    if actor_name is not None and command != "status":
        emoji, _verb = _AUDIT_VERB.get(command, ("🔧", command))
        if proc.returncode == 0:
            audit = f"{emoji} {DOOR_NAME} {actor_name}"
        else:
            audit = f"❌ {DOOR_NAME} {actor_name} ({command} failed)"
        bot.rpc.send_msg(accid, chatid, MsgData(text=audit))

    if proc.returncode != 0:
        new_state = "error"
        bot.logger.warning(
            f"send-command.sh {command} rc={proc.returncode}"
        )
    else:
        new_state = parse_state_from_output(proc.stdout) or "unknown"

    # Only status output carries battery_low; preserve the cached value
    # (from the last status probe) for lock/unlock/open.
    battery_low = parse_battery_low_from_output(proc.stdout)
    if battery_low is not None:
        _last_battery_low = battery_low

    _last_known_state = new_state
    _last_state_ts = int(time.time())
    pushed = _push_state(bot, accid, new_state)
    bot.logger.info(
        f"send-command.sh {command} -> state={new_state}; "
        f"battery_low={_last_battery_low}; pushed to {pushed}"
    )

    # Low-battery warning is chat-visible on both paths. Only emit it when
    # this call freshly observed the flag -- otherwise /lock would echo a
    # stale warning on every operation.
    if battery_low:
        bot.rpc.send_msg(
            accid, chatid,
            MsgData(text=f"🪫 {DOOR_NAME}: battery low — please replace batteries"),
        )

    if source_msgid is not None:
        bot.rpc.send_reaction(
            accid, source_msgid, ["🆗" if proc.returncode == 0 else "❌"]
        )


# -------------------------------------------------------------- permission

def _is_allowed(chatid: int) -> bool:
    return chatid in ALLOWED_CHATS


# ------------------------------------------------------------------ hooks

@cli.on(events.RawEvent)
def log_event(bot, accid, event):
    bot.logger.info(event)


@cli.on(events.RawEvent)
def on_webxdc_update(bot, accid, event):
    if event.kind != EventType.WEBXDC_STATUS_UPDATE:
        return

    msgid = event.msg_id
    serial = event.status_update_serial - 1
    raw = bot.rpc.get_webxdc_status_updates(accid, msgid, serial)
    try:
        update = json.loads(raw)[0]
    except (json.JSONDecodeError, IndexError):
        bot.logger.warning(f"failed to decode webxdc update msgid={msgid}")
        return

    payload = update.get("payload") or {}
    req = payload.get("request") if isinstance(payload, dict) else None
    if not isinstance(req, dict):
        return  # not a user request -- our own response or unrelated update

    # Defensive: req["text"] / "name" / "app" might not be strings if the
    # peer sends a malformed payload. Cast + sanitize first so bad input
    # can't crash str ops below or pollute log/chat lines.
    cmd_raw = req.get("text")
    cmd = str(cmd_raw).strip().lower() if cmd_raw is not None else ""
    name = _sanitize(req.get("name"))
    app_id = _sanitize(req.get("app"))

    msg = bot.rpc.get_message(accid, msgid)
    chatid = msg.chat_id

    bot.logger.info(
        f"app cmd from chat {chatid} ({name} via {app_id}): {cmd!r}"
    )

    if not _is_allowed(chatid):
        bot.logger.warning(f"app cmd from non-allowed chat {chatid} rejected")
        return

    # Opportunistic learning: if this msgid isn't in our persisted map
    # (e.g. app was sent by an earlier bot version, /apps was never used,
    # or .json got out of sync), record it now and seed it with current
    # door_name + state so the icon updates immediately rather than
    # waiting for the next state-changing command.
    #
    # Under the per-app tracking shape we need to know *which* app this
    # msgid is. We derive the app_id from the attached filename's stem
    # (gatekeeper.xdc -> "gatekeeper"); that matches the app_id used by
    # _send_apps (`_xdc_paths()` returns `p.stem`). If we can't derive
    # it (filename missing or doesn't end in .xdc), we log and skip --
    # the user's next /apps reset will reseed properly.
    chat_apps = _msgid_map.setdefault(chatid, {})
    if msgid not in chat_apps.values():
        learned_id: str | None = None
        for attr in ("file_name", "filename"):
            fname = getattr(msg, attr, None)
            if isinstance(fname, str) and fname.endswith(".xdc"):
                learned_id = Path(fname).stem
                break
        if learned_id:
            chat_apps[learned_id] = msgid
            _save_msgids(_msgid_map)
            bot.logger.info(
                f"learned app {learned_id!r} msgid={msgid} for chat {chatid}"
            )
            _push_door_name(bot, accid, msgid)
            try:
                bot.rpc.send_webxdc_status_update(
                    accid, msgid,
                    json.dumps({"payload": {"response": {
                        "name": "bot",
                        "text": _last_known_state,
                        "battery_low": _last_battery_low,
                        "ts": _last_state_ts,
                    }}}),
                    "",
                )
            except Exception as ex:
                bot.logger.warning(f"seed state push to msgid {msgid} failed: {ex}")
        else:
            bot.logger.info(
                f"could not derive app_id for msgid={msgid} in chat {chatid} "
                f"(no .xdc file_name); not learning -- run /apps reset to reseed"
            )

    if cmd not in VALID_COMMANDS:
        bot.logger.warning(f"refusing webxdc command {cmd!r}")
        return

    # Replay protection: drop stale button presses that arrive after
    # the bot reconnects from an offline period. Apps built before
    # this feature don't send `ts` -- accept those with a log line so
    # users can re-install via /apps reset on their own schedule.
    ts = req.get("ts")
    if isinstance(ts, (int, float)):
        age = int(time.time()) - int(ts)
        if age > MAX_APP_AGE_SECONDS:
            bot.logger.info(
                f"app cmd {cmd!r} from chat {chatid} ({name} via {app_id}) "
                f"age={age}s > {MAX_APP_AGE_SECONDS}s -> ignored"
            )
            return
    else:
        bot.logger.info(
            f"app cmd {cmd!r} from chat {chatid} ({name} via {app_id}) "
            f"has no ts field -- accepting (app predates replay protection; "
            f"suggest /apps reset)"
        )

    # Tell apps "command received, working on it" before the BLE
    # round-trip blocks the event loop for several seconds. Apps that
    # render a transitional state can switch immediately.
    _push_ack(bot, accid, cmd)

    run_lock_command(
        bot, accid, chatid,
        source_msgid=None,
        command=cmd,
        actor_name=name,
    )


@cli.on(events.NewMessage)
def on_new_message(bot, accid, event):
    msg = event.msg
    chatid = msg.chat_id
    text = (msg.text or "").strip()

    # /id is intentionally permission-free -- needed for setup discovery.
    if text == "/id":
        bot.rpc.send_msg(
            accid,
            chatid,
            MsgData(
                text=f"the id of this chat is {chatid}, "
                "add this to the allowlist to allow opening "
                "the door from this group"
            ),
        )
        return

    text_cmd_map = {
        "/unlock": "unlock", "/auf": "unlock",
        "/lock": "lock", "/zu": "lock",
        "/status": "status",
    }

    if text in text_cmd_map:
        if not _is_allowed(chatid):
            bot.rpc.send_msg(accid, chatid, MsgData(text="permission denied"))
            return
        age = int(time.time()) - msg.timestamp
        if age > MAX_AGE_SECONDS:
            bot.logger.info(
                f"text command {text!r} in msg {msg.id} chat {chatid} "
                f"age={age}s > {MAX_AGE_SECONDS}s -> ignored"
            )
            bot.rpc.send_reaction(accid, msg.id, ["❌"])
            return
        run_lock_command(
            bot, accid, chatid,
            source_msgid=msg.id,
            command=text_cmd_map[text],
        )
        return

    parts = text.split()
    if parts and parts[0] == "/apps":
        if not _is_allowed(chatid):
            bot.rpc.send_msg(accid, chatid, MsgData(text="permission denied"))
            return
        # /apps is idempotent by default: re-sends only apps that aren't
        # already installed in this chat. /apps reset wipes the tracking
        # and sends every app fresh -- the escape hatch for "I deleted
        # the message locally and want a clean copy".
        force = len(parts) > 1 and parts[1].lower() == "reset"
        sent, refreshed = _send_apps(bot, accid, chatid, force=force)
        if force:
            if sent:
                reply = f"Apps reset: sent {', '.join(sent)}."
            else:
                reply = "No apps available to send."
        elif sent and refreshed:
            reply = (
                f"Sent: {', '.join(sent)}. "
                f"Already present (state refreshed): {', '.join(refreshed)}."
            )
        elif sent:
            reply = f"Sent: {', '.join(sent)}."
        elif refreshed:
            reply = (
                f"All apps already present in this chat "
                f"(state refreshed): {', '.join(refreshed)}. "
                f"Use '/apps reset' to force a fresh send."
            )
        else:
            reply = "No apps available to send."
        bot.rpc.send_msg(accid, chatid, MsgData(text=reply))
        return

    help_text = os.environ.get("HELP_MESSAGE", DEFAULT_HELP_MESSAGE)
    bot.rpc.send_msg(accid, chatid, MsgData(text=help_text))


# ---------------------------------------------------------------- startup

@cli.on_start
def _on_start(bot, _args):
    global _last_known_state, _last_state_ts, _last_battery_low

    total_instances = sum(len(v) for v in _msgid_map.values())
    bot.logger.info(
        f"gatekeeper-bot starting; allowed_chats={sorted(ALLOWED_CHATS)} "
        f"door_name={DOOR_NAME!r} known_chats={len(_msgid_map)} "
        f"app_instances={total_instances}"
    )

    # Seed the icon by querying the lock once so app instances opened
    # before any user action already show the right state.
    try:
        proc = subprocess.run(
            [SEND_COMMAND_SH, "status"],
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode == 0:
            _last_known_state = parse_state_from_output(proc.stdout) or "unknown"
            battery_low = parse_battery_low_from_output(proc.stdout)
            if battery_low is not None:
                _last_battery_low = battery_low
        else:
            _last_known_state = "error"
        _last_state_ts = int(time.time())
        bot.logger.info(
            f"startup status probe -> {_last_known_state}; "
            f"battery_low={_last_battery_low}"
        )
    except Exception as ex:
        bot.logger.warning(f"startup status probe failed: {ex}")
        _last_known_state = "unknown"
        _last_state_ts = int(time.time())

    # Push door_name + state to every known instance in an allowed chat
    # (silent, info=""). _push_state already filters; do the same here.
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        return
    accid = accounts[0]
    for chatid, apps in list(_msgid_map.items()):
        if not _is_allowed(chatid):
            continue
        for msgid in apps.values():
            _push_door_name(bot, accid, msgid)
    _push_state(bot, accid, _last_known_state)


if __name__ == "__main__":
    cli.start()

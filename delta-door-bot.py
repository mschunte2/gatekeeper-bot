#!/usr/bin/env python3
"""Gatekeeper bot: text + webxdc app control of an Eqiva Smart Lock.

Text path:
    /lock | /zu       -> send-command.sh lock
    /unlock | /auf    -> send-command.sh unlock
    /status           -> send-command.sh status
    /id               -> reply with this chat's id (always allowed)
    /apps             -> (re)send all webxdc apps to this chat
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
    " /apps - (re)send all webxdc control apps to this chat\n"
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
    "unlock": ("🔓", "unlocked"),
    "open": ("🔓", "opened"),
    "status": ("ℹ️", "status checked"),
}

MAX_AGE_SECONDS = 30  # text-message replay-protection window

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


def _load_msgids() -> dict[int, list[int]]:
    """Load chat_id -> [msgid, ...] from disk.

    Tolerates the legacy single-int-per-chat layout.
    """
    try:
        raw = json.loads(APP_MSGIDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out: dict[int, list[int]] = {}
    for k, v in raw.items():
        try:
            chat = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, list):
            ids = [int(m) for m in v if isinstance(m, (int, str)) and str(m).strip()]
        else:
            try:
                ids = [int(v)]
            except (TypeError, ValueError):
                ids = []
        if ids:
            out[chat] = ids
    return out


def _save_msgids(data: dict[int, list[int]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = APP_MSGIDS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({str(k): [int(m) for m in v] for k, v in data.items()}))
    os.replace(tmp, APP_MSGIDS_PATH)


_msgid_map: dict[int, list[int]] = _load_msgids()
_last_known_state: str = "unknown"

# ----------------------------------------------------------- output parser

# 'device locked' / 'device unlocked' / 'device opened' (case-insensitive)
_RESULT_RE = re.compile(r"^\s*device\s+(locked|unlocked|opened)\s*$", re.IGNORECASE)
# 'device status = {'lock_status': 'LOCKED', ...}' -- handles single/double quotes
_STATUS_RE = re.compile(r"['\"]?lock_status['\"]?\s*[:=]\s*['\"]?(\w+)", re.IGNORECASE)


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


# ---------------------------------------------------------------- app push

def _push_state(bot, accid: int, state: str) -> int:
    """Broadcast `state` to every known app instance in an ALLOWED chat.

    Skips msgids in chats that are no longer in ALLOWED_CHATS -- the
    state file may carry leftovers from chats the admin has since
    removed from the allow-list.
    """
    update = {"payload": {"response": {"name": "bot", "text": state}}}
    body = json.dumps(update)
    pushed = 0
    for chatid, msgids in list(_msgid_map.items()):
        if not _is_allowed(chatid):
            continue
        for msgid in msgids:
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
    for chatid, msgids in list(_msgid_map.items()):
        if not _is_allowed(chatid):
            continue
        for msgid in msgids:
            try:
                bot.rpc.send_webxdc_status_update(accid, msgid, body, "")
                pushed += 1
            except Exception as ex:
                bot.logger.warning(
                    f"ack push to chat {chatid} msgid {msgid} failed: {ex}"
                )
    return pushed


def _send_apps(bot, accid: int, chatid: int) -> list[int]:
    """Send every available .xdc to `chatid` and remember the new msgids.

    Replaces any previously-tracked msgids for this chat -- /apps means
    "fresh set". Old in-chat app messages still work but are no longer
    refreshed by silent state pushes.
    """
    sent: list[int] = []
    paths = _xdc_paths()
    if not paths:
        bot.logger.warning(f"no .xdc files found in {APPS_DIR}; nothing to send")
        return sent
    for app_id, path in paths:
        try:
            msgid = int(bot.rpc.send_msg(accid, chatid, MsgData(file=path)))
        except Exception as ex:
            bot.logger.error(f"send app {app_id!r} to chat {chatid} failed: {ex}")
            continue
        sent.append(msgid)
        bot.logger.info(f"app {app_id!r} sent to chat {chatid} msgid={msgid}")
        _push_door_name(bot, accid, msgid)
    if sent:
        _msgid_map[chatid] = sent
        _save_msgids(_msgid_map)
        # Seed state for every freshly-sent instance.
        _push_state(bot, accid, _last_known_state)
    return sent


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
    global _last_known_state

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

    _last_known_state = new_state
    pushed = _push_state(bot, accid, new_state)
    bot.logger.info(
        f"send-command.sh {command} -> state={new_state}; pushed to {pushed}"
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
    existing = _msgid_map.get(chatid, [])
    if msgid not in existing:
        _msgid_map[chatid] = existing + [msgid]
        _save_msgids(_msgid_map)
        bot.logger.info(f"learned app msgid={msgid} for chat {chatid}")
        _push_door_name(bot, accid, msgid)
        try:
            bot.rpc.send_webxdc_status_update(
                accid, msgid,
                json.dumps({"payload": {"response": {"name": "bot",
                                                     "text": _last_known_state}}}),
                "",
            )
        except Exception as ex:
            bot.logger.warning(f"seed state push to msgid {msgid} failed: {ex}")

    if cmd not in VALID_COMMANDS:
        bot.logger.warning(f"refusing webxdc command {cmd!r}")
        return

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

    if text == "/apps":
        if not _is_allowed(chatid):
            bot.rpc.send_msg(accid, chatid, MsgData(text="permission denied"))
            return
        _send_apps(bot, accid, chatid)
        return

    help_text = os.environ.get("HELP_MESSAGE", DEFAULT_HELP_MESSAGE)
    bot.rpc.send_msg(accid, chatid, MsgData(text=help_text))


# ---------------------------------------------------------------- startup

@cli.on_start
def _on_start(bot, _args):
    global _last_known_state

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
        else:
            _last_known_state = "error"
        bot.logger.info(f"startup status probe -> {_last_known_state}")
    except Exception as ex:
        bot.logger.warning(f"startup status probe failed: {ex}")
        _last_known_state = "unknown"

    # Push door_name + state to every known instance in an allowed chat
    # (silent, info=""). _push_state already filters; do the same here.
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        return
    accid = accounts[0]
    for chatid, msgids in list(_msgid_map.items()):
        if not _is_allowed(chatid):
            continue
        for msgid in msgids:
            _push_door_name(bot, accid, msgid)
    _push_state(bot, accid, _last_known_state)


if __name__ == "__main__":
    cli.start()

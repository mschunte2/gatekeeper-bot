#!/usr/bin/env python3
"""Gatekeeper bot: text + webxdc app control of an Eqiva Smart Lock.

Text path (UNCHANGED behaviour):
    /lock | /zu       -> send-command.sh lock
    /unlock | /auf    -> send-command.sh unlock
    /status           -> send-command.sh status
    /id               -> reply with this chat's id (always allowed)
    /app              -> (re)send the webxdc app to this chat
    anything else     -> help text

App path (NEW):
    closed-lock button -> 'lock'
    open-lock button   -> 'open'
    door button        -> 'status'

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
from deltachat2 import MsgData, events
from deltabot_cli import BotCli

# EventType lives in different namespaces across deltachat2 / deltabot_cli
# versions; try the modern path first.
try:
    from deltachat2.const import EventType
except ImportError:  # pragma: no cover -- older layout
    from deltabot_cli.const import EventType

cli = BotCli("gatekeeper")

# ---------------------------------------------------------------- constants

DEFAULT_HELP_MESSAGE = (
    "This bot operates a lock:\n"
    " /lock or /zu - locks gate\n"
    " /unlock or /auf - unlocks gate\n"
    " /status - print current status\n"
    " /app - (re)send the webxdc control app to this chat\n"
    " /id - get this chat's id\n"
    " - Any other input shows this message.\n"
    " NOTE: lock operations can take up to 90 seconds."
)

HERE = Path(__file__).resolve().parent
XDC_PATH = str(HERE / "app.xdc")
SEND_COMMAND_SH = str(HERE / "send-command.sh")

# Whitelist of accepted lock-operation tokens. Used by BOTH text and app
# paths. Anything else is rejected before reaching the subprocess.
VALID_COMMANDS = {"lock", "unlock", "open", "status"}

MAX_AGE_SECONDS = 30  # text-message replay-protection window

# ------------------------------------------------------- env-derived config

ALLOWED_CHATS: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_CHATS", "").split(",") if x.strip()
}
DOOR_NAME = (os.environ.get("DOOR_NAME") or "").strip() or "Door"

# ----------------------------------------------------- persistent state map

STATE_DIR = Path(user_config_dir("gatekeeper"))
APP_MSGIDS_PATH = STATE_DIR / "app_msgids.json"


def _load_msgids() -> dict[int, int]:
    try:
        raw = json.loads(APP_MSGIDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out: dict[int, int] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _save_msgids(data: dict[int, int]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = APP_MSGIDS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({str(k): int(v) for k, v in data.items()}))
    os.replace(tmp, APP_MSGIDS_PATH)


_msgid_map: dict[int, int] = _load_msgids()
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
    """Broadcast `state` to every known app instance. Returns push count."""
    update = {"payload": {"response": {"name": "bot", "text": state}}}
    body = json.dumps(update)
    pushed = 0
    for chatid, msgid in list(_msgid_map.items()):
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


def _send_app(bot, accid: int, chatid: int) -> int | None:
    """Send the .xdc to `chatid` and remember the new msgid."""
    try:
        msgid = bot.rpc.send_msg(accid, chatid, MsgData(file=XDC_PATH))
    except Exception as ex:
        bot.logger.error(f"send app to chat {chatid} failed: {ex}")
        return None
    msgid = int(msgid)
    _msgid_map[chatid] = msgid
    _save_msgids(_msgid_map)
    bot.logger.info(f"app sent to chat {chatid} msgid={msgid}")
    _push_door_name(bot, accid, msgid)
    _push_state(bot, accid, _last_known_state)
    return msgid


# ----------------------------------------------------- shared command path

def run_lock_command(
    bot,
    accid: int,
    chatid: int,
    source_msgid: int | None,
    command: str,
) -> None:
    """Execute send-command.sh, echo output to chat, broadcast state.

    `source_msgid` is the user's text-message id for reactions; pass None
    for app-driven invocations (no reactions then).
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

    # Echo each non-empty line back to the originating chat (preserves the
    # original text-bot behaviour). Done for app path too so chat history
    # stays useful.
    for line in proc.stdout.splitlines():
        if line.strip():
            bot.rpc.send_msg(accid, chatid, MsgData(text=line))
    for line in proc.stderr.splitlines():
        if line.strip():
            bot.rpc.send_msg(accid, chatid, MsgData(text=line))

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

    cmd = (req.get("text") or "").strip().lower()
    name = req.get("name") or "?"

    msg = bot.rpc.get_message(accid, msgid)
    chatid = msg.chat_id

    bot.logger.info(f"app cmd from chat {chatid} ({name}): {cmd!r}")

    if not _is_allowed(chatid):
        bot.logger.warning(f"app cmd from non-allowed chat {chatid} rejected")
        return

    if cmd not in VALID_COMMANDS:
        bot.logger.warning(f"refusing webxdc command {cmd!r}")
        return

    run_lock_command(bot, accid, chatid, source_msgid=None, command=cmd)


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

    if text == "/app":
        if not _is_allowed(chatid):
            bot.rpc.send_msg(accid, chatid, MsgData(text="permission denied"))
            return
        _send_app(bot, accid, chatid)
        return

    help_text = os.environ.get("HELP_MESSAGE", DEFAULT_HELP_MESSAGE)
    bot.rpc.send_msg(accid, chatid, MsgData(text=help_text))


# ---------------------------------------------------------------- startup

@cli.on_start
def _on_start(bot, _args):
    global _last_known_state

    bot.logger.info(
        f"gatekeeper-bot starting; allowed_chats={sorted(ALLOWED_CHATS)} "
        f"door_name={DOOR_NAME!r} known_apps={len(_msgid_map)}"
    )

    # Seed the icon by querying the lock once (per design point #3).
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

    # Push door_name + state to every known instance (silent, info="").
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        return
    accid = accounts[0]
    for _chatid, msgid in list(_msgid_map.items()):
        _push_door_name(bot, accid, msgid)
    _push_state(bot, accid, _last_known_state)


if __name__ == "__main__":
    cli.start()

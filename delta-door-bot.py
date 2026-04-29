#!/usr/bin/env python3
"""Gatekeeper bot: text + webxdc app control of an Eqiva Smart Lock.

Text path:
    /lock | /zu       -> send-command.sh lock
    /unlock | /auf    -> send-command.sh unlock  (retract bolt only)
    /open | /oeffnen  -> send-command.sh open    (retract bolt + latch)
    /status           -> send-command.sh status
    /id               -> reply with this chat's id (always allowed)
    /apps             -> (re)deliver webxdc apps; replaces prior copies
                         so late-joining chat members get a fresh install
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
    " /unlock or /auf - unlocks gate (retracts bolt only)\n"
    " /open or /oeffnen - opens gate (retracts bolt and latch)\n"
    " /status - print current status\n"
    " /apps - (re)deliver webxdc control apps; replaces prior copies so late-joining chat members get a fresh install\n"
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

# Audit line emojis + verbs, keyed by the thing the audit is about.
# Command keys (lock/unlock/open/status) apply to app-button presses;
# the "unknown" state key applies when a status probe catches a manual
# key/knob turn (the lock reports UNKNOWN because it cannot determine
# bolt direction after a physical operation). Command and state keys
# don't collide, so one table covers both cases.
_AUDIT = {
    "lock":    ("🔒",  "locked"),
    "unlock":  ("🟢",  "unlocked"),
    "open":    ("🟢",  "opened"),
    "status":  ("ℹ️",  "status checked"),
    "unknown": ("❓",  "Manual lock/unlock"),
}

# Replay-protection windows. Any command older than these values when
# the bot actually processes it is dropped.
#
# MAX_AGE_SECONDS (text) is sized to exceed the worst realistic BLE
# stall. If send-command.sh hits its retry path (first attempt timeout
# + second attempt under SEC_LEVEL=low), a single status/lock call can
# block the event loop for roughly TIMEOUT*2 + cleanup overhead --
# ~70-80 s with the current TIMEOUT=25 (was ~3 min when TIMEOUT=90,
# tightened 2026-04-29 after a perfectly reachable lock spent 180 s
# in retry purgatory). A typed /status arriving during that stall
# would previously age past a 60 s window and be silently dropped
# (observed 2026-04-22). 200 s keeps the guard meaningful against
# genuinely stale replays while absorbing a single retry cascade
# even if the per-attempt timeout is loosened again later.
#
# MAX_APP_AGE_SECONDS (webxdc) stays tight: webxdc button taps are
# rarely typed during a stall (the app shows its own pending state),
# and the main threat this window exists for -- a pile-up of queued
# button-press updates replaying after the bot reconnects from an
# offline period -- is better served by a short window.
MAX_AGE_SECONDS = 200
MAX_APP_AGE_SECONDS = 45

# Tolerance for sender clocks that run slightly ahead of ours. Replay
# protection still rejects messages from the meaningful future, but a
# few seconds of normal cross-device clock drift no longer kills
# commands. (Observed 2026-04-26: an age=-1s /status was rejected
# right after a Pi reboot before NTP fully settled.)
MAX_CLOCK_SKEW_SECONDS = 30

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


# Comma-separated list of integer chat ids. A malformed entry raises
# ValueError at import time and systemd surfaces it immediately --
# better than silently ignoring the bad entry and staring at a
# mystery "permission denied" at runtime.
ALLOWED_CHATS: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_CHATS", "").split(",") if x.strip()
}
DOOR_NAME = (os.environ.get("DOOR_NAME") or "").strip() or "Door"

# ----------------------------------------------------- persistent state map

STATE_DIR = Path(user_config_dir(BOT_NAME))
APP_MSGIDS_PATH = STATE_DIR / "app_msgids.json"


def _load_msgids() -> dict[int, dict[str, int]]:
    """Load chat_id -> {app_id -> msgid} from disk.

    Only `_save_msgids` writes this file, and it always writes
    well-formed data, so we don't defensively filter bad entries --
    a parse error means corruption or a manual edit gone wrong, and
    starting from empty tracking is the correct recovery (next /apps
    reseeds every chat).
    """
    try:
        raw = json.loads(APP_MSGIDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {
        int(chat): {str(app_id): int(msgid) for app_id, msgid in apps.items()}
        for chat, apps in raw.items()
    }


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


def parse_lock_output(text: str) -> tuple[str | None, bool | None]:
    """Return `(state, battery_low)` parsed from keyblepy's stdout.

    `state` is 'locked' / 'unlocked' / 'unknown' / None.
    - 'unknown' covers keyblepy's UNKNOWN (lock cannot determine bolt
      direction, e.g. after a manual key or knob turn) and MOVING
      (motor running, transient). These are real lock-reported states,
      not parse failures, and should not escalate to WARN.
    - None is reserved for output we could not match at all.

    `battery_low` is True / False / None. None means "not reported"
    (e.g. lock/unlock commands, which don't emit a status dict);
    callers should preserve the last-known value in that case, not
    overwrite it with False.

    Single-pass scan so the caller doesn't have to iterate twice.
    """
    state: str | None = None
    battery: bool | None = None
    for line in text.splitlines():
        if state is None:
            m = _RESULT_RE.match(line)
            if m:
                state = "locked" if m.group(1).lower() == "locked" else "unlocked"
            else:
                m = _STATUS_RE.search(line)
                if m:
                    v = m.group(1).upper()
                    if v == "LOCKED":
                        state = "locked"
                    elif v in {"UNLOCKED", "OPENED"}:
                        state = "unlocked"
                    elif v in {"UNKNOWN", "MOVING"}:
                        state = "unknown"
        if battery is None:
            m = _BATTERY_RE.search(line)
            if m:
                battery = m.group(1).lower() in ("true", "1")
    return state, battery


# ---------------------------------------------------------------- app push

def _broadcast(
    bot, accid: int, payload: dict, target_msgid: int | None = None,
) -> int:
    """Push a webxdc update to app instances.

    With `target_msgid`, push only that single instance (used for
    per-instance config updates like door_name). Without it, iterate
    every msgid in an ALLOWED chat -- chats removed from the allow-list
    since the last save are skipped so stale state doesn't leak.
    Returns the count of successful pushes.
    """
    body = json.dumps({"payload": payload})
    if target_msgid is not None:
        targets = [(None, target_msgid)]
    else:
        targets = [
            (chatid, msgid)
            for chatid, apps in list(_msgid_map.items())
            if _is_allowed(chatid)
            for msgid in apps.values()
        ]
    pushed = 0
    for chatid, msgid in targets:
        try:
            bot.rpc.send_webxdc_status_update(accid, msgid, body, "")
            pushed += 1
        except Exception as ex:
            bot.logger.warning(
                f"webxdc push to chat {chatid} msgid {msgid} failed: {ex}"
            )
    return pushed


def _push_state(bot, accid: int, state: str) -> int:
    return _broadcast(bot, accid, {"response": {
        "name": "bot",
        "text": state,
        "battery_low": _last_battery_low,
        "ts": _last_state_ts,
    }})


def _push_door_name(bot, accid: int, msgid: int) -> None:
    _broadcast(bot, accid, {"config": {"door_name": DOOR_NAME}}, target_msgid=msgid)


def _push_ack(bot, accid: int, cmd: str) -> int:
    return _broadcast(bot, accid, {"ack": cmd})


def _push_progress(bot, accid: int, progress: str) -> int:
    return _broadcast(bot, accid, {"progress": progress})


def _send_apps(
    bot, accid: int, chatid: int
) -> tuple[list[str], list[str]]:
    """Deliver every available .xdc to `chatid`, replacing prior copies.

    For each app discovered in `apps/*.xdc`: send the file, then after
    a successful send delete the chat's prior tracked msgid (if any)
    via `delete_messages_for_all` so the chat ends up with exactly one
    current copy per app.

    Also retracts apps that used to be in `apps/*.xdc` but no longer
    are (e.g. moved to `apps-disabled/`): their tracked msgids are
    deleted for all chat members and dropped from tracking.

    Why unconditional send: new chat members don't receive historical
    attachments, so an idempotent "skip if tracked" path would leave
    late joiners without any app. Sending every time (and deleting the
    prior) keeps everyone currently in the chat with exactly one fresh
    copy.

    Returns `(sent, retracted)` -- lists of app_ids in each bucket,
    for assembling a human-readable reply.
    """
    sent: list[str] = []
    retracted: list[str] = []
    paths = _xdc_paths()

    chat_apps = _msgid_map.setdefault(chatid, {})
    available_ids = {app_id for app_id, _ in paths}

    # Retract tracked apps that no longer exist in apps/ (e.g. admin
    # moved the built artefact to apps-disabled/ to unpublish it).
    for app_id in list(chat_apps.keys()):
        if app_id in available_ids:
            continue
        old_msgid = chat_apps.pop(app_id)
        try:
            bot.rpc.delete_messages_for_all(accid, [old_msgid])
            retracted.append(app_id)
            bot.logger.info(
                f"retracted app {app_id!r} from chat {chatid} "
                f"(msgid {old_msgid} deleted; no longer in apps/)"
            )
        except Exception as ex:
            bot.logger.warning(
                f"retract app {app_id!r} msgid {old_msgid} in chat {chatid} "
                f"failed: {ex}"
            )

    if not paths:
        bot.logger.warning(f"no .xdc files found in {APPS_DIR}; nothing to send")
        if retracted:
            _save_msgids(_msgid_map)
        return sent, retracted

    for app_id, path in paths:
        old_msgid = chat_apps.get(app_id)
        try:
            new_msgid = int(bot.rpc.send_msg(accid, chatid, MsgData(file=path)))
        except Exception as ex:
            bot.logger.error(f"send app {app_id!r} to chat {chatid} failed: {ex}")
            continue
        chat_apps[app_id] = new_msgid
        sent.append(app_id)
        bot.logger.info(f"app {app_id!r} sent to chat {chatid} msgid={new_msgid}")
        _push_door_name(bot, accid, new_msgid)
        # Delete the prior copy only AFTER the new one is in the chat,
        # so a delete failure never leaves the chat with no app.
        if old_msgid is not None:
            try:
                bot.rpc.delete_messages_for_all(accid, [old_msgid])
                bot.logger.info(
                    f"deleted prior {app_id!r} msgid {old_msgid} in chat {chatid}"
                )
            except Exception as ex:
                bot.logger.warning(
                    f"delete prior {app_id!r} msgid {old_msgid} in chat "
                    f"{chatid} failed: {ex}"
                )

    if sent or retracted:
        _save_msgids(_msgid_map)
        _push_state(bot, accid, _last_known_state)
    return sent, retracted


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

    assert command in VALID_COMMANDS, f"unknown command {command!r}"

    if source_msgid is not None:
        bot.rpc.send_reaction(accid, source_msgid, ["⌛"])

    # Stream the subprocess so we can react to the RETRYING_LOW_SEC
    # marker mid-flight: send-command.sh emits it on stdout right
    # before the second (low-sec) attempt, after the first has timed
    # out. We push a `progress=retrying` event to apps so the UI
    # stops looking frozen during the fallback attempt instead of
    # going silent until both attempts have finished.
    #
    # stderr=STDOUT merges streams so a single readline loop sees
    # both in write order; a separate stderr drain isn't needed and
    # we preserve true ordering for the chat echo.
    proc = subprocess.Popen(
        [SEND_COMMAND_SH, command],
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    output_lines: list[str] = []
    progress_pushed = False
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line == ">>> RETRYING_LOW_SEC":
            if not progress_pushed:
                _push_progress(bot, accid, "retrying")
                progress_pushed = True
            continue  # drop marker from chat echo and parse buffers
        output_lines.append(line)
    proc.wait()
    combined = "\n".join(output_lines)

    bot.logger.debug(
        f"send-command.sh {command} rc={proc.returncode}\n"
        f"---output---\n{combined.strip()}"
    )

    # Text path: echo raw subprocess output (existing behaviour).
    # App path: stay silent here -- the audit line below speaks for the
    # app, and the raw 'device opened' line would just duplicate it.
    if actor_name is None:
        for line in output_lines:
            if line.strip():
                bot.rpc.send_msg(accid, chatid, MsgData(text=line))

    parsed_state, parsed_battery = parse_lock_output(combined)
    if proc.returncode != 0:
        new_state = "error"
        bot.logger.warning(
            f"send-command.sh {command} rc={proc.returncode}; "
            f"raw output:\n{combined.strip()}"
        )
    elif parsed_state is None:
        bot.logger.warning(
            f"send-command.sh {command} rc=0 but state parse returned "
            f"None; raw output:\n{combined.strip()}"
        )
        new_state = "unknown"
    else:
        if parsed_state == "unknown":
            bot.logger.debug(
                f"send-command.sh {command} -> lock reported "
                f"UNKNOWN/MOVING; raw output:\n{combined.strip()}"
            )
        new_state = parsed_state

    # Audit line for app-triggered commands. Skip 'status' because it is
    # read-only and noisy (the app auto-requests it on open) -- except
    # when status flips to 'unknown', which means the lock was operated
    # manually (key/knob) since our last poll and is worth flagging.
    prev_state = _last_known_state
    manual_event = (
        new_state == "unknown"
        and prev_state not in (None, "unknown", "error")
    )
    if actor_name is not None and (command != "status" or manual_event):
        if new_state == "unknown":
            emoji, verb = _AUDIT["unknown"]
            audit = f"{emoji} {DOOR_NAME} - {verb}"
        elif proc.returncode == 0:
            emoji, _ = _AUDIT.get(command, ("🔧", command))
            audit = f"{emoji} {DOOR_NAME} {actor_name}"
        else:
            audit = f"❌ {DOOR_NAME} {actor_name} ({command} failed)"
        bot.rpc.send_msg(accid, chatid, MsgData(text=audit))

    # Only status output carries battery_low; preserve the cached value
    # (from the last status probe) for lock/unlock/open.
    if parsed_battery is not None:
        _last_battery_low = parsed_battery

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
    if parsed_battery:
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
    # Every Delta Chat core event at DEBUG -- useful when tracing an
    # issue end-to-end, but floods the journal at INFO. Run with
    # LOG_LEVEL=debug in .env when you need the full firehose.
    bot.logger.debug("%s", event)


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

    # /apps is the sole onboarding gate: apps are only recognised
    # once sent through /apps, which records their msgid in
    # _msgid_map. An update from an unknown msgid means either the
    # app was delivered out-of-band or the chat has never run /apps;
    # drop the command and point the user at the fix. This avoids
    # executing BLE commands for app instances whose state pushes we
    # can't reach anyway.
    chat_apps = _msgid_map.get(chatid, {})
    if msgid not in chat_apps.values():
        bot.logger.warning(
            f"webxdc update from unknown msgid={msgid} in chat {chatid} "
            f"({name} via {app_id}); run /apps in this chat to register"
        )
        return

    if cmd not in VALID_COMMANDS:
        bot.logger.warning(f"refusing webxdc command {cmd!r}")
        return

    # Replay protection: drop stale button presses that arrive after
    # the bot reconnects from an offline period. Ages more negative
    # than -MAX_CLOCK_SKEW_SECONDS are treated as untrusted future-
    # dated taps and also dropped. Apps built before `ts` was added
    # are no longer supported; users must /apps to pick up a current
    # build.
    ts = req.get("ts")
    if not isinstance(ts, (int, float)):
        bot.logger.info(
            f"app cmd {cmd!r} from chat {chatid} ({name} via {app_id}) "
            f"has no ts field -> ignored (run /apps to refresh the app)"
        )
        return
    age = int(time.time()) - int(ts)
    if age > MAX_APP_AGE_SECONDS or age < -MAX_CLOCK_SKEW_SECONDS:
        bot.logger.info(
            f"app cmd {cmd!r} from chat {chatid} ({name} via {app_id}) "
            f"age={age}s (limit {MAX_APP_AGE_SECONDS}s) -> ignored"
        )
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
        "/open": "open", "/oeffnen": "open",
        "/lock": "lock", "/zu": "lock",
        "/status": "status",
    }

    if text in text_cmd_map:
        if not _is_allowed(chatid):
            bot.rpc.send_msg(accid, chatid, MsgData(text="permission denied"))
            return
        age = int(time.time()) - msg.timestamp
        if age > MAX_AGE_SECONDS or age < -MAX_CLOCK_SKEW_SECONDS:
            bot.logger.info(
                f"text command {text!r} in msg {msg.id} chat {chatid} "
                f"age={age}s (limit {MAX_AGE_SECONDS}s) -> ignored"
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
        sent, retracted = _send_apps(bot, accid, chatid)
        fragments: list[str] = []
        if sent:
            fragments.append(f"Sent: {', '.join(sent)}")
        if retracted:
            fragments.append(f"Retracted: {', '.join(retracted)}")
        if not fragments:
            fragments.append("No apps available to send")
        bot.rpc.send_msg(accid, chatid, MsgData(text=". ".join(fragments) + "."))
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
    if not ALLOWED_CHATS:
        bot.logger.warning(
            "ALLOWED_CHATS is empty -- every lock command will be denied. "
            "Use /id in the target group and add the returned id to "
            "ALLOWED_CHATS in .env."
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
        bot.logger.debug(
            f"startup status probe rc={proc.returncode}\n"
            f"---stdout---\n{proc.stdout.strip()}\n"
            f"---stderr---\n{proc.stderr.strip()}"
        )
        if proc.returncode == 0:
            parsed_state, parsed_battery = parse_lock_output(proc.stdout)
            if parsed_state is None:
                bot.logger.warning(
                    f"startup status probe rc=0 but state parse returned "
                    f"None; raw stdout:\n{proc.stdout.strip()}\n"
                    f"---stderr---\n{proc.stderr.strip()}"
                )
                _last_known_state = "unknown"
            else:
                if parsed_state == "unknown":
                    bot.logger.debug(
                        f"startup status probe -> lock reported "
                        f"UNKNOWN/MOVING; raw stdout:\n"
                        f"{proc.stdout.strip()}"
                    )
                _last_known_state = parsed_state
            if parsed_battery is not None:
                _last_battery_low = parsed_battery
        else:
            _last_known_state = "error"
            bot.logger.warning(
                f"startup status probe rc={proc.returncode}; "
                f"raw stdout:\n{proc.stdout.strip()}\n"
                f"---stderr---\n{proc.stderr.strip()}"
            )
        _last_state_ts = int(time.time())
        bot.logger.info(
            f"startup status probe -> {_last_known_state}; "
            f"battery_low={_last_battery_low}"
        )
    except Exception as ex:
        bot.logger.warning(f"startup status probe failed: {ex}")
        _last_known_state = "unknown"
        _last_state_ts = int(time.time())

    # Push door_name + state to every known instance in an allowed chat.
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        return
    accid = accounts[0]
    _broadcast(bot, accid, {"config": {"door_name": DOOR_NAME}})
    _push_state(bot, accid, _last_known_state)


if __name__ == "__main__":
    cli.start()

#/home/pi/gatekeeper-bot/venv/bin/python3 
from deltachat2 import MsgData, events
from deltabot_cli import BotCli

import os
import time
import subprocess

cli = BotCli("gatekeeper")

DEFAULT_HELP_MESSAGE = (
    "This bot operates a lock:\n"
    " /lock or /zu - locks gate\n"
    " /unlock or /auf - unlocks gate\n"
    " /status - Print current status of the lock.\n"
    " /id - get ID of the chat\n"
    " - Any other input displays this message...\n"
    " NOTE: operations can take up to 90 Seconds."
)


# Comma-separated DeltaChat chat ids that may trigger lock operations.
# Use the bot's /id command to discover ids and add them to .env.
allowed_chats = [int(x) for x in os.environ.get("ALLOWED_CHATS", "").split(",") if x.strip()]

def execute_command(bot,accid,msg,command):
  now = int(time.time())        # current Unix time in seconds
  age = now - msg.timestamp     # message age in seconds
  maxage = 30
  if age>maxage: 
     bot.logger.info(f"Command {command} in message {msg.id} in chat {msg.chat_id} is older than {maxage}s -> ignored")
     bot.rpc.send_reaction(accid, msg.id, ["❌"])
     return
  bot.rpc.send_reaction(accid, msg.id, ["⌛"])
  proc = subprocess.run(["./send-command.sh",command], encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  for line in proc.stdout.split('\n'):
     if line!="":
       bot.rpc.send_msg(accid, msg.chat_id, MsgData(line))
  for line in proc.stderr.split('\n'):
     if line!="":
       bot.rpc.send_msg(accid, msg.chat_id, MsgData(line))
  bot.rpc.send_reaction(accid, msg.id, ["🆗"])


@cli.on(events.RawEvent)
def log_event(bot, accid, event):
    bot.logger.info(event)

@cli.on(events.NewMessage)
def echo(bot, accid, event):
    # TODO: make this only work in one allowed verified group
    # TODO: check the time this was sent that it is not too far ago
    # or and use realtime channels
    msg = event.msg
    if msg.text=="/unlock" or msg.text == "/auf":
        if not msg.chat_id in allowed_chats:
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="permission denied"))
            return
        execute_command(bot,accid,msg,"unlock")
    elif msg.text == "/lock" or msg.text == "/zu":
        execute_command(bot,accid,msg,"lock")
    elif msg.text == "/status":
        execute_command(bot,accid,msg,"status")
    elif msg.text == "/id":
       bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="the id of this chat is {id}, add this to the allowlist to allow opening the door from this group".format(id = msg.chat_id)))
    else:
       help_text = os.environ.get("HELP_MESSAGE", DEFAULT_HELP_MESSAGE)
       bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=help_text))

if __name__ == "__main__":
    cli.start()

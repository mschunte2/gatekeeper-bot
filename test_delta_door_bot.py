"""Parser-focused tests for delta-door-bot.py.

The parsers are the only non-trivial pure logic in the bot and they sit
on top of keyblepy's stdout format, which we do not control. A single
unnoticed change in the lock's output would silently turn every status
probe into parse-returned-None. Run:

    python3 test_delta_door_bot.py       # or: python3 -m unittest

Stdlib-only (no pytest required) so this runs anywhere the bot runs.
The bot module is loaded via importlib because its filename contains a
dash (not a valid Python module name).
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

# The bot module imports deltabot-cli / deltachat2 / appdirs at the top
# even though the pure parsers under test need none of them. Stub just
# the surface the import touches so the module loads on a bare Python
# without running `pip install -r requirements.txt` locally.
def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_stub("appdirs", user_config_dir=lambda *_a, **_kw: "/tmp/test-gatekeeper-cfg")

class _EventType:
    WEBXDC_STATUS_UPDATE = object()
_stub("deltachat2",
      EventType=_EventType,
      MsgData=lambda **kw: kw,
      events=_stub("deltachat2.events", RawEvent=object, NewMessage=object))


class _BotCli:
    def __init__(self, *_a, **_kw): pass
    def on(self, *_a, **_kw):        return lambda f: f
    on_start = lambda self, f: f
_stub("deltabot_cli", BotCli=_BotCli)

_SPEC = importlib.util.spec_from_file_location(
    "_bot_under_test", Path(__file__).parent / "delta-door-bot.py",
)
_bot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_bot)


class ParseLockOutput(unittest.TestCase):
    # (stdout, expected_state, expected_battery_low)
    CASES = [
        # Simple "device <state>" single-line results from lock/unlock/open.
        # No battery field -> battery is None (caller preserves cache).
        ("device locked\n", "locked", None),
        ("device unlocked\n", "unlocked", None),
        ("device opened\n", "unlocked", None),
        ("  device  locked  \n", "locked", None),  # whitespace tolerance
        ("DEVICE LOCKED\n", "locked", None),        # case-insensitive

        # Dict-repr lines from status queries. keyblepy emits something
        # like "device status = {'lock_status': 'LOCKED', ...}".
        ("device status = {'lock_status': 'LOCKED', 'battery_low': False}\n",
         "locked", False),
        ("device status = {'lock_status': 'LOCKED', 'battery_low': True}\n",
         "locked", True),
        ('device status = {"lock_status": "UNLOCKED"}\n', "unlocked", None),
        ("{'lock_status': 'OPENED'}\n", "unlocked", None),
        ('{"battery_low": true}\n', None, True),
        ('{"battery_low": 1}\n', None, True),
        ('{"battery_low": 0}\n', None, False),

        # UNKNOWN / MOVING are real lock states (manual operation or
        # motor-in-flight) -- must return "unknown", not None.
        ("device status = {'lock_status': 'UNKNOWN'}\n", "unknown", None),
        ("device status = {'lock_status': 'MOVING'}\n", "unknown", None),

        # Multi-line output: parser must scan every line.
        (
            "connecting...\n"
            "bond established\n"
            "device status = {'lock_status': 'LOCKED', 'battery_low': False}\n"
            "disconnected\n",
            "locked", False,
        ),

        # Whitespace-only / empty.
        ("\n\n   \n", None, None),
        ("", None, None),

        # Genuinely unparseable returns None so the caller escalates
        # to WARN with the raw bytes for future diagnosis.
        ("some weird new output from keyblepy\n", None, None),
        ("device is fine\n", None, None),
    ]

    def test_cases(self):
        for stdout, expected_state, expected_battery in self.CASES:
            with self.subTest(stdout=stdout.strip()[:40]):
                self.assertEqual(
                    _bot.parse_lock_output(stdout),
                    (expected_state, expected_battery),
                )


class Sanitize(unittest.TestCase):
    CASES = [
        ("Alice", "?", "Alice"),
        ("  trim me  ", "?", "trim me"),
        ("contains\x00control\x01chars", "?", "contains control chars"),
        ("", "?", "?"),
        (None, "?", "?"),
        (42, "?", "42"),
        ("x" * 200, "?", "x" * 64),
    ]

    def test_cases(self):
        for value, fallback, expected in self.CASES:
            with self.subTest(value=value):
                self.assertEqual(_bot._sanitize(value, fallback=fallback), expected)


if __name__ == "__main__":
    unittest.main()

"""
run_log.py - save EVERYTHING that happens in a run to a file.

The console only shows short previews (tool output is cut at 200 characters)
and is gone when the program ends. When an agent does something strange
("why did QA change its own test?"), we need the full story. So every model
reply, every tool call and every tool result goes into one file per run:

    logs/run-20261022-150412.jsonl

JSONL = "JSON Lines": one JSON object per line, one line per event. It is easy
to append to while the run is going, and easy to read back with a script
(see show_log.py). Example line:

    {"time": "15:04:13", "agent": "QA", "event": "tool_call", "name": "run_tests", "input": {}}

Usage:
    run_log.start("calculator")      # once, at the start of a run
    run_log.write("QA", "tool_call", name=..., input=...)   # anywhere

If start() was never called (e.g. in unit tests), write() does nothing.
"""

import json
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"

_current_file = None  # the log file of the run in progress, or None


def start(label: str = "run") -> Path:
    """Begin a new log file for this run and return its path."""
    global _current_file
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _current_file = LOG_DIR / f"run-{stamp}-{label}.jsonl"
    return _current_file


def stop():
    global _current_file
    _current_file = None


def write(agent: str, event: str, **data):
    """Append one event. Values may be SDK objects; _plain() turns them into JSON."""
    if _current_file is None:
        return
    record = {"time": datetime.now().strftime("%H:%M:%S"), "agent": agent, "event": event}
    record.update({key: _plain(value) for key, value in data.items()})
    with open(_current_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _plain(value):
    """Convert SDK content blocks (and lists of them) to plain dicts for JSON."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "model_dump"):  # real anthropic SDK objects
        return value.model_dump(exclude_none=True)
    if hasattr(value, "__dict__"):    # our fake test objects
        return {k: _plain(v) for k, v in vars(value).items()}
    return value

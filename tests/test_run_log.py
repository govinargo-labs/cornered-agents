"""Tests for run logging: every model reply and tool call lands in the log, in full."""

import json

import pytest

import run_log
import show_log
import tools
from agents.base import Agent
from tests.fakes import FakeClient, reply, text_block, tool_call


@pytest.fixture(autouse=True)
def temp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(run_log, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "ws").mkdir()
    yield
    run_log.stop()  # never leak an open log into other tests


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_write_does_nothing_without_start(tmp_path):
    run_log.write("Dev", "anything", x=1)
    assert not (tmp_path / "logs").exists()


def test_agent_run_is_logged_in_full():
    long_content = "x = 1\n" * 100  # 600 chars: the console would cut this, the log must not
    client = FakeClient([
        reply("tool_use", tool_call("c1", "write_file", {"path": "a.py", "content": long_content})),
        reply("end_turn", text_block("Done.")),
    ])
    path = run_log.start("test")
    Agent("Dev", "You are a test.", client=client).run("Write a.py")

    events = read_events(path)
    kinds = [e["event"] for e in events]
    assert kinds == ["agent_start", "model_reply", "tool_result", "model_reply", "agent_end"]

    first_reply = events[1]
    assert first_reply["content"][0]["input"]["content"] == long_content  # not shortened
    assert events[2]["name"] == "write_file" and events[2]["is_error"] is False
    assert events[4]["final_text"] == "Done."


def test_show_log_can_print_every_event(capsys):
    client = FakeClient([
        reply("tool_use", tool_call("c1", "list_files", {})),
        reply("end_turn", text_block("All good.")),
    ])
    path = run_log.start("test")
    Agent("Dev", "You are a test.", client=client).run("Look around")

    for event in read_events(path):
        show_log.show_event(event, show_thinking=True)
    printed = capsys.readouterr().out
    assert "CALLS list_files" in printed and "All good." in printed

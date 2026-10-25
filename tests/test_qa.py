"""Tests for the QA Reviewer role (fake model, no API key needed)."""

import pytest

import tools
from agents.qa import QA_TOOLS, make_qa
from tests.fakes import FakeClient, reply, tool_call


@pytest.fixture(autouse=True)
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE", tmp_path)
    return tmp_path


def test_qa_is_not_offered_write_file():
    assert "write_file" not in {schema["name"] for schema in QA_TOOLS}


def test_qa_cannot_write_even_if_it_tries(temp_workspace):
    # A model can "call" a tool it was never offered. The loop must refuse.
    client = FakeClient([
        reply("tool_use", tool_call("c1", "write_file", {"path": "hack.py", "content": "x"})),
        reply("tool_use", tool_call("c2", "submit_review", {"verdict": "pass", "feedback": "ok"})),
    ])
    make_qa(client=client).run("Review")

    assert not (temp_workspace / "hack.py").exists()
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "not available" in tool_result["content"]


def test_submit_review_stops_the_loop_and_returns_the_verdict():
    client = FakeClient([
        reply("tool_use", tool_call("c1", "run_tests", {})),
        reply("tool_use", tool_call("c2", "submit_review",
                                    {"verdict": "fail", "feedback": "1. add('a', 1) crashes"})),
    ])
    result = make_qa(client=client).run("Review")

    assert result.stop_reason == "done"
    assert result.output == {"verdict": "fail", "feedback": "1. add('a', 1) crashes"}
    assert len(client.requests) == 2  # no extra model call after submit_review


# --- Nudges: an agent that "says" it will submit, but doesn't ----------------

from tests.fakes import text_block


def test_agent_is_reminded_to_call_its_finish_tool():
    client = FakeClient([
        reply("end_turn", text_block("Looks fine. I will now submit my review.")),  # forgot the tool
        reply("tool_use", tool_call("c1", "submit_review", {"verdict": "pass", "feedback": "ok"})),
    ])
    result = make_qa(client=client).run("Review")

    assert result.output == {"verdict": "pass", "feedback": "ok"}
    reminder = client.requests[1]["messages"][-1]
    assert reminder["role"] == "user" and "You have not called submit_review" in reminder["content"]


def test_agent_gives_up_after_too_many_reminders():
    client = FakeClient([reply("end_turn", text_block("I will submit soon."))] * 3)
    result = make_qa(client=client).run("Review")

    assert result.stop_reason == "no_finish_tool"
    assert result.output is None
    assert len(client.requests) == 3  # 1 try + 2 reminders


# --- Last step: hand in your answer before the step limit --------------------

def test_last_step_tells_the_agent_to_call_its_finish_tool():
    client = FakeClient([
        reply("tool_use", tool_call("c1", "run_tests", {})),
        reply("tool_use", tool_call("c2", "run_tests", {})),
        reply("tool_use", tool_call("c3", "submit_review", {"verdict": "fail", "feedback": "1. bug"})),
    ])
    result = make_qa(client=client, max_steps=3).run("Review")

    assert result.output == {"verdict": "fail", "feedback": "1. bug"}
    last_message = client.requests[2]["messages"][-1]
    assert "LAST step" in last_message["content"][-1]["text"]
    # On the last step, the finish tool is the ONLY tool offered.
    assert [t["name"] for t in client.requests[2]["tools"]] == ["submit_review"]
    assert len(client.requests[1]["tools"]) > 1
    # Earlier requests got no warning.
    assert all(block["type"] == "tool_result" for block in client.requests[1]["messages"][-1]["content"])


def test_last_step_refuses_a_tool_that_was_not_offered(monkeypatch):
    # Seen live: on its last step the model called run_tests anyway, and our
    # loop ran it, so QA never gave a verdict. Now it is refused, not run.
    ran = []
    monkeypatch.setattr("tools.run_tool", lambda name, args: ran.append(name) or ("ok", False))
    client = FakeClient([
        reply("tool_use", tool_call("c1", "list_files", {})),
        reply("tool_use", tool_call("c2", "run_tests", {})),  # last step: not offered
    ])
    make_qa(client=client, max_steps=2).run("Review")
    assert ran == ["list_files"]


def test_last_step_forces_the_finish_tool_on_a_local_model(monkeypatch):
    monkeypatch.setattr("config.LLM_THINKING", "off")
    client = FakeClient([
        reply("tool_use", tool_call("c1", "list_files", {})),
        reply("tool_use", tool_call("c2", "submit_review", {"verdict": "pass", "feedback": "ok"})),
    ])
    make_qa(client=client, max_steps=2).run("Review")
    assert "tool_choice" not in client.requests[0]
    assert client.requests[1]["tool_choice"] == {"type": "tool", "name": "submit_review"}


def test_last_step_is_not_forced_when_thinking_is_on(monkeypatch):
    # Claude rejects a forced tool call while thinking; it gets only the tool list.
    monkeypatch.setattr("config.LLM_THINKING", "on")
    client = FakeClient([
        reply("tool_use", tool_call("c1", "list_files", {})),
        reply("tool_use", tool_call("c2", "submit_review", {"verdict": "pass", "feedback": "ok"})),
    ])
    make_qa(client=client, max_steps=2).run("Review")
    assert "tool_choice" not in client.requests[1]

"""
Tests for the agent loop, using a FAKE model. No API key, no cost, same result every run.

Lesson: the loop is just code. We can test it by scripting what the "model"
replies, then checking our loop reacts correctly (runs tools, stops, etc.).
"""

from types import SimpleNamespace

import pytest

import tools
from agents.base import Agent
from tests.fakes import FakeClient, reply, text_block, tool_call


@pytest.fixture(autouse=True)
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE", tmp_path)
    return tmp_path


def make_agent(replies, max_steps=20):
    client = FakeClient(replies)
    return Agent("Test", "You are a test agent.", max_steps=max_steps, client=client), client


# --- Tests -----------------------------------------------------------------

def test_stops_when_model_does_not_ask_for_tools():
    agent, client = make_agent([reply("end_turn", text_block("Nothing to do."))])
    result = agent.run("Say hi")

    assert result.stop_reason == "done"
    assert result.steps == 1
    assert result.final_text == "Nothing to do."


def test_runs_tool_and_sends_result_back(temp_workspace):
    agent, client = make_agent([
        reply("tool_use", tool_call("call_1", "write_file", {"path": "a.py", "content": "x = 1\n"})),
        reply("end_turn", text_block("Wrote a.py")),
    ])
    result = agent.run("Write a.py")

    # The tool really ran: the file exists in the workspace.
    assert (temp_workspace / "a.py").read_text() == "x = 1\n"
    assert result.stop_reason == "done" and result.steps == 2

    # The 2nd request must end with our tool_result, linked by the same id.
    last_message = client.requests[1]["messages"][-1]
    assert last_message["role"] == "user"
    tool_result = last_message["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "call_1"
    assert tool_result["is_error"] is False


def test_tool_errors_go_back_to_the_model():
    agent, client = make_agent([
        reply("tool_use", tool_call("call_1", "read_file", {"path": "../.env"})),
        reply("end_turn", text_block("OK, I can't read that.")),
    ])
    agent.run("Read the secrets")

    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "outside the workspace" in tool_result["content"]


def test_several_tool_calls_in_one_reply_get_one_user_message():
    agent, client = make_agent([
        reply("tool_use",
              tool_call("c1", "write_file", {"path": "a.py", "content": ""}),
              tool_call("c2", "list_files", {})),
        reply("end_turn", text_block("done")),
    ])
    agent.run("go")

    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["c1", "c2"]


def test_step_limit_stops_a_never_ending_agent():
    # A "model" that calls list_files forever.
    forever = [reply("tool_use", tool_call(f"c{i}", "list_files", {})) for i in range(10)]
    agent, client = make_agent(forever, max_steps=3)
    result = agent.run("loop forever")

    assert result.stop_reason == "max_steps"
    assert result.steps == 3
    assert len(client.requests) == 3


def test_counts_tokens_across_steps():
    agent, _ = make_agent([
        reply("tool_use", tool_call("c1", "list_files", {})),
        reply("end_turn", text_block("done")),
    ])
    result = agent.run("go")
    assert (result.input_tokens, result.output_tokens) == (200, 20)


# --- Safety stops: API errors and the context budget -----------------------

import httpx2  # the HTTP library the anthropic SDK uses
import anthropic

import config


class BrokenClient(FakeClient):
    """A server that fails, like oMLX aborting a request when memory runs out."""

    def create(self, **request):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "http://localhost/v1/messages"))


def test_api_error_stops_the_agent_cleanly():
    agent = Agent("Test", "You are a test agent.", client=BrokenClient([]))
    result = agent.run("anything")  # must not raise

    assert result.stop_reason == "api_error"
    assert "API error" in result.final_text


def test_context_budget_stops_a_growing_conversation(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_BUDGET", 150)  # each fake reply reports 100 tokens in
    big = SimpleNamespace(input_tokens=200, output_tokens=10)
    replies = [reply("tool_use", tool_call("c1", "list_files", {})),
               reply("tool_use", tool_call("c2", "list_files", {})),
               reply("end_turn", text_block("never reached"))]
    replies[1].usage = big  # step 2: the conversation is now over budget
    agent, client = make_agent(replies)
    result = agent.run("go")

    assert result.stop_reason == "context_limit"
    assert result.steps == 2

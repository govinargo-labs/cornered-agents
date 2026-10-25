"""
Tests for the Developer role. We script a "perfect developer" with the fake
model and check the real tools did the work. No API key needed.
"""

import pytest

import tools
from agents.developer import DEVELOPER_PROMPT, make_developer
from tests.fakes import FakeClient, reply, text_block, tool_call


@pytest.fixture(autouse=True)
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE", tmp_path)
    return tmp_path


def test_developer_writes_code_runs_tests_and_finishes(temp_workspace):
    client = FakeClient([
        reply("tool_use", tool_call("c1", "list_files", {})),
        reply("tool_use",
              tool_call("c2", "write_file", {"path": "add.py", "content": "def add(a, b):\n    return a + b\n"}),
              tool_call("c3", "write_file", {"path": "test_add.py",
                                             "content": "from add import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"})),
        reply("tool_use", tool_call("c4", "run_tests", {})),
        reply("end_turn", text_block("Wrote add.py and test_add.py. 1 test passes.")),
    ])
    result = make_developer(client=client).run("Build add.py")

    assert result.stop_reason == "done"
    assert (temp_workspace / "add.py").exists()

    # The Developer's job description is sent with every request.
    assert all(r["system"][0]["text"] == DEVELOPER_PROMPT for r in client.requests)

    # The model saw the REAL test output from run_tests.
    test_output = client.requests[3]["messages"][-1]["content"][0]["content"]
    assert test_output.startswith("Tests PASSED")


def test_developer_cannot_use_qa_tools(temp_workspace):
    # Even if the model asks for QA's tool, the loop refuses it.
    client = FakeClient([
        reply("tool_use", tool_call("c1", "write_qa_test", {"path": "test_qa_x.py", "content": "x"})),
        reply("end_turn", text_block("ok")),
    ])
    make_developer(client=client).run("Build it")

    assert not (temp_workspace / "test_qa_x.py").exists()
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "not available" in tool_result["content"]

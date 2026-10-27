"""
Tests for the Developer <-> QA loop in company.py.

Here we don't even need a fake model: we replace whole agents with tiny
scripted stand-ins. The loop only cares about what run() returns.
"""

import pytest

import company
import tools
from agents.base import AgentResult
from company import build_task

PASSING_TEST = "def test_ok():\n    assert True\n"
FAILING_TEST = "def test_bad():\n    assert False\n"


@pytest.fixture(autouse=True)
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE", tmp_path)
    return tmp_path


class ScriptedDeveloper:
    """Writes a passing or failing test file on each attempt, and remembers its tasks."""

    def __init__(self, test_files, summaries=None):
        self.test_files = list(test_files)
        self.summaries = list(summaries or [])
        self.tasks = []

    def run(self, task):
        self.tasks.append(task)
        tools.write_file("test_work.py", self.test_files.pop(0))
        summary = self.summaries.pop(0) if self.summaries else "done"
        return AgentResult(final_text=summary, stop_reason="done", steps=1)


class ScriptedQA:
    def __init__(self, verdicts, fix_tests=False):
        self.verdicts = list(verdicts)  # e.g. [("fail", "1. bug"), ("pass", "ok")]
        self.fix_tests = fix_tests      # True = QA "fixes its own test" when it runs
        self.runs = 0
        self.tasks = []

    def run(self, task):
        self.runs += 1
        self.tasks.append(task)
        if self.fix_tests:
            tools.write_file("test_work.py", PASSING_TEST)
        verdict = self.verdicts.pop(0)
        output = None if verdict is None else {"verdict": verdict[0], "feedback": verdict[1]}
        return AgentResult(final_text="", stop_reason="done", steps=1, output=output)


def test_approved_on_first_attempt():
    dev, qa = ScriptedDeveloper([PASSING_TEST]), ScriptedQA([("pass", "all good")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert result.status == "approved"
    assert result.attempts == 1


def test_qa_feedback_goes_back_to_the_developer():
    dev = ScriptedDeveloper([PASSING_TEST, PASSING_TEST])
    qa = ScriptedQA([("fail", "1. empty input crashes"), ("pass", "fixed")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert result.status == "approved" and result.attempts == 2
    assert dev.tasks[0] == "Build it"  # first attempt: just the task
    assert "REJECTED" in dev.tasks[1] and "empty input crashes" in dev.tasks[1]


def test_failing_tests_skip_qa():
    dev = ScriptedDeveloper([FAILING_TEST, PASSING_TEST])
    qa = ScriptedQA([("pass", "ok")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert result.status == "approved" and result.attempts == 2
    assert qa.runs == 1  # QA only ran once: attempt 1 was rejected by our own test check
    assert "tests do not pass" in dev.tasks[1]


def test_gives_up_after_max_retries():
    dev = ScriptedDeveloper([PASSING_TEST] * 4)
    qa = ScriptedQA([("fail", "still broken")] * 4)
    result = build_task("Build it", developer=dev, qa=qa, max_retries=3)

    assert result.status == "needs_human"
    assert result.attempts == 4  # 1 attempt + 3 retries
    assert result.feedback == "still broken"


def test_missing_qa_verdict_asks_a_human():
    dev, qa = ScriptedDeveloper([PASSING_TEST]), ScriptedQA([None])
    result = build_task("Build it", developer=dev, qa=qa)
    assert result.status == "needs_human"


# --- Phase 3: PM plan + building the whole board ----------------------------

import json

import task_board
from agents.pm import make_pm
from company import build_board, plan_product
from tests.fakes import FakeClient, reply, tool_call

PLAN = {
    "spec": "## Goal\nA tiny tool.",
    "tasks": [
        {"title": "Core", "description": "core.py", "examples": ["f(1) -> 2"]},
        {"title": "CLI", "description": "python core.py 1", "examples": ["prints 2"]},
    ],
}


def test_plan_product_saves_the_pms_plan(tmp_path):
    client = FakeClient([reply("tool_use", tool_call("c1", "submit_plan", PLAN))])
    board_path = tmp_path / "tasks.json"
    board = plan_product("a tiny tool", pm=make_pm(client=client), board_path=board_path)

    assert json.loads(board_path.read_text()) == board
    assert [t["title"] for t in board["tasks"]] == ["Core", "CLI"]
    # The PM was offered read tools, run_python (sandboxed) and submit_plan: it cannot write code.
    offered = {t["name"] for t in client.requests[0]["tools"]}
    assert offered == {"list_files", "read_file", "run_python", "submit_plan"}


def test_build_board_builds_every_task_in_order(tmp_path):
    board_path = tmp_path / "tasks.json"
    task_board.save(task_board.new_board("idea", PLAN), board_path)
    dev = ScriptedDeveloper([PASSING_TEST, PASSING_TEST])
    qa = ScriptedQA([("pass", "ok"), ("pass", "ok")])

    board = build_board(board_path, developer=dev, qa=qa)

    assert [t["status"] for t in board["tasks"]] == ["done", "done"]
    assert "YOUR TASK (1 of 2): Core" in dev.tasks[0]
    assert "YOUR TASK (2 of 2): CLI" in dev.tasks[1]


def test_build_board_stops_at_a_task_needing_a_human_and_can_resume(tmp_path):
    board_path = tmp_path / "tasks.json"
    task_board.save(task_board.new_board("idea", PLAN), board_path)

    # Run 1: task 1 is rejected 4 times -> needs_human, task 2 is never started.
    build_board(board_path, developer=ScriptedDeveloper([PASSING_TEST] * 4),
                qa=ScriptedQA([("fail", "broken")] * 4))
    saved = task_board.load(board_path)
    assert [t["status"] for t in saved["tasks"]] == ["needs_human", "todo"]
    assert saved["tasks"][0]["feedback"] == "broken"

    # A human fixes things and marks task 1 as todo again. Run 2 resumes from the file.
    saved["tasks"][0]["status"] = "todo"
    task_board.save(saved, board_path)
    board = build_board(board_path, developer=ScriptedDeveloper([PASSING_TEST] * 2),
                        qa=ScriptedQA([("pass", "ok")] * 2))
    assert [t["status"] for t in board["tasks"]] == ["done", "done"]
    assert board["tasks"][0]["attempts"] == 5  # 4 in run 1 + 1 in run 2


# --- Disputes: the Developer may say a QA test is wrong ----------------------

DISPUTE = "Done.\n\nQA TEST DISPUTE: test_bad expects 3, but the task says 2."


def test_dispute_sends_failing_tests_to_qa_instead_of_back_to_the_developer():
    dev = ScriptedDeveloper([FAILING_TEST], summaries=[DISPUTE])
    qa = ScriptedQA([("pass", "I was wrong, fixed my test.")], fix_tests=True)
    result = build_task("Build it", developer=dev, qa=qa)

    assert result.status == "approved" and result.attempts == 1
    assert "OBJECTION" in qa.tasks[0] and "QA TEST DISPUTE:" in qa.tasks[0]
    assert "the task says 2" in qa.tasks[0]


def test_qa_pass_is_overruled_when_tests_still_fail():
    # QA says "pass" but did not fix the failing test: our code does not accept that.
    dev = ScriptedDeveloper([FAILING_TEST, PASSING_TEST], summaries=[DISPUTE, "fixed"])
    qa = ScriptedQA([("pass", "fine"), ("pass", "fine")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert result.status == "approved" and result.attempts == 2
    assert result.history[0]["verdict"] == "pass overruled (tests fail)"
    assert "QA approved, but the tests do not pass" in dev.tasks[1]


# --- Spec problems: the TASK itself can be wrong ------------------------------

def test_spec_problem_verdict_stops_without_retrying():
    # No code can meet an impossible task, so retries would only waste time.
    dev = ScriptedDeveloper([PASSING_TEST] * 4)
    qa = ScriptedQA([("spec_problem", "random.Random(42) gives [81, 14, ...], not [73, 15, ...]")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert result.status == "needs_human" and result.attempts == 1
    assert result.feedback.startswith("SPEC PROBLEM (found by QA)")
    assert "[81, 14, ...]" in result.feedback


def test_developer_spec_problem_reaches_qa_even_with_failing_tests():
    summary = "Done.\n\nSPEC PROBLEM: the task says [73, 15, ...] but random gives [81, 14, ...]."
    dev = ScriptedDeveloper([FAILING_TEST], summaries=[summary])
    qa = ScriptedQA([("spec_problem", "confirmed")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert qa.runs == 1  # failing tests did NOT send it straight back to the Developer
    assert "SPEC PROBLEM: the task says" in qa.tasks[0]
    assert result.status == "needs_human"


# --- Stuck detection: the same tests fail twice -> QA decides why ------------

from company import failing_tests


def test_failing_tests_are_read_from_the_pytest_summary():
    output = ("Tests FAILED (exit code 1)\n...\n=== short test summary info ===\n"
              "FAILED test_dice.py::test_seed - assert [6, 1] == [3, 5]\n"
              "ERROR test_cli.py::test_run\n2 failed in 0.1s\n")
    assert failing_tests(output) == {"test_dice.py::test_seed", "test_cli.py::test_run"}


def test_same_failure_twice_goes_to_qa_who_may_call_it_a_spec_problem():
    dev = ScriptedDeveloper([FAILING_TEST] * 4)
    qa = ScriptedQA([("spec_problem", "random.Random(42) gives [6, 1], the task says [3, 5]")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert len(dev.tasks) == 2            # attempt 1: tests fail -> Developer; attempt 2: stuck
    assert qa.runs == 1 and "STUCK" in qa.tasks[0] and "test_work.py::test_bad" in qa.tasks[0]
    assert result.status == "needs_human" and result.history[1]["stuck"]


def test_qa_feedback_on_a_stuck_task_goes_back_to_the_developer():
    dev = ScriptedDeveloper([FAILING_TEST, FAILING_TEST, PASSING_TEST])
    qa = ScriptedQA([("fail", "1. use random.Random(seed), not your own generator"), ("pass", "ok")])
    result = build_task("Build it", developer=dev, qa=qa)

    assert result.status == "approved" and result.attempts == 3
    assert "use random.Random(seed)" in dev.tasks[2]


# --- Plan check: rejected plans go back to the PM -----------------------------

HEDGED_PLAN = {"spec": "## Goal\nDice.", "tasks": [
    {"title": "Roll", "description": "roll()", "examples": ["roll(2, 6, 42) -> e.g. [3, 5]"]}]}


def test_rejected_plan_goes_back_to_the_pm(tmp_path):
    client = FakeClient([
        reply("tool_use", tool_call("c1", "submit_plan", HEDGED_PLAN)),
        reply("tool_use", tool_call("c2", "submit_plan", PLAN)),
    ])
    board = plan_product("dice", pm=make_pm(client=client), board_path=tmp_path / "tasks.json")

    assert [t["title"] for t in board["tasks"]] == ["Core", "CLI"]
    second_request = client.requests[1]["messages"][0]["content"]
    assert "REJECTED" in second_request and "is a guess" in second_request
    assert "e.g. [3, 5]" in second_request  # the PM sees its own previous plan


def test_pm_gets_a_limited_number_of_fixes(tmp_path):
    client = FakeClient([reply("tool_use", tool_call(f"c{i}", "submit_plan", HEDGED_PLAN)) for i in range(3)])
    with pytest.raises(task_board.PlanError, match="is a guess"):
        plan_product("dice", pm=make_pm(client=client), board_path=tmp_path / "tasks.json")
    assert len(client.requests) == 3  # 1 plan + 2 fixes


# --- Seeded examples must be computed, not guessed ----------------------------

SEEDED_PLAN = {"spec": "## Goal\nDice.", "tasks": [
    {"title": "Roll", "description": "roll()", "examples": ["roll_dice(1, 20, seed=1) -> [5]"]}]}


def test_seeded_examples_without_run_python_go_back_to_the_pm(tmp_path):
    client = FakeClient([
        reply("tool_use", tool_call("c1", "submit_plan", SEEDED_PLAN)),         # guessed
        reply("tool_use", tool_call("c2", "run_python", {"code": "print(1)"})),  # now computes
        reply("tool_use", tool_call("c3", "submit_plan", SEEDED_PLAN)),
    ])
    board = plan_product("dice", pm=make_pm(client=client), board_path=tmp_path / "tasks.json")

    assert board["tasks"][0]["examples"] == ["roll_dice(1, 20, seed=1) -> [5]"]
    retry_request = client.requests[1]["messages"][0]["content"]
    assert "never called run_python" in retry_request


def test_plans_without_seeded_examples_need_no_run_python(tmp_path):
    client = FakeClient([reply("tool_use", tool_call("c1", "submit_plan", PLAN))])
    plan_product("tool", pm=make_pm(client=client), board_path=tmp_path / "tasks.json")
    assert len(client.requests) == 1


# --- The honest exit must survive the way a language model formats it -------------

@pytest.mark.parametrize("summary", [
    "Done.\n\nQA TEST DISPUTE: the test asserts True with no approval.",      # as the prompt asks
    "Done.\n\n**QA TEST DISPUTE:** the test asserts True.",                    # bold, colon inside
    "Done.\n\nThis is a **QA TEST DISPUTE**: the test asserts True.",          # seen live: colon OUTSIDE the bold
    "Done.\n\n`QA TEST DISPUTE:` the test asserts True.",                      # backticks
    "Done.\n\n*QA TEST DISPUTE*: the test asserts True.",                      # italics
    "Done.\n\nqa test dispute: the test asserts True.",                        # case
    "Done.\n\n**SPEC PROBLEM**: parity(4) must be both 'even' and 'odd'.",
])
def test_a_dispute_is_found_however_it_is_formatted(summary):
    dispute = company.find_dispute(summary)
    assert dispute and ("asserts True" in dispute or "parity(4)" in dispute)


def test_the_dispute_keeps_its_marker_and_everything_after_it():
    summary = "Wrote release.py.\n\n**QA TEST DISPUTE**: first reason.\nSecond line."
    dispute = company.find_dispute(summary)
    # It starts at the marker itself, as the exact-match version did; markdown
    # in front of it is not part of the objection.
    assert dispute.startswith("QA TEST DISPUTE")
    assert dispute.endswith("Second line.")
    assert "Wrote release.py" not in dispute


@pytest.mark.parametrize("summary", [
    "Everything passes.",
    "I considered raising a dispute but did not.",     # the words, with no marker
    "The QA test is wrong.",                          # an opinion, not the channel
    "",
])
def test_no_marker_means_no_dispute(summary):
    assert company.find_dispute(summary) == ""

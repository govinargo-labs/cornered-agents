"""
Tests for Phase 4: CEO, Docs Writer, DevOps (git), and the human approval gate.

The git tests run real git, but only inside a temporary folder.
"""

import subprocess

import pytest

import git_ops
import task_board
import tools
from agents.base import AgentResult
from agents.ceo import make_ceo
from agents.devops import make_devops
from agents.docs import DOCS_TOOLS, make_docs_writer
from company import ask_human, build_board, commit_work, plan_product, release, write_brief
from agents.pm import make_pm
from tests.fakes import FakeClient, reply, text_block, tool_call


@pytest.fixture(autouse=True)
def temp_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE", workspace)
    return workspace


def git(workspace, *args):
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True).stdout


def devops_says(message):
    return make_devops(client=FakeClient([reply("tool_use", tool_call("c1", "submit_commit", {"message": message}))]))


# --- Sandbox: agents keep their hands off .git, Docs only writes markdown ----

@pytest.mark.parametrize("path", [".git/hooks/pre-commit", "sub/.git/config", ".git"])
def test_agents_cannot_touch_git_internals(path):
    with pytest.raises(tools.ToolError, match="managed by the company"):
        tools.safe_path(path)


def test_list_files_hides_git(temp_workspace):
    tools.write_file("app.py", "x = 1\n")
    git_ops.stage_all()
    # .gitignore is a normal file the agents may see; the .git/ folder is hidden.
    assert tools.list_files().splitlines() == [".gitignore", "app.py"]


def test_write_doc_only_writes_markdown():
    assert "Wrote" in tools.write_doc("README.md", "# Hi\n")
    with pytest.raises(tools.ToolError, match="only write markdown"):
        tools.write_doc("app.py", "print('sneaky')")


def test_docs_writer_cannot_write_code():
    assert {t["name"] for t in DOCS_TOOLS} == {"list_files", "read_file", "write_doc"}


# --- git_ops: fixed commands, local only -------------------------------------

def test_commit_uses_the_devops_identity_and_keeps_messages_as_text(temp_workspace):
    tools.write_file("app.py", "x = 1\n")
    assert "app.py" in git_ops.stage_all()
    git_ops.commit('feat: add app\n\n- "; rm -rf ~ $(whoami)')  # just text, never run

    assert git(temp_workspace, "log", "-1", "--format=%an <%ae>").strip() == \
        "agent-company DevOps <devops@agent-company.invalid>"
    assert "rm -rf ~ $(whoami)" in git(temp_workspace, "log", "-1", "--format=%B")


def test_git_hooks_never_run(temp_workspace):
    # Even if a hook got into .git somehow, our commits switch hooks off.
    git_ops.ensure_repo()
    hook = temp_workspace / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {temp_workspace / 'HOOK_RAN'}\n")
    hook.chmod(0o755)
    tools.write_file("app.py", "x = 1\n")
    git_ops.stage_all()
    git_ops.commit("feat: add app")
    assert not (temp_workspace / "HOOK_RAN").exists()


def test_source_hidden_by_gitignore_blocks_the_commit(temp_workspace):
    """Case study 004: a .gitignore of "*.py" made the agent's own code
    invisible to DevOps, to git history and to every human reviewer, while the
    commit still succeeded. Planted directly here, so this tests the OUTCOME
    check rather than the tools.py name rule."""
    git_ops.ensure_repo()
    tools.write_file("calc.py", "def add(a, b):\n    return a + b\n")
    git_ops.stage_all()
    git_ops.commit("feat: add calc")

    (temp_workspace / ".gitignore").write_text("*.py\n")
    (temp_workspace / "hidden_backdoor.py").write_text("# nobody sees this\n")
    with pytest.raises(git_ops.GitError, match="would never see them"):
        git_ops.stage_all()


def test_the_normal_gitignore_is_not_a_false_alarm(temp_workspace):
    """Our own GITIGNORE hides __pycache__, *.pyc and .env on purpose. If that
    tripped the check, nothing could ever be committed."""
    git_ops.ensure_repo()
    tools.write_file("app.py", "x = 1\n")
    (temp_workspace / "__pycache__").mkdir(exist_ok=True)
    (temp_workspace / "__pycache__" / "app.pyc").write_bytes(b"\x00")
    (temp_workspace / ".env").write_text("SECRET=1\n")
    assert "app.py" in git_ops.stage_all()


def test_versions_count_up(temp_workspace):
    tools.write_file("app.py", "x = 1\n")
    git_ops.stage_all()
    git_ops.commit("feat: add app")
    assert git_ops.next_version() == "v0.1.0"
    git_ops.tag("v0.1.0", "first")
    assert git_ops.next_version() == "v0.2.0"


def test_there_is_no_push():
    # Publishing is a human decision. Keep it that way.
    assert not any("push" in name for name in dir(git_ops))


# --- DevOps: the agent writes the message, our code commits ------------------

def test_commit_work_uses_the_devops_message(temp_workspace):
    tools.write_file("app.py", "x = 1\n")
    commit = commit_work("Task 1: core", devops_says("feat: add core module"))
    assert commit and git(temp_workspace, "log", "-1", "--format=%s").strip() == "feat: add core module"


def test_commit_work_falls_back_when_the_model_fails(temp_workspace):
    tools.write_file("app.py", "x = 1\n")
    lazy = make_devops(client=FakeClient([reply("end_turn", text_block("ok"))] * 3))
    commit_work("Task 1: core\nlong description", lazy)
    assert git(temp_workspace, "log", "-1", "--format=%s").strip() == "chore: Task 1: core"


def test_commit_work_skips_when_nothing_changed():
    tools.write_file("app.py", "x = 1\n")
    commit_work("Task 1", devops_says("feat: one"))
    assert commit_work("Task 1 again", devops_says("feat: two")) is None


PLAN = {"spec": "## Goal\nA tool.", "tasks": [
    {"title": "Core", "description": "core.py", "examples": ["f(1) -> 2"]},
    {"title": "CLI", "description": "cli", "examples": ["prints 2"]},
]}


class WritesAndPasses:
    """Stand-in Developer: writes a new passing test per task."""
    def __init__(self):
        self.count = 0

    def run(self, task):
        self.count += 1
        tools.write_file(f"test_{self.count}.py", "def test_ok():\n    assert True\n")
        return AgentResult(final_text="done", stop_reason="done", steps=1)


class AlwaysPass:
    def run(self, task):
        return AgentResult(final_text="", stop_reason="done", steps=1,
                           output={"verdict": "pass", "feedback": "ok"})


def test_build_board_commits_each_approved_task(tmp_path, temp_workspace):
    board_path = tmp_path / "tasks.json"
    task_board.save(task_board.new_board("idea", PLAN), board_path)
    devops = make_devops(client=FakeClient([
        reply("tool_use", tool_call("c1", "submit_commit", {"message": "feat: core"})),
        reply("tool_use", tool_call("c2", "submit_commit", {"message": "feat: cli"})),
    ]))
    board = build_board(board_path, developer=WritesAndPasses(), qa=AlwaysPass(), devops=devops)

    assert all(t["commit"] for t in board["tasks"])
    assert git(temp_workspace, "log", "--format=%s").split("\n")[:2] == ["feat: cli", "feat: core"]


# --- CEO brief goes to the PM ------------------------------------------------

def test_ceo_brief_reaches_the_pm_and_the_board(tmp_path):
    ceo = make_ceo("submit_brief", client=FakeClient([reply("tool_use", tool_call(
        "c1", "submit_brief", {"product_name": "dicecli", "brief": "Roll dice from the terminal."}))]))
    brief = write_brief("dice thing", ceo=ceo)

    pm_client = FakeClient([reply("tool_use", tool_call("c1", "submit_plan", PLAN))])
    board = plan_product("dice thing", pm=make_pm(client=pm_client), board_path=tmp_path / "t.json", brief=brief)

    assert "Roll dice from the terminal." in pm_client.requests[0]["messages"][0]["content"]
    assert board["product_name"] == "dicecli"


def test_ceo_cannot_write_files():
    ceo = make_ceo("submit_release_review", client=FakeClient([]))
    assert not {"write_file", "write_qa_test", "write_doc"} & ceo.allowed_tools


# --- Release: docs, review, and the human approval gate ----------------------

def done_board(tmp_path):
    board = task_board.new_board("idea", PLAN)
    for t in board["tasks"]:
        t["status"] = "done"
    path = tmp_path / "tasks.json"
    task_board.save(board, path)
    return path


def release_agents(decision="ready"):
    docs = make_docs_writer(client=FakeClient([
        reply("tool_use", tool_call("c1", "write_doc", {"path": "README.md", "content": "# Tool\n"})),
        reply("end_turn", text_block("Wrote README.md")),
    ]))
    ceo = make_ceo("submit_release_review", client=FakeClient([reply("tool_use", tool_call(
        "c1", "submit_release_review",
        {"decision": decision, "release_notes": "First version.", "concerns": ""}))]))
    return dict(docs=docs, ceo=ceo, devops=devops_says("docs: add README"))


def test_release_needs_an_exact_yes(tmp_path, temp_workspace):
    path = done_board(tmp_path)
    assert release(path, ask=lambda q: "sure", **release_agents()) == "rejected"
    assert git(temp_workspace, "tag") == ""  # nothing tagged
    assert (temp_workspace / "README.md").exists()  # the docs were still written and committed


def test_release_tags_after_approval(tmp_path, temp_workspace):
    path = done_board(tmp_path)
    assert release(path, ask=lambda q: "yes", **release_agents()) == "released"
    assert git(temp_workspace, "tag").strip() == "v0.1.0"
    assert task_board.load(path)["release"]["version"] == "v0.1.0"


def test_a_green_summary_that_turns_red_is_not_tagged(tmp_path, temp_workspace):
    """Case study 004: the test line in the release summary is measured BEFORE
    the human is asked, and on the web UI that question can wait for hours. A
    tag is permanent, so re-check at the last moment."""
    path = done_board(tmp_path)
    tools.write_file("test_green.py", "def test_ok():\n    assert True\n")

    def approve_then_break(question):
        # Stands in for anything that touches the workspace while the human
        # thinks: another run, a resumed task, an editor.
        tools.write_file("test_green.py", "def test_ok():\n    assert False\n")
        return "yes"

    assert release(path, ask=approve_then_break, **release_agents()) == "rejected"
    assert git(temp_workspace, "tag") == ""  # nothing was tagged


def test_an_already_red_build_is_still_the_humans_call(tmp_path, temp_workspace):
    """We only block a green-to-red change. If it was already failing, the
    summary said so and approving anyway is a decision the human is allowed
    to make."""
    path = done_board(tmp_path)
    tools.write_file("test_red.py", "def test_bad():\n    assert False\n")
    assert release(path, ask=lambda q: "yes", **release_agents()) == "released"
    assert git(temp_workspace, "tag").strip() == "v0.1.0"


def test_release_refuses_unfinished_work(tmp_path):
    path = tmp_path / "tasks.json"
    task_board.save(task_board.new_board("idea", PLAN), path)  # tasks are still todo
    assert release(path, ask=lambda q: "yes", **release_agents()) == "not_built"


def test_no_terminal_means_no():
    def no_terminal(question):
        raise EOFError
    assert ask_human("Release?", no_terminal) is False


def test_devops_is_forced_to_call_submit_commit():
    client = FakeClient([reply("tool_use", tool_call("c1", "submit_commit", {"message": "feat: x"}))])
    make_devops(client=client).run("commit this")
    assert client.requests[0]["tool_choice"] == {"type": "tool", "name": "submit_commit"}


def test_other_agents_choose_their_tools_freely():
    client = FakeClient([reply("tool_use", tool_call("c1", "submit_brief",
                                                     {"product_name": "x", "brief": "y"}))])
    make_ceo("submit_brief", client=client).run("idea")
    assert "tool_choice" not in client.requests[0]


# --- Console: every step's output is shown in full ---------------------------

def test_the_plan_is_printed_in_full(tmp_path, capsys):
    pm_client = FakeClient([reply("tool_use", tool_call("c1", "submit_plan", PLAN))])
    plan_product("a tool", pm=make_pm(client=pm_client), board_path=tmp_path / "t.json")
    out = capsys.readouterr().out
    assert "TASKS (2):" in out
    assert "1. Core" in out and "2. CLI" in out
    assert "example: f(1) -> 2" in out


def test_qa_feedback_and_commit_message_are_printed(tmp_path, capsys):
    board_path = tmp_path / "tasks.json"
    task_board.save(task_board.new_board("idea", PLAN), board_path)
    devops = make_devops(client=FakeClient([
        reply("tool_use", tool_call("c1", "submit_commit", {"message": "feat: core\n\n- add core.py"})),
        reply("tool_use", tool_call("c2", "submit_commit", {"message": "feat: cli"})),
    ]))
    build_board(board_path, developer=WritesAndPasses(), qa=AlwaysPass(), devops=devops)
    out = capsys.readouterr().out
    assert "QA's review: PASS" in out
    assert "- add core.py" in out  # the commit body, not only the subject

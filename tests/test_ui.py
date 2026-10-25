"""
Tests for the web UI: ui_runs.py (starting runs, reading their logs, files)
and the file-based approval in company.py. No browser, no real model.
"""

import json
import threading
import time

import pytest

import company
import run_log
import task_board
import tools
import ui_runs


@pytest.fixture(autouse=True)
def temp_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_runs, "PROJECTS", tmp_path / "projects")
    return tmp_path / "projects"


def make_project(temp_projects, name="20261023-120000-dice", owner=None, pid=None):
    folder = temp_projects / name
    (folder / "workspace").mkdir(parents=True)
    meta = {"idea": "dice", "owner": owner, "started": "2026-10-23T12:00:00", "pid": pid}
    (folder / "meta.json").write_text(json.dumps(meta))
    return folder


# --- Starting runs -------------------------------------------------------------------

class FakePopen:
    started = []

    def __init__(self, cmd, **kwargs):
        FakePopen.started.append((cmd, kwargs))
        self.pid = 999999


def test_start_run_passes_the_idea_as_one_argument(monkeypatch, temp_projects):
    FakePopen.started = []
    monkeypatch.setattr(ui_runs.subprocess, "Popen", FakePopen)
    idea = 'a tool"; rm -rf ~; echo "'
    folder = ui_runs.start_run(idea, owner="a@example.com")

    cmd, kwargs = FakePopen.started[0]
    assert cmd[-1] == idea and "shell" not in kwargs  # plain text, never a shell command
    assert cmd[-2] == "run" and "--approve-via-file" in cmd
    assert ui_runs.read_meta(folder)["owner"] == "a@example.com"


@pytest.mark.parametrize("idea, error", [("   ", "Describe"), ("x" * 3000, "under")])
def test_start_run_checks_the_idea(idea, error):
    with pytest.raises(ui_runs.RunError, match=error):
        ui_runs.start_run(idea)


def test_only_one_run_at_a_time(monkeypatch, temp_projects):
    import os
    make_project(temp_projects, pid=os.getpid())  # "running": this very process exists
    with pytest.raises(ui_runs.RunError, match="busy"):
        ui_runs.start_run("another idea")


def test_users_only_see_their_own_projects(temp_projects):
    make_project(temp_projects, "20261023-1-a", owner="a@example.com")
    make_project(temp_projects, "20261023-2-b", owner="b@example.com")
    assert [f.name for f in ui_runs.list_projects("a@example.com")] == ["20261023-1-a"]


@pytest.mark.parametrize("bad", ["../..", "..", "a/b", "", "UPPER", "does-not-exist"])
def test_project_ids_from_the_url_are_checked(bad):
    with pytest.raises(ui_runs.RunError):
        ui_runs.project_folder(bad)


# --- Files ---------------------------------------------------------------------------

def test_only_workspace_files_can_be_read(temp_projects):
    folder = make_project(temp_projects)
    (folder / "workspace" / "app.py").write_text("x = 1\n")
    (folder / "workspace" / ".git").mkdir()
    (folder / "workspace" / ".git" / "config").write_text("secret")

    assert ui_runs.workspace_files(folder) == ["app.py"]
    assert ui_runs.read_workspace_file(folder, "app.py") == "x = 1\n"
    for name in ["../meta.json", ".git/config", "/etc/passwd"]:
        with pytest.raises(ui_runs.RunError):
            ui_runs.read_workspace_file(folder, name)


def test_zip_has_the_product_files(temp_projects):
    import io
    import zipfile
    folder = make_project(temp_projects)
    (folder / "workspace" / "app.py").write_text("x = 1\n")
    names = zipfile.ZipFile(io.BytesIO(ui_runs.workspace_zip(folder))).namelist()
    assert names == ["app.py"]


# --- Reading progress from the log ---------------------------------------------------

BOARD = {"spec": "## Goal", "tasks": [{"id": 1, "title": "Core", "description": "d", "examples": ["e"],
                                       "status": "todo", "attempts": 0, "feedback": ""}]}

EVENTS = [
    {"agent": "CEO", "event": "agent_start"},
    {"agent": "company", "event": "brief", "product_name": "dicecli", "brief": "Roll dice."},
    {"agent": "PM", "event": "agent_start"},
    {"agent": "company", "event": "plan_rejected", "error": "guess"},
    {"agent": "company", "event": "plan_saved", "board": BOARD},
    {"agent": "company", "event": "task_start", "task": BOARD["tasks"][0]},
    {"agent": "company", "event": "attempt_start", "attempt": 1},
    {"agent": "Developer", "event": "agent_end", "final_text": "Built core.py", "stop_reason": "done",
     "input_tokens": 100, "output_tokens": 10},
    {"agent": "company", "event": "test_check", "output": "Tests PASSED\n3 passed"},
    {"agent": "company", "event": "verdict", "verdict": "pass", "feedback": "Good."},
    {"agent": "company", "event": "task_end", "task": {"id": 1, "status": "done", "commit": "abc123"}},
    {"agent": "Docs Writer", "event": "agent_start"},
    {"agent": "company", "event": "commit", "commit": "def456"},
    {"agent": "company", "event": "release_review", "decision": "ready", "release_notes": "v1",
     "concerns": ""},
    {"agent": "company", "event": "release_summary", "version": "v0.1.0", "tests": "Tests PASSED"},
    {"agent": "company", "event": "waiting_for_approval", "question": "Release v0.1.0?"},
]


def test_summary_follows_the_whole_run():
    s = ui_runs.summarize(EVENTS, running=True)
    assert s["brief"]["product_name"] == "dicecli"
    assert s["plan_rejections"] == ["guess"]
    assert s["board"] == BOARD
    task = s["tasks"][1]
    assert task["status"] == "done" and task["commit"] == "abc123"
    assert task["attempts"][0] == {"number": 1, "developer": "Built core.py", "developer_stop": "done",
                                        "tests": "Tests PASSED\n3 passed", "verdict": "pass",
                                        "feedback": "Good."}
    assert s["release"]["version"] == "v0.1.0" and s["release"]["decision"] == "ready"
    assert s["state"] == "waiting for you" and s["stage"] == "approval"
    assert s["tokens"] == 110


@pytest.mark.parametrize("extra, running, state", [
    ([{"event": "approval", "approved": True}, {"event": "run_end", "ok": True}], False, "released"),
    ([{"event": "approval", "approved": False}, {"event": "run_end", "ok": False}], False, "not released"),
    ([], False, "stopped"),  # the process ended without run_end: it crashed or was stopped
])
def test_final_states(extra, running, state):
    assert ui_runs.summarize(EVENTS + extra, running)["state"] == state


def test_needs_human_state():
    events = EVENTS[:7] + [{"event": "task_end", "task": {"id": 1, "status": "needs_human"}},
                           {"event": "run_end", "ok": False}]
    assert ui_runs.summarize(events, running=False)["state"] == "needs a human"


def test_half_written_last_line_is_skipped(temp_projects):
    folder = make_project(temp_projects)
    (folder / "logs").mkdir()
    (folder / "logs" / "run-1.jsonl").write_text('{"event": "brief", "product_name": "x", "brief": "y"}\n{"event": "pl')
    assert [e["event"] for e in ui_runs.read_events(folder)] == ["brief"]


# --- company.py side: project folders and approval through a file ---------------------

def test_use_project_moves_every_file_into_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE", tools.WORKSPACE)
    monkeypatch.setattr(task_board, "BOARD_PATH", task_board.BOARD_PATH)
    monkeypatch.setattr(run_log, "LOG_DIR", run_log.LOG_DIR)
    folder = company.use_project(tmp_path / "p")
    assert tools.WORKSPACE == folder / "workspace" and tools.WORKSPACE.is_dir()
    assert task_board.BOARD_PATH == folder / "tasks.json"
    assert run_log.LOG_DIR == folder / "logs"


def test_ask_via_file_waits_for_the_answer(tmp_path):
    answer = tmp_path / "approval.txt"
    ask = company.ask_via_file(answer, timeout=5, poll=0.05)
    threading.Timer(0.2, lambda: answer.write_text("yes")).start()
    assert company.ask_human("Release?", ask) is True
    assert not answer.exists()  # used up: it can't approve the next release too


def test_an_old_answer_never_approves_a_new_release(tmp_path):
    answer = tmp_path / "approval.txt"
    answer.write_text("yes")  # left over from an earlier run
    ask = company.ask_via_file(answer, timeout=0.2, poll=0.05)
    assert company.ask_human("Release?", ask) is False


def test_no_answer_in_time_means_no(tmp_path):
    ask = company.ask_via_file(tmp_path / "approval.txt", timeout=0.1, poll=0.02)
    start = time.monotonic()
    assert company.ask_human("Release?", ask) is False
    assert time.monotonic() - start < 2


# --- The page itself (Streamlit's headless AppTest: no browser, no server) -------------

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(__import__("pathlib").Path(__file__).parent.parent / "streamlit_app.py")


def write_log(folder, events):
    (folder / "logs").mkdir(exist_ok=True)
    (folder / "logs" / "run-1.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_new_product_page_starts_a_run(monkeypatch):
    started = []
    monkeypatch.setattr(ui_runs, "start_run", lambda idea, owner=None: started.append(idea) or
                        (ui_runs.PROJECTS / "x"))
    at = AppTest.from_file(APP, default_timeout=10).run()
    assert not at.exception
    at.text_area[0].input("A word counter CLI")
    next(b for b in at.button if b.label == "Start the company").click().run()
    assert started == ["A word counter CLI"]


def test_product_page_shows_brief_plan_and_review(temp_projects):
    folder = make_project(temp_projects)
    write_log(folder, EVENTS + [{"event": "run_end", "ok": False}])
    at = AppTest.from_file(APP, default_timeout=10)
    at.query_params["project"] = folder.name
    at.run()
    assert not at.exception
    assert at.main.title[0].value == "dicecli"
    page = " ".join(m.value for m in at.markdown)
    assert "Roll dice." in page and "Built core.py" in page and "Good." in page


def test_local_mode_shows_every_project(temp_projects):
    folder = make_project(temp_projects, owner="b@example.com")
    at = AppTest.from_file(APP, default_timeout=10)
    at.query_params["project"] = folder.name
    at.run()
    # Without sign-in configured, this is the single-user local mode: all projects are visible.
    assert not at.error


# --- Red team in the UI ---------------------------------------------------------------

def test_start_redteam_runs_one_scenario_in_its_own_folder(monkeypatch, temp_projects):
    FakePopen.started = []
    monkeypatch.setattr(ui_runs.subprocess, "Popen", FakePopen)
    folder = ui_runs.start_redteam("impossible_qa", owner="a@example.com")

    cmd, kwargs = FakePopen.started[0]
    assert cmd[-1] == "impossible_qa" and "shell" not in kwargs
    # --project keeps this session's workspaces out of the real ./workspace.
    assert "--project" in cmd and str(folder) in cmd
    meta = ui_runs.read_meta(folder)
    assert meta["kind"] == "redteam" and meta["owner"] == "a@example.com"
    assert ui_runs.is_redteam(folder)


def test_start_redteam_without_a_scenario_runs_them_all(monkeypatch, temp_projects):
    FakePopen.started = []
    monkeypatch.setattr(ui_runs.subprocess, "Popen", FakePopen)
    ui_runs.start_redteam(None)
    assert "--all" in FakePopen.started[0][0]


def test_an_unknown_scenario_is_refused(temp_projects):
    with pytest.raises(ui_runs.RunError, match="Unknown scenario"):
        ui_runs.start_redteam("../../etc/passwd")


def test_a_product_run_is_not_a_redteam_run(temp_projects):
    assert not ui_runs.is_redteam(make_project(temp_projects))


REDTEAM_EVENTS = [
    {"agent": "redteam", "event": "scenario_start", "scenario": "impossible_qa", "task": "build it"},
    {"agent": "redteam", "event": "scenario_end", "scenario": "impossible_qa", "gamed": False,
     "disputed": True, "stop_reason": "done", "steps": 7, "findings": [],
     "protected_changed": [], "tests_reported_green": False, "summary": "QA TEST DISPUTE: ..."},
    {"agent": "Developer", "event": "model_reply", "step": 1},  # ignored: not a redteam event
    {"agent": "redteam", "event": "scenario_start", "scenario": "hiding_place", "task": "report"},
    {"agent": "redteam", "event": "scenario_end", "scenario": "hiding_place", "gamed": True,
     "disputed": False, "stop_reason": "done", "steps": 9,
     "findings": ["reporter.py: excludes QA's test file by name"],
     "protected_changed": [], "tests_reported_green": True, "summary": "done"},
]


def test_summarize_redteam_reports_each_scenarios_verdict():
    summary = ui_runs.summarize_redteam(REDTEAM_EVENTS, running=False)
    assert summary["finished"] == 2
    assert summary["gamed"] == ["hiding_place"]
    first, second = summary["scenarios"]
    assert first["disputed"] and first["done"] and first["steps"] == 7
    assert second["findings"] == ["reporter.py: excludes QA's test file by name"]
    assert ui_runs.redteam_state(summary, False) == "found a hole"


def test_a_scenario_still_running_is_not_counted_as_done():
    summary = ui_runs.summarize_redteam(REDTEAM_EVENTS[:1], running=True)
    assert summary["scenarios"][0]["done"] is False and summary["finished"] == 0
    assert ui_runs.redteam_state(summary, True) == "working"


def test_every_scenario_holding_is_reported_as_a_pass():
    summary = ui_runs.summarize_redteam(REDTEAM_EVENTS[:2], running=False)
    assert summary["gamed"] == []
    assert ui_runs.redteam_state(summary, False) == "guard rails held"


def test_a_session_that_produced_nothing_is_not_a_pass():
    # Crashed or stopped before the first scenario ended: never call that "held".
    assert ui_runs.redteam_state(ui_runs.summarize_redteam([], False), False) == "stopped"


def test_the_page_can_list_the_scenarios():
    names = [name for name, _ in ui_runs.scenarios()]
    assert "impossible_qa" in names and "hiding_place" in names
    assert all(summary for _, summary in ui_runs.scenarios())

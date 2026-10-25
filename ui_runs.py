"""
ui_runs.py - everything the web UI needs, without any Streamlit code.

The UI never runs agents inside the web server. It starts company.py as a
separate process, in its own project folder:

    projects/<id>/
        meta.json        idea, owner, start time, process id
        console.txt      what company.py prints
        workspace/       the product (its own git repository)
        tasks.json       the task board
        logs/run-*.jsonl every event of the run
        approval.txt     the UI writes "yes" or "no" here; company.py is waiting for it

Why a separate process?
  - A run takes 10-60 minutes. Web pages reload, browsers close; the run goes on.
  - company.py stays the ONE place that decides what happens. The UI only
    starts runs, READS their log, and passes on the human's answer.
  - The log we already write for debugging is all the UI needs to show progress.

Keeping this logic out of streamlit_app.py makes it easy to test.
"""

import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
PROJECTS = ROOT / "projects"
MAX_IDEA_CHARS = 2000
# A local model answers one request at a time, and every run costs tokens.
MAX_ACTIVE_RUNS = int(os.environ.get("UI_MAX_ACTIVE_RUNS", "1"))


class RunError(Exception):
    """A run can't be started; the message is shown to the user."""


# --- Projects ----------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "product"


def read_meta(folder: Path) -> dict:
    try:
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def list_projects(owner=None) -> list:
    """Project folders, newest first. With an owner, only that user's projects."""
    if not PROJECTS.exists():
        return []
    folders = [f for f in PROJECTS.iterdir() if (f / "meta.json").exists()]
    if owner is not None:
        folders = [f for f in folders if read_meta(f).get("owner") == owner]
    return sorted(folders, key=lambda f: read_meta(f).get("started", ""), reverse=True)


def project_folder(project_id: str) -> Path:
    """A project folder from an id in the URL. The id must be a plain folder
    name, so a crafted URL can't point us anywhere else."""
    if not re.fullmatch(r"[a-z0-9-]+", project_id or ""):
        raise RunError("Unknown project.")
    folder = PROJECTS / project_id
    if not (folder / "meta.json").exists():
        raise RunError("Unknown project.")
    return folder


# --- Starting, watching and stopping a run -----------------------------------

def is_running(folder: Path) -> bool:
    pid = read_meta(folder).get("pid")
    if not pid:
        return False
    try:
        # If the run is our child process, collect it once it has ended
        # (otherwise it stays a "zombie" that still looks alive).
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        pass  # started by an earlier web server process: just ask if it exists
    try:
        os.kill(pid, 0)  # signal 0 = "are you there?", it does not stop anything
        return True
    except OSError:
        return False


def active_runs() -> list:
    return [f for f in list_projects() if is_running(f)]


def start_run(idea: str, owner=None) -> Path:
    """Start `company.py run` for this idea in a new project folder."""
    idea = idea.strip()
    if not idea:
        raise RunError("Describe your product idea first.")
    if len(idea) > MAX_IDEA_CHARS:
        raise RunError(f"Keep the idea under {MAX_IDEA_CHARS} characters.")
    if len(active_runs()) >= MAX_ACTIVE_RUNS:
        raise RunError("The company is busy with another product. Try again when that run ends.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = PROJECTS / f"{stamp}-{_slug(idea)}"
    folder.mkdir(parents=True)
    meta = {"idea": idea, "owner": owner, "started": datetime.now().isoformat(timespec="seconds")}
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    with open(folder / "console.txt", "w", encoding="utf-8") as console:
        process = subprocess.Popen(
            # A list of arguments, no shell: the idea is passed as plain text,
            # so nothing the user types can become a command.
            [sys.executable, str(ROOT / "company.py"), "--project", str(folder),
             "--approve-via-file", "run", idea],
            cwd=ROOT, stdout=console, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},  # print lines as they happen
            start_new_session=True,  # its own process group, so "stop" ends its children too
        )
    meta["pid"] = process.pid
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return folder


def stop_run(folder: Path):
    pid = read_meta(folder).get("pid")
    if pid and is_running(folder):
        os.killpg(pid, signal.SIGTERM)


def answer_approval(folder: Path, approved: bool):
    """Pass the human's decision to the waiting company.py (see ask_via_file)."""
    (folder / "approval.txt").write_text("yes" if approved else "no", encoding="utf-8")


def answer_pending(folder: Path) -> bool:
    """An answer was given but company.py hasn't picked it up yet (within 2 seconds)."""
    return (folder / "approval.txt").exists()


# --- Reading a run's progress from its log -----------------------------------

def read_events(folder: Path) -> list:
    """All events of the project's newest run log ([] before it exists)."""
    logs = sorted((folder / "logs").glob("run-*.jsonl"))
    if not logs:
        return []
    events = []
    for line in logs[-1].read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass  # the last line may be half written while we read
    return events


def summarize(events: list, running: bool) -> dict:
    """Turn the event stream into what the UI shows: the brief, the plan, every
    task with its attempts, the release review, and the current state."""
    s = {"brief": None, "board": None, "plan_rejections": [], "tasks": {}, "current_task": None,
         "release": {}, "waiting": None, "approved": None, "ok": None, "error": None,
         "activity": None, "tokens": 0, "stage": "starting"}
    attempt = None
    for e in events:
        kind, agent = e.get("event"), e.get("agent")
        if kind == "agent_start":
            s["activity"] = agent
            s["stage"] = {"CEO": "ceo", "PM": "plan", "Docs Writer": "docs"}.get(agent, s["stage"])
        elif kind == "agent_end":
            s["tokens"] += e.get("input_tokens", 0) + e.get("output_tokens", 0)
            if agent == "Developer" and attempt is not None:
                attempt["developer"] = e.get("final_text", "")
                attempt["developer_stop"] = e.get("stop_reason")
        elif kind == "brief":
            s["brief"] = {"product_name": e.get("product_name"), "brief": e.get("brief")}
        elif kind == "plan_rejected":
            s["plan_rejections"].append(e.get("error", ""))
        elif kind == "plan_saved":
            s["board"] = e.get("board")
            s["stage"] = "build"
        elif kind == "plan_error":
            s["error"] = f"Planning failed: {e.get('error')}"
        elif kind == "task_start":
            task = e.get("task", {})
            s["current_task"] = task.get("id")
            s["tasks"][task.get("id")] = {"attempts": [], "status": "building", "commit": None}
        elif kind == "attempt_start" and s["current_task"] in s["tasks"]:
            attempt = {"number": e.get("attempt")}
            s["tasks"][s["current_task"]]["attempts"].append(attempt)
        elif kind in ("test_check", "verdict", "stuck", "dispute") and attempt is not None:
            if kind == "test_check":
                attempt["tests"] = e.get("output", "")
            elif kind == "verdict":
                attempt["verdict"] = e.get("verdict")
                attempt["feedback"] = e.get("feedback", "")
            else:
                attempt[kind] = True
        elif kind == "task_end":
            task = e.get("task", {})
            entry = s["tasks"].setdefault(task.get("id"), {"attempts": []})
            entry.update(status=task.get("status"), commit=task.get("commit"),
                         feedback=task.get("feedback", ""))
            s["current_task"] = None
            attempt = None
        elif kind == "commit" and s["stage"] in ("docs", "review"):
            s["release"]["docs_commit"] = e.get("commit")
        elif kind == "release_review":
            s["stage"] = "review"
            s["release"].update(decision=e.get("decision"), notes=e.get("release_notes"),
                                concerns=e.get("concerns"))
        elif kind == "release_summary":
            s["release"].update(version=e.get("version"), tests=e.get("tests"),
                                history=e.get("history"))
        elif kind == "waiting_for_approval":
            s["waiting"] = e.get("question")
            s["stage"] = "approval"
            s["activity"] = "you"
        elif kind == "approval":
            s["waiting"] = None
            s["approved"] = e.get("approved")
        elif kind == "run_end":
            s["ok"] = e.get("ok")

    s["state"] = _state(s, running)
    return s


def _state(s: dict, running: bool) -> str:
    """One word for the run's status, for the badge in the UI."""
    if s["approved"]:
        return "released"
    if s["approved"] is False:
        return "not released"
    if running:
        return "waiting for you" if s["waiting"] else "working"
    if any(t.get("status") == "needs_human" for t in s["tasks"].values()):
        return "needs a human"
    if s["ok"] is None or s["error"]:
        return "stopped"  # ended without run_end: crashed, stopped, or planning failed
    return "finished" if s["ok"] else "stopped"


# --- The product's files -------------------------------------------------------

def workspace_files(folder: Path) -> list:
    """The product's files (relative paths), without git internals."""
    workspace = folder / "workspace"
    if not workspace.exists():
        return []
    return sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*")
                  if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
                  and ".pytest_cache" not in p.parts)


def read_workspace_file(folder: Path, name: str) -> str:
    """Only files from workspace_files() can be read, so the UI can't be used to read anything else."""
    if name not in workspace_files(folder):
        raise RunError("Unknown file.")
    return (folder / "workspace" / name).read_text(encoding="utf-8", errors="replace")


def workspace_zip(folder: Path) -> bytes:
    """The product as a .zip, to download (git history not included)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name in workspace_files(folder):
            z.write(folder / "workspace" / name, name)
    return buffer.getvalue()


def console_tail(folder: Path, lines=200) -> str:
    try:
        text = (folder / "console.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def elapsed(folder: Path) -> str:
    started = read_meta(folder).get("started")
    if not started:
        return ""
    seconds = max(0, int(time.time() - datetime.fromisoformat(started).timestamp()))
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


# --- Red team ------------------------------------------------------------------
#
# redteam.py corners an agent on purpose and reports whether it went around the
# rules. The UI runs it exactly like a product run: a separate process, its own
# folder, and the page reads the log. Nothing new to trust.

REDTEAM = ROOT / "redteam.py"


def scenarios() -> list:
    """[(name, one-line summary)] for the page, read from redteam.py itself.

    Imported lazily: redteam.py imports agents, which reads config and would
    make the UI fail at import time when no model is configured.
    """
    import redteam
    return [(s.name, s.summary) for s in redteam.SCENARIOS.values()]


def start_redteam(scenario: str = None, owner=None) -> Path:
    """Start redteam.py in a new session folder. No scenario = run all four."""
    import redteam
    if scenario is not None and scenario not in redteam.SCENARIOS:
        raise RunError("Unknown scenario.")
    if len(active_runs()) >= MAX_ACTIVE_RUNS:
        raise RunError("The company is busy with another run. Try again when that one ends.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = PROJECTS / f"{stamp}-redteam-{_slug(scenario or 'all')}"
    folder.mkdir(parents=True)
    meta = {"idea": f"Red team: {scenario or 'all scenarios'}", "owner": owner,
            "kind": "redteam", "scenario": scenario,
            "started": datetime.now().isoformat(timespec="seconds")}
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    command = [sys.executable, str(REDTEAM), "--project", str(folder)]
    command += [scenario] if scenario else ["--all"]
    with open(folder / "console.txt", "w", encoding="utf-8") as console:
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=console, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
        )
    meta["pid"] = process.pid
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return folder


def is_redteam(folder: Path) -> bool:
    return read_meta(folder).get("kind") == "redteam"


def summarize_redteam(events: list, running: bool) -> dict:
    """What the page shows: one entry per scenario, plus the overall verdict.

    Reads the same run log as everything else: redteam.py writes a
    scenario_start when it begins one and a scenario_end with the verdict.
    """
    summary = {"scenarios": [], "running": running}
    for event in events:
        if event.get("agent") != "redteam":
            continue
        if event["event"] == "scenario_start":
            summary["scenarios"].append({"name": event.get("scenario", "?"),
                                         "task": event.get("task", ""), "done": False})
        elif event["event"] == "scenario_end":
            for entry in summary["scenarios"]:
                if entry["name"] == event.get("scenario"):
                    entry.update({k: v for k, v in event.items()
                                  if k not in ("time", "agent", "event")}, done=True)
    done = [s for s in summary["scenarios"] if s["done"]]
    summary["gamed"] = [s["name"] for s in done if s.get("gamed")]
    summary["finished"] = len(done)
    return summary


def redteam_state(summary: dict, running: bool) -> str:
    """One word for the badge. 'gamed' is a finding, not an error: it is the
    harness doing its job."""
    if running:
        return "working"
    if not summary["finished"]:
        return "stopped"
    return "found a hole" if summary["gamed"] else "guard rails held"

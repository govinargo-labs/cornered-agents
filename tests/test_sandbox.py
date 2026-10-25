"""
Tests for sandbox.py: the code the agents write must not escape the workspace.

Each test plants an "attacker" test file in the workspace, runs it through
tools.run_tests() (the same path the agents use), and checks the attack failed.
The attacker files ASSERT that they were blocked, so "Tests PASSED" = safe.

They run on macOS (Seatbelt) and on Linux with bubblewrap installed. A blocked
file can show up as different errors: PermissionError on macOS, but on Linux
the file is often simply not there (FileNotFoundError) or on a read-only file
system. All of those are OSError, so that is what most attackers expect.
"""

import platform

import pytest

import sandbox
import tools

# Skip only where there is no sandbox to test. If bwrap is installed but does
# not work, the tests run and fail: that is something you want to know.
HAS_SANDBOX = platform.system() == "Darwin" or (platform.system() == "Linux" and sandbox._find_bwrap() is not None)
pytestmark = pytest.mark.skipif(not HAS_SANDBOX, reason="needs macOS (Seatbelt) or Linux with bubblewrap (bwrap)")


@pytest.fixture(autouse=True)
def temp_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE", workspace)
    monkeypatch.delenv("AGENT_SANDBOX", raising=False)
    return workspace


def run_attack(code):
    tools.write_file("test_attack.py", code)
    return tools.run_tests()


def test_api_keys_are_not_passed_to_agent_code(monkeypatch):
    monkeypatch.setenv("API_KEY_LLM", "secret-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-456")
    output = run_attack(
        "import os\n"
        "def test_attack():\n"
        "    assert 'API_KEY_LLM' not in os.environ and 'ANTHROPIC_API_KEY' not in os.environ\n")
    assert output.startswith("Tests PASSED"), output


def test_agent_code_cannot_write_outside_the_workspace(temp_workspace):
    outside = temp_workspace.parent / "outside.txt"
    output = run_attack(
        "import pytest\n"
        "def test_attack():\n"
        "    with pytest.raises(OSError):\n"
        f"        open({str(outside)!r}, 'w').write('pwned')\n")
    assert output.startswith("Tests PASSED"), output
    assert not outside.exists()


def test_agent_code_cannot_read_your_home_folder():
    output = run_attack(
        "import pytest\nfrom pathlib import Path\n"
        "def test_attack():\n"
        "    with pytest.raises(PermissionError):\n"  # on Linux too: the stand-in home is locked
        f"        list(Path({str(sandbox.Path.home())!r}).iterdir())\n")
    assert output.startswith("Tests PASSED"), output


def test_agent_code_has_no_network():
    output = run_attack(
        "import socket, pytest\n"
        "def test_attack():\n"
        "    with pytest.raises(OSError):\n"
        "        socket.create_connection(('127.0.0.1', 8001), timeout=2)\n")
    assert output.startswith("Tests PASSED"), output


def test_programs_started_by_agent_code_are_locked_in_too():
    # The Developer's CLI tests start a subprocess. It must inherit the sandbox.
    output = run_attack(
        "import subprocess\n"
        "def test_attack():\n"
        f"    r = subprocess.run(['ls', {str(sandbox.Path.home())!r}], capture_output=True)\n"
        "    assert r.returncode != 0\n")
    assert output.startswith("Tests PASSED"), output


def test_normal_work_still_runs():
    output = run_attack(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "def test_ok(tmp_path):\n"
        "    Path('note.txt').write_text('hi')\n"            # write in the workspace
        "    (tmp_path / 'x').write_text('hi')\n"            # pytest's temp folders
        "    r = subprocess.run([sys.executable, '-c', 'print(2)'], capture_output=True, text=True)\n"
        "    assert r.stdout.strip() == '2'\n")              # CLI tests work
    assert output.startswith("Tests PASSED"), output


# --- Fail closed ---------------------------------------------------------------

def test_no_sandbox_means_no_agent_code(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox, "_find_bwrap", lambda: None)  # Linux without bubblewrap
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.mode()


def test_broken_bubblewrap_means_no_agent_code(monkeypatch, tmp_path):
    # Installed but not working (e.g. Docker blocks it): still no agent code.
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\necho 'creating new namespace failed' >&2\nexit 1\n")
    fake_bwrap.chmod(0o755)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox, "_find_bwrap", lambda: str(fake_bwrap))
    with pytest.raises(sandbox.SandboxUnavailable, match="namespace failed"):
        sandbox.mode()


def test_sandbox_can_only_be_switched_off_explicitly(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setenv("AGENT_SANDBOX", "off")
    assert sandbox.mode() == "off"


# --- run_python: the PM computes examples instead of guessing them ------------

def test_run_python_computes_exact_values():
    output = tools.run_python("import random\nprint(random.Random(42).randint(1, 20))")
    assert output == "exit code 0\n4\n"  # the PM once guessed [1] for this


def test_pm_sees_the_computed_value(tmp_path):
    from agents.pm import make_pm
    from company import plan_product
    from tests.fakes import FakeClient, reply, tool_call
    plan = {"spec": "## Goal\nDice.", "tasks": [{"title": "Roll", "description": "roll()",
                                                  "examples": ["roll_dice(1, 20, seed=42) -> [4]"]}]}
    client = FakeClient([
        reply("tool_use", tool_call("c1", "run_python", {"code": "import random\nprint(random.Random(42).randint(1, 20))"})),
        reply("tool_use", tool_call("c2", "submit_plan", plan)),
    ])
    plan_product("dice", pm=make_pm(client=client), board_path=tmp_path / "tasks.json")
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert not tool_result["is_error"] and tool_result["content"] == "exit code 0\n4\n"


def test_run_python_cannot_touch_the_workspace_or_home(temp_workspace):
    tools.write_file("app.py", "SECRET = 1\n")
    output = tools.run_python(
        "from pathlib import Path\n"
        f"for p in [{str(temp_workspace / 'app.py')!r}, {str(sandbox.Path.home() / '.zshrc')!r}]:\n"
        "    try:\n        Path(p).read_text(); print('READ', p)\n"
        "    except OSError:\n        print('blocked')\n"
        "try:\n"
        f"    Path({str(temp_workspace / 'evil.py')!r}).write_text('x'); print('WROTE')\n"
        "except OSError:\n    print('blocked')\n")
    assert output == "exit code 0\nblocked\nblocked\nblocked\n", output
    assert not (temp_workspace / "evil.py").exists()


def test_only_the_pm_gets_run_python():
    from agents.developer import DEVELOPER_TOOLS
    from agents.qa import QA_TOOLS
    assert "run_python" not in {t["name"] for t in DEVELOPER_TOOLS + QA_TOOLS}

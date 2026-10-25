"""
sandbox.py - run the agents' code in a locked box.

The gap this closes: "agents never get a shell" protected our TOOLS, but
run_tests executes the code the agents WROTE, with your user's full rights.
That code could read ~/.ssh, steal API keys from the environment, delete your
files, or send data over the internet. (We noticed when the Developer started
writing helper scripts that call subprocess.)

So every test run goes through here:
  1. A clean environment: only PATH, HOME, TMPDIR and LANG. No API keys,
     nothing from your shell profile.
  2. On macOS, the built-in Seatbelt sandbox (sandbox-exec) enforces:
       - no network at all;
       - writes only to the workspace and a private temp folder;
       - in your home folder, file CONTENTS can only be read from the
         workspace and the Python install (so no ~/.ssh, no .env, no
         other projects). File NAMES are still visible: a known limit.
  3. On Linux, bubblewrap (bwrap) builds a fresh, mostly empty file system
     for the agent code:
       - no network (it gets its own empty network namespace);
       - the system folders (/usr, /etc, ...) and the Python install,
         read-only;
       - the workspace and a private temp folder, writable;
       - nothing else: no other projects, no /tmp of the host, and your home
         folder is an empty locked folder (it cannot even be listed).
  4. Other systems: we refuse to run agent code unprotected, unless you
     explicitly set AGENT_SANDBOX=off (e.g. inside a throwaway container or CI).

Fail closed: if the protection is not there (or does not work), nothing runs.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class SandboxUnavailable(Exception):
    """We cannot protect this machine, so we will not run agent code."""


def mode() -> str:
    """"seatbelt" (macOS), "bwrap" (Linux), "off" (explicitly disabled), or raise."""
    if os.environ.get("AGENT_SANDBOX", "").lower() == "off":
        return "off"
    if platform.system() == "Darwin" and Path(SANDBOX_EXEC).exists():
        return "seatbelt"
    if platform.system() == "Linux":
        bwrap = _find_bwrap()
        if bwrap is None:
            raise SandboxUnavailable(
                "bubblewrap is not installed, so agent code will not run. "
                "Install it (Debian/Ubuntu: apt install bubblewrap), or run inside a "
                "throwaway container or VM and set AGENT_SANDBOX=off there.")
        problem = _bwrap_problem(bwrap)
        if problem:
            raise SandboxUnavailable(
                f"bubblewrap is installed but cannot build a sandbox here ({problem}), "
                "so agent code will not run. In Docker, start the container with "
                "--security-opt seccomp=unconfined --security-opt apparmor=unconfined "
                "(see README, 'Host the UI').")
        return "bwrap"
    raise SandboxUnavailable(
        "No sandbox available on this system, so agent code will not run. "
        "Run inside a throwaway container or VM and set AGENT_SANDBOX=off there.")


# --- macOS: Seatbelt -----------------------------------------------------------

def _quote(path) -> str:
    """A path as a string literal in the sandbox profile language."""
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(workspace: Path, temp_dir: Path) -> str:
    """The Seatbelt rules. Later rules win over earlier ones."""
    readable = [workspace, temp_dir, Path(sys.prefix), Path(sys.base_prefix)]
    private = [Path.home(), Path("/private/tmp"), Path("/private/var/folders"), Path("/Volumes")]
    real = lambda p: Path(p).resolve()
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        # Writes: only the workspace, our private temp folder, and harmless devices.
        "(deny file-write*)",
        f"(allow file-write* (subpath {_quote(real(workspace))}) (subpath {_quote(real(temp_dir))})",
        '    (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/fd/"))',
        # Reads: no file contents from your home, other temp files, or external disks...
        "(deny file-read-data " + " ".join(f"(subpath {_quote(real(p))})" for p in private) + ")",
        # ...except what the tests need: the workspace and the Python install.
        "(allow file-read-data " + " ".join(f"(subpath {_quote(real(p))})" for p in readable) + ")",
        # pytest lists the folders ABOVE the workspace while it collects tests.
        # Allow listing exactly those folders (names only; not their files, not
        # any other folder like ~/.ssh).
        "(allow file-read-data " + " ".join(f"(literal {_quote(p)})" for p in real(workspace).parents) + ")",
    ])


# --- Linux: bubblewrap ---------------------------------------------------------

# The system folders agent code may read (programs, libraries, /etc config).
# Everything else of the host simply does not exist inside the sandbox.
SYSTEM_DIRS = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc"]


def _find_bwrap():
    """The path of the bwrap program, or None. (A function so tests can fake it.)"""
    return shutil.which("bwrap")


_bwrap_checked = {}  # bwrap path -> problem text, or "" if it works. Checked once.


def _bwrap_problem(bwrap: str) -> str:
    """Try the real sandbox once with a no-op command. "" = it works.

    bwrap can be installed but still fail, e.g. inside Docker, which blocks the
    kernel features it needs by default. Better to find out here, with a clear
    message, than in the middle of an agent's test run.
    """
    if bwrap not in _bwrap_checked:
        with tempfile.TemporaryDirectory(prefix="agent-sandbox-check-") as folder:
            folder = Path(folder).resolve()
            command = [bwrap, *bwrap_args(folder, folder), sys.executable, "-c", "pass"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                problem = "" if result.returncode == 0 else (result.stderr.strip() or f"exit code {result.returncode}")
            except (OSError, subprocess.TimeoutExpired) as error:
                problem = str(error)
        _bwrap_checked[bwrap] = problem
    return _bwrap_checked[bwrap]


def bwrap_args(workspace: Path, temp_dir: Path) -> list:
    """The bubblewrap options. They run in order, like building a new file system step by step."""
    args = [
        # Own namespaces for everything: network (so: no network), processes,
        # users, ... The agent code cannot see or signal our processes.
        "--unshare-all",
        "--die-with-parent",  # if we stop (e.g. timeout), the agent code stops too
        "--new-session",      # cannot type commands into our terminal
    ]
    if os.geteuid() == 0:
        # Running as root (e.g. a container): make sure root powers do not come along.
        args += ["--cap-drop", "ALL"]

    # The system folders, read-only. On many systems /bin, /lib, ... are just
    # links into /usr; we copy those links instead of mounting twice.
    for folder in SYSTEM_DIRS:
        if os.path.islink(folder):
            args += ["--symlink", os.readlink(folder), folder]
        elif os.path.isdir(folder):
            args += ["--ro-bind", folder, folder]
    args += ["--proc", "/proc", "--dev", "/dev"]  # a fresh /proc and a minimal /dev

    # Your home folder: an empty, in-memory stand-in, so ~/.ssh, ~/.aws, .env
    # files, other projects... are simply not there.
    home = Path.home().resolve()
    hide_home = home != Path("/") and home.is_dir()
    if hide_home:
        args += ["--tmpfs", str(home)]

    # The Python install (it may live inside your home, e.g. a venv), read-only.
    for folder in {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}:
        args += ["--ro-bind", str(folder), str(folder)]

    # The only writable places: the workspace and our private temp folder.
    args += ["--bind", str(workspace), str(workspace), "--bind", str(temp_dir), str(temp_dir)]

    if hide_home:
        # Lock the stand-in home: folders inside it can still be entered (the
        # venv, the workspace), but it cannot be listed or written to.
        args += ["--chmod", "0111", str(home), "--remount-ro", str(home)]
    # Make the rest of the new file system read-only: no writing "next to" the
    # workspace either.
    args += ["--remount-ro", "/", "--chdir", str(workspace)]
    return args


# --- Both ----------------------------------------------------------------------

def clean_env(temp_dir: Path) -> dict:
    """The only environment variables agent code gets. No keys, no secrets."""
    return {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(temp_dir),
        "TMPDIR": str(temp_dir),
        # C.UTF-8 exists on every Linux; en_US.UTF-8 often does not in containers.
        "LANG": "en_US.UTF-8" if platform.system() == "Darwin" else "C.UTF-8",
    }


class Sandbox:
    """Use as: with Sandbox(workspace) as box: subprocess.run(box.wrap(cmd), env=box.env, ...)"""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.mode = mode()  # raises SandboxUnavailable before we create anything

    def __enter__(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="agent-sandbox-")).resolve()
        self.env = clean_env(self.temp_dir)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def wrap(self, command: list) -> list:
        if self.mode == "off":
            return command
        if self.mode == "bwrap":
            return [_find_bwrap(), *bwrap_args(self.workspace, self.temp_dir), *command]
        return [SANDBOX_EXEC, "-p", profile(self.workspace, self.temp_dir), *command]

"""
run_safely.py - run agent-written code inside the same sandbox its tests used.

The agents' tests run sandboxed. Running the finished product does NOT: it
executes as you, with your files, your keys and your network. The code passed
its tests, which this repo spends four case studies showing is not evidence
that it does what it claims.

So: read the code first. If you would rather try it before reading all of it,
run it through here. Same protection the tests had - no network, no API keys
in the environment, no reading your home folder, no writing outside the
product's own folder.

    venv/bin/python run_safely.py workspace main.py
    venv/bin/python run_safely.py projects/20261025-120000-dice/workspace main.py --sides 20

What this does NOT protect against: anything you run afterwards outside this
script, and the limits listed in SECURITY.md. The sandbox is a seatbelt, not
a guarantee.
"""

import subprocess
import sys
from pathlib import Path

import sandbox


def main(argv):
    if len(argv) < 2:
        print(__doc__.split("    venv")[0].strip())
        print("\nUsage: python run_safely.py <workspace-folder> <script.py> [args...]")
        return 2

    folder = Path(argv[0]).resolve()
    if not folder.is_dir():
        print(f"Not a folder: {folder}")
        return 2
    script = folder / argv[1]
    if not script.is_file():
        print(f"No such file in {folder}: {argv[1]}")
        return 2

    try:
        box_mode = sandbox.mode()
    except sandbox.SandboxUnavailable as e:
        # Fail closed, exactly like run_tests: no sandbox, no run.
        print(f"Refusing to run: {e}")
        return 1
    if box_mode == "off":
        print("Refusing to run: AGENT_SANDBOX=off disables the sandbox. "
              "Unset it, or run inside a throwaway container or VM.")
        return 1

    print(f"[run_safely] sandbox: {box_mode} | folder: {folder} | script: {argv[1]}")
    print("[run_safely] no network, no secrets, writes confined to the folder above.\n")
    with sandbox.Sandbox(folder) as box:
        result = subprocess.run(
            box.wrap([sys.executable, str(script), *argv[2:]]),
            cwd=folder, env=box.env,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

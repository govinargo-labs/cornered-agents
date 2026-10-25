"""
tools.py - the "hands" of our agents.

An LLM can only produce text. It cannot read a file or run a test by itself.
So we give it TOOLS: normal Python functions that WE run on its behalf.

The flow is always:
  1. We describe each tool to the model (name + what it does + its inputs).
     Those descriptions are TOOL_SCHEMAS at the bottom of this file.
  2. The model answers with "please call read_file with path='x.py'".
  3. Our code runs the real Python function and sends the result back.

Safety rule: every tool works ONLY inside the ./workspace folder.
The model never gets a shell and can never touch files outside workspace/.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import sandbox

# The only folder agents are allowed to touch.
# Tests replace this with a temporary folder, so always read it at call time.
WORKSPACE = Path(__file__).parent / "workspace"

# Long outputs waste tokens (= money) and confuse the model. Cut them down.
MAX_OUTPUT_CHARS = 10_000

# The biggest file an agent may write. Nothing it legitimately builds comes near
# this; without a cap, a stuck agent in a retry loop can fill the disk, and a
# file bigger than read_file's limit can never be read back to fix it.
MAX_FILE_CHARS = 200_000

# The most files list_files will name. A deep or wide tree would otherwise flood
# the prompt with paths and push the real instructions out of the context.
MAX_LISTED_FILES = 300

# A test run that hangs (e.g. an infinite loop the agent wrote) is killed after this.
TEST_TIMEOUT_SECONDS = 60


class ToolError(Exception):
    """A problem we want to report back to the model, so it can fix its request."""


def safe_path(relative_path: str) -> Path:
    """Turn a path from the model into a real path, but only inside WORKSPACE.

    Why this matters: the model could ask for "../.env" (by mistake, or because
    a file it read told it to). resolve() turns "workspace/../.env" into the
    real absolute path, and then we check it is still inside the workspace.
    """
    workspace = WORKSPACE.resolve()
    full_path = (workspace / relative_path).resolve()
    if full_path != workspace and workspace not in full_path.parents:
        raise ToolError(f"Path '{relative_path}' is outside the workspace. Use paths like 'slugify.py'.")
    # .git is the company's version history, managed by OUR code (git_ops.py).
    # An agent that could write .git/hooks/pre-commit would get its code run
    # by our next "git commit": a backdoor to running anything. So: hands off.
    if ".git" in full_path.relative_to(workspace).parts:
        raise ToolError("The .git folder is managed by the company, not by agents.")
    return full_path


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [cut: {len(text) - MAX_OUTPUT_CHARS} more characters]"


def _truncate_middle(text: str) -> str:
    """Like _truncate, but keep the END too. pytest puts its most useful part,
    the list of failed tests and the totals, at the very end."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head, tail = MAX_OUTPUT_CHARS * 7 // 10, MAX_OUTPUT_CHARS * 3 // 10
    return f"{text[:head]}\n... [cut: {len(text) - head - tail} characters] ...\n{text[-tail:]}"


# ---------------------------------------------------------------------------
# The tools themselves: plain Python functions that return a string.
# ---------------------------------------------------------------------------

def list_files() -> str:
    """List every file in the workspace (relative paths, one per line)."""
    workspace = WORKSPACE.resolve()
    skip = {"__pycache__", ".pytest_cache", ".git"}
    files = [
        str(p.relative_to(workspace))
        for p in sorted(workspace.rglob("*"))
        if p.is_file() and not skip.intersection(p.parts)
    ]
    if not files:
        return "(workspace is empty)"
    if len(files) > MAX_LISTED_FILES:
        extra = len(files) - MAX_LISTED_FILES
        return "\n".join(files[:MAX_LISTED_FILES]) + f"\n... [{extra} more files not listed]"
    return "\n".join(files)


def read_file(path: str) -> str:
    """Return the text of one file in the workspace."""
    full_path = safe_path(path)
    if not full_path.is_file():
        raise ToolError(f"File not found: {path}")
    return _truncate(full_path.read_text(encoding="utf-8"))


# Test files with this prefix belong to QA. Only write_qa_test may create or change
# them, so the Developer cannot "fix" a failing QA test instead of fixing its code.
QA_TEST_PREFIX = "test_qa_"

# Files that change HOW pytest runs (which tests run, which are skipped).
# Lesson learned: we once protected only QA's test files, and the Developer wrote
# a conftest.py that skipped QA's test instead. Protect the outcome, not just a name.
TEST_CONFIG_FILES = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}

# Python imports these automatically at startup, before pytest's own code runs,
# so they are another way to decide what a test run reports. --noconftest does
# not help here: they are not conftest files.
AUTO_IMPORTED_FILES = {"sitecustomize.py", "usercustomize.py"}

# .gitignore decides what the DevOps agent can SEE and commit. An agent that
# writes "*.py" into it makes its own source invisible: `git add -A` stages
# nothing, the staged diff shown to DevOps is empty, the commit succeeds, the
# task is marked done, and the code still sits on disk running in the tests.
# Nobody reading git history ever sees it. Case study 004. Our code writes the
# initial .gitignore (git_ops.GITIGNORE); agents have no reason to change it.
VERSION_CONTROL_FILES = {".gitignore", ".gitattributes", ".gitmodules"}

# Secret/config files. An agent asked to make a test about real configuration
# pass will otherwise WRITE the config file containing the expected value, then
# write honest code that reads it: every test green, the data invented.
# (redteam.py "exfiltrate" scenario, case study 003.) These names are also the
# ones worth planting to confuse a human reading the workspace later.
CREDENTIAL_FILES = {".env", ".env.local", ".env.production", ".netrc", ".npmrc",
                    "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}

# Tests run with the workspace as the working directory, and Python puts the
# working directory first on sys.path. So a workspace file named like a
# standard-library module (typing.py, json.py, enum.py, ...) is imported
# INSTEAD of the real one, by pytest itself. "raise SystemExit(0)" in such a
# file ends the run with exit code 0 and no output at all: a silent green.
# Case study 003. We compare without the .py suffix, lowercased, because
# import names are what matter.
STDLIB_MODULE_NAMES = {name.lower() for name in sys.stdlib_module_names}

# The same trick works with the packages the test run itself imports, and those
# are not in sys.stdlib_module_names because they are installed, not built in.
TEST_RUNNER_MODULE_NAMES = {"pytest", "_pytest", "py", "pluggy", "iniconfig", "packaging"}


def _reject_qa_tampering(full_path: Path, content: str):
    """Refuse code that reaches into one of QA's test modules.

    pytest imports every test file into ONE process, in alphabetical order. So a
    Developer file that sorts earlier (test_aaa_*.py) can do:

        import test_qa_counter
        del test_qa_counter.test_counts_words

    QA's file on disk is untouched, but the test no longer exists when pytest
    looks for it, and the run goes green. Case study 003. Writing QA's file is
    already blocked; this blocks reaching into it from somewhere else.
    """
    # Our own module does this legitimately (see QA_TEST_PREFIX above), and so
    # may a test that checks this rule, so only look at agent-written code.
    if QA_TEST_PREFIX not in content:
        return
    for line in content.splitlines():
        stripped = line.strip()
        # "import test_qa_x", "from test_qa_x import ...", "importlib...('test_qa_x')"
        if (stripped.startswith(("import ", "from ")) or "import_module" in stripped
                or "__import__" in stripped) and QA_TEST_PREFIX in stripped:
            raise ToolError(
                f"This file imports one of QA's test modules ({stripped!r}). Importing a "
                "test_qa_ module lets code change QA's tests while they run, so it is not "
                "allowed. If you believe a QA test is wrong, raise a QA TEST DISPUTE instead.")


# An overwrite that throws away most of a file's content is how an agent
# "deletes" something when it has no delete tool. Seen live: asked for a test
# that could only pass if three leftover files stopped counting, the Developer
# overwrote each with "# no content" and then wrote code that skipped
# comment-only files. Case study 004.
#
# The numbers are deliberately loose: real rewrites shrink files all the time.
# We only stop the case that is hard to explain as work: a file with real
# content replaced by almost nothing.
# 80 characters is about three lines of code: below that there is nothing much
# to destroy, and stub files are a normal way to start. The real case had a
# 185-character file replaced by "# no content".
GUTTING_MIN_OLD_CHARS = 80
GUTTING_MAX_NEW_RATIO = 0.2   # keeping under a fifth of it looks like emptying, not editing


def _reject_gutting(full_path: Path, content: str, path: str):
    """Refuse replacing a file that has real content with almost nothing."""
    if not full_path.is_file():
        return
    old = full_path.read_text(encoding="utf-8", errors="replace")
    if len(old) < GUTTING_MIN_OLD_CHARS:
        return
    if len(content.strip()) > len(old.strip()) * GUTTING_MAX_NEW_RATIO:
        return
    raise ToolError(
        f"This would replace '{path}' ({len(old)} characters) with only {len(content)}, "
        "which empties the file rather than changing it. If a file is in your way, say so "
        "in your summary instead of blanking it; if you are rewriting it, write the real "
        "new version.")


def _reject_oversized(content: str, path: str):
    """Refuse a file no honest task needs. Shared by every write tool."""
    if len(content) > MAX_FILE_CHARS:
        raise ToolError(
            f"'{path}' would be {len(content)} characters, over the {MAX_FILE_CHARS} limit. "
            "Split the work into smaller files, or write less.")


def _reject_protected_name(full_path: Path, path: str):
    """Refuse a write that would change what a test run REPORTS, rather than
    what the code does. Shared by write_file and write_doc."""
    # .lower(): on a case-insensitive disk "CONFTEST.PY" is the same file as
    # "conftest.py", and on Linux a name that only differs in case would slip
    # past a case-sensitive check.
    name = full_path.name.lower()
    if name in TEST_CONFIG_FILES:
        raise ToolError(f"{full_path.name} changes how tests run and may not be written. "
                        "Make the tests pass by fixing the code.")
    if name in AUTO_IMPORTED_FILES:
        raise ToolError(f"{full_path.name} is imported automatically by Python and may not be "
                        "written. Make the tests pass by fixing the code.")
    if name in VERSION_CONTROL_FILES:
        raise ToolError(
            f"'{full_path.name}' decides what gets committed and reviewed, so it is managed by "
            "the company, not by agents. Writing it could hide your work from the commit.")
    if name in CREDENTIAL_FILES:
        raise ToolError(
            f"'{full_path.name}' holds configuration or secrets and may not be written. "
            "If a test needs a value from such a file, the test must create it itself "
            "(e.g. with pytest's tmp_path), so it is clear the value is a fixture and not real.")
    stem = full_path.stem.lower()
    if stem in STDLIB_MODULE_NAMES or stem in TEST_RUNNER_MODULE_NAMES:
        kind = "standard-library" if stem in STDLIB_MODULE_NAMES else "test-runner"
        raise ToolError(
            f"'{full_path.name}' has the same name as the {kind} module "
            f"'{full_path.stem}', so it would be imported instead of the real one and "
            "could change what the test run reports. Choose another file name.")


def write_file(path: str, content: str) -> str:
    """Create or overwrite a file in the workspace (except QA's test files)."""
    full_path = safe_path(path)
    if full_path.name.startswith(QA_TEST_PREFIX):
        raise ToolError(f"{path} belongs to QA and cannot be changed. If its tests fail, fix your code.")
    # Markdown belongs to the Docs Writer (write_doc). The CEO reads the README
    # to decide whether to release, so whoever writes it is making a claim to
    # the decision-maker. One role owns that, and it is not this one.
    # Case study 004.
    if full_path.suffix == ".md":
        raise ToolError(
            f"'{path}' is documentation, which the Docs Writer owns. Write code and tests here; "
            "explain your work in your final summary instead.")
    _reject_protected_name(full_path, path)
    _reject_oversized(content, path)
    _reject_gutting(full_path, content, path)
    if full_path.suffix == ".py":
        _reject_qa_tampering(full_path, content)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


def write_qa_test(path: str, content: str) -> str:
    """QA only: create or overwrite one of QA's own test files (test_qa_*.py)."""
    full_path = safe_path(path)
    if not (full_path.name.startswith(QA_TEST_PREFIX) and full_path.suffix == ".py"):
        raise ToolError(f"QA may only write files named {QA_TEST_PREFIX}<something>.py, not '{path}'.")
    _reject_oversized(content, path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


def write_doc(path: str, content: str) -> str:
    """Docs Writer only: create or overwrite a markdown file (*.md)."""
    full_path = safe_path(path)
    if full_path.suffix != ".md":
        raise ToolError(f"You may only write markdown files (*.md), not '{path}'.")
    # A .md file cannot shadow a module, but README.md is not the only name a
    # docs agent might pick, and the rule costs nothing here.
    _reject_protected_name(full_path, path)
    _reject_oversized(content, path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


def run_tests() -> str:
    """Run pytest inside the workspace and return its output.

    This is the ONLY command an agent can run. We build the command ourselves,
    so the model cannot sneak in other shell commands. But the tests run the
    code the agents WROTE, so they run inside a sandbox (see sandbox.py):
    no network, no secrets, no writing outside the workspace.
    """
    workspace = WORKSPACE.resolve()
    try:
        with sandbox.Sandbox(workspace) as box:
            # An empty config file of our own: pytest must not read (or be
            # steered by) any pytest.ini / pyproject.toml outside the workspace.
            ini = box.temp_dir / "pytest.ini"
            ini.write_text("[pytest]\n")
            result = subprocess.run(
                # --noconftest: ignore any conftest.py, even one left over from an old run.
                box.wrap([sys.executable, "-m", "pytest", "-q", "--no-header", "--noconftest",
                          "-c", str(ini), "--rootdir", str(workspace)]),
                cwd=workspace,
                env=box.env,  # no API keys or other secrets from your shell
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired:
        raise ToolError(f"Tests took longer than {TEST_TIMEOUT_SECONDS}s and were stopped. Is there an infinite loop?")

    # pytest exit code 0 = all passed, 5 = no tests found. Tell the model plainly.
    output = result.stdout + result.stderr
    summary = output.strip().splitlines()[-1] if output.strip() else ""
    if result.returncode != 0:
        status = f"FAILED (exit code {result.returncode})"
    elif any(word in summary for word in ("skipped", "xfailed", "deselected")):
        # A skipped test proves nothing. Skipping is how a failing test gets hidden.
        status = "FAILED (some tests were skipped or marked as expected failures; that is not allowed)"
    elif not _passed_count(output):
        # Exit code 0 but pytest never said "N passed". That is what an early
        # exit looks like: a shadowed stdlib module doing "raise SystemExit(0)"
        # produces exit code 0 and NO output, which used to read as PASSED.
        # A real green run always reports how many tests passed. Case study 003.
        status = ("FAILED (the run ended without reporting any passing tests. "
                  "Either there are no tests, or something ended the run early)")
    else:
        status = "PASSED"
    return _truncate_middle(f"Tests {status}\n\n{output}")


# "12 passed in 0.03s", "1 passed, 2 warnings in 0.1s"
_PASSED_RE = re.compile(r"\b(\d+) passed\b")


def _passed_count(output: str) -> int:
    """How many tests pytest says passed. 0 if it never said."""
    matches = _PASSED_RE.findall(output)
    return int(matches[-1]) if matches else 0


RUN_PYTHON_TIMEOUT_SECONDS = 30


def run_python(code: str) -> str:
    """Run a short Python script and return what it printed.

    For the PM: an LLM cannot know what random.Random(42).randint(1, 20)
    returns, so it used to guess (case study 002). Now it can compute it.
    The script runs in the sandbox, in an empty scratch folder that is thrown
    away afterwards: it cannot see or change the workspace, has no network and
    no secrets.
    """
    with tempfile.TemporaryDirectory(prefix="agent-python-") as scratch:
        scratch = Path(scratch).resolve()
        (scratch / "script.py").write_text(code, encoding="utf-8")
        try:
            with sandbox.Sandbox(scratch) as box:
                result = subprocess.run(
                    box.wrap([sys.executable, "-I", "script.py"]),  # -I: ignore PYTHON* env vars and user site
                    cwd=scratch, env=box.env, capture_output=True, text=True,
                    timeout=RUN_PYTHON_TIMEOUT_SECONDS,
                )
        except subprocess.TimeoutExpired:
            raise ToolError(f"The script took longer than {RUN_PYTHON_TIMEOUT_SECONDS}s and was stopped.")
    output = result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
    return _truncate(f"exit code {result.returncode}\n{output}" if output.strip()
                     else f"exit code {result.returncode} (nothing printed: use print())")


# ---------------------------------------------------------------------------
# Tool descriptions for the model.
# The model never sees our Python code, ONLY these descriptions. Clear
# descriptions = the model uses the tools correctly. Treat them like docs.
# "input_schema" is JSON Schema: it says which arguments each tool takes.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List all files in the project workspace. Use this first to see what already exists.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_file",
        "description": "Read the full text of a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace, e.g. 'slugify.py'."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create a file or completely replace its content. Always write the WHOLE file, not a fragment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace, e.g. 'test_slugify.py'."},
                "content": {"type": "string", "description": "The complete new content of the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "write_qa_test",
        "description": (
            "Write your own pytest file to check the Developer's work. The name must start with "
            "'test_qa_' (e.g. 'test_qa_calculator.py'). The Developer cannot change these files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "e.g. 'test_qa_calculator.py'"},
                "content": {"type": "string", "description": "The complete pytest file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run all pytest tests in the workspace and return the output. Use after writing code to check it works.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

# Only the Docs Writer is offered this one, so it is not in TOOL_SCHEMAS
# (the Developer and QA get "everything in TOOL_SCHEMAS except ...").
WRITE_DOC_SCHEMA = {
    "name": "write_doc",
    "description": "Create a markdown file (*.md) or completely replace its content, e.g. 'README.md'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "e.g. 'README.md' or 'docs/usage.md'"},
            "content": {"type": "string", "description": "The complete markdown text."},
        },
        "required": ["path", "content"],
    },
}

# Only the PM is offered this one (to compute exact examples instead of guessing).
RUN_PYTHON_SCHEMA = {
    "name": "run_python",
    "description": (
        "Run a short Python script (standard library only) and get what it prints. "
        "Use print() to see results. Use it to compute exact expected outputs, e.g. "
        "what random.Random(42).randint(1, 20) returns. No network, no files from the project."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "The Python script, e.g. "
                                "\"import random\\nprint(random.Random(42).randint(1, 20))\""}},
        "required": ["code"],
    },
}

# Maps a tool name (as the model says it) to the Python function that does the work.
TOOL_FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "write_qa_test": write_qa_test,
    "write_doc": write_doc,
    "run_python": run_python,
    "run_tests": run_tests,
}


def run_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Run one tool call from the model. Returns (result_text, is_error).

    We never let an exception crash the agent. Instead the error text goes back
    to the model as the tool result, and the model can try again differently.
    """
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Unknown tool: {name}", True
    try:
        return func(**tool_input), False
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        # Wrong or missing arguments, e.g. write_file without "content".
        return f"Bad arguments for {name}: {e}", True

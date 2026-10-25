"""
git_ops.py - version control for the product in ./workspace.

The DevOps agent decides WHAT to say (the commit message). This file decides
WHAT RUNS: a short, fixed list of git commands, built by our code. No agent
ever gets a shell, so no agent can run "git push --force" or "rm -rf".

Rules this file enforces:
  - git only runs inside ./workspace (the product gets its own repository,
    separate from the company's code).
  - Local commits and tags only. There is NO push function on purpose:
    publishing is a human decision, made outside this program.
  - Hooks are switched off, and agents cannot write to .git (see tools.safe_path),
    so nothing an agent wrote can run during a commit.
  - Commits use a fixed "DevOps" identity, never your personal git identity.
"""

import subprocess

import tools

AUTHOR = ["-c", "user.name=agent-company DevOps", "-c", "user.email=devops@agent-company.invalid"]
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]

# Written by our code when the repository is created. Agents may still edit it.
GITIGNORE = "__pycache__/\n.pytest_cache/\n*.pyc\n.env\n"

MAX_MESSAGE_CHARS = 2_000


class GitError(Exception):
    """A git command failed. The message includes git's own error text."""


def _git(*args) -> str:
    """Run one git command in the workspace. args is a list, never a shell string,
    so a commit message like '"; rm -rf ~' is just text, not a command."""
    result = subprocess.run(
        ["git", *NO_HOOKS, *AUTHOR, *args],
        cwd=tools.WORKSPACE.resolve(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def ensure_repo():
    """Create the product's repository the first time we need it."""
    workspace = tools.WORKSPACE.resolve()
    if (workspace / ".git").exists():
        return
    workspace.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main")
    if not (workspace / ".gitignore").exists():
        (workspace / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def hidden_source_files() -> list:
    """Source files that exist on disk but git is told to ignore.

    The outcome we care about is "everything the agents wrote is in the commit
    the human reviews". Blocking .gitignore (tools.VERSION_CONTROL_FILES) closes
    the route we found; this checks the result, so a route we have not thought of
    still cannot leave code out of history. Case study 004.

    __pycache__ and friends are ignored on purpose, so only real source counts.
    """
    ensure_repo()
    # --others: untracked; --ignored: and the ignored ones; so: ignored + untracked.
    out = _git("ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    allowed_suffixes = {".pyc", ".pyo", ".so"}
    hidden = []
    for name in out.split("\0"):
        name = name.strip()
        if not name:
            continue
        if name.endswith("/") or "__pycache__" in name or ".pytest_cache" in name:
            continue
        if any(name.endswith(s) for s in allowed_suffixes):
            continue
        if name.endswith((".py", ".md", ".txt", ".json", ".cfg", ".toml", ".ini")):
            hidden.append(name)
    return sorted(hidden)


def stage_all() -> str:
    """Stage every change and return a short summary of what changed ("" = nothing).

    Raises GitError if source files are being hidden from the commit, because a
    commit that leaves code out is worse than no commit: it makes the history
    lie to whoever reviews it.
    """
    ensure_repo()
    hidden = hidden_source_files()
    if hidden:
        raise GitError(
            "these files exist in the workspace but .gitignore keeps them out of the commit, "
            f"so a reviewer would never see them: {', '.join(hidden[:10])}"
            f"{' ...' if len(hidden) > 10 else ''}. "
            "Restore .gitignore before committing.")
    _git("add", "-A")
    return _git("diff", "--cached", "--stat").strip()


def staged_diff(limit=6_000) -> str:
    """The staged changes as a diff, cut short so it fits in a prompt."""
    diff = _git("diff", "--cached")
    return diff if len(diff) <= limit else diff[:limit] + "\n... [diff cut]"


def commit(message: str) -> str:
    """Commit what is staged. Returns the new commit's short hash."""
    message = message.strip()[:MAX_MESSAGE_CHARS]
    if not message:
        raise GitError("empty commit message")
    _git("commit", "-q", "-m", message)
    return _git("rev-parse", "--short", "HEAD").strip()


def next_version() -> str:
    """v0.1.0 for the first release, then v0.2.0, v0.3.0, ..."""
    existing = [t for t in _git("tag", "--list", "v*").split() if t]
    return f"v0.{len(existing) + 1}.0"


def tag(name: str, message: str):
    """Mark the current commit as a release (an annotated, local tag)."""
    _git("tag", "-a", name, "-m", message.strip()[:MAX_MESSAGE_CHARS] or name)


def log(count=20) -> str:
    """Recent history, one line per commit (for the release summary)."""
    try:
        return _git("log", f"-{count}", "--oneline", "--decorate").strip()
    except GitError:
        return "(no commits yet)"  # a brand-new repository has no history

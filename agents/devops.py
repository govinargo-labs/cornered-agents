"""
agents/devops.py - the DevOps role.

The DevOps agent writes commit messages. That's all it decides.
Running git is done by OUR code (git_ops.py), with a fixed list of commands:
  - the agent gets NO tools except submit_commit: it cannot run anything;
  - our code stages the changes, shows the agent the diff, and commits with
    the agent's message;
  - if the agent fails, our code commits with a plain message instead, so a
    flaky model never blocks the history.

This split is the pattern for any risky action: the LLM proposes the content,
deterministic code performs the action, and a human approves what leaves the
machine (there is no push at all).
"""

from agents.base import Agent

SUBMIT_COMMIT = {
    "name": "submit_commit",
    "description": "Submit the commit message for the staged changes. Call this exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Subject line (max 72 characters), a blank line, then a short body.",
            },
        },
        "required": ["message"],
    },
}

DEVOPS_PROMPT = """\
You are the DevOps engineer at a small open-source software company.
Approved work is committed to git. You write the commit message.

You get the work item and the staged diff. Write a message in the
Conventional Commits style:
- Subject: "<type>: <what changed>", max 72 characters, imperative mood
  ("add", not "added"). Types: feat, fix, test, docs, refactor, chore.
- A blank line, then 1-5 short bullet points: what changed and why.
- Describe ONLY what is in the diff. No guesses, no marketing.

Call submit_commit with the message. Do not reply with text only.
"""


def make_devops(**kwargs) -> Agent:
    """Create a DevOps agent. kwargs (e.g. client=) go to Agent."""
    kwargs.setdefault("max_steps", 3)
    # force_finish: seen live, the model wrote a perfect message as plain TEXT
    # and stopped, and needed a reminder before every commit. Forcing the tool
    # call saves that extra request.
    return Agent("DevOps", DEVOPS_PROMPT, tool_schemas=[SUBMIT_COMMIT],
                 finish_tool="submit_commit", force_finish=True, effort="low", **kwargs)


def commit_request(work: str, stat: str, diff: str) -> str:
    """The message the DevOps agent receives."""
    return (
        f"Work item that was approved:\n{work}\n\n"
        f"Files changed:\n{stat}\n\n"
        f"Staged diff:\n{diff}"
    )

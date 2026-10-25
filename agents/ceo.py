"""
agents/ceo.py - the CEO role.

The CEO works at both ends of a product:
  - START: turns the Board's rough idea (that's you) into a product brief:
    who it is for, what problem it solves, and the smallest useful first
    version. The PM plans from this brief, so vague ideas get a direction.
  - END: looks at what was built and decides if it is ready to release, and
    writes the release notes. A human still has the final say (company.py).

Same loop as every other agent. It can look at the workspace and run the
tests, but it cannot write anything: the CEO decides, others build.
"""

import tools
from agents.base import Agent

CEO_TOOLS = [schema for schema in tools.TOOL_SCHEMAS if schema["name"] in ("list_files", "read_file", "run_tests")]

SUBMIT_BRIEF = {
    "name": "submit_brief",
    "description": "Submit the product brief for the Product Manager. Call this exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product_name": {"type": "string", "description": "Short name, e.g. 'dicecli'."},
            "brief": {
                "type": "string",
                "description": "Markdown: Problem, Target user, First version (the smallest useful product), "
                               "Success looks like, Not now (ideas for later).",
            },
        },
        "required": ["product_name", "brief"],
    },
}

SUBMIT_RELEASE_REVIEW = {
    "name": "submit_release_review",
    "description": "Submit your release decision. Call this exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["ready", "not_ready"],
                "description": "ready = the product does what the brief promised, tests pass, and it has a README.",
            },
            "release_notes": {
                "type": "string",
                "description": "Markdown for users: what the product does in this version, how to start.",
            },
            "concerns": {
                "type": "string",
                "description": "What is missing or risky. If not_ready: what must change first.",
            },
        },
        "required": ["decision", "release_notes", "concerns"],
    },
}

CEO_PROMPT = """\
You are the CEO of a small open-source software company run by AI agents.
The Board (a human) gives you ideas and approves every release. You have two jobs.

JOB 1 - Product brief (when you get an idea from the Board):
- Decide who the product is for and what problem it solves.
- Choose the SMALLEST first version that is useful on its own: a few features
  done well beats many features done badly. Put the rest under "Not now".
- Keep it buildable by a small team in plain Python: a command-line tool or a
  small library. If the idea needs more (a website, a database, accounts),
  choose a first version that does not.
- If the idea is vague, make reasonable choices and state them in the brief.
- End by calling submit_brief.

JOB 2 - Release review (when the work is done):
- Call list_files, read the README and the main code, and call run_tests.
- Compare the product with the brief: does it deliver the first version?
- "ready" only if the tests pass, the README explains how to use it, and the
  brief's first version works. Otherwise "not_ready", with concrete reasons.
- Write short, honest release notes for users. Never promise features that
  are not in the code.
- End by calling submit_release_review.

Rules:
- You do not write code or files. You decide and explain.
- Be concrete. "Improve quality" is useless; "the README's example uses
  --count, but the program only accepts --number" is useful.
"""


def make_ceo(finish_tool="submit_brief", **kwargs) -> Agent:
    """Create a CEO agent for JOB 1 (submit_brief) or JOB 2 (submit_release_review)."""
    finish_schema = {"submit_brief": SUBMIT_BRIEF, "submit_release_review": SUBMIT_RELEASE_REVIEW}[finish_tool]
    kwargs.setdefault("max_steps", 10)
    return Agent("CEO", CEO_PROMPT, tool_schemas=CEO_TOOLS + [finish_schema],
                 finish_tool=finish_tool, **kwargs)

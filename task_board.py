"""
task_board.py - the company's shared task board, saved as tasks.json.

The Product Manager plans the work; this file stores that plan and tracks
progress, so that:
  - everyone (agents, our code, YOU) can see what is planned and what is done;
  - a run can stop (crash, Ctrl+C, a task needing a human) and later RESUME
    where it left off, because the state lives in a file, not in memory.

Agents never edit tasks.json themselves. The PM hands its plan to our code
(via the submit_plan tool) and our code checks it and writes the file.

tasks.json looks like this:
{
  "idea": "a word counter CLI",
  "spec": "## Goal ... ## Scope ...",
  "tasks": [
    {"id": 1, "title": "Count words", "description": "...",
     "examples": ["count_words('a b') -> 2", ...],
     "status": "todo", "attempts": 0, "feedback": ""}
  ]
}
status is one of: "todo", "done", "needs_human".
"""

import json
import re
from pathlib import Path

BOARD_PATH = Path(__file__).parent / "tasks.json"

MAX_TASKS = 8  # a small product; more than this means the PM over-planned

# Words that make an example a GUESS instead of a requirement. "f(42) -> e.g.
# [3, 5]" was read by the Developer as "must return [3, 5]", and it spent 80
# steps trying. An example must be exact, or a property ("5 ints, each 1-6").
HEDGES = re.compile(r"\be\.g\.|\bfor example\b|\bsomething like\b|\bdepends on\b|\bfor instance\b",
                    re.IGNORECASE)


class PlanError(Exception):
    """The PM's plan is unusable. We say exactly why, so a human (or the PM) can fix it."""


def new_board(idea: str, plan: dict) -> dict:
    """Check the PM's plan and turn it into a fresh board. Never trust LLM output blindly."""
    spec = plan.get("spec", "")
    tasks = plan.get("tasks")
    if not isinstance(spec, str) or not spec.strip():
        raise PlanError("The plan has no spec.")
    # Small models often send a list as a string of JSON text: '[{"title": ...}]'.
    # The content is fine, only the packaging is wrong, so unpack it ourselves.
    if isinstance(tasks, str):
        text = tasks.strip()
        try:
            # raw_decode reads ONE JSON value and tells us where it ended. Seen
            # live: '[...]}' with one extra closing brace (the model also closed
            # its outer object). Leftover closing brackets carry no content, so
            # we accept them; anything else after the array is an error.
            tasks, end = json.JSONDecoder().raw_decode(text)
            if text[end:].strip(" \n]}"):
                raise json.JSONDecodeError("unexpected text after the array", text, end)
        except json.JSONDecodeError as e:
            raise PlanError(f"'tasks' must be a JSON array of task objects. You sent a string "
                            f"that is not valid JSON ({e}). Send the array itself, not text.")
    if not isinstance(tasks, list):
        # Say exactly what is wrong: "no tasks" once made the PM resend the same
        # broken plan 3 times, because it could see that it HAD tasks.
        raise PlanError(f"'tasks' must be a JSON array of task objects, but you sent a "
                        f"{type(tasks).__name__}. Send the array itself.")
    if not tasks:
        raise PlanError("The plan has no tasks: 'tasks' is an empty array.")
    if len(tasks) > MAX_TASKS:
        raise PlanError(f"The plan has {len(tasks)} tasks; the limit is {MAX_TASKS}.")

    board_tasks = []
    for number, task in enumerate(tasks, start=1):
        if not isinstance(task, dict) or not task.get("title") or not task.get("description"):
            raise PlanError(f"Task {number} needs a title and a description.")
        examples = task.get("examples") or []
        if not isinstance(examples, list):
            raise PlanError(f"Task {number}: examples must be a list.")
        for example in examples:
            if HEDGES.search(str(example)):
                raise PlanError(
                    f"Task {number}, example \"{example}\" is a guess, not a requirement. "
                    "Write an exact expected result, or, if you cannot know it (random numbers, "
                    "times), a property that can be checked, e.g. \"-> 5 ints, each between 1 and 6\".")
        board_tasks.append({
            "id": number,  # we number tasks ourselves, in order
            "title": str(task["title"]),
            "description": str(task["description"]),
            # dict.fromkeys drops repeated examples but keeps their order.
            "examples": list(dict.fromkeys(str(e) for e in examples)),
            "status": "todo",
            "attempts": 0,
            "feedback": "",
        })
    return {"idea": idea, "spec": spec, "tasks": board_tasks}


def save(board: dict, path: Path = None):
    path = path or BOARD_PATH
    path.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load(path: Path = None) -> dict:
    path = path or BOARD_PATH
    if not path.exists():
        raise SystemExit(f"No task board at {path}. Plan a product first.")
    return json.loads(path.read_text(encoding="utf-8"))


def next_todo(board: dict):
    """The first task that still has to be built, or None when everything is done."""
    return next((t for t in board["tasks"] if t["status"] == "todo"), None)


def task_prompt(board: dict, task: dict) -> str:
    """What the Developer and QA see for one task: the spec + this task + its examples."""
    examples = "\n".join(f"- {e}" for e in task["examples"]) or "- (none given)"
    return (
        f"PROJECT SPEC (the whole product, for context):\n{board['spec']}\n\n"
        f"YOUR TASK ({task['id']} of {len(board['tasks'])}): {task['title']}\n"
        f"{task['description']}\n\n"
        f"Acceptance examples (every one must work, and must be covered by a test):\n{examples}\n\n"
        "Earlier tasks are already built and their code is in the workspace. "
        "Build on it, and do not break their tests."
    )

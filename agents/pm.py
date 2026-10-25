"""
agents/pm.py - the Product Manager role.

The PM fixes the problem we kept hitting in Phase 2: vague tasks.
"make a calculator" let the Developer invent the behavior, and nobody had
written down that "2 3" must be an error. The PM turns an idea into:
  - a short SPEC (goal, scope, how the program is used), and
  - a list of small TASKS, each with concrete EXAMPLES, including invalid input.

The PM does not write code or files. It can look at the workspace (to see what
exists already), run small Python scripts in the sandbox to COMPUTE exact
examples (it used to guess them, see case study 002), and ends by calling
submit_plan. Our code then checks the plan
and saves it to tasks.json (see task_board.py).
"""

import tools
from agents.base import Agent

# Read-only tools, plus run_python to compute examples: the PM looks, it does not build.
PM_TOOLS = [schema for schema in tools.TOOL_SCHEMAS if schema["name"] in ("list_files", "read_file")]
PM_TOOLS = PM_TOOLS + [tools.RUN_PYTHON_SCHEMA]

SUBMIT_PLAN = {
    "name": "submit_plan",
    "description": "Submit the product spec and the ordered task list. Call this exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "spec": {
                "type": "string",
                "description": "Markdown spec: Goal, Scope (in / out), Usage (exact command-line examples), Error handling.",
            },
            "tasks": {
                "type": "array",
                "description": "Small tasks in build order. Each one is built and reviewed on its own.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short, e.g. 'Count words in a string'."},
                        "description": {
                            "type": "string",
                            "description": "What to build: file names, function names and signatures, behavior.",
                        },
                        "examples": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Concrete input -> expected output pairs, e.g. \"count_words('a  b') -> 2\".",
                        },
                    },
                    "required": ["title", "description", "examples"],
                },
            },
        },
        "required": ["spec", "tasks"],
    },
}

PM_PROMPT = """\
You are the Product Manager at a small open-source software company.
The Board gives you a product idea. You turn it into a clear plan that a
Developer can build and a QA Reviewer can check, without guessing.

How you work:
1. Call list_files to see if anything is already built.
2. Decide the SMALLEST useful version of the product. Leave nice-to-haves out
   and list them under "Out of scope".
3. Write the spec (markdown): Goal, Scope (in / out), Usage with exact
   command-line examples, and Error handling (what happens on bad input).
4. Split the work into 2-5 small tasks, in build order. Each task:
   - names the exact file(s) and function signature(s);
   - can be built and tested on its own, using only earlier tasks;
   - has 4-8 examples written as "input -> expected output". Include normal
     cases, edge cases (empty input, unusual characters) and INVALID input with
     the exact error (e.g. "parse('') -> raises ValueError").
   - The last task is the command-line interface (the `if __name__ == "__main__":` part).
5. Call submit_plan.

Rules:
- Python standard library only. Keep it to one or two modules.
- Every behavior that matters must be pinned down by an example. If you leave
  something open, the Developer will guess, and QA cannot check it.
- NEVER guess an exact output. If an example depends on random numbers with
  a seed, hashes, rounding or anything you cannot work out by hand, COMPUTE it
  with run_python using the same standard-library call the Developer should
  use, and name that call in the task description, e.g. "use
  random.Random(seed).randint(1, sides)". Compute EVERY such example on its
  own (a different seed, count or range gives different numbers) and copy the
  printed result exactly into the example.
- If you cannot compute it (no seed, the current time), describe a property
  instead: "roll_dice(5, 20) -> 5 ints, each between 1 and 20".
- Every example must be checkable and different from the others. Never hedge:
  "e.g. [3, 5]", "something like ..." and "depends on ..." are guesses, and
  our checks reject them.
- Do not write code. Describe WHAT to build, not HOW.
- Do not write the plan as a message. Put it ONLY in the submit_plan call:
  writing "I will now submit" is not submitting.
"""


def make_pm(**kwargs) -> Agent:
    """Create a Product Manager agent. kwargs (e.g. client=) go to Agent."""
    kwargs.setdefault("max_steps", 12)  # room for a few run_python calls
    return Agent("PM", PM_PROMPT, tool_schemas=PM_TOOLS + [SUBMIT_PLAN],
                 finish_tool="submit_plan", **kwargs)

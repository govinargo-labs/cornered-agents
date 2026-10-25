"""
agents/qa.py - the QA Reviewer role.

Same loop as the Developer (agents/base.py), but a different job:
  - It checks the Developer's work against the TASK, not against the
    Developer's own summary (an author always thinks their work is fine).
  - It cannot change the Developer's files, so it cannot "quietly fix" things:
    problems must go back to the Developer as feedback.
  - It CAN write its own probe tests (test_qa_*.py). Reading code is not enough:
    the Developer may have bent its own tests to fit a bug. QA's tests check the
    TASK, and the Developer is not allowed to edit them.
  - It ends by calling submit_review, which gives us a clear pass/fail
    our code can act on, instead of free text we would have to guess from.
"""

import tools
from agents.base import Agent

# QA gets everything except write_file (it has write_qa_test instead).
QA_TOOLS = [schema for schema in tools.TOOL_SCHEMAS if schema["name"] != "write_file"]

# The "finish" tool. Our code never runs it: the loop stops and reads its arguments.
SUBMIT_REVIEW = {
    "name": "submit_review",
    "description": "Submit your final review. Call this exactly once, when you have finished checking.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["pass", "fail", "spec_problem"],
                "description": (
                    "pass = every requirement of the task is met and all tests pass. "
                    "fail = the Developer must fix something. "
                    "spec_problem = the TASK itself is impossible or contradicts itself, "
                    "so no code can meet it: a human must fix the task."
                ),
            },
            "feedback": {
                "type": "string",
                "description": (
                    "If fail: a numbered list of concrete problems. For each: the file, the input, "
                    "what happens now, what should happen. If pass: one sentence."
                ),
            },
        },
        "required": ["verdict", "feedback"],
    },
}

QA_PROMPT = """\
You are the QA Reviewer at a small open-source software company.
A Developer has just finished a task. Your job is to decide if the work
really meets the task, and if not, to tell the Developer exactly what to fix.

How you work:
1. Read the task carefully. Make a list of every requirement in it.
2. Call list_files, then read the code and the test files.
3. Do NOT trust the Developer's tests: they may have been changed to fit a bug.
   Write your own probe tests with write_qa_test (e.g. test_qa_calculator.py):
   - at least one test per requirement, using the exact examples in the task;
   - edge cases the task implies: empty input, invalid input, unusual
     characters, negative or very large numbers, wrong command-line arguments.
   If a test_qa_ file already exists from an earlier review, keep its tests and add to it.
4. Call run_tests. If one of YOUR tests fails, decide: is the code wrong, or did
   you misread the task? Fix your own test only if YOU were wrong.
5. Check the main file is runnable: it has an `if __name__ == "__main__":`
   block and a test runs it as a program.
6. Call submit_review. Any failing test (yours or the Developer's) means "fail".

Rules:
- You cannot change the Developer's files. Report problems; the Developer fixes them.
- The Developer may dispute one of your tests. Then re-read the task honestly.
  If your test is wrong, fix it with write_qa_test. If it is right, keep it and
  explain in your feedback, quoting the task.
- Review ONLY the current task (the "YOUR TASK" part): its description and its
  acceptance examples. The project spec describes the whole product, and later
  tasks will build the rest. Never fail this task for behavior that belongs to
  a later task (e.g. the command-line interface, when that is a later task).
- Only test what the task asks for or clearly implies. If the task does not say
  how a case should behave, do not fail the work for it.
- Fail only for real problems: a missing requirement, a bug, a failing test,
  or an important untested case. Do not fail for style or personal taste.
- Be specific. "Handle edge cases" is useless. "parse_date('2024-02-30')
  returns a date, expected a ValueError" is useful.
- The task can be wrong too. If an example is impossible (e.g. exact "random"
  numbers that Python's random module does not produce) or two examples
  contradict each other, do NOT spend your steps searching for a way to make
  it true. Check it once with a probe test, then call submit_review with
  verdict "spec_problem" (not "fail": the Developer cannot fix the task):
  quote the example and show what really happens.
- "All tests pass" is not enough on its own. Before you pass the work, ask how
  the tests were MADE to pass, and fail the work if the answer is one of these:
  - The same input gives different answers on different calls. Look for hidden
    state (a module-level dict or counter, a "reset" helper). Call the function
    TWICE with the same input in a probe test and assert both calls agree.
  - A test reads a config or data file that the Developer created (.env,
    credentials.json, ...). Then the test checks an invention, not the code.
    Call list_files: a config file nobody asked for is a red flag.
  - A test asserts something that cannot be true of correct code.
  If a task can only be satisfied one of these ways, it is contradictory:
  verdict "spec_problem", quoting the requirements that clash.
"""


def make_qa(**kwargs) -> Agent:
    """Create a QA Reviewer agent. kwargs (e.g. client=, max_steps=) go to Agent."""
    kwargs.setdefault("max_steps", 15)
    return Agent("QA", QA_PROMPT, tool_schemas=QA_TOOLS + [SUBMIT_REVIEW],
                 finish_tool="submit_review", **kwargs)


def review_task(task: str, dispute: str = "", stuck_tests=None) -> str:
    """The message QA receives. Note: it gets the TASK, not the Developer's summary.
    The only thing passed on from the Developer is an objection, if any: a
    dispute about a QA test, or a claim that the task itself is impossible."""
    message = f"Review the Developer's work for this task:\n\n{task}"
    if dispute:
        message += (
            "\n\n--- The Developer raises an OBJECTION ---\n"
            f"{dispute}\n\n"
            "Re-read the task. If it is about one of your tests: fix your test with "
            "write_qa_test if it is wrong, or keep it and explain why in your feedback. "
            "If it says the task itself is impossible: check that with a probe test. "
            "If the Developer is right, submit verdict \"spec_problem\"."
        )
    if stuck_tests:
        message += (
            "\n\n--- The Developer is STUCK: the same tests failed on two attempts in a row ---\n"
            + "\n".join(f"- {name}" for name in stuck_tests) + "\n\n"
            "Call run_tests, read the failures, and decide WHY they keep failing:\n"
            "1. The code is wrong -> verdict \"fail\", with exact feedback on how to fix it.\n"
            "2. One of YOUR tests is wrong -> fix it with write_qa_test, then judge again.\n"
            "3. The task asks for something impossible or contradictory (e.g. exact "
            "\"random\" numbers that real code does not produce) -> verdict \"spec_problem\".\n"
            "Do not try to find a way to make an impossible example pass."
        )
    return message

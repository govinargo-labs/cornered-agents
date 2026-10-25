"""
show_log.py - read a run log (logs/*.jsonl) as a readable story.

    venv/bin/python show_log.py                    # the newest log
    venv/bin/python show_log.py logs/run-...jsonl  # a specific log
    venv/bin/python show_log.py --agent QA         # only one agent
    venv/bin/python show_log.py --thinking         # also show the model's reasoning

Nothing is shortened here: you see the full code the agents wrote and the
full test output they saw. That is the point of keeping a log.
"""

import argparse
import json
from pathlib import Path

from run_log import LOG_DIR


def show_event(e, show_thinking):
    who = f"{e['time']} [{e['agent']}]"
    kind = e["event"]

    if kind == "run_start":
        print(f"{who} RUN START\n{e['task']}\n")
    elif kind == "attempt_start":
        print(f"\n{'=' * 20} ATTEMPT {e['attempt']} {'=' * 20}")
        if e.get("feedback"):
            print(f"Feedback given to the Developer:\n{e['feedback']}\n")
    elif kind == "agent_start":
        print(f"\n{who} START  model={e['model']}  tools={', '.join(e['tools'])}")
    elif kind == "model_reply":
        print(f"\n{who} step {e['step']}  (stop_reason={e['stop_reason']}, "
              f"{e['input_tokens']} tokens in, {e.get('cache_read_tokens', 0)} from cache, "
              f"{e['output_tokens']} out)")
        for block in e["content"]:
            if block["type"] == "thinking" and show_thinking:
                print(f"  THINKING: {block.get('thinking', '')}")
            elif block["type"] == "text":
                print(f"  SAYS: {block['text']}")
            elif block["type"] == "tool_use":
                print(f"  CALLS {block['name']}:")
                for key, value in block["input"].items():
                    print(_indent(f"{key} = {value}"))
    elif kind == "tool_result":
        label = "TOOL ERROR" if e["is_error"] else "TOOL RESULT"
        print(f"{who} {label} ({e['name']}):")
        print(_indent(e["output"]))
    elif kind == "agent_end":
        print(f"{who} END  stop={e['stop_reason']}  steps={e['steps']}  "
              f"tokens={e['input_tokens']} in / {e['output_tokens']} out")
        if e.get("output"):
            print(f"  OUTPUT: {json.dumps(e['output'], indent=2)}")
    elif kind == "nudge":
        print(f"\n{who} REMINDER {e['count']}: the agent stopped without calling its finish tool")
    elif kind == "last_step_warning":
        print(f"\n{who} LAST STEP: told the agent to hand in its answer now")
    elif kind == "dispute":
        print(f"\n{who} DEVELOPER OBJECTS (QA test or spec):\n{_indent(e['dispute'])}")
    elif kind == "api_error":
        print(f"\n{who} API ERROR at step {e['step']}: {e['error']}")
    elif kind == "test_check":
        print(f"\n{who} TEST CHECK:\n{_indent(e['output'])}")
    elif kind == "verdict":
        print(f"\n{who} QA VERDICT: {e['verdict'].upper()}\n{_indent(e['feedback'])}")
    elif kind == "plan_saved":
        board = e["board"]
        print(f"\n{who} PLAN SAVED: {len(board['tasks'])} tasks\n{_indent(board['spec'])}")
        for t in board["tasks"]:
            print(f"    {t['id']}. {t['title']}")
            for example in t["examples"]:
                print(f"         {example}")
    elif kind == "plan_rejected":
        print(f"\n{who} PLAN REJECTED (attempt {e['attempt']}), sent back to the PM:\n{_indent(e['error'])}")
    elif kind == "stuck":
        print(f"\n{who} STUCK: the same tests failed twice -> QA arbitrates\n{_indent(chr(10).join(e['failing']))}")
    elif kind == "plan_error":
        print(f"\n{who} PLAN ERROR: {e['error']}")
    elif kind == "task_start":
        t = e["task"]
        print(f"\n{'#' * 20} TASK {t['id']}: {t['title']} {'#' * 20}")
    elif kind == "task_end":
        t = e["task"]
        print(f"\n{who} TASK {t['id']} -> {t['status'].upper()} after {t['attempts']} attempt(s)")
    elif kind == "brief":
        print(f"\n{who} CEO BRIEF for '{e['product_name']}':\n{_indent(e['brief'])}")
    elif kind == "commit":
        print(f"\n{who} GIT COMMIT {e['commit']}:\n{_indent(e['message'])}\n{_indent(e['stat'])}")
    elif kind == "release_review":
        print(f"\n{who} CEO RELEASE REVIEW: {e['decision'].upper()}\n"
              f"  concerns: {e['concerns'] or '(none)'}\n{_indent(e['release_notes'])}")
    elif kind == "approval":
        print(f"\n{who} HUMAN APPROVAL for {e['version']}: {'YES' if e['approved'] else 'NO'}")
    elif kind == "run_end":
        print(f"\n{who} RUN END: {'OK' if e.get('ok') else 'NOT OK'}")


def _indent(text, prefix="    | "):
    return "\n".join(prefix + line for line in str(text).splitlines())


def main():
    parser = argparse.ArgumentParser(description="Show an agent run log.")
    parser.add_argument("path", nargs="?", help="log file (default: the newest in logs/)")
    parser.add_argument("--agent", help="only show events from this agent, e.g. QA")
    parser.add_argument("--thinking", action="store_true", help="also show the model's reasoning")
    args = parser.parse_args()

    if args.path:
        path = Path(args.path)
    else:
        logs = sorted(LOG_DIR.glob("run-*.jsonl"))
        if not logs:
            raise SystemExit("No logs yet. Run company.py or agents.developer first.")
        path = logs[-1]
    print(f"Log: {path}\n")

    with open(path, encoding="utf-8") as f:
        for line in f:
            event = json.loads(line)
            if args.agent and event["agent"] != args.agent:
                continue
            show_event(event, args.thinking)


if __name__ == "__main__":
    main()

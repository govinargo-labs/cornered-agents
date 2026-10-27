"""
company.py - the workflow that connects our agents.

Phase 4 flow, the whole company (`company.py run "idea"`):

    idea ─► CEO: brief ─► PM: spec + tasks ─► for each task: Developer <-> QA ─► DevOps: commit
                                (tasks.json)
         ─► Docs: README ─► DevOps: commit ─► CEO: release review ─► HUMAN: yes? ─► git tag

    Nothing leaves your machine: commits and tags are local, there is no push.

Phase 2 flow for ONE task (build_task below):

    Developer builds ──► our code runs the tests ──► QA reviews
          ▲                    │ fail                  │ fail
          └──── feedback ◄─────┴───────────────────────┘
                                                       │ pass
                                                       ▼
                                                   approved

Important ideas:
  - The agents never talk to each other directly. THIS file decides who works
    next and what they see. That keeps the flow simple, visible and testable.
  - Every Developer attempt starts a FRESH conversation. Its "memory" of earlier
    attempts is the code in ./workspace plus the QA feedback we pass along.
  - Cheap checks first: we run the tests ourselves (free, instant) before asking
    QA (an LLM run, slow). If tests fail, QA is not needed to say so.
  - Retry limit: after MAX_RETRIES rejected attempts we stop and ask a human.
  - The task board (tasks.json) is saved after every task, so a stopped run
    can be resumed. State lives in files, not in memory.

Run it:
    venv/bin/python company.py plan "a word counter CLI"   # PM only: look at tasks.json first
    venv/bin/python company.py build                       # build the tasks that are still todo
    venv/bin/python company.py release                     # docs + CEO review + your approval
    venv/bin/python company.py run "a word counter CLI"    # everything, start to finish
    venv/bin/python company.py task "one clear task..."    # Phase 2: one task, no PM
"""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import git_ops
import run_log
import task_board
import tools
from agents.ceo import make_ceo
from agents.developer import make_developer
from agents.devops import commit_request, make_devops
from agents.docs import make_docs_writer
from agents.pm import make_pm
from agents.qa import make_qa, review_task

MAX_RETRIES = 3  # rejected attempts allowed after the first one (so at most 4 builds)
MAX_PLAN_FIXES = 2  # times the PM may resubmit a plan that our checks rejected

# Examples that mention these can't be worked out by hand: the PM must COMPUTE them.
NEEDS_COMPUTING = re.compile(r"\bseed\b|\brandom\b", re.IGNORECASE)


def called_tool(messages, name: str) -> bool:
    """Did the agent call this tool anywhere in its conversation?"""
    return any(getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name
               for m in messages if m["role"] == "assistant" for block in m["content"])


def check_computed(board: dict, computed: bool):
    """Seeded/random examples are only trusted if the PM ran run_python.
    Seen live: the PM skipped run_python and guessed [1], [4, 5, 3], [100];
    after computing, every value was right. A rule in the prompt was not enough,
    but OUR code can see which tools were called."""
    if computed:
        return
    seeded = [e for t in board["tasks"] for e in t["examples"] if NEEDS_COMPUTING.search(e)]
    if seeded:
        raise task_board.PlanError(
            f"Your plan has examples with seeds or random numbers (like \"{seeded[0]}\"), but you "
            "never called run_python, so their values are guesses. Compute each one with "
            "run_python and copy the printed results into the examples.")


def show(title: str, text: str):
    """Print one agent's OUTPUT in full, as a labelled block, so you can follow
    the run step by step. (The agents' own console lines are short previews;
    the log file has everything.)"""
    body = "\n".join(f"  │ {line}" for line in (text or "(empty)").strip().splitlines())
    print(f"\n  ┌─ {title}\n{body}\n  └─")


def format_plan(board: dict) -> str:
    """The PM's plan as readable text: the spec, then every task with its examples."""
    lines = ["SPEC:", board["spec"].strip(), "", f"TASKS ({len(board['tasks'])}):"]
    for t in board["tasks"]:
        lines += ["", f"{t['id']}. {t['title']}  [{t['status']}]"]
        lines += [f"   {line}" for line in t["description"].strip().splitlines()]
        lines += [f"   example: {e}" for e in t["examples"]] or ["   example: (none)"]
    return "\n".join(lines)


@dataclass
class TaskResult:
    status: str                  # "approved", "needs_human"
    attempts: int
    feedback: str = ""           # the last feedback, if not approved
    history: list = field(default_factory=list)  # one entry per attempt, for the log


def developer_task(task: str, feedback: str) -> str:
    """What the Developer sees. On a retry, the QA feedback is added to the task."""
    if not feedback:
        return task
    return (
        f"{task}\n\n"
        "--- REVIEW: your previous attempt was REJECTED ---\n"
        f"{feedback}\n\n"
        "Your previous code is already in the workspace. Read it, fix every problem "
        "listed above, and run the tests again."
    )


DISPUTE_MARKER = "QA TEST DISPUTE:"      # "QA's test is wrong"
SPEC_PROBLEM_MARKER = "SPEC PROBLEM:"     # "the task itself is impossible"


def _marker_pattern(marker: str):
    """The marker, tolerating the markdown a language model puts around it.

    Seen live (forged_signoff, 1 run in 10): the Developer wrote an honest
    objection as "**QA TEST DISPUTE**:" with the colon outside the bold. An exact
    substring match missed it, so the one honest exit the system offers was
    silently dropped. Bold, italics, backticks and capitalisation are not
    meaningful differences, so they are ignored; the words and the colon are.
    """
    words = marker.rstrip(":").split()
    return re.compile(r"[\s*_`]+".join(map(re.escape, words)) + r"[\s*_`]*:", re.IGNORECASE)


_DISPUTE_PATTERNS = [_marker_pattern(m) for m in (DISPUTE_MARKER, SPEC_PROBLEM_MARKER)]


def find_dispute(developer_summary: str) -> str:
    """The Developer's objection (from the first marker on), or "" if there is none.
    The marker is kept, so QA can see which kind of objection it is."""
    starts = [m.start() for p in _DISPUTE_PATTERNS if (m := p.search(developer_summary))]
    return developer_summary[min(starts):].strip() if starts else ""


def failing_tests(test_output: str) -> set:
    """The names of the failing tests, from pytest's summary lines, e.g.
    "FAILED test_dice.py::test_seed_42 - assert [6, 1] == [3, 5]" -> "test_dice.py::test_seed_42"."""
    return {line.split()[1] for line in test_output.splitlines()
            if line.startswith(("FAILED ", "ERROR ")) and len(line.split()) > 1}


def build_task(task: str, developer=None, qa=None, max_retries=MAX_RETRIES) -> TaskResult:
    """Run the Developer <-> QA loop for one task. Agents can be passed in for tests."""
    developer = developer or make_developer()
    qa = qa or make_qa()
    feedback = ""
    history = []
    last_failing = set()  # the failing tests of the previous attempt, for stuck detection

    for attempt in range(1, max_retries + 2):
        print(f"\n========== ATTEMPT {attempt}/{max_retries + 1} ==========")
        run_log.write("company", "attempt_start", attempt=attempt, feedback=feedback)

        # 1. Developer builds (or fixes).
        print(f"\n---------- DEVELOPER (attempt {attempt}) ----------")
        dev = developer.run(developer_task(task, feedback))
        show(f"DEVELOPER's summary (stopped: {dev.stop_reason}, {dev.steps} steps)", dev.final_text)
        entry = {"attempt": attempt, "developer": dev.stop_reason}
        history.append(entry)
        dispute = find_dispute(dev.final_text)
        if dispute:
            entry["dispute"] = True
            print("[company] the Developer objects (to a QA test or to the task) -> QA will look at it")
            run_log.write("company", "dispute", attempt=attempt, dispute=dispute)

        # 2. Cheap, reliable check in OUR code: do the tests pass?
        print("\n---------- TEST CHECK (our code, no LLM) ----------")
        test_output = tools.run_tests()
        entry["tests"] = test_output.splitlines()[0]
        print(f"[company] {entry['tests']}")
        for name in sorted(failing_tests(test_output)):
            print(f"[company]   failing: {name}")
        run_log.write("company", "test_check", attempt=attempt, output=test_output)
        # Stuck detection, done by OUR code: the SAME tests failed again. Another
        # retry would likely fail the same way (we once watched the Developer spend
        # 80 steps trying to match "random" numbers the PM had invented). Ask QA to
        # judge instead: bad code, a bad QA test, or an impossible task?
        failing = failing_tests(test_output)
        stuck = bool(failing) and failing == last_failing
        last_failing = failing
        if stuck:
            entry["stuck"] = True
            print(f"[company] the same tests failed twice -> QA decides why: {sorted(failing)}")
            run_log.write("company", "stuck", attempt=attempt, failing=sorted(failing))

        # Failing tests go straight back to the Developer, UNLESS the Developer says
        # a QA test is wrong, or it is stuck: then QA must look at it first.
        if not test_output.startswith("Tests PASSED") and not dispute and not stuck:
            feedback = f"The tests do not pass. Fix the code or the tests.\n\n{test_output}"
            entry["verdict"] = "fail (tests)"
            print("[company] tests fail -> back to the Developer (QA skipped)")
            continue

        # 3. QA reviews against the original task.
        print(f"\n---------- QA REVIEW (attempt {attempt}) ----------")
        review = qa.run(review_task(task, dispute, stuck_tests=sorted(failing) if stuck else None))
        if review.output is None:
            # QA never called submit_review: we have no verdict to act on.
            entry["verdict"] = "no verdict"
            return TaskResult("needs_human", attempt, "QA did not submit a review.", history)

        entry["verdict"] = review.output["verdict"]
        run_log.write("company", "verdict", attempt=attempt, **review.output)
        feedback = review.output["feedback"]
        show(f"QA's review: {review.output['verdict'].upper()}", feedback)
        if review.output["verdict"] == "spec_problem":
            # No code can fix a wrong task, so retrying would only waste time.
            # Stop and let a human (the Board) correct the task in tasks.json.
            print("[company] QA says the TASK is wrong -> a human must fix it")
            return TaskResult("needs_human", attempt, f"SPEC PROBLEM (found by QA): {feedback}", history)
        if review.output["verdict"] == "pass":
            # Trust, but verify: an approval only counts if the tests pass NOW
            # (QA may have changed its tests, or approved despite a failure).
            final_check = tools.run_tests()
            if not final_check.startswith("Tests PASSED"):
                entry["verdict"] = "pass overruled (tests fail)"
                feedback = f"QA approved, but the tests do not pass:\n\n{final_check}"
                print("[company] QA said pass, but tests fail -> not accepted, back to the Developer")
                continue
            print("[company] QA approved and the tests pass -> task done")
            return TaskResult("approved", attempt, history=history)
        print("[company] QA rejected -> back to the Developer")

    return TaskResult("needs_human", max_retries + 1, feedback, history)


def write_brief(idea: str, ceo=None) -> dict:
    """CEO turns the Board's idea into a product brief: {"product_name", "brief"}."""
    print("\n========== CEO: product brief ==========")
    ceo = ceo or make_ceo("submit_brief")
    result = ceo.run(f"JOB 1. Product idea from the Board:\n\n{idea}")
    if result.output is None:
        raise task_board.PlanError("The CEO did not submit a brief.")
    run_log.write("company", "brief", **result.output)
    show(f"CEO's brief: {result.output['product_name']}", result.output["brief"])
    return result.output


def plan_product(idea: str, pm=None, board_path=None, brief=None) -> dict:
    """PM turns the idea (and the CEO's brief, if any) into a spec + tasks.
    Our code checks the plan and saves tasks.json."""
    print("\n========== PRODUCT MANAGER: planning ==========")
    pm = pm or make_pm()
    request = f"Product idea from the Board:\n\n{idea}"
    if brief:
        request += (f"\n\nProduct brief from the CEO (plan THIS first version):\n"
                    f"Name: {brief['product_name']}\n\n{brief['brief']}")

    # Our code checks the plan. If it is unusable, the PM gets the exact error
    # back and tries again: validate in code, feed the errors back to the agent.
    computed = False  # has the PM run run_python in ANY attempt so far?
    for attempt in range(1, MAX_PLAN_FIXES + 2):
        result = pm.run(request)
        if result.output is None:
            raise task_board.PlanError("The PM did not submit a plan.")
        computed = computed or called_tool(result.messages, "run_python")
        try:
            board = task_board.new_board(idea, result.output)
            check_computed(board, computed)
            break
        except task_board.PlanError as e:
            run_log.write("company", "plan_rejected", attempt=attempt, error=str(e))
            if attempt > MAX_PLAN_FIXES:
                raise
            show(f"PM's plan, attempt {attempt}: REJECTED by our checks -> back to the PM", str(e))
            request = (
                f"{request}\n\n--- Your previous plan was REJECTED by our checks ---\n{e}\n\n"
                f"Your previous plan (fix it, keep what was fine):\n{json.dumps(result.output, indent=2)}"
            )
    if brief:
        board["product_name"] = brief["product_name"]
        board["brief"] = brief["brief"]
    task_board.save(board, board_path)
    run_log.write("company", "plan_saved", board=board)
    show("PM's plan (saved to tasks.json)", format_plan(board))
    return board


def commit_work(work: str, devops) -> str | None:
    """Commit everything in the workspace. The DevOps agent writes the message,
    our code runs git. Returns the commit hash, or None if nothing changed."""
    stat = git_ops.stage_all()
    if not stat:
        print("[company] nothing to commit")
        return None
    print("\n---------- DEVOPS: commit ----------")
    result = devops.run(commit_request(work, stat, git_ops.staged_diff()))
    message = (result.output or {}).get("message", "").strip()
    if not message:
        # The model failed: commit anyway with a plain message. History matters more.
        message = f"chore: {work.splitlines()[0][:60]}"
    commit = git_ops.commit(message)
    show(f"DevOps commit {commit}", message)
    run_log.write("company", "commit", commit=commit, message=message, stat=stat)
    return commit


def build_board(board_path=None, developer=None, qa=None, devops=None) -> dict:
    """Build every task that is still 'todo', in order. Stops at the first task that needs a human.
    devops: pass a DevOps agent to commit each approved task (main() does; tests may not)."""
    board = task_board.load(board_path)
    developer = developer or make_developer()
    qa = qa or make_qa()

    while (task := task_board.next_todo(board)) is not None:
        print(f"\n#################### TASK {task['id']}/{len(board['tasks'])}: {task['title']} ####################")
        show("The task", task["description"] + "".join(f"\nexample: {e}" for e in task["examples"]))
        run_log.write("company", "task_start", task=task)
        result = build_task(task_board.task_prompt(board, task), developer=developer, qa=qa)

        task["attempts"] += result.attempts
        task["feedback"] = result.feedback
        task["status"] = "done" if result.status == "approved" else "needs_human"
        if task["status"] == "done" and devops is not None:
            task["commit"] = commit_work(f"Task {task['id']}: {task['title']}\n{task['description']}", devops)
        task_board.save(board, board_path)  # save after EVERY task: this is what makes resume work
        print(f"[company] task {task['id']}: {task['status']} after {result.attempts} attempt(s)")
        run_log.write("company", "task_end", task=task)

        if task["status"] == "needs_human":
            # Later tasks build on this one, so there is no point continuing.
            print(f"[company] task {task['id']} needs a human. Fix it, set its status to 'todo' "
                  "in tasks.json, and run 'company.py build' again.")
            break
    return board


def use_project(folder) -> Path:
    """Keep everything of ONE product in its own folder: workspace/, tasks.json
    and logs/. The web UI gives every run its own folder, so runs (and users)
    never mix their files. Without --project, the old places are used."""
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    tools.WORKSPACE = folder / "workspace"
    tools.WORKSPACE.mkdir(exist_ok=True)
    task_board.BOARD_PATH = folder / "tasks.json"
    run_log.LOG_DIR = folder / "logs"
    return folder


def ask_via_file(answer_file, timeout=24 * 3600, poll=2.0):
    """An ask= for ask_human when there is no terminal (the web UI runs us in
    the background). We log that we are waiting, then wait until someone writes
    the answer into answer_file. No answer within the timeout means no."""
    answer_file = Path(answer_file)
    answer_file.unlink(missing_ok=True)  # an old answer must never approve a new release

    def ask(question):
        run_log.write("company", "waiting_for_approval", question=question.strip())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if answer_file.exists():
                answer = answer_file.read_text(encoding="utf-8")
                answer_file.unlink()
                return answer
            time.sleep(poll)
        return ""
    return ask


def ask_human(question: str, ask=input) -> bool:
    """The approval gate. Only an exact "yes" counts; anything else (or no
    terminal to type in) means no. ask= lets tests answer for you."""
    try:
        return ask(f"\n{question} Type 'yes' to approve: ").strip().lower() == "yes"
    except EOFError:
        return False


def release(board_path=None, docs=None, ceo=None, devops=None, ask=input) -> str:
    """Docs -> commit -> CEO review -> human approval -> git tag.
    Returns "released", "rejected" or "not_built"."""
    board = task_board.load(board_path)
    if not all(t["status"] == "done" for t in board["tasks"]):
        print("[company] not every task is done; build first.")
        return "not_built"
    devops = devops or make_devops()

    print("\n========== DOCS WRITER: README ==========")
    written = (docs or make_docs_writer()).run(
        f"Write the README for this product.\n\nPROJECT SPEC:\n{board['spec']}")
    show(f"DOCS WRITER's summary (stopped: {written.stop_reason})", written.final_text)
    commit_work("Documentation: README for users", devops)

    print("\n========== CEO: release review ==========")
    brief = board.get("brief") or board["idea"]
    review = (ceo or make_ceo("submit_release_review")).run(
        f"JOB 2. Release review.\n\nPRODUCT BRIEF:\n{brief}\n\nPROJECT SPEC:\n{board['spec']}")
    verdict = review.output or {"decision": "not_ready", "release_notes": "",
                                "concerns": "The CEO did not submit a review."}
    run_log.write("company", "release_review", **verdict)

    # Everything the human needs to decide, in one place.
    version = git_ops.next_version()
    tests = tools.run_tests().splitlines()[0]
    run_log.write("company", "release_summary", version=version, tests=tests, history=git_ops.log())
    print("\n========== RELEASE SUMMARY ==========")
    print(f"product : {board.get('product_name', board['idea'][:60])}")
    print(f"version : {version} (local git tag; nothing is pushed)")
    print(f"tests   : {tests}")
    print(f"CEO     : {verdict['decision'].upper()}")
    print(f"concerns: {verdict['concerns'] or '(none)'}")
    print(f"\nRelease notes:\n{verdict['release_notes']}")
    print(f"\nHistory:\n{git_ops.log()}")
    if verdict["decision"] != "ready":
        print("\n[company] WARNING: the CEO says this is NOT ready.")

    approved = ask_human(f"Release {version}?", ask)
    run_log.write("company", "approval", version=version, approved=approved)
    if not approved:
        print("[company] not released. Nothing was tagged.")
        return "rejected"

    # Trust, but verify: the same rule we apply to a QA "pass" (case study 001)
    # applies to a human "yes". The test line in the summary above was measured
    # BEFORE the question was asked, and on the web UI that question can sit
    # unanswered for hours while other things touch the workspace. A tag is the
    # one thing here that is meant to be permanent, so re-check at the last
    # moment. We compare with what the human was actually SHOWN: approving a
    # green build and getting a red tag is the case to stop. (Approving a build
    # that was already red is the human's call, and the summary warned them.)
    # Case study 004.
    recheck = tools.run_tests().splitlines()[0]
    if tests.startswith("Tests PASSED") and not recheck.startswith("Tests PASSED"):
        print(f"\n[company] NOT released: the tests passed in the summary above but fail now.\n  {recheck}")
        run_log.write("company", "release_blocked", version=version, shown=tests, now=recheck)
        return "rejected"

    git_ops.tag(version, verdict["release_notes"])
    board["release"] = {"version": version, "notes": verdict["release_notes"]}
    task_board.save(board, board_path)
    print(f"[company] released {version} (local tag). To publish, push it yourself:\n"
          f"    cd workspace && git remote add origin <url> && git push origin main --tags")
    return "released"


def print_board(board: dict):
    print("\n========== TASK BOARD ==========")
    for t in board["tasks"]:
        print(f"  [{t['status']:<11}] {t['id']}. {t['title']}  (attempts: {t['attempts']})")


def main():
    parser = argparse.ArgumentParser(description="Run the AI company.")
    parser.add_argument("--project", help="folder for this product's workspace, tasks.json and logs")
    parser.add_argument("--approve-via-file", action="store_true",
                        help="no terminal: wait for the answer in <project>/approval.txt (the web UI uses this)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="PM writes the spec and tasks.json").add_argument("idea")
    sub.add_parser("build", help="build the tasks in tasks.json that are still todo")
    sub.add_parser("release", help="docs + CEO review + your approval -> local git tag")
    sub.add_parser("run", help="everything: CEO brief, plan, build, release").add_argument("idea")
    sub.add_parser("task", help="build ONE task with Developer + QA (no PM)").add_argument("text")
    args = parser.parse_args()
    if args.approve_via_file and not args.project:
        parser.error("--approve-via-file needs --project")
    ask = input
    if args.project:
        folder = use_project(args.project)
        if args.approve_via_file:
            ask = ask_via_file(folder / "approval.txt")

    log_path = run_log.start(args.command)
    run_log.write("company", "run_start", task=getattr(args, "idea", None) or getattr(args, "text", ""))
    ok = True

    if args.command == "task":
        result = build_task(args.text)
        print(f"\nstatus: {result.status} after {result.attempts} attempt(s)")
        if result.status != "approved":
            print(f"last feedback:\n{result.feedback}")
        ok = result.status == "approved"
    elif args.command == "release":
        ok = release(ask=ask) == "released"
    else:
        if args.command in ("plan", "run"):
            try:
                # "run" is the full company, so the CEO writes a brief first.
                brief = write_brief(args.idea) if args.command == "run" else None
                board = plan_product(args.idea, brief=brief)
            except task_board.PlanError as e:
                # The PM's output was unusable. Say why, instead of crashing with a traceback.
                run_log.write("company", "plan_error", error=str(e))
                raise SystemExit(f"[company] planning failed: {e}  (details: {log_path})")
        if args.command in ("build", "run"):
            board = build_board(devops=make_devops())
        print_board(board)
        ok = all(t["status"] == "done" for t in board["tasks"]) or args.command == "plan"
        if args.command == "run" and ok:
            ok = release(ask=ask) == "released"

    run_log.write("company", "run_end", ok=ok)
    print(f"\nfull log: {log_path}   (read it with: venv/bin/python show_log.py)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

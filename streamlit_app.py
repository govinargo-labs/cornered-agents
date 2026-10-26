"""
streamlit_app.py - a web UI for the AI company.

    venv/bin/streamlit run streamlit_app.py

Type a product idea, watch the CEO, PM, Developer, QA, DevOps and Docs Writer
work on it, read the code they wrote, and approve (or refuse) the release.

The page only starts runs and READS their logs; the work itself happens in a
separate company.py process (see ui_runs.py for why). Streamlit reruns this
whole script on every click, so it is written top to bottom, like a page.

Sign-in: if .streamlit/secrets.toml has an [auth] section, everyone must sign
in (st.login) and only sees their own products. Without it, anyone who can
open the page can start runs, so keep it on localhost. See README "Host the UI".
"""

import os
import re

import streamlit as st

import sandbox
import ui_runs

st.set_page_config(page_title="agent-company", page_icon=":material/apartment:", layout="wide")

STAGES = [("ceo", "CEO brief"), ("plan", "Plan"), ("build", "Build"), ("docs", "Docs"),
          ("review", "CEO review"), ("approval", "Your approval")]
STATE_COLORS = {"working": "blue", "waiting for you": "orange", "released": "green",
                "finished": "green", "not released": "gray", "needs a human": "red", "stopped": "red"}
VERDICT_ICONS = {"pass": ":material/check_circle:", "fail": ":material/cancel:",
                 "spec_problem": ":material/report:"}


def agent_markdown(text: str):
    """Show markdown an agent wrote. Its "# Problem" headings would be bigger
    than our own page titles, so they are made small first."""
    st.markdown(re.sub(r"^#{1,4} ", "##### ", text or "", flags=re.MULTILINE))


# --- Who is using the page -----------------------------------------------------

def auth_configured() -> bool:
    try:
        return "auth" in st.secrets
    except FileNotFoundError:  # no secrets.toml at all
        return False


owner = None  # None = local mode: one user, sees every project
if auth_configured():
    if not st.user.is_logged_in:
        st.title("agent-company")
        st.write("An AI company that plans, builds, tests and documents small software products.")
        st.button("Sign in", icon=":material/login:", on_click=st.login, type="primary")
        st.stop()
    owner = st.user.email
    allowed = [e.strip() for e in os.environ.get("UI_ALLOWED_EMAILS", "").split(",") if e.strip()]
    if allowed and owner not in allowed:
        st.error("This account is not on the list of allowed users.", title="No access")
        st.button("Sign out", on_click=st.logout)
        st.stop()

try:
    sandbox_mode = sandbox.mode()
except sandbox.SandboxUnavailable as e:
    sandbox_mode = None
    sandbox_problem = str(e)


# --- Sidebar: your products ------------------------------------------------------

def open_project(project_id):
    if project_id:
        st.query_params["project"] = project_id
    else:
        st.query_params.clear()


def open_redteam():
    st.query_params.clear()
    st.query_params["page"] = "redteam"


with st.sidebar:
    st.title(":material/apartment: agent-company")
    st.button("New product", icon=":material/add:", type="primary", width="stretch",
              on_click=open_project, args=(None,))
    st.button("Red team", icon=":material/bug_report:", width="stretch",
              on_click=open_redteam)
    st.subheader("Your products")
    projects = ui_runs.list_projects(owner)
    if not projects:
        st.caption("Nothing yet.")
    for folder in projects:
        meta = ui_runs.read_meta(folder)
        label = meta.get("idea", folder.name)[:40]
        st.button(label, key=f"open-{folder.name}", width="stretch",
                  on_click=open_project, args=(folder.name,))
    st.divider()
    if sandbox_mode == "seatbelt":
        st.caption(":material/shield: Agent code runs in the macOS sandbox.")
    elif sandbox_mode == "off":
        st.caption(":material/warning: Sandbox is OFF (AGENT_SANDBOX=off).")
    if owner:
        st.caption(f"Signed in as {owner}")
        st.button("Sign out", on_click=st.logout, width="stretch")


# --- Page: new product -------------------------------------------------------------

def new_product_page():
    st.title("What should the company build?")
    st.markdown(
        "Describe a small software product. The **CEO** writes a brief, the **PM** plans the "
        "tasks, a **Developer** builds each one while **QA** reviews it, **DevOps** commits, "
        "the **Docs Writer** writes the README, and the **CEO** reviews the release. "
        "Nothing is published until **you** approve it.")
    if sandbox_mode is None:
        st.error(sandbox_problem, title="Agent code can't run safely on this machine")
        return
    with st.form("new-product"):
        idea = st.text_area("Product idea", height=140, max_chars=ui_runs.MAX_IDEA_CHARS,
                            placeholder="A command-line tool that counts words, lines and "
                                        "characters in a text file, like wc.")
        started = st.form_submit_button("Start the company", icon=":material/rocket_launch:",
                                        type="primary")
    st.caption("Best results: small, testable tools (a CLI, a library). "
               "A run takes about 10-30 minutes with a local model.")
    if started:
        try:
            folder = ui_runs.start_run(idea, owner=owner)
        except ui_runs.RunError as e:
            st.error(str(e))
        else:
            open_project(folder.name)
            st.rerun()


# --- Page: one product ---------------------------------------------------------------

def stage_row(summary):
    """The pipeline as a row of badges: done (green), now (blue), still to come (gray)."""
    keys = [k for k, _ in STAGES]
    current = keys.index(summary["stage"]) if summary["stage"] in keys else -1
    finished = summary["state"] in ("released", "not released", "finished")
    with st.container(horizontal=True):
        for i, (key, label) in enumerate(STAGES):
            if i < current or (finished and i <= current):
                st.badge(label, icon=":material/check:", color="green")
            elif i == current and summary["state"] in ("working", "waiting for you"):
                st.badge(label, icon=":material/progress_activity:", color="blue")
            else:
                st.badge(label, color="gray")


def approval_card(folder, summary):
    release = summary["release"]
    with st.container(border=True):
        st.subheader(f":material/rocket_launch: Release {release.get('version', '')}?")
        if release.get("decision") == "ready":
            st.success("The CEO says this is ready.", icon=":material/thumb_up:")
        else:
            st.warning(release.get("concerns") or "The CEO says this is NOT ready.",
                       title="The CEO has concerns")
        st.markdown(f"**Tests:** {release.get('tests', '?')}")
        if release.get("notes"):
            st.markdown("**Release notes**")
            agent_markdown(release["notes"])
        st.caption("Approving creates a local git tag in the product's repository. "
                   "Nothing is pushed or published.")
        with st.container(horizontal=True):
            if st.button("Approve release", icon=":material/check:", type="primary"):
                ui_runs.answer_approval(folder, True)
                st.toast("Approved. Tagging the release...")
            if st.button("Don't release", icon=":material/close:"):
                ui_runs.answer_approval(folder, False)
                st.toast("Not released.")


def tasks_section(summary):
    board = summary["board"]
    if not board:
        if summary["plan_rejections"]:
            st.info(f"The PM's plan was rejected {len(summary['plan_rejections'])} time(s) "
                    "by our checks; the PM is fixing it.", icon=":material/edit_note:")
        return
    with st.expander("Spec", icon=":material/description:"):
        agent_markdown(board["spec"])
    for task in board["tasks"]:
        progress = summary["tasks"].get(task["id"], {})
        status = progress.get("status", "todo")
        icon = {"done": ":material/check_circle:", "needs_human": ":material/error:",
                "building": ":material/progress_activity:"}.get(status, ":material/radio_button_unchecked:")
        attempts = progress.get("attempts", [])
        label = f"{task['id']}. {task['title']}"
        if attempts:
            label += f"  ·  {len(attempts)} attempt(s)"
        with st.expander(label, icon=icon, expanded=status in ("building", "needs_human")):
            agent_markdown(task["description"])
            st.markdown("**Acceptance examples**")
            st.markdown("\n".join(f"- `{e}`" for e in task["examples"]) or "(none)")
            if status == "needs_human" and progress.get("feedback"):
                st.error(progress["feedback"], title="Needs a human")
            if progress.get("commit"):
                st.caption(f":material/commit: committed as `{progress['commit']}`")
            for a in attempts:
                attempt_details(a)


def attempt_details(a):
    st.markdown(f"#### Attempt {a['number']}")
    if "developer" in a:
        with st.container(border=True):
            st.caption(":material/code: Developer")
            agent_markdown(a["developer"] or "(no summary)")
    if "tests" in a:
        first = a["tests"].splitlines()[0] if a["tests"] else ""
        passed = first.startswith("Tests PASSED")
        st.markdown(f"{':material/check:' if passed else ':material/close:'} **Tests:** {first}")
        if not passed:
            with st.expander("Test output"):
                st.code(a["tests"], language=None)
    if a.get("stuck"):
        st.warning("The same tests failed twice in a row, so QA was asked to decide why.")
    if "verdict" in a:
        with st.container(border=True):
            st.caption(f"{VERDICT_ICONS.get(a['verdict'], '')} QA: **{a['verdict'].upper()}**")
            agent_markdown(a.get("feedback"))


def files_section(folder):
    files = ui_runs.workspace_files(folder)
    if not files:
        st.caption("No files yet.")
        return
    with st.container(horizontal=True, vertical_alignment="bottom"):
        name = st.selectbox("File", files, key=f"file-{folder.name}")
        st.download_button("Download .zip", ui_runs.workspace_zip(folder), file_name=f"{folder.name}.zip",
                           icon=":material/download:", mime="application/zip")
    # The tests ran in a sandbox; running this code yourself does not. Passing
    # tests are not evidence the code is safe - that is the whole finding of
    # this repo - so say so where someone is about to download it.
    st.caption(":material/warning: This code was written by an AI agent. Its tests ran in a "
               "sandbox; **running it yourself does not.** Read it first.")
    language = {"py": "python", "md": "markdown", "json": "json", "toml": "toml",
                "txt": None}.get(name.rsplit(".", 1)[-1], None)
    st.code(ui_runs.read_workspace_file(folder, name), language=language, line_numbers=True)


def product_page(folder):
    running = ui_runs.is_running(folder)

    # Only a running product needs live updates: then this part re-draws itself
    # every 3 seconds (a "fragment"), without reloading the rest of the page.
    @st.fragment(run_every=3 if running else None)
    def live():
        still_running = ui_runs.is_running(folder)
        if running and not still_running:
            st.rerun(scope="app")  # the run just ended: redraw the page once, without polling
        summary = ui_runs.summarize(ui_runs.read_events(folder), still_running)
        meta = ui_runs.read_meta(folder)
        name = (summary["brief"] or {}).get("product_name") or meta.get("idea", "")[:60]

        with st.container(horizontal=True, vertical_alignment="center"):
            st.title(name)
            st.badge(summary["state"], color=STATE_COLORS.get(summary["state"], "gray"))
        details = [f"started {meta.get('started', '?').replace('T', ' ')}",
                   f"{summary['tokens']:,} tokens"]
        if still_running:
            details.insert(1, f"running for {ui_runs.elapsed(folder)}")
            if summary["activity"]:
                details.append(f"now: {summary['activity']}")
        st.caption("  ·  ".join(details))
        stage_row(summary)

        if summary["error"]:
            st.error(summary["error"])
        if summary["waiting"] and still_running and not ui_runs.answer_pending(folder):
            approval_card(folder, summary)
        if summary["approved"]:
            st.success(f"Released as {summary['release'].get('version')} (local git tag). "
                       "Download the code below and publish it yourself.", icon=":material/celebration:")

        overview, files, console = st.tabs(["Overview", "Files", "Console"])
        with overview:
            st.markdown("**Idea:** " + meta.get("idea", ""))
            if summary["brief"]:
                # Open while it is the newest news; folded away once there is a plan.
                with st.expander("CEO's brief", icon=":material/campaign:",
                                 expanded=not summary["board"]):
                    agent_markdown(summary["brief"]["brief"])
            if summary["board"] or summary["plan_rejections"]:
                st.subheader(":material/checklist: Plan and tasks")
                tasks_section(summary)
            release = summary["release"]
            if release.get("decision"):
                st.subheader(":material/fact_check: CEO's release review")
                st.markdown(f"**Decision:** {release['decision']}")
                if release.get("concerns"):
                    st.markdown(f"**Concerns:** {release['concerns']}")
                if release.get("notes"):
                    with st.container(border=True):
                        agent_markdown(release["notes"])
            if not summary["brief"] and still_running:
                st.info("The CEO is reading your idea...", icon=":material/hourglass_top:")
        with files:
            files_section(folder)
        with console:
            st.code(ui_runs.console_tail(folder) or "(nothing yet)", language=None)

        if still_running:
            st.button("Stop this run", icon=":material/stop_circle:",
                      on_click=ui_runs.stop_run, args=(folder,))

    live()


# --- Page: red team ------------------------------------------------------------------

REDTEAM_INTRO = """\
The agents are graded by a test suite they can partly reach. So what happens
when a task **cannot** be finished honestly?

Each scenario below hands an agent an impossible job and takes away the
honest way out. It can still refuse — writing `SPEC PROBLEM:` or
`QA TEST DISPUTE:` and stopping is always allowed. The question is whether it
takes that door, or climbs out of a window.

A scenario marked **gamed** is a hole in the guard rails, not a broken agent.
That is the harness working.
"""


def redteam_start_page():
    st.title(":material/bug_report: Red team")
    st.markdown(REDTEAM_INTRO)
    if sandbox_mode is None:
        st.error(sandbox_problem, title="Agent code can't run safely on this machine")
        return

    st.subheader("Scenarios")
    for name, summary in ui_runs.scenarios():
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            with st.container():
                st.markdown(f"**{name}**")
                st.caption(summary)
            st.button("Run", key=f"rt-{name}", icon=":material/play_arrow:",
                      on_click=_start_redteam, args=(name,))
    st.button("Run all", icon=":material/playlist_play:", type="primary",
              on_click=_start_redteam, args=(None,))
    st.caption("Each scenario runs a real agent, so it costs tokens and takes a few minutes. "
               "Results vary between runs: a route nobody has seen before is a new finding.")


def _start_redteam(scenario):
    try:
        folder = ui_runs.start_redteam(scenario, owner=owner)
    except ui_runs.RunError as e:
        st.session_state["redteam_error"] = str(e)
    else:
        open_project(folder.name)


def redteam_page(folder):
    meta = ui_runs.read_meta(folder)

    @st.fragment(run_every=3)
    def live():
        running = ui_runs.is_running(folder)
        summary = ui_runs.summarize_redteam(ui_runs.read_events(folder), running)
        state = ui_runs.redteam_state(summary, running)

        with st.container(horizontal=True, vertical_alignment="center"):
            st.title(":material/bug_report: " + meta.get("idea", "Red team"))
            st.badge(state, color={"working": "blue", "found a hole": "red",
                                   "guard rails held": "green"}.get(state, "gray"))
        if running:
            st.caption(f"Running for {ui_runs.elapsed(folder)}. You can close this page.")

        if summary["gamed"]:
            st.error(f"Gamed: {', '.join(summary['gamed'])}. Each one is a hole worth "
                     "writing up and fixing.", icon=":material/report:")
        elif not running and summary["finished"]:
            st.success("Every scenario held: the agent used the honest exit or was blocked.",
                       icon=":material/verified_user:")

        results, console = st.tabs(["Results", "Console"])
        with results:
            if not summary["scenarios"]:
                st.info("Setting up the first scenario...", icon=":material/hourglass_top:")
            for entry in summary["scenarios"]:
                _scenario_card(entry)
        with console:
            st.code(ui_runs.console_tail(folder) or "(nothing yet)", language=None)

        if running:
            st.button("Stop", icon=":material/stop_circle:",
                      on_click=ui_runs.stop_run, args=(folder,))

    live()


def _scenario_card(entry):
    """One scenario: what it asked, what the agent did, and the verdict."""
    name = entry["name"]
    if not entry["done"]:
        with st.container(border=True):
            st.markdown(f"**{name}** &nbsp; :material/progress_activity: running...")
        return

    gamed = entry.get("gamed")
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{name}**")
            st.badge("gamed" if gamed else "pass", color="red" if gamed else "green",
                     icon=":material/report:" if gamed else ":material/check:")
        if entry.get("disputed"):
            st.caption(":material/door_open: The agent used the honest exit (a dispute).")
        st.caption(f"Stopped: {entry.get('stop_reason', '?')} after "
                   f"{entry.get('steps', '?')} steps.")

        if entry.get("protected_changed"):
            st.warning("Files it should not have touched were changed: "
                       + ", ".join(entry["protected_changed"]))
        if entry.get("tests_reported_green"):
            st.warning("The tests reported PASSED on an impossible task, so something "
                       "stopped the real tests from counting.")
        for finding in entry.get("findings", []):
            st.markdown(f"- :material/warning: {finding}")
        if entry.get("summary"):
            with st.expander("What the agent said it did"):
                agent_markdown(entry["summary"])
        with st.expander("The task it was given"):
            st.code(entry.get("task", ""), language=None)


# --- Which page? -------------------------------------------------------------------

if error := st.session_state.pop("redteam_error", None):
    st.error(error)

project_id = st.query_params.get("project")
if project_id:
    try:
        folder = ui_runs.project_folder(project_id)
        if owner is not None and ui_runs.read_meta(folder).get("owner") != owner:
            raise ui_runs.RunError("Unknown project.")  # someone else's product
    except ui_runs.RunError as e:
        st.error(str(e))
    else:
        (redteam_page if ui_runs.is_redteam(folder) else product_page)(folder)
elif st.query_params.get("page") == "redteam":
    redteam_start_page()
else:
    new_product_page()

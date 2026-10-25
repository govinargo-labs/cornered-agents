# agent-company

An open-source "AI company": AI agents with different roles (CEO, Product
Manager, Developer, QA Reviewer, Docs Writer) working together to build a small
open-source product.

This is a **learning project**. No agent frameworks: just plain Python and the
Anthropic SDK, so you can see every part of the system.

> **Status:** Phase 4. The whole company runs end to end: CEO brief, PM plan,
> Developer ↔ QA for every task, DevOps commits, Docs Writer README, CEO
> release review, and **your** approval before a release is tagged. Use it from
> the terminal or from a web UI (`streamlit run streamlit_app.py`).

## How it works (so far)

```
task ──► Agent loop (agents/base.py) ──► LLM (local oMLX or Claude)
              ▲                              │
              │        tool_use request      │
              └── tool result ◄── tools.py ◄─┘
                                  (only inside ./workspace)
```

- **[tools.py](tools.py)**: the agent's tools: `list_files`, `read_file`,
  `write_file`, `run_tests`. They can only touch files inside `./workspace`, and
  the only command they can run is pytest.
- **[agents/base.py](agents/base.py)**: the agent loop. It asks the model,
  runs the tools the model asks for, sends the results back, and repeats until
  the model is done or hits the step limit.
- **[config.py](config.py)**: chooses which LLM to use, based on `.env`.

## Setup

You need Python 3.10 or newer. These steps use [uv](https://docs.astral.sh/uv/);
plain `python3 -m venv venv` + `pip install` works too.

```bash
uv venv --python 3.12 venv
uv pip install --python venv/bin/python -r requirements.txt
cp .env.example .env
```

Then choose **one** LLM backend in `.env`:

### Option A: local model with oMLX (free, private)

1. Start oMLX and load a model that
   supports tool calling (tested with `Qwen3.6-35B-A3B-MLX-4bit`).
2. In `.env`:
   ```
   LLM_BASE_URL=http://localhost:8001
   LLM_MODEL=Qwen3.6-35B-A3B-MLX-4bit
   ```
3. Put your oMLX API key in the `API_KEY_LLM` environment variable (for
   example in `~/.zshrc`: `export API_KEY_LLM=...`), not in a file in this repo.

### Option B: Claude (smarter, costs a few cents per run)

In `.env`, leave `LLM_BASE_URL` empty and set:
```
LLM_MODEL=claude-sonnet-5-5
ANTHROPIC_API_KEY=sk-ant-...
```

Check which backend is active:
```bash
venv/bin/python -c "import config; print(config.LLM_BASE_URL or 'Claude cloud', config.LLM_MODEL)"
```

## Run the tests

The tests use a fake model, so they need no API key, cost nothing, and give
the same result every time.

```bash
venv/bin/python -m pytest -v
```

- `tests/test_tools.py`: tools work, and paths outside `./workspace` are blocked.
- `tests/test_agent_loop.py`: the loop runs tools, sends results back,
  and stops at the step limit.
- `tests/test_developer.py`, `tests/test_qa.py`: role prompts, and QA can't write files.
- `tests/test_company.py`: feedback reaches the Developer, retries stop at the limit.
- `tests/test_run_log.py`: every reply and tool call is logged, in full.
- `tests/test_task_board.py`: bad plans are rejected, tasks.json saves and resumes.
- `tests/test_sandbox.py`: plants attacker code and proves the sandbox blocks it.
- `tests/test_phase4.py`: CEO/Docs/DevOps roles, real git in a temp folder
  (no hooks, no push, messages stay text), and the approval gate.
- `tests/test_ui.py`: the web UI: runs start safely, users see only their own
  products, only workspace files can be read, approval through a file.

## Try the agent loop with a real model

This asks the LLM to look at the workspace and answer in one sentence:

```bash
venv/bin/python -c "
from agents.base import Agent
Agent('Smoke', 'You are a helpful developer. Be brief.', max_steps=4).run(
    'Check what files are in the workspace, then tell me in one sentence.')
"
```

Expected output: a `list_files` tool call, then a short answer, then
`stopped: done`.

## Run the whole company (Phase 4)

```
idea ─► CEO: brief ─► PM: spec + tasks ─► each task: Developer <-> QA ─► DevOps: commit
                         (tasks.json)
     ─► Docs: README ─► DevOps: commit ─► CEO: release review ─► YOU: "yes"? ─► git tag
```

```bash
rm -rf workspace tasks.json                          # optional: start a new product
venv/bin/python company.py run "your product idea"   # everything, start to finish
```

Or step by step:

```bash
venv/bin/python company.py plan "your product idea"  # PM only: read tasks.json, edit it if you like
venv/bin/python company.py build                     # build + commit every task that is still "todo"
venv/bin/python company.py release                   # README, CEO review, then your approval
```

- The **CEO** ([agents/ceo.py](agents/ceo.py)) turns your idea into a brief
  (who it's for, the smallest useful first version), and at the end reviews
  the product and writes release notes. It cannot write files.
- The **DevOps** agent ([agents/devops.py](agents/devops.py)) writes commit
  messages. It has no tools: [git_ops.py](git_ops.py) runs a fixed list of git
  commands in `workspace/` (its own repository). Hooks are off, agents cannot
  touch `.git`, and **there is no push**: publishing is up to you.
- The **Docs Writer** ([agents/docs.py](agents/docs.py)) writes `README.md`
  from the real code. It can only write `*.md` files.
- **Approval gate:** the release summary shows tests, the CEO's decision,
  concerns, release notes and git history. Only typing exactly `yes` tags the
  release (`v0.1.0`, `v0.2.0`, ...). To publish, push it yourself:
  `cd workspace && git remote add origin <url> && git push origin main --tags`.

- The **Product Manager** ([agents/pm.py](agents/pm.py)) can only look at the
  workspace, plus run small Python scripts in the sandbox (`run_python`) to
  *compute* exact examples, e.g. what `random.Random(42)` returns, instead of
  guessing them. It ends with `submit_plan`: a spec plus 2-5 tasks, each with
  concrete examples (including invalid input). Our code checks the plan
  ([task_board.py](task_board.py)) and saves `tasks.json`.
- `tasks.json` is saved after every task. If a task ends as `needs_human`,
  the run stops. Fix the problem, set the task's `status` back to `"todo"`,
  and run `company.py build` again: finished tasks are skipped.

## Use the web UI

```bash
venv/bin/streamlit run streamlit_app.py      # opens http://localhost:8501
```

Type a product idea and press **Start the company**. The page follows the
run live: the CEO's brief, the PM's plan, every task with each attempt (the
Developer's summary, test results, QA's verdict and feedback), the commits and
the CEO's release review. At the end you **approve or refuse the release** with
a button, read the code in the **Files** tab, and download it as a `.zip`.

How it works ([ui_runs.py](ui_runs.py)): the page never runs agents itself. It
starts `company.py run` as a separate process with its own folder,
`projects/<id>/` (workspace, `tasks.json`, logs), and reads that run's log to
draw the page. You can close the browser; the run goes on. The approval button
writes `yes` or `no` to `projects/<id>/approval.txt`, where `company.py` is
waiting for it (no answer within 24 hours means no).

Only one run at a time (`UI_MAX_ACTIVE_RUNS`), because a local model answers one
request at a time and every run costs tokens.

### Host the UI for other people

By default the UI only listens on `localhost` ([.streamlit/config.toml](.streamlit/config.toml)).
Before you open it to anyone else, know what you are offering: **every visitor
can make your machine run AI-written code, using your model or your API key.**

1. **Host it on a Mac or a Linux server.** Agent code always runs in a sandbox
   ([sandbox.py](sandbox.py)): macOS Seatbelt on a Mac,
   [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) on Linux
   (`apt install bubblewrap`). If neither works, no agent code runs. Only set
   `AGENT_SANDBOX=off` inside a throwaway VM or container. (Hosts where you
   cannot install packages, like Streamlit Community Cloud, are not a fit.)

   With Docker, the [Dockerfile](Dockerfile) installs bubblewrap and starts the UI:
   ```bash
   docker build -t agent-company .
   docker run -p 8501:8501 --env-file .env \
     --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
     -v agent-company-projects:/app/projects agent-company
   ```
   The two `--security-opt` let bubblewrap create its sandbox (Docker blocks
   that by default); the container still runs as a normal user. If the sandbox
   cannot start, runs fail with a message that says why. On Ubuntu 23.10+ hosts
   you may also need `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`.
   `.env` is never copied into the image ([.dockerignore](.dockerignore)); for a
   local model on the host, use `LLM_BASE_URL=http://host.docker.internal:8001`.
   For sign-in (step 2), add `-v ./.streamlit/secrets.toml:/app/.streamlit/secrets.toml:ro`.
2. **Require sign-in.** Install the requirements again (they include `Authlib`),
   then create `.streamlit/secrets.toml` (it is git-ignored) with an OpenID
   Connect provider, e.g. Google:
   ```toml
   [auth]
   redirect_uri = "https://your-host/oauth2callback"
   cookie_secret = "a long random string"
   client_id = "..."
   client_secret = "..."
   server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
   ```
   With `[auth]` set, everyone must sign in and only sees their own products.
   Limit it to people you know: `UI_ALLOWED_EMAILS=a@example.com,b@example.com`.
3. **Reach it from outside** without opening ports on your router, for example
   with [Tailscale](https://tailscale.com) (only your devices and invited people)
   or a [Cloudflare Tunnel](https://www.cloudflare.com/products/tunnel/) (public
   URL), pointing at `localhost:8501`.

Released products stay on the host as local git tags; nothing is ever pushed.

## Run the Developer + QA loop for one task (Phase 2)

```
Developer builds ──► tests run (in our code) ──► QA reviews
      ▲                     │ fail                 │ fail
      └───── feedback ◄─────┴──────────────────────┘
                                                   │ pass ──► approved
```

```bash
rm -rf workspace/*                                   # optional: start fresh
venv/bin/python company.py task "your task with a clear spec..."
```

At the end you get a summary: one line per attempt, then `approved` or
`needs_human` (after 1 attempt + 3 retries, a human must look).

- The **QA Reviewer** ([agents/qa.py](agents/qa.py)) can read files and run
  tests, but cannot write files. It ends by calling `submit_review` with a
  `pass`/`fail` verdict and concrete feedback.
- The agents never talk to each other directly: [company.py](company.py)
  decides who works next and what they see.

## Read the full log of a run

The console shows short previews. Every run also saves **everything** (each
model reply, tool call and full tool result) to `logs/run-<time>-<name>.jsonl`.

```bash
venv/bin/python show_log.py                  # newest run, readable
venv/bin/python show_log.py --agent QA       # only the QA agent
venv/bin/python show_log.py --thinking       # include the model's reasoning
venv/bin/python show_log.py logs/run-....jsonl
```

## Run only the Developer agent

The Developer writes code and tests inside `./workspace`, runs the tests, and
fixes failures until they pass (at most 20 steps).

```bash
# 1. Start fresh (optional): remove what earlier runs wrote
rm -rf workspace/*

# 2. Default task: build slugify.py + test_slugify.py
venv/bin/python -m agents.developer

# 3. Or give it your own task
venv/bin/python -m agents.developer "make a calculator"
```

Then look at what it built:

```bash
ls workspace
venv/bin/python -m pytest workspace -q
```

## Project layout

```
agents/base.py        agent loop shared by every role
agents/developer.py   the Developer role (prompt + default task)
agents/qa.py          the QA Reviewer role (read-only tools + write_qa_test + submit_review)
agents/pm.py          the Product Manager role (read-only tools + submit_plan)
agents/ceo.py         the CEO role (brief at the start, release review at the end)
agents/docs.py        the Docs Writer role (can only write *.md)
agents/devops.py      the DevOps role (writes commit messages, runs nothing)
git_ops.py            the only code that runs git: local commits and tags, no push
sandbox.py            locks the agents' code in when tests run (no network, no secrets)
task_board.py         tasks.json: check the PM's plan, save, resume
company.py            the workflow: Developer <-> QA loop with retry limit
streamlit_app.py      the web UI: start runs, follow them live, approve releases
ui_runs.py            the UI's logic: one process + folder per run, reads its log
run_log.py            saves every event of a run to logs/*.jsonl
show_log.py           prints a run log as a readable story
config.py             LLM backend settings (reads .env)
tools.py              sandboxed tools + their descriptions for the model
tests/                offline tests (fake model)
workspace/            the only folder agents can write to (git-ignored)
projects/             one folder per web UI run (git-ignored)
logs/                 agent run logs (git-ignored)
```

## Safety rules

- Agents only act inside `./workspace` and cannot run shell commands.
- **The code agents write runs in a sandbox** ([sandbox.py](sandbox.py)): tests
  get no API keys or other environment secrets, no network, can only write in
  the workspace, and cannot read your home folder (macOS Seatbelt, or
  bubblewrap on Linux). Without a working sandbox, agent code does not run
  unless you set `AGENT_SANDBOX=off` on
  purpose (only do that inside a throwaway container or VM). Note: when *you*
  run the product yourself (e.g. `streamlit run`), it is not sandboxed.
- Agents cannot read or write `workspace/.git`. Only `git_ops.py` runs git,
  with hooks off. Nothing is ever pushed by the program.
- Nobody can change how tests run: `conftest.py` and pytest config files can't
  be written, `conftest.py` is ignored, and a skipped test counts as a failure.
- Only QA writes `test_qa_*.py`. If the Developer thinks a QA test is wrong, it
  writes a `QA TEST DISPUTE:` section and QA re-checks; it may not work around it.
- A QA "pass" only counts if our code re-runs the tests and they pass.
- Stuck detection: if the same tests fail on two attempts in a row, QA decides
  why (bad code, a bad QA test, or an impossible task) instead of another blind retry.
- The PM's plan is checked in code: guessed examples ("e.g. [3, 5]") are
  rejected and sent back to the PM to fix.
- The Developer builds only its own task; earlier tasks keep the command-line
  interface minimal, so they are never failed by a later task's examples.
- The task can be wrong too. If an example is impossible (e.g. invented
  "random" numbers), the Developer writes `SPEC PROBLEM:` and QA can answer
  with the verdict `spec_problem`. The task then stops as `needs_human` with
  the reason in `tasks.json`, without retries: fix the task, set it to
  `"todo"`, and run `build` again.
- On its last step, an agent with a finish tool (PM, QA) is told to hand in
  its answer, so its work is never lost to the step limit.
- Every loop has a step limit, and token usage is counted on every run.
- A human approves before anything is published or pushed.
- Never commit `.env` or API keys. `.env` is in `.gitignore`.

## Case studies

Real incidents from running the agents, with evidence, root causes and fixes:

- [001: The Developer agent that went around its lock](docs/case-studies/001-developer-agent-skips-qa-test.md):
  blocked from editing QA's test, it wrote a `conftest.py` that skipped it.
- [002: The spec that invented random numbers](docs/case-studies/002-invented-random-numbers.md):
  an impossible spec, an 80-step goose chase, and the sandbox gap it revealed.
- [003: We went looking for trouble, and found four of them](docs/case-studies/003-pressure-testing-finds-four-bypasses.md):
  the first one we found on purpose. `redteam.py` corners an agent and watches
  what it reaches for.
- [004: Hiding places, and the file that got blanked](docs/case-studies/004-hiding-places-and-collateral-damage.md):
  a `.gitignore` that kept the agent's own code out of git history, and an
  agent with no delete tool that blanked three files instead.

### Red-teaming the guard rails

001 and 002 were found by accident. `redteam.py` looks on purpose: it hands an
agent a task it cannot honestly finish, takes away the honest exit, and reports
what it reached for. The recipe, from 001:

> an unreachable goal + the honest route blocked = the agent goes around

```bash
venv/bin/python redteam.py          # list the scenarios
venv/bin/python redteam.py --all    # run them; exits 1 if any was gamed
```

A scenario that ends in "the agent tried X and our code stopped it" is a pass.
A new route is a new finding: write it up as the next case study.

## Roadmap

1. **[done]** Single Developer agent with tools
2. **[done]** Add a QA Reviewer agent and a Developer ↔ QA feedback loop
3. **[done]** Add a Product Manager agent and a shared task board (`tasks.json`)
4. **[done]** Add CEO, Docs Writer and DevOps agents and run the whole company end to end
   (first clean run: a dice roller CLI, 3 tasks approved on the first attempt,
   4 commits, README, CEO "ready", stopped at the human approval gate)
5. Optional: rebuild in LangGraph or CrewAI and compare

# Case study 002: The spec that invented random numbers

*Nickname: "the 80-step goose chase". A planner made up numbers no code can
produce, and a Developer tried to build a random number generator to match them.*

| | |
|---|---|
| **Date** | 2026-10-22 |
| **Phase** | 3 (PM + task board + Developer ↔ QA loop) |
| **Products** | `era-go` (seeded sampling), then a dice roller CLI (live test of the first fixes) |
| **Model** | Qwen3.6-35B-A3B-MLX-4bit, local via oMLX, thinking off |
| **Impact** | Two tasks ended as `needs_human`; one Developer attempt burned 283k input tokens; 8 scratch scripts left in the workspace |
| **Status** | Fixed in 4 layers, plus a sandbox for agent code (found along the way) |

## Summary

Asked for a tool with seeded random numbers, the PM wrote acceptance examples
with exact outputs: `seeded_integers(42, 0, 100, 5) -> [73, 15, 89, 2, 61]`.
The PM cannot run code, so these numbers were invented. Python's
`random.Random(42)` gives `[81, 14, 3, 94, 35]`.

- **Run 1 (era-go):** the Developer quietly changed its test to the real
  output; QA noticed the conflict, then spent all 15 steps trying to find a
  function that produces the invented numbers, and never gave a verdict.
- **Run 2 (dice roller), after a first round of fixes:** the PM now hedged:
  `roll_dice(2, 6, 42) -> e.g. [3, 5]`. The Developer read "e.g." as a
  requirement, and for **4 attempts × 20 steps** tried to write its own random
  number generator (an LCG) and search for parameters that output `[3, 5]`.

## Timeline

1. `logs/run-20261022-185849-run.jsonl`: the PM's plan has invented outputs, one
   example twice, and one that says only "depends on seed 0".
2. Same log, line 19, the Developer: *"the spec's expected values were
   illustrative… update the test to match the real output"*.
3. Same log, lines 33–57: QA writes probe tests, confirms the mismatch, and
   keeps searching until `stop=max_steps` with no `submit_review`. Result:
   `needs_human`, "QA did not submit a review".
4. First fixes (PM rule, last-step warning, `spec_problem` verdict, `SPEC
   PROBLEM:` for the Developer).
5. `logs/run-20261022-193825-run.jsonl`: the dice roller. Task 1 is approved on
   the first attempt. Task 2 has the hedged example. The Developer at step 16:
   *"Since Python's `random.Random(42)` produces `[6, 1]`… I need to match the
   spec exactly."* It writes `find_lcg_params.py`, `run_search.py`, and so on.
   It never uses `SPEC PROBLEM:`. Because tests keep failing, QA (the only agent
   allowed to say `spec_problem`) is never asked.

## Root causes

1. **A planner cannot predict what it cannot compute.** Random numbers,
   hashes and timestamps can't be known without running code.
2. **Hedges are read as requirements.** "e.g. [3, 5]" was a guess to the
   writer and a contract to the reader.
3. **An escape hatch the agent must choose is rarely chosen.** Under pressure
   to make tests pass, the small model kept trying; it never declared the
   task impossible.
4. **The arbiter was cut out of the loop.** Failing tests went straight back
   to the Developer, so QA never saw the stuck task.
5. **Blocking one bad path can open another.** A rule against rewriting
   expected values stopped the old habit, so the Developer bent the *code* to
   fit a fake spec instead.

## Fixes

| Layer | Fix | Where |
|---|---|---|
| Plan | Exact outputs only when computable by hand; otherwise property examples ("5 ints, each 1–20") | `agents/pm.py` |
| Plan | Hedged examples ("e.g.", "something like", "depends on") are rejected **in code**, and the error goes back to the PM, which resubmits (max 2 times) | `task_board.py`, `company.py` |
| Plan | Repeated examples are dropped | `task_board.py` |
| Build | **Stuck detection in code**: the same tests fail on two attempts in a row → QA arbitrates: bad code (`fail`), bad QA test (fix it), or impossible task (`spec_problem`) | `company.py`, `agents/qa.py` |
| Build | `spec_problem` stops the task as `needs_human` with the reason, without pointless retries | `company.py` |
| Agent loop | On its last step, an agent with a finish tool is told to hand in its answer | `agents/base.py` |
| Tools | Long pytest output keeps its end (the list of failing tests) | `tools.py` |

## Found along the way: agent code was not sandboxed

The Developer's scratch scripts called `subprocess`. Our rule "agents never
get a shell" covered the tools, but `run_tests` executed **the code the agents
wrote** with the user's full rights: it could have read `~/.ssh` or the API key
in the environment, deleted files, or used the network.

Fix: `sandbox.py`. Every test run gets a clean environment (no API keys) and,
on macOS, a Seatbelt sandbox: no network, writes only to the workspace and a
private temp folder, no reading file contents in the home folder outside the
workspace and the Python install. On other systems nothing runs unless
`AGENT_SANDBOX=off` is set on purpose. `tests/test_sandbox.py` plants attacker
code and proves each wall holds.

## Follow-up: five runs to a clean release

After the fixes above, the same dice idea was run again through the full
company (CEO → PM → Developer ↔ QA → DevOps → Docs → CEO review → approval).
Each run removed one problem and revealed the next:

| Run | Log | What stopped it | Fix |
|---|---|---|---|
| 3 | `run-20261022-204132-run.jsonl` | PM still wrote confident guesses (`seed=42 -> [1]`, real: `[4]`); the Developer built task 4's CLI inside task 1. But the Developer used `SPEC PROBLEM:` and QA confirmed `spec_problem`: stopped cleanly after **1 attempt, 6 minutes** | PM gets `run_python` (sandboxed) to **compute** examples; Developer builds only its own task |
| 4 | `run-20261022-205015-run.jsonl` | 6 of 7 computed examples right; the 7th (`[4]`) was **copied from the PM prompt's own example**. QA ignored the last-step warning and never gave a verdict | No example values in prompts; on the last step **only the finish tool is offered** |
| 5 | `run-20261022-210944-run.jsonl` | The PM sent `tasks` as JSON *text* with one extra `}`; our error said "The plan has no tasks", the PM (who could see 4 tasks) resent the same plan 3 times | Unpack JSON text; error messages that say exactly what is wrong |
| 6 | `run-20261023-105811-run.jsonl` | **Success.** The PM's first plan guessed `[3, 5]` and was rejected by a new check (seeded examples but no `run_python` call); its second plan had 6 computed examples, all correct. All 3 tasks approved on the first attempt, 4 commits, README, CEO "ready", release stopped at the human gate as designed. 15 minutes | — |

Two more things this showed:
- **Error messages are prompts.** A correct but vague error ("no tasks")
  made the feedback loop useless. A precise one gets fixed in one call.
- **Examples in prompts get copied**, numbers included. Teach the format with
  placeholders, not with real values.

## Lessons

1. **Detect in code, don't hope the agent asks.** "The same tests failed
   twice" is a fact our code can see. The agent's own judgement of "I'm
   stuck" was never used.
2. **Validate outputs in code and send the errors back.** A rejected plan with
   an exact reason is fixed by the PM in one more call.
3. **Prompts lower the rate of a behaviour; they don't remove it.** Layers
   (prompt → code check → arbiter → human) are what make it reliable.
4. **Every incident is a chance to look for the next one.** Watching the
   Developer write scripts revealed a security gap that had been there since
   Phase 1.

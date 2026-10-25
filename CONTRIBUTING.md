# Contributing

The most useful contribution to this repo is **a new finding**.

## Found a route the guard rails miss?

That is the point of the project. Open an issue or a pull request with:

1. **How to reproduce it.** A `redteam.py` scenario is ideal; a few lines
   against `tools.py` is fine too.
2. **What it defeats.** Which rule did it get around, and is the rule a
   *route* rule (blocks one technique) or an *outcome* check (verifies the
   result)? A route that defeats an outcome check is a much bigger finding.
3. **Whether a human would have noticed.** The dangerous ones are silent.

If it holds up, it becomes a case study in [`docs/case-studies/`](docs/case-studies/)
with the logs, the root causes and the fix.

## Running the tests

```bash
venv/bin/python -m pytest -q      # offline, no API key, ~18s
```

Every guard rail needs a test that proves it still works. A fix without one
will be asked for one — case study 003 exists because a rule from 001 had a
hole nobody was testing for.

## Running the red team

```bash
venv/bin/python redteam.py --all
```

This calls a real model, so it costs money and the results vary between runs.
That variation is intentional: **a route nobody has seen before is a new
finding**, not a flaky test. It is deliberately not part of CI — see
[.github/workflows/README.md](.github/workflows/README.md).

## Style

- Comments explain *why*, and name the incident when there was one. Most rules
  in `tools.py` exist because an agent did something specific; that history is
  the most useful thing in the file.
- Prefer an outcome check over a route rule wherever the choice exists.
- A new restriction needs an honest exit beside it. Blocking a route without
  giving the agent a legitimate alternative moves the problem rather than
  fixing it — this repo has re-learned that three times.

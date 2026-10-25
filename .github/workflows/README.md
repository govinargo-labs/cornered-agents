# CI

`security-regressions.yml` runs the offline test suite on every push and pull
request. It needs no API key: the tests use a fake model.

The red-team scenarios (`redteam.py --all`) are deliberately **not** in CI.
They call a real model, so they cost money and their results vary between
runs. A required check that fails at random is a check people learn to ignore
— the same failure mode as a detector that cries wolf (see case study 003).
Run them by hand, or on a schedule, when you change the guard rails.

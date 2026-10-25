# Security

This project **runs code written by a language model on your machine**. That is
its purpose, and it is the main thing to understand before using it.

## What is contained

Agent-written code only ever runs inside a sandbox ([sandbox.py](sandbox.py)):
macOS Seatbelt, or [bubblewrap](https://github.com/containers/bubblewrap) on
Linux. If neither is available, **nothing runs** — the sandbox fails closed
rather than falling back to an unprotected run.

Verified behaviour inside the sandbox:

| | |
|---|---|
| Network | no access at all (`PermissionError`) |
| Your home folder | not readable; on Linux it is replaced entirely |
| Your `.env` and API keys | not present in the environment, not readable |
| Writes | the workspace and a private temp folder only |
| Other projects on disk | not reachable |

Outside the sandbox, the tool layer confines agents to `./workspace`. Path
traversal (`../../etc/passwd`), absolute paths, and writes through a symlink
that points outside the workspace are all refused. Agents cannot touch `.git`,
and git runs with hooks disabled, so nothing an agent writes can execute during
a commit. No command is ever built as a shell string; `shell=True` does not
appear in this repo.

The one command an agent can run is pytest, with a 60-second timeout.

## What is NOT contained

Stated plainly, because these are real:

- **`AGENT_SANDBOX=off` disables all of it.** Only set it inside a throwaway
  container or VM.
- **Code you run yourself is not sandboxed.** When you execute the product the
  agents built (`python workspace/main.py`, `streamlit run ...`), it runs with
  your full user rights. Read it first.
- **macOS Seatbelt leaks file names.** Contents in your home folder are
  blocked; names remain visible. Linux (bubblewrap) does not have this gap.
- **The agents can still waste money.** A stuck retry loop costs API tokens.
  Step limits and retry limits exist, but watch your usage.
- **Known bypasses are documented**, not hidden. See
  [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) § 3 for the current list of
  what gets through, including gaming that can only be detected rather than
  prevented.

## Hosting the web UI

By default the UI listens on `localhost` only. **Exposing it to anyone else
means letting them run AI-written code on your machine, using your API key.**
If you host it, read the "Host the UI" section of the [README](README.md)
first and turn on sign-in.

## Reporting a problem

If you find a way around the guard rails, that is a finding and it is welcome
— see [CONTRIBUTING.md](CONTRIBUTING.md). Open an issue with steps to
reproduce.

For something that puts users of this repo at risk rather than exposing an
agent's behaviour, please open an issue marked **security** rather than
posting a working exploit first.

## Disclaimer

This is an independent research project, provided **"as is" without warranty of
any kind**, as set out in Sections 7 and 8 of the [Apache License 2.0](LICENSE).

You run it at your own risk. The author accepts no liability for any damage,
data loss, cost (including API charges), or other harm arising from its use.
You are responsible for the environment you run it in, for reviewing any code
the agents produce before executing or publishing it, and for complying with
the terms of whichever model provider you use.

Nothing here is a guarantee that the sandbox or any other guard rail cannot be
defeated. The entire point of this repo is that guard rails fail in ways their
authors did not predict.

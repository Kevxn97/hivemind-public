# HiveMind

Local project memory for Codex and Claude. Preserve decisions, their sources and
open work between sessions. Knowledge stays in a separate, readable Markdown
vault with a journal; agents receive bounded context for the selected project.

This is an early personal project. The core runs locally with Python's standard
library. Python 3.11+ and macOS or Linux with SQLite FTS5 are required. Windows
is not supported directly; Linux under WSL is a possible but unverified setup.

## Start

Clone the project and install it into an isolated environment:

```sh
git clone https://github.com/Kevxn97/hivemind-public.git
cd hivemind-public
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/hive init "$HOME/.local/share/hivemind" --project /path/to/your/project
```

The guided setup asks for a vault, first project and agent. Existing content is
never imported or overwritten. You can also use deterministic commands:

```sh
.venv/bin/hive init "$HOME/.local/share/hivemind" --project /path/to/project --name "My Project" --agent codex --yes
.venv/bin/hive self-test
.venv/bin/hive connect codex
.venv/bin/hive connect codex --apply
.venv/bin/hive doctor
```

`connect` previews changes; `--apply` installs them. Use `claude` for Claude Code.
Both session lifecycle hooks and a small instruction block are managed together.
Existing unrelated instructions and hooks are preserved. Codex requires its own
hook review via `/hooks` (or the desktop hook settings) before executing them.
`disconnect AGENT --apply` removes the managed connection.

## Your vault

Choose a vault with `--root PATH`, then `HIVEMIND_HOME`, then the default
`~/.local/share/hivemind`, in that order. Always keep it outside the application.
The distribution contains code, schemas and synthetic tests. It contains no
personal knowledge, operational state, or previous repository history.

Initialization enables no imports or background service. HiveMind itself makes
no model calls in setup, retrieval, journaling or its self-test. Context passed
to a connected agent is subject to that agent host's processing and permissions.
Local storage is private by location and filesystem permissions, not encryption.
Back up the whole vault, including `.history`, privately.

Automatic source sync and background installation are unavailable in this public
preview. The supported path is explicit project context and attributable session
handoffs. Optional Calendar helpers require separate local configuration.

## Agent workflow

```sh
hive --root /path/to/vault session-open /path/to/project --agent codex --json
hive --root /path/to/vault session-context SESSION_ID --task "Current task" --json
hive --root /path/to/vault session-draft SESSION_ID --out /tmp/handoff.json
# Fill the source, decisions, reasons and work changes in the draft.
hive --root /path/to/vault session-close SESSION_ID --file /tmp/handoff.json --json
```

Use the session ID offered by a successful hook instead of opening a duplicate.
If there is no durable update, close with `--no-change`. SessionEnd records an
ended runtime only; it does not invent a summary or successful handoff.
Retrieved context is evidence with a source and freshness limit, never execution
authority. Respect the user's permission for knowledge updates.

`hive contract --json` describes the machine contract. `hive evidence ID@REVISION`
loads precise evidence. `hive browse`, `hive search`, `hive show ID` and the terminal
provide manual access. Use `COMMAND --help` for detailed arguments.

## Verification

`hive self-test --json` runs a synthetic first-memory round trip and writes a
reproducible evidence report outside your vault. `hive doctor --json` inspects
configuration and vault consistency without repairs. Neither proves that a real
agent trusted a hook, used the offered memory, or found it helpful.

For development, run `python3 scripts/verify.py`. It retains outcomes and source
hashes for the existing regression suite and public setup tests. No provider
account or personal dataset is needed.

## License

[MIT](LICENSE) · Copyright 2026 Kevin (Kevxn97).

Source is available in [hivemind-public](https://github.com/Kevxn97/hivemind-public).
The Python package has not been published to a registry; install from this source
tree or a locally built wheel.

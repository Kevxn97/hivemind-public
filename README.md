# HiveMind

Local project memory for Codex and Claude. Preserve decisions, their sources and
open work between sessions. Knowledge stays in a separate, readable Markdown
vault with a journal; agents receive bounded context for the selected project.

Built by Kevin ([Kevxn97](https://github.com/Kevxn97)) as an early personal
project. The core runs locally with Python's standard
library. Python 3.11+ and macOS or Linux with SQLite FTS5 are required. Windows
is not supported directly; Linux under WSL is a possible but unverified setup.

## Start

Clone the project and install it into an isolated environment:

```sh
git clone https://github.com/Kevxn97/hivemind-public.git
cd hivemind-public
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
.venv/bin/hive init "$HOME/.local/share/hivemind" --project /path/to/your/project
```

Keep this environment active for the `hive` commands below so they use this
installation. `deactivate` leaves the environment when you are done.

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

## See it work in two minutes

After installation, run `.venv/bin/hive demo`. It creates a separate synthetic
project, saves a decision, switches sessions, corrects the decision, and checks
the next session's context. No account or model call is needed. The printed paths
lead to a readable transcript and reproducible evidence.

[Replay the demo](docs/DEMO.md) · [Try it with your own agent](docs/TRY_IT.md)

## Stay in control

Run `hive memory` for the guided terminal menu, or press **m** in the main
terminal. Inspect recent memories and their sources, correct the content,
archive an entry, undo a change, or pause incoming updates.

```sh
hive memory list
hive memory show PROJECT_ID
hive memory pause --reason "Review incoming knowledge"
```

Mutations show a preview first. The menu asks before saving; the CLI requires
`--apply --preview TOKEN` with the exact preview token. Corrections reach the
next session. Undo adds a compensating event and retains the earlier journal.
Pausing blocks incoming session handoffs and source captures while retrieval
and deliberate manual corrections remain available. This controls HiveMind;
it does not pause the connected agent or revoke its provider access.

[Memory controls and correction examples](docs/MEMORY.md)

## Optional Gmail, Calendar and MCP sources

```sh
hive sources setup gmail --agent codex
hive sources setup calendar --agent claude
hive sources setup mcp --agent codex --project /path/to/project \
  --name team-knowledge --url https://mcp.example.com/service
```

Gmail and Calendar setup guides you through the host's connection and sign-in
flow. Generic MCP setup previews a project configuration; `--apply` installs
the chosen entry while preserving unrelated settings. Credentials stay with the
host. Completing local setup does not prove provider connectivity.

Ask your connected agent to select one relevant message or event, explain the
durable conclusion and prepare `hive sources capture --file selected.json`.
Review the source and preview before applying it. HiveMind stores the selected
conclusion and provenance as reported knowledge. It does not scan the mailbox
or calendar automatically.

[Source setup, capture template and removal](docs/SOURCES.md)

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
preview. Use explicit project context, attributable session handoffs and selected
source captures. Optional native Calendar helpers require separate local setup.

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

The [CI workflow](.github/workflows/ci.yml) runs source checks and fresh-wheel
acceptance on macOS/Linux with Python 3.11/3.14, retaining failure reports too.
Its configuration is not a claim that a hosted run has already passed. Actual
host trust, OAuth sign-in and usefulness on real tasks need separate validation.
See [release preparation](docs/RELEASE.md) and [changes](CHANGELOG.md).

## License

[MIT](LICENSE) · Copyright 2026 Kevin (Kevxn97).

Source is available in [hivemind-public](https://github.com/Kevxn97/hivemind-public).
The Python package has not been published to a registry; install from this source
tree or a locally built wheel.

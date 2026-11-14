# HiveMind

<img src="assets/branding/hivemind.png" alt="HiveMind: a gold H-shaped honeycomb beside the gold and ivory wordmark" width="480">

Local project memory for Codex and Claude. Preserve decisions, their sources and
open work between sessions. Knowledge stays in a separate, readable Markdown
vault with a journal; agents receive bounded context for the selected project.

Built by Kevin ([Kevxn97](https://github.com/Kevxn97)) as an early personal
project. The core runs locally with Python's standard
library. Python 3.11+ and macOS or Linux with SQLite FTS5 are required. Windows
is not supported directly; Linux under WSL is a possible but unverified setup.

## Start

Download the [source ZIP](https://github.com/Kevxn97/hivemind-public/archive/refs/heads/main.zip),
extract it, and open a terminal in the extracted folder. Start with one command:

```sh
python3 run.py
```

The first run asks for your project folder, Codex or Claude, and one thing to
remember. Review the summary and confirm. The chosen agent connection has its
own short settings preview. Codex users review the hooks with `/hooks`, then
open a fresh session in their project and ask “What do you remember about this
project?”

Your local vault defaults to `~/.local/share/hivemind`. Use `--root PATH` to
choose another location. Keep the downloaded application folder in place while
using its agent hooks. Later runs open the terminal workspace. `setup` returns
to the guided setup and can add another project.

```sh
python3 run.py setup
python3 run.py remember "Use SQLite for the local pilot."
python3 run.py sources
```

Memory and source dialogs show a preview and ask once before saving. A single
project and connected agent are selected automatically. With several projects,
choose one by name or number. Python 3.11+ is the prerequisite for this direct
start. The application uses the standard library.

## Install the `hive` command

For regular use, install the same application in an isolated environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
hive setup
```

The examples below use the installed `hive` command. From the downloaded source
folder, use `python3 run.py` in place of `hive`. `hive connect codex` previews
the managed instructions and hooks, and `--apply` installs them. Use `claude`
for Claude Code. `disconnect AGENT --apply` removes the managed connection.

## See it work in two minutes

Run `python3 run.py demo` from the downloaded source, or `hive demo` after
installation. It creates a separate synthetic
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
hive sources
hive sources setup gmail
hive sources setup calendar
hive sources setup mcp
```

The menu guides you through Gmail, Calendar or a remote MCP server. Gmail and
Calendar setup provides host sign-in steps and a ready-to-paste agent prompt
for your selected project. MCP setup asks for the server name and HTTPS endpoint,
then shows the project settings for confirmation. Credentials stay with the host.

Ask the connected agent to select one relevant message or event and propose a
short memory with its source. After approval, it can save that reviewed selection
with a capture command. You can also enter a summary and source directly through
the menu. Captures retain their source reference, reported evidence and journal.

```sh
hive remember "The pilot covers one team." --yes
hive sources capture --kind gmail --summary "The pilot covers one team." \
  --source "mail:reviewed-message" --yes
```

`--yes` saves the supplied selection in scripts. The existing file-and-token
workflow remains available for detailed agent automation.

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

## Optional automation

Run `hive automation`, or press **b** in the terminal, to configure selected
sources, a Codex profile, background startup and weekly knowledge review. Every
feature starts **off** and can be enabled or disabled separately. The menu shows
the selected vault and previews each change before one confirmation.

1. Select individual local text files or scoped Gmail, Calendar, Wispr or MCP
   sources. Each source can be disabled or removed while keeping its knowledge.
2. For source sync, choose an installed Codex executable and an existing profile.
   Verify login and connector access in that profile first. HiveMind retains its
   model and permissions; sync uses that account and may incur model usage/costs.
3. Enable sync and run it once. Completion requires validated source receipts;
   unavailable connectors remain blocked rather than being marked complete.
4. Enable background startup to run after login, using a separate service for
   each vault on macOS (launchd) or Linux (systemd user service).
5. Optionally enable weekly local review. It prepares up to five evidence-linked
   items for inspection, without model calls or automatically rewriting facts.

The default schedule checks changed local inputs after 120 quiet seconds, with
at least 30 minutes between automatic syncs, and remote sources daily at 09:00.
Weekly review defaults to Sunday at 09:00. Change the times in the menu. Incoming
updates can be paused across handoffs, captures and new automation runs. Disabling
background startup removes only that vault's service. Already running sync jobs
finish independently and retain their results and history.

[Activation, deactivation and agent commands](docs/AUTOMATION.md)

Optional native Calendar helpers require separate local setup. Remote Calendar
sync uses the connected agent's tools and the scope selected by the user.

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

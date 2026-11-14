# Connect a source and save the first useful memory

Start in a terminal with the source menu. Choose Gmail, Google Calendar, a remote
MCP server, or a reviewed memory you already have.

From the downloaded source folder, run `python3 run.py sources`. The examples
below use the installed `hive` command. Source users can replace `hive` with
`python3 run.py` in each example.

```sh
hive sources
```

The capture form shows the project, asks for a short summary and source, and
shows one preview before asking whether to save. A single available project is
selected automatically. With several projects, choose a name or number.
Type `cancel` or `quit` at a prompt to leave without saving. EOF also cancels.

For a quick manual memory, use a short sentence. Review the preview and confirm
in a terminal, or provide `--yes` as explicit consent to save it.

```sh
hive remember "The pilot covers one team and its internal knowledge workflow."
hive remember "The pilot covers one team." --project "Pilot" --yes
```

Manual memories use `user:memory` as their source reference. Override it with
`--source` when a more specific reference is available. Titles are derived from
the summary. A summary may contain up to 280 printable characters.

Use `--root /absolute/path/to/vault` to select a particular vault. Guided source
prompts include the exact selected vault and executable command. With redirected
input or `--json`, capture previews without prompting unless `--yes` is supplied.

## Gmail and Google Calendar

Choose a connected agent and project in the menu, or use a setup command.
HiveMind infers the agent when exactly one has a completed connection record in
the selected vault. It infers a single active internal project. Supply a project
name, cluster or registered folder to select another project.

```sh
hive sources setup gmail
hive sources setup calendar --agent claude --project "Pilot"
```

The output provides host connection steps and a copyable prompt. Paste that
prompt into the selected agent. It asks the agent to retrieve one relevant item,
show a short proposed memory with its source and review time, and wait for your
approval. The agent then runs the displayed capture command with those exact
reviewed values. You can complete this flow without writing JSON or copying
preview tokens.

The host owns sign-in, access scopes, connector tools and credentials. Available
connectors depend on the host, account and workspace. HiveMind reports the host
connection as `unverified`; its local connection record does not establish live
source access.

In a ChatGPT/Codex host that offers plugins, open Plugins, choose Gmail or Google
Calendar, and complete Connect. Review the account and requested access. The
OpenAI walkthrough describes this connection flow. [OpenAI plugin walkthrough](https://developers.openai.com/training/walkthroughs/plugins-and-skills)

For Claude, connect the service at `https://claude.ai/customize/connectors`.
Claude Code subscription sessions can receive those connectors and show them in
`/mcp`. Gmail and Google Calendar authentication belongs to the claude.ai
connector flow. API-key sessions and organization controls can affect
availability. [Claude Google Workspace connectors](https://support.claude.com/en/articles/10166901-use-google-workspace-connectors), [Claude Code connector setup](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-claudeai)

Keep private message bodies and credentials in their original systems. Save the
durable conclusion you reviewed with a stable source reference.

## Connect a remote MCP server to one project

Use an existing, registered project folder and an endpoint you have chosen.
The terminal flow asks for the server name and HTTPS endpoint, shows its local
configuration, and asks for confirmation.

```sh
hive sources setup mcp
```

The agent and project can be inferred when each has one configured choice.
Supply them explicitly when needed. Noninteractive setup previews by default;
use `--apply` to apply the displayed project settings.

```sh
hive sources setup mcp --agent codex \
  --project /absolute/path/to/project \
  --name team-knowledge --url https://mcp.example.com/service
```

The Codex entry is appended to the project's `.codex/config.toml`. Codex supports
project configuration in trusted projects and handles server authentication in
the host. [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)

Use `--agent claude` for a project `.mcp.json` entry with the HTTP transport.
Start Claude Code in that project, review project trust and MCP settings, then
complete host authentication where supported. [Claude Code project MCP configuration](https://code.claude.com/docs/en/mcp#project-scope)

HiveMind accepts an HTTPS endpoint with a server path. Supply a URL without user
info, query parameters or a fragment. Keep access tokens in host-managed
credentials. Setup and removal run locally. Use `/mcp` in the agent to check
connectivity and authentication.

Existing host settings are preserved. Codex comments and unrelated text are
preserved byte-for-byte. Claude JSON retains unrelated values with normalized
formatting. Ownership lives in the selected vault's `.state/source-connections/`
directory. Repeating the same setup is idempotent. An existing server name owned
elsewhere, malformed configuration, a changed managed entry, or interrupted
ownership state requires review before another write.

Remove an owned entry through the same project selection. In a terminal, review
and confirm; in a noninteractive script, add `--apply` after reviewing a preview.

```sh
hive sources remove --name team-knowledge
```

Removal clears the owned server entry and its HiveMind ownership record. Host
credentials remain managed through the host's sign-out controls. To change an
endpoint, remove the owned entry first, then preview the new setup. Source setup
and capture honor the selected vault's paused state.

## Capture through command flags

Supply one reviewed selection directly, including its stable reference. The
observation timestamp defaults to the current time. Set it explicitly when
replaying the same selection or using a preview token across separate calls.

```sh
hive sources capture --project "Pilot" --kind gmail \
  --title "Pilot scope agreed" \
  --summary "The pilot covers one team and its internal knowledge workflow." \
  --source "https://mail.google.com/mail/u/0/#inbox/SELECTED_MESSAGE_ID" \
  --observed-at "2026-11-14T09:00:00Z" --yes
```

`--kind` accepts `gmail`, `calendar`, `mcp` or `manual` and defaults to `manual`.
The source reference is required for connector content. A manual selection
defaults to `user:memory`. The project must be active and internal in the selected
vault. The capture is a draft lesson with `reported` evidence, a source reference
and a journal event. Assess the summary according to that evidence status.

`--yes` authorizes the exact assembled selection. HiveMind validates it, binds
its project and note revisions, and applies it through the normal journal writer.
A concurrent project or note change prevents the write. An identical selection,
including its observation timestamp, returns `already_captured`. A changed
existing capture requires review and is preserved.

## Existing JSON and exact-preview workflows

File capture remains available for agents and scripts. Save one JSON object in
`selected.json` with exactly these fields and no more than 32 KiB overall.

```json
{
  "project": "projects/your-project",
  "title": "Pilot scope agreed",
  "summary": "The pilot covers one team and its internal knowledge workflow.",
  "source": {
    "kind": "gmail",
    "resource": "https://mail.google.com/mail/u/0/#inbox/SELECTED_MESSAGE_ID",
    "title": "Selected pilot scope email",
    "observed_at": "2026-11-14T09:00:00Z"
  }
}
```

Preview and apply the same file using its returned token.

```sh
hive sources capture --file selected.json
hive sources capture --file selected.json --apply --preview RETURNED_TOKEN
```

The token binds the file's exact bytes, project revision, capture-note revision
and selected vault. A changed file or project requires a fresh preview. `--file`
cannot be mixed with flags that assemble another selection. Explicit `--yes`
consent also works for a selected file.

Use `--json` for structured output, including `capture_template`, `agent_prompt`,
`capture_command`, `preview`, `note`, `source_state` and configuration targets.

Check the result through normal retrieval.

```sh
hive brief /absolute/path/to/project
hive show SOURCE_NOTE_ID
hive check
```

## Verification scope

The source suites use temporary synthetic homes, project configs, fictional
summaries and credential-free subprocess environments. Real PTYs exercise the
source menu, forms, confirmations, cancellation and EOF. CLI cases check project
and agent inference, ambiguity, explicit consent, validation, preview guards,
pause, replay, preservation and provenance. Existing source tests cover MCP
ownership, drift, removal and revision conflicts.

The suites run from a checkout or with `HIVEMIND_TEST_PYTHON` pointing to an
independently installed wheel environment. Every run saves commands, outcomes,
source hashes and synthetic environment details through `scripts/verify.py`.
Host sign-in and live provider behavior require verification in the selected host.

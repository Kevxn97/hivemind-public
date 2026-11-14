# Connect a source and capture a selected update

HiveMind uses the agent host for Gmail, Google Calendar and MCP authentication.
Start with the guided command output.

```sh
hive sources
hive sources setup gmail --agent codex
hive sources setup calendar --agent claude
```

The setup output includes host instructions, official documentation and a JSON
capture template. The host owns account sign-in, access scopes, connector tools
and credentials. Available connectors depend on the host, account and workspace.

## Gmail and Google Calendar

In a ChatGPT/Codex host that offers plugins, open Plugins, choose Gmail or Google
Calendar, and complete Connect. Review the account and requested access. The
OpenAI walkthrough describes this connection flow. [OpenAI plugin walkthrough](https://developers.openai.com/training/walkthroughs/plugins-and-skills)

For Claude, connect the service at `https://claude.ai/customize/connectors`.
Claude Code subscription sessions can receive those connectors and show them in
`/mcp`. Gmail and Google Calendar authentication belongs to the claude.ai
connector flow. API-key sessions and organization controls can affect
availability. [Claude Google Workspace connectors](https://support.claude.com/en/articles/10166901-use-google-workspace-connectors), [Claude Code connector setup](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-claudeai)

Ask the host for one specific message or event relevant to the chosen project.
Review its contents and source link. Write the durable conclusion into the
capture template, keeping private message bodies and credentials in their
original systems.

## Connect a remote MCP server to one project

Use an existing, registered project folder and a server endpoint you have chosen.
Preview its local configuration.

```sh
hive sources setup mcp --agent codex \
  --project /absolute/path/to/project \
  --name team-knowledge --url https://mcp.example.com/service
```

Review the displayed config path and entry, then repeat with `--apply`.
The Codex entry is appended to the project's `.codex/config.toml`. Codex supports
project configuration in trusted projects and handles server authentication in
the host. [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)

Use `--agent claude` for a project `.mcp.json` entry with the HTTP transport.
Start Claude Code in that project, review project trust and MCP settings, then
complete host authentication where supported. [Claude Code project MCP configuration](https://code.claude.com/docs/en/mcp#project-scope)

HiveMind accepts an HTTPS endpoint with a server path. Supply a URL without user
info, query parameters or a fragment. Keep access tokens in host-managed
credentials. Setup and removal run locally and report the host connection as
`unverified`. Use `/mcp` in the agent to check connectivity and authentication.

Existing host settings are preserved. Codex comments and unrelated text are
preserved byte-for-byte. Claude JSON retains unrelated values with normalized
formatting. Ownership lives in the selected vault's `.state/source-connections/`
directory. Repeating the same setup is idempotent. An existing server name owned
elsewhere, malformed configuration, a changed managed entry, or interrupted
ownership state requires review before another write.

Preview removal of the owned entry, then repeat with `--apply`.

```sh
hive sources remove --agent codex \
  --project /absolute/path/to/project --name team-knowledge
```

Removal clears the owned server entry and its HiveMind ownership record. Host
credentials remain managed through the host's sign-out controls. To change an
endpoint, remove the owned entry first, then preview the new setup. Source setup
and capture honor the selected vault's paused state.

## Capture one reviewed selection

Save a JSON object in `selected.json`. Use the source's stable reference and the
time at which you reviewed it.

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

`kind` accepts `gmail`, `calendar` or `mcp`. The selected project must be active
and internal in the chosen vault. The JSON object contains exactly these fields,
with one source, at most 32 KiB overall and a summary of at most 280 characters.
The selection is stored as a draft lesson in that project, with `reported`
evidence and its source reference. Its summary remains source content and must
be assessed according to its evidence status.

Preview the complete selection.

```sh
hive sources capture --file selected.json
```

Review the title, summary, project, source and returned preview token. Apply that
exact preview using the returned token.

```sh
hive sources capture --file selected.json --apply --preview RETURNED_TOKEN
```

The token binds the selected file's exact bytes, project revision, capture-note
revision and selected vault. A changed file or project requires a fresh preview.
The canonical write uses HiveMind's revision-checked journal. Repeating an
identical selection returns `already_captured` and keeps the existing note.

Use `--root /absolute/path/to/vault` on every command when working with a vault
other than the default. Use `--json` for structured output, including the
`capture_template`, `preview`, `note`, `source_state` and configuration target.

Check the result through normal retrieval.

```sh
hive brief /absolute/path/to/project
hive show SOURCE_NOTE_ID
hive check
```

## Verification scope

The source-connection E2E suite uses temporary synthetic homes, project configs,
fictional selected summaries and a credential-free subprocess environment. It
checks preview, install, removal, preservation, drift, validation, capture,
revision conflicts, replay and provenance through the public CLI. The suite runs
from a checkout or with `HIVEMIND_TEST_PYTHON` pointing to an independently
installed wheel environment. Host sign-in and live provider behavior require
verification in the selected host.

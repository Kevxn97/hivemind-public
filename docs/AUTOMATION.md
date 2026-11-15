# Optional automation

Open `hive automation`, or press **b** in the TUI. The header identifies the vault.
The controls use the same terminal layout, gold accents and keyboard navigation
as the main views. Choose **1–9** for an action, **Enter** to continue through
fields, **y** to confirm or **Esc** to cancel. No change is saved on cancellation.
New vaults start with every feature off. Upgrades preserve existing selections;
provider registration and service migration are described below.
`hive automation status --json` is a read-only configuration/runtime snapshot.

## Guided setup

Select one registered project and one existing text file. An optional exact
Markdown heading limits the file to that section. Source selection reads only
that bounded file and saves its path, not a directory import. Canonical notes,
history, configuration and state inside the vault cannot be selected as inputs.
Files over the content limit or with a missing selected heading fail visibly.

For Gmail, Calendar, Wispr or MCP, connect the source in the Codex profile you
intend to use, then describe what HiveMind may read. Select the initial window
(1–31 days). Follow-up runs use verified checkpoints with an overlap. Remote
availability is **unverified** until an actual run produces valid coverage
receipts. Credentials remain with the host. Project-only MCP configuration from
`hive sources setup mcp` must also be available to the profile used for background
runs, which use a private temporary working folder outside the vault. No connector is automatically installed
or granted access by selecting a source.

Choose the installed Codex executable and its existing profile directory. Both
must be outside the vault. Explicit confirmation records their resolved paths
and an executable SHA-256 under `~/.config/hivemind/providers/`, outside the vault.
Changing `config/features.json` cannot authorize a different executable or profile.
An executable update requires a fresh provider preview and confirmation.

The selected login and model remain unchanged. Each sync overrides only its own
execution policy: read-only local commands, no approval escalation, and no loaded
command-allow rules. The installed CLI must support these options; incompatible
or managed-disallowed settings fail instead of falling back to broader access.
No global profile files are edited. Use a dedicated profile for connector access.
Connector tools have their own permissions: the shell sandbox does **not** make
all MCP/app tools read-only or enforce mailbox filters. Configure only the read
tools and account scope you intend to allow; remote coverage receipts are reports
from the provider, not an independent proof that its account scope was obeyed.

The native runner prepares the selected documents and relevant non-personal
project notes in a private temporary directory. The agent returns a bounded JSON
proposal; it does not write the vault. HiveMind validates note schema, selected
project, exact citations, current revisions and source receipts, then applies
accepted note changes through the journal. Extra execution fields, invented
workflow/verification metadata and raw receipt payload fields are rejected.
Temporary inputs and proposals are removed after normal or handled-failure exit;
forced process/host termination can leave a temporary directory for local cleanup.

Enable source sync after reviewing the model-use notice, then choose **Run source
sync once**. This starts a bounded Codex job using that profile. It may consume
the account's allowance or cost money. Source and scope limits are in its manifest;
failed, incomplete or blocked sources retain their last successful checkpoint.
Agent exit alone is never enough to claim a completed sync. No transcript or raw provider output is retained in daemon state or logs. Store
only attributable summaries. One changed/unavailable local source leaves its
checkpoint unchanged; independent valid sources can still complete.

Enable **background startup** to create a user service tied to this vault and
installation. macOS uses launchd; Linux needs a working `systemd --user` session.
No administrator privileges, system service or Linux lingering are configured.
Keep the application installation in place while the service is installed.

Enable **weekly review** independently of source sync. This local pass creates
`.state/maintenance.json` with up to five candidates, exact note revisions and
reasons such as changed sources, disputed claims or overdue checks. It requires
no Codex account. Read it with `hive automation review` and use the existing
memory/verification commands for deliberate, journaled corrections. It never
marks claims verified or moves review dates just to empty the queue. Source scans
are bounded and continue across weekly passes; personal-sensitivity notes remain
outside the project-review interface.

## Pause, disable and remove

- **Pause incoming updates** prevents new handoffs, captures, sync and weekly
  review. Retrieval and deliberate manual corrections remain available.
- Disable **source sync** to stop future Codex dispatches. Weekly local review
  can continue independently.
- Disable **weekly review** to stop future review reports.
- Disable **background startup** to stop and remove this vault's user service.
  Per-vault names and ownership receipts prevent changes to another vault or the
  legacy personal HiveMind service. Changed or unowned service files need review.
- Disable/remove a selected source to exclude it from future plans. Previously
  acquired knowledge and source checkpoints are retained. This does not revoke
  provider permissions; remove the connection through the agent host for that.

Already running sync jobs finish independently. Their canonical writes and source
receipts retain normal integrity checks. Disabling is not a destructive rollback.

## Agent/CLI use

From a source checkout, substitute `python3 run.py` for `hive`. Always use `--root`
for an isolated test vault. These commands return a preview without changing data.

```sh
hive --root /path/to/vault automation source add notes \
  --path /path/to/project/notes.md --project projects/my-project
hive --root /path/to/vault automation source add gmail \
  --remote gmail --scope "Messages about the selected pilot" \
  --project projects/my-project --lookback-days 7
hive --root /path/to/vault automation provider \
  --codex /path/to/codex --agent-home /path/to/dedicated-profile
hive --root /path/to/vault automation enable sync
hive --root /path/to/vault automation run sync
hive --root /path/to/vault automation enable maintenance
hive --root /path/to/vault automation enable background
hive --root /path/to/vault automation schedule --daily-hour 10 \
  --maintenance-day 6 --maintenance-hour 11
hive --root /path/to/vault automation source disable gmail
hive --root /path/to/vault automation disable background
```

Repeat the exact command with `--apply --preview TOKEN` from its response to apply.
A changed configuration invalidates the token. The interactive menu asks once
instead. `hive sync`, `hive start` and `hive stop` lead to the same reviewed actions.
Status distinguishes enabled, installed and currently running. The daily/weekly
schedule catches up after login; sleep does not trigger a flood of missed runs.
Failed source jobs back off for at least an hour.

## Verification scope

`scripts/verify.py --pattern test_automation_e2e.py` exercises fresh/upgrade-safe
defaults, cancellation, stale previews, source selection, actual worker/guard
subprocesses with a synthetic provider, pause, bounded review and both service
formats with a recording service-manager boundary. It stores reproducible
evidence. This does not prove any real login, OAuth connector or model output.

Codex runtime flags are documented in the official
[developer commands](https://learn.chatgpt.com/docs/developer-commands#codex-exec).

## Upgrading an existing automation setup

Existing mutable vault configuration is not automatically trusted. Before the
next source run, disable sync, preview and confirm the provider again, then
re-enable sync. Knowledge, source choices and checkpoints remain in place.
A malformed old approval blocks execution but does not block this replacement.

```sh
hive automation disable sync
hive automation provider --codex /path/to/codex --agent-home /path/to/profile
hive automation enable sync
```

Confirm each preview separately with its exact `--apply --preview TOKEN`, or use
the interactive automation menu. Registration does not start a provider run.

After a package upgrade, run `hive automation status`. An owned older login
service is flagged with `needs_update`; preview and apply `hive automation enable
background` to reinstall it. This preserves the service identity and scheduler
settings. Updated services use the trusted installation directory, and installed
Python entrypoints use safe-path mode, so a Python file in the vault cannot shadow
the installed package. An existing service is not silently rewritten by opening
status; perform this migration before relying on unattended startup again.

The selected model/profile may still allow broad reads or connector actions.
This release enforces the **local command write boundary**, validates the data
that HiveMind applies, and pins the native executable. It does not claim full
account isolation or semantic resistance to every malicious source instruction.

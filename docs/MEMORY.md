# Memory controls

`hive memory` opens a simple management menu in a terminal. Press `m` in the
existing terminal workspace to open the same menu. Piped and JSON invocations
return the recent-memory list.

```sh
hive memory list
hive memory list --history --limit 50 --cluster projects/my-project
hive memory show my-project
hive memory status --json
```

The list shows changed time, producer and source references. `show` includes the
complete current note and its journal history with actor, reason, context/source
reference and revision hashes. Archived memories remain available through
`show`, journal history and `list --history`.

## Correct a false decision

Prepare the full corrected text in a UTF-8 file. Correcting the summary also
requires the complete body so that the old assertion leaves the current prose.
Run the command once to inspect its preview, then repeat it with the returned
preview token and `--apply`.

```sh
hive memory edit my-project --summary 'Use SQLite for the local pilot.' \
  --body-file corrected-project.md --source user:reviewed-project-decision \
  --reason 'Correct the earlier database decision' --json

hive memory edit my-project --summary 'Use SQLite for the local pilot.' \
  --body-file corrected-project.md --source user:reviewed-project-decision \
  --reason 'Correct the earlier database decision' \
  --apply --preview TOKEN --json
```

`--body TEXT` accepts inline content and `--title TEXT` changes the title.
`--actor` defaults to `user/memory`. The preview binds the exact input and current
revision. A changed note, changed file, or changed request requires a fresh
preview. Applying also rechecks the canonical revision under the write lock.

A content correction clears the note's structured claims, experience, handoff
fields and verification records. The preview and applied response identify
these cleared fields. Existing independent work items are preserved, including
migration of legacy handoff next steps into canonical work items. Review the
complete body and retained work items when correcting related decisions.
Corrections attach a labelled source and carry `reported` evidence. Earlier
sources and every earlier note revision remain in the canonical journal.
A fresh agent session receives the corrected active content.

## Check a correction in the next session

To follow a correction, open `hive memory`, choose **9 Correction checks**, then
**a Add**. Enter the ID of a project memory you corrected with `memory edit`.
Review its exact revision and source, enter a typical follow-up question, and
choose Codex or Claude. Confirm to start the check. You can also use the CLI
with the revision from `hive memory show NOTE_ID`.

```sh
hive memory check add NOTE_ID@REVISION --agent claude \
  --question "How many times should the importer retry?"
hive memory check list
hive memory check show CHECK_ID
```

Open a fresh session for the chosen agent in the same project and ask your
saved question. HiveMind records the revision and source label in the startup
context it offered. A startup with the saved question records a startup offer.
A separate matching context refresh fills the follow-up entry. Saving your
review fixes both observations for that session. A startup with an unrelated
explicit task is skipped. A matching question ignores capitalization and extra
whitespace.
Saving a check does not change which memories are selected for the agent.

Use **s Show** to find the session ID. After reading the actual answer, choose
**r Review answer** to record whether the agent used the correction and whether
you had to correct it again. You can leave the review pending. HiveMind does
not read conversations or infer either answer from delivery.

```sh
hive memory check review CHECK_ID --session SESSION_ID --used yes --corrected-again no
hive memory check stop CHECK_ID
```

The check keeps its expected revision when the note changes again. Its report
shows the revision and source that actually appeared, including a missing
offer. Stopping a check keeps its existing observations in your local vault.

## Archive and undo

```sh
hive memory archive my-project --reason 'Outside the current project scope' --json
hive memory archive my-project --reason 'Outside the current project scope' \
  --apply --preview TOKEN --json

hive memory undo EVENT_ID --reason 'Restore the prior decision' --json
hive memory undo EVENT_ID --reason 'Restore the prior decision' \
  --apply --preview TOKEN --json
```

Archiving removes a note from active context. Undo creates a compensating journal
event with the prior content. Undoing creation archives the note. An undo is
accepted while all affected notes still have the target event's exact revision.
If subsequent edits exist, inspect their history and prepare a current correction.
The journal always retains the original event and the compensation.

## Pause and resume incoming knowledge

```sh
hive memory pause --reason 'Review incoming knowledge' --json
hive memory pause --reason 'Review incoming knowledge' --apply --preview TOKEN --json
hive memory resume --reason 'Review completed' --json
hive memory resume --reason 'Review completed' --apply --preview TOKEN --json
```

Pause gates incoming session handoffs and source-import writes. Reading context,
closing a session without a knowledge change and explicit manual corrections
remain available. A paused handoff stays retryable. If a manual correction
changed its target revision, draft a fresh handoff before retrying.

The processing state is local at `.state/automation.json`, with actor, time,
reason and revision. The processing switch and incoming knowledge commits use
the canonical vault lock, so a completed pause prevents subsequent incoming
commits until resume. Ordinary explicit draft/apply remains a manual write path.

## Agent memories

Once the actual task is known, use `session-context SESSION_ID --task TASK` to
retrieve matching memories alongside current project context. Use the same agent
identity for each session. The installed Codex and Claude startup adapters use
`codex` and `claude`. CLI callers can supply their own stable name to
`session-open --agent`.

```bash
hive agent-memory remember SESSION_ID \
  --topic "SQLite migration" \
  --attempt "Copied the database while its journal was still active" \
  --result "The copied fixture missed the last write" \
  --when "Migrating a vault with an active SQLite journal"
hive session-context NEXT_SESSION_ID --task "Verify the SQLite migration"
```

An agent saves a short observation from its own active session. The memory is
scoped to that agent and project. Repeating the same write returns the same
memory ID. Treat observations as context and verify them where the task requires
it. For a lesson useful to other agents, write an attributable local evidence
file under `sources/` or `INBOX/`, then explicitly share it.

```bash
hive agent-memory share SESSION_ID MEMORY_ID --source sources/migration-report.md
hive session-feedback NEXT_SESSION_ID --outcome helpful --used-memory MEMORY_ID
```

Agents in the same project can retrieve the shared observation as a proposed
lesson while the source matches its recorded hash. The hash establishes source
identity. Review the source for what its contents support. When useful, curate
a durable project lesson through the existing note workflow.

`memory_refs` lists the memories actually included in the current response.
`session-status` tracks the accumulated `memory_offered` IDs. Feedback accepts
only offered IDs. The latest explicit rating per memory and episode contributes
one signal for that agent, after task relevance. Restoring a previous rating
records a new report. Repeating the same feedback while its ratings are current
reuses its receipt. Both views respect the word budget. The compact view also
respects the UTF-8 byte cap.

Memories live in `.state/agent-memory.sqlite3` and the feedback identifiers live
in local session receipts. Both stay on the current device. Agent names scope
retrieval within a vault shared by callers with local filesystem access.

The incoming processing pause applies to saving and sharing agent memories.
Reading existing context remains available during a pause.

## Agent integration

`memory_control.configure(sub)` and `execute(args, root)` provide the CLI adapter.
Every result includes human-readable `text`, while JSON exposes complete note,
revision, source and event fields. Mutation previews have `status="preview"`,
`preview`, `changes` and `cleared`. Applied mutations have `status="applied"`
and the canonical `event` when knowledge changed.

Incoming writers call `hive.apply(..., respect_pause=True)`. The default is
`False` for explicit manual operations. `automation.require_running(root)` must
be called inside the canonical lock when integrating another incoming write
path. `automation.status(root)` returns `paused` and `revision`.
`automation.pause` and `automation.resume` require keyword arguments `actor`,
`reason` and `expected_revision`.

An apply payload can include `expected_revisions`, a mapping of existing note IDs
to SHA-256 revisions. These read preconditions are checked under the write lock
before any change. Source imports use this to bind project context while writing
a separate captured note.

## External editor changes

A manual edit to an existing canonical Markdown note is detected by `hive check`,
`hive doctor` and the agent startup hook. The hook still allows the session to
start, but explicitly reports unavailable context instead of silently supplying
an empty memory. Use the selected vault (`--root /path/to/vault`) when needed.

```sh
hive adopt NOTE_ID --actor USER --reason "Reviewed editor correction" --context "source:reference"
# Review the exact diff and returned token, then repeat:
hive adopt NOTE_ID --actor USER --reason "Reviewed editor correction" --context "source:reference" --apply --preview TOKEN
hive check
```

The preview is bound to the vault, note bytes, journal revision and supplied
provenance. Changing any of these requires a fresh preview. Adoption preserves
exact UTF-8 bytes, including line endings, and appends an attributable journal
event. It does not silently adopt other edited notes. Resolve each reported note
separately. A moved or deleted note needs its canonical location restored first;
ID, type and creation time cannot be changed through adoption. Invalid metadata,
symlink paths and pending transactions are refused. Preserve a pending transaction
and use `hive recover`; a later conflicting editor save is never overwritten.

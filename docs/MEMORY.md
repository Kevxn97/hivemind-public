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

# Dreaming for repeated corrections

Dreaming is off until you approve a rule for a project in your local vault.
When a task matches, HiveMind can include one complete tentative hint with its
conditions, exceptions, uncertainty, and evidence references. Approval and
retrieval use the local CLI. They create no model calls or scheduled jobs.

## Prepare two corrections

Choose two distinct occasions when someone actually corrected the same mistake.
Use internal lesson or decision notes in the project, with a supported
`experience` and a confirmed `correction` observation for each bound source.
Attach a readable text source under the vault's `sources/` or `INBOX/` and
include its SHA-256 as `sha256` in the note. Use the source's actual event ID
as `origin_event_id` when available. Save changes through the existing journal
workflow. The command checks that each reference points to the current exact
revision and that its source hash matches. Copied source bytes, a repeated event
ID, or the same recorded observation count as one correction. The approver
checks that the two events concern the same mistake and support the statement.

## Approve a rule

For this fictional bakery, the rule reminds the agent to include temperature
units on a recipe card. Replace the example revision placeholders and source
IDs with actual local references from `hive show NOTE_ID`. Use two to four
sources, a concrete task type, and conditions that describe the narrow use case.

```json
{
  "version": 1,
  "id": "explicit-oven-units",
  "project": "projects/fictional-bakery",
  "statement": "Print the temperature unit next to every oven setting on the recipe card.",
  "uncertainty": "Only two fictional recipe cards have been reviewed.",
  "when": {"task_kind": "recipe-card"},
  "unless": [{"format": "legacy"}],
  "sources": [
    {"ref": "first-correction@REVISION_SHA256", "source_id": "first-correction"},
    {"ref": "second-correction@REVISION_SHA256", "source_id": "second-correction"}
  ]
}
```

```sh
hive dreaming status projects/fictional-bakery
hive dreaming enable --file /path/to/rule.json --actor local/operator \
  --reason 'Reviewed two separate recipe-card corrections'
```

This saves the approved rule in the selected vault's local `.state/dreaming/`
directory. Each project starts with an empty registry. The fictional example
here and the automated test fixtures activate no rules in your vault.

## Retrieve or remove a rule

```sh
hive brief projects/fictional-bakery --task 'Write a recipe card' \
  --task-kind recipe-card --task-attr format=printed
hive session-context SESSION_ID --task 'Write a recipe card' \
  --task-kind recipe-card --task-attr format=printed --json
hive dreaming disable projects/fictional-bakery explicit-oven-units
```

You can supply the same flags at `session-open`. Attributes are exact matches,
and the project comes from the brief or session. Every `when` entry must match.
Every exception clause must have at least one known mismatch. In the example,
`format=legacy` or an unknown format withholds the rule. Pass
`--override-rule RULE_ID` to withhold an otherwise matching rule when current
instructions differ. Free-text `--task` is the ordinary context search query.

Matching reads recheck source hashes and pinned revisions. A changed or missing
source withholds its rule. Review the updated evidence before reapproving. A
complete rule must fit with the ordinary project context within the word and,
for compact sessions, UTF-8 byte limits. When a rule is delivered, JSON includes
`dreaming.offered_rule_ids` (inside `context` in the full session view). Inspect
its references with `hive evidence NOTE_ID@REVISION --source SOURCE_ID`.

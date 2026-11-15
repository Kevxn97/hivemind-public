# Codex → Claude → Codex continuity pilot

This protocol uses seven tasks in one fictional sandbox-importer project. It
tests whether fresh host sessions carry forward the right decisions, respect a
correction and require fewer repeated explanations from the operator. Use
[the pilot fixture](../scripts/fixtures/continuity-pilot.json) for the exact
starting evidence, prompts, rescue cards and scoring checklist.

The fictional project is a candidate for a controlled live-agent pilot.
Evidence about everyday benefit would require an agreed real project, seven
ordinary tasks from that project and permission to share the selected context
with both hosts. No such project or prompts are assumed here.

## Prepare the two workspaces locally

From the source checkout, create a disposable workspace. This runs local
Python and HiveMind commands and saves the fixture specification and starting
revisions under `$pilot_root/evidence`.

```sh
pilot_repo="$(pwd -P)"
pilot_root="$(mktemp -d "${TMPDIR:-/tmp}/hivemind-continuity.XXXXXXXX")"
export pilot_repo pilot_root
python3 -B - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

repo = Path(os.environ["pilot_repo"])
root = Path(os.environ["pilot_root"]).resolve()
spec_path = repo / "scripts/fixtures/continuity-pilot.json"
spec = json.loads(spec_path.read_text())
fixture = spec["fixture"]
evidence = root / "evidence"
evidence.mkdir()
(evidence / "spec.json").write_bytes(spec_path.read_bytes())
for arm in ("baseline", "hivemind"):
    workspace = root / arm / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text(fixture["readme"])

baseline = root / "baseline/workspace"
(baseline / "HANDOFF.md").write_text(
    "# Current project handoff\n\n"
    + fixture["source_files"]["sources/overview.md"].removeprefix("# Sandbox importer scope\n\n")
    + "\n" + fixture["source_files"]["sources/storage-original.md"]
    + "\nThe withdrawn hosted proposal remains in sources/hosted-draft.md.\n"
)
vault = root / "hivemind/vault"
workspace = root / "hivemind/workspace"
setup = [sys.executable, "-B", str(repo / "run.py"), "--root", str(vault),
         "setup", "--project", str(workspace), "--name", spec["project_name"],
         "--agent", "none", "--yes", "--json"]
subprocess.run(setup, check=True, cwd=workspace)
for resource, content in fixture["source_files"].items():
    for destination in (baseline, vault):
        path = destination / resource
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

cli = [sys.executable, "-B", str(repo / "hivemind/hive.py"), "--root", str(vault)]
project_id = spec["cluster"].split("/")[-1]
original = json.loads(subprocess.check_output([*cli, "show", project_id]))
changes = []
for row in fixture["notes"]:
    source = vault / row["resource"]
    note = {
        **{key: row[key] for key in ("id", "type", "title", "summary", "status")},
        "cluster": spec["cluster"], "body": source.read_text(),
        "evidence": "reported", "sensitivity": "internal",
        "aliases": [], "tags": [], "links": [],
        "sources": [{
            "id": row["source_id"], "resource": row["resource"], "title": row["title"],
            "observed_at": fixture["observed_at"], "sha256": hashlib.sha256(source.read_bytes()).hexdigest()
        }],
        "verified": [], "stale_after": "2100-01-01T00:00:00Z"
    }
    if row["id"] != project_id:
        note["links"].append({"relation": "about", "target": project_id})
    changes.append({"note": note,
                    "expected_revision": original["revision"] if row["id"] == project_id else "new"})
seed = evidence / "seed.json"
seed.write_text(json.dumps({"changes": changes}, indent=2) + "\n")
for filename, args in (
    ("seed-receipt.json", ["apply", str(seed), "--actor", "pilot-fixture",
                           "--reason", "Seed synthetic pilot", "--context", "fixture:continuity-pilot"]),
    ("starting-check.json", ["check"])
):
    (evidence / filename).write_bytes(subprocess.check_output([*cli, *args]))
for row in fixture["notes"]:
    (evidence / f"starting-{row['id']}.json").write_bytes(
        subprocess.check_output([*cli, "show", row["id"]])
    )
inputs = [spec_path, *sorted((repo / "hivemind").glob("*.py")), repo / "run.py"]
files = [*sorted(baseline.rglob("*")), workspace / "README.md"]
(evidence / "starting-hashes.json").write_text(json.dumps({
    "repo": {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
    "project_files": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in files if p.is_file()}
}, indent=2) + "\n")
print("Prepared", root)
PY
```

Keep the fixture and answer key with the operator. Each agent should only have
access to its own project workspace and the tools and memory for its arm. Use a
fresh vault and independent test profiles before any rerun.

### Optional offline payload comparison

The existing comparison tool can verify local snapshot and retrieval wiring.
Run it before either live arm changes the fixture.

```sh
python3 -B scripts/compare_agent_context.py prepare \
  --root "$pilot_root/hivemind/vault" --code-root "$pilot_repo/hivemind" \
  --spec "$pilot_repo/scripts/fixtures/continuity-pilot.json" \
  --out "$pilot_root/evidence/offline"
python3 -B scripts/compare_agent_context.py report "$pilot_root/evidence/offline"
```

This compares full and compact CLI offers from the same frozen starting
revision. Each question starts from that revision, including questions whose
live answers depend on later tasks. The live controls, task order and answer
scoring below apply to the continuity experiment. CLI bytes measure payload
size. Provider token usage, cost and real answering behavior need their own
observations.

## Run after the operator approves host use

Use the same selected Codex and Claude models, access mode and tool permissions
in both arms. Set up independent, authorized test profiles for each host and
arm. The baseline uses `README.md`, `HANDOFF.md` and the original local source
files. Tell baseline agents that `HANDOFF.md` is available for normal project
continuity. They may read it and update it during authorized handoff tasks.
The HiveMind arm uses the same project facts in the selected vault and the
normal startup and handoff workflow.

Preview the connections by replacing `HOST` and `PROFILE_DIR` below with the
host name and the corresponding HiveMind-arm profile directory. The profiles
used for the baseline should have no HiveMind connection.

```sh
python3 "$pilot_repo/run.py" --root "$pilot_root/hivemind/vault" \
  connect HOST --agent-home PROFILE_DIR --json
```

Once live execution and the two connection targets are approved, repeat that
command with `--apply` for `codex` and `claude`. Launch each host using the
reviewed profile, approve the local hooks if the host requires it and record
the observed host/model versions. Verify the HiveMind context offer at each
fresh start and save its session ID. If the offer is missing, log the failure
before any manual continuation. Record manual recovery as a protocol deviation.

Seal the fixture before either arm starts. Decide arm order by coin toss and
record it, then run one arm end to end before starting the other. Within each
arm use these three fresh conversations. Deliver the JSON prompts verbatim in
order. Start Claude and the second Codex conversation without chat history or
copied answers from the prior conversation.

| Conversation | Tasks | State to inspect after the conversation |
| --- | --- | --- |
| Codex 1 | 01 original storage, 02 save retry decision | Capture the new retry decision, rationale and source. Verify the session saved and closed. |
| Claude 2 | 03 recall retries, 04 correct storage, 05 abstain and save open question | Verify the current storage correction, preservation of its history and the saved open question. |
| Codex 3 | 06 recall correction in German, 07 recall retries and production status | Check that the final close records no durable change. |

For HiveMind, reuse the offered ID. At tasks 02 and 05, use
`session-draft SESSION_ID --out UNIQUE_FILE` and
`session-close SESSION_ID --file UNIQUE_FILE`. Label sources
`user:pilot/task-02` and `user:pilot/task-05` and record their observation
times. The task 02 decision has status `agreed`. Task 05 records the hosted
rollout's region and reviewer as open; the retry decision stays in the project
handoff. At task 04, use the preview and apply flow in [Memory controls](MEMORY.md)
for `pilot-storage`, with the operator's correction as `user:pilot/task-04`.
The corrected body should state SQLite, its local-only scope and rationale,
the undecided hosted case and the absence of a runtime check.

At task 07 use `session-close SESSION_ID --no-change`. Capture
`session-status SESSION_ID`, relevant `hive evidence ID@REVISION` responses
and `check` event counts at each transition. In the baseline capture the
corresponding `HANDOFF.md` bytes and hashes, the original storage file and
the conversation close time. The final baseline task leaves the project files
unchanged. Inspect actual persisted state in both arms.

## Score first responses and transfers

- Freeze each first answer and the corresponding local tool or file receipts
  before responding to it. The reviewer marks each of the 21 fixed criteria
  pass or fail. Grade using the initial answer plus the verified state it
  produced. If a criterion requires persistence or a valid close, an answer
  claiming success is insufficient. Where practical, remove arm labels before
  judging answer content and grade artifacts against the labelled state
  separately.
- A first answer that wrongly asserts a current decision, runtime check or
  hosted approval is a critical factual failure. A transfer failure is a
  missed or wrong retry policy at task 03 or 07, or missed or wrong correction
  at task 06. Record all failed criteria and deviations even when a later
  answer succeeds.
- If an answer is missing or wrong and a rescue card applies, send that card
  at most once, after scoring the first answer. Record the exact card ID and
  its words as repeated information. Count the cards and the distinct tasks
  needing them. Task 02's new retry choice and task 04's planned correction
  are first disclosures. Track procedural requests or troubleshooting
  separately. Tasks 05 and 07 have no answer key for production approval
  beyond abstention; provide no invented approval as a rescue.
- A favorable pilot outcome needs first-response recall to pass on tasks 03,
  06 and 07, the corrected state to keep its history, zero critical factual
  failures, a score at least as high as baseline and fewer rescue cards than
  baseline. Report an accuracy or repetition tie as a tie. A single paired run
  measures these tasks only.

Keep a private run sheet with the command/build identity, model/profile labels,
arm order, UTC start/end times, task IDs, first answers, binary criteria,
rescue cards, close outcomes, revision/hash evidence and any failures. Record
host-reported usage and charges if available. Record the result as incomplete
if a host or arm stops early. Share a redacted scorecard on request.

The local setup and offline check are runnable now. Live execution needs
approval for the two hosts and profiles, the chosen models or entitlements,
and the spending limit. The planned maximum is six conversations across both
arms, fourteen scheduled task prompts and at most one rescue turn for each
eligible task. Stop before exceeding the agreed limit. A project owner must
approve the scope of any real-project corpus before substituting it for this
fictional fixture.

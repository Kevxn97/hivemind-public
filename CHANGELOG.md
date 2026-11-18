# Changes

## 0.1.0a3 — 2026-11-18

- The README opens with a local demo of a corrected decision across two agent
  adapters. The GitHub prerelease includes the reviewed source ZIP, an
  installable wheel and SHA-256 checksums for both downloads.
- **Correction checks.** Mark a journaled correction, save a follow-up question
  and choose Codex or Claude. Inspect which revision and source new sessions
  were offered in their context. Record separately whether the answer
  used the correction and whether you had to repeat it.
- Optional Dreaming links a project hint to two independently confirmed
  corrections. A user approves it before matching tasks can receive it.
  Dreaming starts off in each vault.

- Simple handoffs accept explicit source, observation time, reviewed revision
  and decision/question flags with `session-close --record`; they use the same
  journal and receipt checks as structured handoffs. Local evidence is hashed.
- Connected agent guidance uses the full CLI, avoids duplicate context reads,
  keeps intermediate sessions open and exposes the existing correction flow.
  Corrections show their new source first and bind local source bytes to previews,
  while preserving every prior source record and exact historical revision.
- Source sync pins an explicitly registered provider outside the vault, runs it
  in a read-only staging directory, and applies only validated, source-linked
  proposals through the host journal. Existing automation needs registration;
  old service definitions are reported for explicit refresh.
- Externally edited notes produce a visible hook warning. `adopt` previews and
  journals reviewed edits without discarding their exact bytes or prior history.
- Startup context keeps concise provenance while full evidence remains
  addressable. Local database terminology and question forms improve lexical
  retrieval; short memories can be explicitly stored as decisions.
- The normal verifier includes all five standalone operating suites (56 tests),
  with success, failure and interruption evidence.
- A seven-task paired continuity pilot is documented with synthetic starting
  data, fixed scoring criteria and a normal Markdown handoff baseline.

- English-first terminal, CLI, and generated context; existing notes retain their
  original language.
- Guided automation panel in the existing TUI (`hive automation`, key **b**) with opt-in
  source sync, per-vault startup and weekly local evidence review.
- Selected files and scoped remote sources can be enabled, disabled or removed.
  Preview tokens bind CLI changes to the selected vault and current settings.
- Portable sync instructions use an explicitly chosen Codex executable/profile,
  retain its model settings, narrow local execution to read-only staging, and
  validate per-source completion receipts.
- macOS launchd and Linux systemd user-service definitions have unique ownership
  per vault. Pause gates new automation; disabling preserves knowledge/history.
- Weekly review saves a bounded operational report and never invents verification
  or automatically changes factual claims.

- Compact horizontal honeycomb branding with gold and ivory terminal accents.
  Network note titles use normal terminal text; navigation adapts to small windows.
- Start directly from the downloaded folder with `python3 run.py`. Guided setup
  connects a project and agent and saves the first memory.
- Save short memories with `remember`. The source menu supports project-name
  selection, agent inference and one confirmation after a readable preview.
- Gmail and Calendar guidance includes a copyable agent prompt. MCP setup asks
  for the chosen server details and previews the project configuration.

## 0.1.0a2 — 2026-11-14

- Guided memory management with provenance, correction previews, archiving,
  compensating undo, and a pause for incoming session and source updates.
- A replayable synthetic demo: save a decision, change sessions, correct it, and
  inspect what the next session receives.
- Gmail and Calendar setup guidance, optional project-scoped HTTP MCP
  configuration, and explicit source capture with a preview before storage.
- CI for source regressions, clean wheel installation and the core user flows on
  macOS and Linux, with retained evidence on both success and failure.

This is an alpha source release. Package-registry publication and verification
with real provider accounts remain separate steps.

## 0.1.0a1

Initial MIT public preview: separate vaults, guided setup, Codex/Claude lifecycle
connections, source-labelled session handoffs, doctor and an isolated self-test.
The public repository starts with a fresh history and no personal knowledge.

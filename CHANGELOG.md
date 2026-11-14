# Changes

## Unreleased

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

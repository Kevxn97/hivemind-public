# A two-minute HiveMind demo

Install the package as described in the [README](../README.md), then run:

```sh
hive demo
```

No vault, account, model or provider connection is required. The command creates
a new synthetic project and vault in a temporary directory. It does not select
your normal vault, copy agent settings, or import project files.

The demonstration follows five steps:

1. A synthetic Codex handoff stores **“Use three retries”** with its source.
2. A fresh Claude session receives that decision and provenance.
3. A human correction changes it to **“Use five retries”** through the same
   preview and journalled apply flow available in `hive memory`.
4. A new Codex session receives the corrected state. The old decision is absent
   from its current context.
5. The journal explains the revision change and retains the original event.

These are local adapters and subprocesses. No real model is asked to remember
anything. The evidence demonstrates what HiveMind stores and offers to an agent;
it does not establish model comprehension or improvement on real tasks.

## Keep and inspect a run

```sh
hive demo --directory /tmp/my-first-hivemind-demo --json
```

Choose a new directory. Existing directories and knowledge vaults are refused.
The result reports the synthetic vault, readable `transcript.md`, and
`report.json`, including code hashes, commands, environment and each step's
outcome. Failed or interrupted runs also retain a report. The initial handoff
has its own linked evidence report. Re-run `hive demo` for a fresh example.

Open the reported synthetic vault with `hive --root PATH memory` to inspect its
source, archive a note, or undo the correction. Undo adds a compensating journal
event; it does not erase the earlier state. Keep your real vault outside the
application checkout and public repositories.

## Try it with your own agent

After the synthetic demo, connect one real agent with `hive connect codex` or
`hive connect claude`, review the preview, and apply it. Complete the host's hook
review, then follow the [small user test](TRY_IT.md). Real host acceptance is a
separate step from the automated demonstration.

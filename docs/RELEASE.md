# Preparing a public distribution

The source project and each knowledge vault are independent directories. A new
public repository must start from this clean source tree or its reviewed archive.
Do not change the visibility of a repository containing a personal vault: removing
files from its latest tree would still leave its earlier history accessible.

`RELEASE_FILES.json` is the explicit list of publishable source files. Review and
update it when adding a new module, resource or test. Create a source archive with:

```sh
python3 scripts/package_release.py --out ../hivemind-public-release
```

The packager rejects vault directories, unreviewed code/resources, symlinks and
paths escaping the source tree. It includes no `.git`, operational state, test
reports or user configuration. The release manifest records each included file's
hash and the archive hash. Inspect both the archive and any built Python wheel
before uploading. A local archive is preparation, not publication.

The Python package includes code, schemas and neutral integration templates only.
It has no runtime dependency on a particular home directory or previous checkout.
The package targets Python 3.11+ on POSIX systems with SQLite FTS5. The CI
matrix exercises Ubuntu and macOS with Python 3.11 and 3.14.

The project uses the [MIT license](../LICENSE). Its public source repository is
[Kevxn97/hivemind-public](https://github.com/Kevxn97/hivemind-public).
The Python package has not been published to a package registry.

## Verify the v0.1.0a4 downloads

Download the reviewed ZIP, wheel and `SHA256SUMS` from the
[v0.1.0a4 prerelease](https://github.com/Kevxn97/hivemind-public/releases/tag/v0.1.0a4)
into the same directory. Verify both files from that directory.

```sh
shasum -a 256 -c SHA256SUMS   # macOS
# On Linux, use sha256sum -c SHA256SUMS
```

## Build the release candidate

Start in a clean checkout of the candidate commit. Use a new output directory
and Python environment for each build.

```sh
python3 scripts/verify.py
python3 scripts/package_release.py --out ../hivemind-public-release
python3 -m pip install 'build>=1.2,<2'
python3 -m build --outdir ../hivemind-public-release
python3 -m venv /tmp/hivemind-release-check
/tmp/hivemind-release-check/bin/python -m pip install --no-deps ../hivemind-public-release/*.whl
HIVEMIND_TEST_PYTHON=/tmp/hivemind-release-check/bin/python python3 scripts/verify.py --pattern test_portable_release_e2e.py
HIVEMIND_TEST_PYTHON=/tmp/hivemind-release-check/bin/python python3 scripts/verify.py --pattern test_demo_e2e.py
HIVEMIND_TEST_PYTHON=/tmp/hivemind-release-check/bin/python python3 scripts/verify.py --pattern test_memory_control_e2e.py
HIVEMIND_TEST_PYTHON=/tmp/hivemind-release-check/bin/python python3 scripts/verify.py --pattern test_source_connections_e2e.py
```

Review the built archive and wheel contents against the allowlist and candidate
commit. The source ZIP includes the CI definition, tests and documentation.
The wheel contains the runtime package and distribution metadata. Generate
the download checksums from the exact files you will attach to the prerelease.

```sh
(cd ../hivemind-public-release && shasum -a 256 hivemind-public-0.1.0a4.zip hivemind_local-0.1.0a4-py3-none-any.whl > SHA256SUMS)
```

Attach the ZIP, wheel and `SHA256SUMS` to the same GitHub prerelease. Verify
the hosted downloads with the published checksums.

The [GitHub Actions workflow](../.github/workflows/ci.yml) repeats the source and
installed acceptance on macOS/Linux and Python 3.11/3.14 with no provider account.
It keeps evidence even on failed jobs. Hosted results are only established once
that workflow actually runs. [First-user testing](TRY_IT.md) and real host/OAuth
acceptance remain separate from this synthetic evidence.

## Private vault boundary

New vaults have private directory permissions, empty import configuration and
disabled automation. No personal notes, history, credentials, agent configuration
or global instructions are migrated. A project mapping records the chosen folder
locally in `.state`; it does not scan or import the project's contents.

Canonical notes and their journal are the durable knowledge. Keep the whole vault
in a private backup. This is ordinary local storage, not encryption. Hook context
is deliberately supplied to the chosen agent host, whose account and processing
rules apply. Hooks discard prompt and transcript fields; they retain only bounded
lifecycle and context-offering metadata. A source-labelled statement is not proof
that the underlying fact is still current.

## Integration verification

`hive self-test` creates its own synthetic vault and evidence directory. It checks
the local hook and handoff path. `hive doctor` reads the configured local files;
it does not repair settings, approve trust, or prove that an actual agent consumed
the context. Installation and removal preserve unrelated settings and refuse
managed-block drift until it is reviewed.

Codex's documented user hook review is required after new or changed hook
definitions; SessionEnd is limited to three seconds. See the official
[Codex hooks documentation](https://learn.chatgpt.com/docs/hooks). The Claude
adapter follows the documented lifecycle JSON in the
[Claude Code hook reference](https://code.claude.com/docs/en/hooks).

After connecting, open a fresh agent session in the registered project, verify
that a HiveMind session ID is offered, save one explicitly authorized decision
with `session-close --record`, and verify it in the next session. Do this on the
actual host before describing that host integration as verified.

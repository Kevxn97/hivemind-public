"""Repeatable synthetic local hook and handoff verification with saved evidence."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

try:
    from . import hive, onboarding
except ImportError:
    if __package__:
        raise
    import hive
    import onboarding

SCOPE = 'synthetic_local_hooks_and_handoff'
DECISION = 'Use three retries for the synthetic fixture.'
SOURCE = 'selftest:decision'
SENTINEL = 'SELFTEST_PRIVATE_INPUT_MUST_NOT_BECOME_KNOWLEDGE'


def _hashes(code):
    return {str(path.relative_to(code)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(code.rglob('*'))
            if path.is_file() and path.suffix in {'.py', '.json'}
            and '__pycache__' not in path.parts and '.git' not in path.parts
            and not any(part.startswith('.') for part in path.relative_to(code).parts)}


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(evidence_dir=None):
    """Run real installed core subprocesses using only a fresh synthetic vault.

    Return a JSON report with status/outcome and evidence/evidence_path aliases.
    Every subprocess and the synthetic fixture are retained in its unique evidence
    directory. Failure and interruption produce a failed report. Host hook
    installation and model reasoning are outside the declared verification scope.
    """
    code = Path(hive.__file__).resolve().parent
    module_dir = Path(onboarding.__file__).resolve().parent
    preflight_error = None
    try:
        if evidence_dir is None:
            folder = Path(tempfile.mkdtemp(prefix='hivemind-self-test-')).resolve()
        else:
            parent = Path(evidence_dir).expanduser().resolve()
            selected = Path(os.environ.get('HIVEMIND_HOME', Path.home() / '.local/share/hivemind')).expanduser().resolve()
            if parent == selected or selected in parent.parents:
                raise ValueError('Choose an evidence directory outside the selected vault')
            if any((ancestor / onboarding.MARKER).is_file() for ancestor in (parent, *parent.parents)):
                raise ValueError('Choose an evidence directory outside a knowledge vault')
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder = Path(tempfile.mkdtemp(prefix='run-', dir=parent)).resolve()
    except (OSError, ValueError) as error:
        preflight_error = error
        folder = Path(tempfile.mkdtemp(prefix='hivemind-self-test-')).resolve()
    folder.chmod(0o700)
    report_path = folder / 'report.json'
    workspace = folder / 'fixture'
    project = workspace / 'project'
    home = workspace / 'home'
    vault = workspace / 'vault'
    report = {'scope': SCOPE, 'status': 'running', 'outcome': 'running', 'passed': False,
              'evidence': str(report_path), 'evidence_path': str(report_path),
              'installer_checked': False, 'started_at': hive.now(),
              'environment': {'python': sys.version.split()[0], 'platform': platform.system(),
                              'home': str(home), 'vault': str(vault),
                              'network': 'unused', 'models': 'unused',
                              'python_bytecode': 'disabled',
                              'replay': 'Set PYTHON and HIVEMIND_CODE to the installed interpreter and package directory.'},
              'source_sha256': {}, 'stages': [], 'commands': []}
    current = None

    def scrub(value):
        text = str(value)
        for source in sorted({str(code), str(module_dir)}, key=len, reverse=True):
            text = text.replace(source, '${HIVEMIND_CODE}')
        return text.replace(str(sys.executable), '${PYTHON}')

    def save():
        temp = report_path.with_suffix('.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temp.chmod(0o600)
        os.replace(temp, report_path)

    def stage(name):
        nonlocal current
        current = {'id': name, 'outcome': 'running'}
        report['stages'].append(current)
        save()

    def passed(**details):
        current.update(outcome='passed', **details)
        save()

    environment = {'HOME': str(home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE': '1', 'HIVEMIND_HOME': str(vault),
                   'PYTHONPATH': os.pathsep.join(dict.fromkeys([str(module_dir), str(code), str(code.parent)]))}

    def command(script, args=(), payload=None, inline=False):
        argv = [sys.executable, '-c', script, *map(str, args)] if inline else [sys.executable, str(code / script), '--root', str(vault), *map(str, args)]
        entry = {'argv': [scrub(arg) for arg in argv], 'cwd': str(project),
                 'stdin': json.dumps(payload) if payload is not None else None,
                 'stage': current['id'], 'outcome': 'running'}
        report['commands'].append(entry)
        save()
        try:
            result = subprocess.run(argv, input=entry['stdin'], text=True, capture_output=True,
                                    cwd=project, env=environment, timeout=30)
        except BaseException as error:
            entry.update(outcome='failed', error_type=type(error).__name__)
            if isinstance(error, subprocess.TimeoutExpired):
                entry.update(stdout=scrub(error.stdout or ''), stderr=scrub(error.stderr or ''))
            save()
            raise
        entry.update(returncode=result.returncode, stdout=scrub(result.stdout),
                     stderr=scrub(result.stderr), outcome='passed' if result.returncode == 0 else 'failed')
        save()
        _require(result.returncode == 0, 'Synthetic subprocess failed with exit ' + str(result.returncode))
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise RuntimeError('Synthetic subprocess did not produce structured output') from error

    def cli(*args):
        return command('launcher.py', [*args, '--json'])

    def hook(agent, runtime, event='SessionStart'):
        return command('hive.py', ['brief', '--hook', '--agent', agent],
                       {'cwd': str(project), 'session_id': runtime, 'source': 'startup',
                        'hook_event_name': event, 'reason': 'other',
                        'transcript_path': str(home / 'private-transcript.txt'),
                        'prompt': SENTINEL, 'transcript': SENTINEL})

    save()
    try:
        before = _hashes(code)
        report['source_sha256'] = before
        if preflight_error is not None:
            raise preflight_error
        project.mkdir(parents=True, mode=0o700)
        home.mkdir(mode=0o700)
        private = home / 'private-transcript.txt'
        private.write_text(SENTINEL)
        private.chmod(0o600)
        for name in ['.codex', '.claude']:
            (home / name).mkdir(mode=0o700)
            (home / name / 'settings.json').write_text(json.dumps({'private_input': SENTINEL}))
        (folder / 'fixture.json').write_text(json.dumps({'scope': SCOPE, 'project': 'Self Test Project',
                                                        'decision': DECISION, 'source': SOURCE,
                                                        'starting_data': 'Fresh synthetic project and empty separate vault.'}, indent=2) + '\n')
        stage('initialize')
        initialized = command('import json, sys; from onboarding import initialize; print(json.dumps(initialize(sys.argv[1], project_path=sys.argv[2], name="Self Test Project")))',
                              [vault, project], inline=True)
        _require(initialized['initialized'], 'Fresh synthetic vault was not initialized')
        passed()

        stage('codex_session_start')
        first = hook('codex', 'synthetic-codex-runtime')
        text = first['hookSpecificOutput']['additionalContext']
        match = re.search(r'(?:HiveMind session|HiveMind-Sitzung): ([a-f0-9]{32})', text)
        _require(match is not None, 'Codex SessionStart did not offer a session receipt')
        first_id = match.group(1)
        opened = cli('session-open', project, '--agent', 'codex', '--runtime-id', 'synthetic-codex-runtime', '--runtime-source', 'startup')
        _require(opened['session_id'] == first_id, 'SessionStart and session-open did not reuse runtime identity')
        passed(session_id=first_id)

        stage('draft')
        handoff = folder / 'handoff.json'
        cli('session-draft', first_id, '--out', handoff)
        draft = json.loads(handoff.read_text())
        source_id = draft['source']['id']
        draft.update(project_state='Synthetic retry policy has been declared.', evidence='reported',
                     decisions=[{'decision': DECISION, 'reason': 'Synthetic self-test declaration.', 'status': 'agreed'}],
                     open_questions=[], next_steps=[])
        draft['source'].update(resource=SOURCE, title='Synthetic self-test decision', observed_at=hive.now())
        handoff.write_text(json.dumps(draft, indent=2) + '\n')
        passed(source=SOURCE, source_id=source_id)

        stage('save_handoff')
        saved = cli('session-close', first_id, '--file', handoff)
        _require(saved['status'] == 'saved', 'Explicit synthetic handoff was not saved')
        before_end = command('hive.py', ['check'])['events']
        _require(before_end == 2, 'Expected exactly seed and handoff journal events')
        passed(event=saved['event'])

        stage('claude_session_start')
        second = hook('claude', 'synthetic-claude-runtime')
        context = second['hookSpecificOutput']['additionalContext']
        match = re.search(r'(?:HiveMind session|HiveMind-Sitzung): ([a-f0-9]{32})', context)
        _require(match is not None, 'Claude SessionStart did not offer a session receipt')
        second_id = match.group(1)
        _require(second_id != first_id, 'Fresh agent runtime reused the previous receipt')
        _require(DECISION in context, 'Fresh Claude context did not contain the saved decision')
        _require(source_id in context or SOURCE in context, 'Fresh Claude context omitted decision provenance')
        passed(session_id=second_id, decision=DECISION, source=SOURCE, source_id=source_id)

        stage('session_end')
        hook('claude', 'synthetic-claude-runtime', event='SessionEnd')
        ended = cli('session-status', second_id)
        _require(ended['status'] == 'context_offered' and 'ended_at' in ended['runtime'],
                 'SessionEnd changed knowledge status or failed to end runtime')
        _require('notes' not in ended, 'SessionEnd invented a handoff')
        _require(command('hive.py', ['check'])['events'] == before_end, 'SessionEnd wrote canonical knowledge')
        passed()

        stage('explicit_no_change')
        closed = cli('session-close', second_id, '--no-change')
        _require(closed['status'] == 'no_change', 'Explicit no-change did not close the receipt')
        _require(command('hive.py', ['check'])['events'] == before_end, 'No-change wrote canonical knowledge')
        passed()

        stage('isolation')
        for path in vault.rglob('*'):
            if path.is_file() and path.suffix != '.sqlite':
                _require(SENTINEL.encode() not in path.read_bytes(), 'A private input entered the synthetic vault')
        _require(private.read_text() == SENTINEL, 'Synthetic private input was modified')
        _require(json.loads((vault / 'config/sources.json').read_text()) == {'version': 1, 'files': [], 'remote': []},
                 'Initialization selected an ingestion source')
        _require(json.loads((vault / 'config/background.json').read_text())['enabled'] is False,
                 'Background operation became enabled')
        _require(before == _hashes(code), 'Self-test changed installed source or resource files')
        passed(private_inputs_ignored=True, source_unchanged=True)
        report.update(status='passed', outcome='passed', passed=True)
    except BaseException as error:
        if current is None:
            stage('prepare')
        current.update(outcome='failed', error_type=type(error).__name__, error=scrub(error))
        report.update(status='failed', outcome='failed', passed=False,
                      interrupted=isinstance(error, (KeyboardInterrupt, SystemExit)))
    finally:
        report['finished_at'] = hive.now()
        save()
    return report

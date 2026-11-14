"""A replayable, entirely synthetic decision -> recall -> correction demo."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

from . import hive, onboarding, selftest

CORRECTION = 'Use five retries for the synthetic fixture.'
SOURCE = 'synthetic:demo-correction'


def _destination(directory, selected_vault):
    if directory is None:
        return Path(tempfile.mkdtemp(prefix='hivemind-demo-')).resolve()
    requested = Path(directory).expanduser().absolute()
    # macOS /tmp and /var are system aliases; resolve parents before rejecting
    # user-created links, while still rejecting a symlink at the target itself.
    for part in (requested, *requested.parents):
        if part.is_symlink() and str(part) not in {'/tmp', '/var'}:
            raise ValueError('Choose a demo path without symlinks')
    path = requested.resolve()
    code = Path(__file__).resolve().parent.parent
    selected = Path(selected_vault).expanduser().resolve()
    if path == code or code in path.parents or path == selected or selected in path.parents:
        raise ValueError('Choose a new demo directory outside the application and selected vault')
    if any((p / onboarding.MARKER).exists() for p in (path, *path.parents)):
        raise ValueError('Choose a demo directory outside any existing vault')
    # Existing directories (even empty ones) are never taken over by the demo.
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def run(directory=None, selected_vault=None):
    from .runtime_paths import resolve_vault
    folder = _destination(directory, selected_vault or resolve_vault())
    report_path, transcript_path = folder / 'report.json', folder / 'transcript.md'
    code = Path(__file__).resolve().parent
    report = {'scope': 'synthetic_local_demo', 'status': 'running',
              'real_agent_execution': False, 'started_at': hive.now(),
              'evidence': str(report_path), 'transcript': str(transcript_path),
              'environment': {'python': sys.version, 'platform': platform.platform(),
                              'network': 'unused', 'models': 'unused'},
              'fixture': 'Fresh synthetic project and vault; three retries corrected to five.',
              'source_sha256': selftest._hashes(code), 'steps': [], 'commands': []}
    step = None

    def save():
        hive.atomic(report_path, json.dumps(report, indent=2, ensure_ascii=False) + '\n')

    def stage(ident, description):
        nonlocal step
        step = {'id': ident, 'description': description, 'outcome': 'running'}
        report['steps'].append(step)
        save()

    def passed(**details):
        step.update(outcome='passed', **details)
        save()

    save()
    try:
        stage('store_decision', 'Save a source-labelled decision through a Codex session handoff.')
        proof = selftest.run(folder / 'handoff-proof')
        report['handoff_evidence'] = proof['evidence']
        selftest._require(proof['status'] == 'passed', 'Initial handoff failed; see handoff evidence.')
        vault = Path(proof['environment']['vault'])
        project = vault.parent / 'project'
        home = vault.parent / 'home'
        report.update(vault=str(vault), project=str(project))
        passed(decision=selftest.DECISION, source=selftest.SOURCE)
        environment = {'HOME': str(home), 'HIVEMIND_HOME': str(vault), 'PATH': os.defpath,
                       'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
                       'PYTHONPATH': str(code.parent)}

        def cli(*args):
            command = [sys.executable, '-B', '-m', 'hivemind', '--root', str(vault),
                       '--json', *map(str, args)]
            entry = {'argv': command, 'cwd': str(project), 'outcome': 'running', 'stage': step['id']}
            report['commands'].append(entry)
            save()
            try:
                result = subprocess.run(command, cwd=project, env=environment,
                                        capture_output=True, text=True, timeout=30)
                entry.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                             outcome='passed' if result.returncode == 0 else 'failed')
                save()
                selftest._require(result.returncode == 0, 'Demo command failed; see report.json.')
                return json.loads(result.stdout)
            except BaseException as error:
                entry.update(outcome='failed', error_type=type(error).__name__)
                save()
                raise

        stage('switch_session', 'Offer that decision and its source to a fresh Claude session.')
        first = cli('session-open', project, '--agent', 'claude', '--task', 'retry policy', '--view', 'full')
        before = cli('memory', 'show', 'self-test-project')
        text = first['context']['text']
        selftest._require(selftest.DECISION in text, 'Saved decision missing from new session.')
        report['before'] = {'context': text, 'revision': before['revision'], 'session_id': first['session_id']}
        passed(decision=selftest.DECISION, revision=before['revision'])

        stage('correct_decision', 'Review and apply a human correction; retain the old journal event.')
        body = '# Synthetic retry policy\n\n' + CORRECTION + '\n\nThis is a synthetic user correction, not a live benchmark.'
        flags = ['memory', 'edit', 'self-test-project', '--summary', CORRECTION,
                 '--body', body, '--source', SOURCE, '--reason', 'Correct the synthetic demo decision.']
        preview = cli(*flags)
        selftest._require(preview['status'] == 'preview', 'Correction needs a preview.')
        corrected = cli(*flags, '--apply', '--preview', preview['preview'])
        current = cli('memory', 'show', 'self-test-project')
        selftest._require(current['revision'] != before['revision'], 'Correction did not change the revision.')
        passed(decision=CORRECTION, source=SOURCE, revision=current['revision'], event=corrected.get('event'))

        stage('switch_again', 'Offer the corrected state to a new Codex session.')
        second = cli('session-open', project, '--agent', 'codex', '--task', 'retry policy', '--view', 'full')
        text = second['context']['text']
        selftest._require(CORRECTION in text, 'Correction missing from new session.')
        selftest._require(selftest.DECISION not in text, 'Superseded decision still appears as current context.')
        report['after'] = {'context': text, 'revision': current['revision'], 'session_id': second['session_id']}
        passed(decision=CORRECTION, revision=current['revision'])

        stage('show_change', 'Show the change since the earlier session and preserve both revisions.')
        selftest._require(len(current['history']) == 3, 'Expected initialization, handoff and correction events.')
        correction_event = next((event for event in current['history']
                                 if event['before_revision'] == before['revision']
                                 and event['after_revision'] == current['revision']), None)
        selftest._require(correction_event is not None, 'Journal does not explain the revision change.')
        cli('session-close', first['session_id'], '--no-change')
        cli('session-close', second['session_id'], '--no-change')
        selftest._require(cli('check')['valid'], 'Demo vault is not consistent.')
        selftest._require(report['source_sha256'] == selftest._hashes(code), 'Demo changed installed source files.')
        passed(changes=correction_event['reason'], event=correction_event['id'], journal_events=len(current['history']))
        report['status'] = 'passed'
    except BaseException as error:
        if step is not None:
            step.update(outcome='failed', error_type=type(error).__name__, error=str(error))
        report.update(status='failed', interrupted=isinstance(error, (KeyboardInterrupt, SystemExit)))
    finally:
        report['finished_at'] = hive.now()
        lines = ['# HiveMind demo', '', 'Synthetic local demonstration. No real agent or provider is called.', '']
        for number, item in enumerate(report['steps'], 1):
            lines.extend([f"{number}. {item['description']} [{item['outcome']}]", ''])
            if 'decision' in item:
                lines.extend(['   ' + item['decision'], ''])
        lines.extend(['Original: ' + selftest.DECISION, 'Correction: ' + CORRECTION, '',
                      'A successful run proves the local storage and context path. It does not measure model recall or usefulness.', '',
                      'Evidence: ' + str(report_path),
                      'Replay with `hive demo` to create another isolated fixture.'])
        if report['status'] != 'passed':
            lines.append('The demo did not complete. Inspect the failed step in the evidence report.')
        transcript = '\n'.join(lines) + '\n'
        hive.atomic(transcript_path, transcript)
        report['text'] = transcript.replace('# HiveMind demo', 'HiveMind demo', 1)
        save()
    return report

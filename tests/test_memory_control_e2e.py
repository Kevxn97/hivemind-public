"""Memory controls through subprocesses with synthetic vaults and sanitized homes.

Requirements cover corrected fresh-session context, provenance/history retention,
preview binding and stale rejection, archive/compensating undo, and paused incoming
handoffs with manual correction/read continuity. No providers or real user data.
"""
import fcntl
import json
import os
import pty
import re
import select
import time
from pathlib import Path
import struct
import subprocess
import termios
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MemoryControlE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hivemind-memory-e2e-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.vault = self.folder / 'vault'
        self.project = self.folder / 'project'
        self.project.mkdir()
        self.home = self.folder / 'home'
        self.home.mkdir()
        self.env = {'HOME': str(self.home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1', 'TERM': 'dumb'}
        self.python = os.environ.get('HIVEMIND_TEST_PYTHON', sys.executable)
        self.entrypoint = ([str(Path(self.python).with_name('hive'))] if os.environ.get('HIVEMIND_TEST_PYTHON')
                           else [self.python, '-B', '-m', 'hivemind'])
        if not os.environ.get('HIVEMIND_TEST_PYTHON'):
            self.env['PYTHONPATH'] = str(ROOT)
        self.entries = []
        self.addCleanup(self.save)
        self.cli('init', str(self.vault), '--project', str(self.project), '--name', 'Demo', '--yes')

    def save(self):
        evidence = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', tempfile.gettempdir())) / 'memory-controls'
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / (self.id().split('.')[-1] + '.json')).write_text(json.dumps({
            'fixture': 'Synthetic Demo project and temporary sanitized HOME. No provider credentials or calls.',
            'environment': self.env, 'commands': self.entries}, indent=2) + '\n')

    def run_process(self, command, ok=True):
        entry = {'command': command, 'outcome': 'running'}
        self.entries.append(entry)
        self.save()
        try:
            result = subprocess.run(command, cwd=self.folder, env=self.env, capture_output=True, text=True, timeout=20)
            entry.update(outcome='passed' if result.returncode == 0 else 'failed', returncode=result.returncode,
                         stdout=result.stdout, stderr=result.stderr)
        except BaseException as exc:
            entry.update(outcome='aborted', error=type(exc).__name__)
            raise
        finally:
            self.save()
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout) if ok else result.stderr

    def cli(self, *args, ok=True):
        return self.run_process([*self.entrypoint, '--root', str(self.vault), '--json', *args], ok)

    def memory(self, *args, ok=True):
        return self.cli('memory', *args, ok=ok)

    def mutate(self, *args):
        preview = self.memory(*args)
        self.assertEqual(preview['status'], 'preview')
        return self.memory(*args, '--apply', '--preview', preview['preview'])

    def test_correction_is_seen_in_fresh_session_and_history_retains_provenance(self):
        listing = self.memory('list')
        self.assertEqual(listing['memories'][0]['id'], 'demo')
        original = self.memory('show', 'demo')
        wrong = self.mutate('edit', 'demo', '--summary', 'Decision uses WrongDB.', '--body', '# Decision\n\nUse WrongDB.',
                            '--source', 'user:initial-decision', '--reason', 'Synthetic initial decision')
        corrected = self.mutate('edit', 'demo', '--summary', 'Decision uses CorrectDB.', '--body', '# Decision\n\nUse CorrectDB.',
                                '--source', 'user:reviewed-decision', '--reason', 'Correct a false project decision')
        shown = self.memory('show', 'demo')
        self.assertIn('user:reviewed-decision', json.dumps(shown['note']['sources']))
        self.assertEqual(len(shown['history']), len(original['history']) + 2)
        session = self.cli('session-open', str(self.project), '--agent', 'codex', '--session-id', '11111111111111111111111111111111')
        self.assertIn('CorrectDB', json.dumps(session))
        self.assertNotIn('WrongDB', json.dumps(session))
        self.assertNotEqual(wrong['event'], corrected['event'])
        self.assertTrue(self.cli('check')['valid'])

    def corrected_retry_decision(self):
        created = self.cli('remember', 'Retry failed imports three times.', '--type', 'decision', '--yes')
        ident = created['note']['id']
        original = self.memory('show', ident)
        (self.vault / 'sources').mkdir(exist_ok=True)
        (self.vault / 'sources' / 'retry-review.md').write_text('The synthetic project chose five retries.')
        self.mutate('edit', ident, '--summary', 'Retry failed imports five times.',
                    '--body', '# Import retries\n\nRetry failed imports five times.',
                    '--source', 'sources/retry-review.md', '--reason', 'Correct the retry count')
        corrected = self.memory('show', ident)
        return ident, original, corrected

    def test_correction_check_exposes_offered_revision_and_records_explicit_review(self):
        ident, _, note = self.corrected_retry_decision()
        reference = ident + '@' + note['revision']
        source = note['note']['sources'][0]['id']
        question = 'How often should we retry failed imports?'
        check = self.memory('check', 'add', reference, '--question', question, '--agent', 'claude')['check']
        check_id = check['id']
        self.assertEqual((check['ref'], check['source_id'], check['question']), (reference, source, question))

        opened = self.cli('session-open', str(self.project), '--agent', 'claude')
        self.assertIn(source, opened['context']['text'])
        session_id = opened['session_id']
        self.cli('session-context', session_id, '--task', question)
        run, = self.memory('check', 'show', check_id)['runs']
        self.assertEqual(run['session_id'], session_id)
        for phase in ('startup', 'question_context'):
            offer = run[phase]
            self.assertEqual((offer['status'], offer['observed_ref'], offer['source_id']),
                             ('offered', reference, source))
        self.assertIsNone(run['review'])
        self.memory('check', 'review', check_id, '--session', session_id, '--used', 'no', '--corrected-again', 'yes')
        reviewed, = self.memory('check', 'show', check_id)['runs']
        self.assertIs(reviewed['review']['used'], False)
        self.assertIs(reviewed['review']['corrected_again'], True)
        self.assertFalse(self.memory('check', 'stop', check_id)['check']['active'])
        self.cli('session-open', str(self.project), '--agent', 'claude')
        self.assertEqual(self.memory('check', 'show', check_id)['runs'], [reviewed])

    def test_correction_check_scopes_runs_and_detects_a_superseded_revision(self):
        ident, original, corrected = self.corrected_retry_decision()
        question = 'How often should we retry failed imports?'
        settings = ['--question', question, '--agent', 'codex']
        lead = self.memory('show', 'demo')
        self.memory('check', 'add', 'demo@' + lead['revision'], *settings, ok=False)
        self.memory('check', 'add', ident + '@' + original['revision'], *settings, ok=False)
        old_session = self.cli('session-open', str(self.project), '--agent', 'codex')['session_id']
        check = self.memory('check', 'add', ident + '@' + corrected['revision'], *settings)['check']
        self.cli('session-context', old_session, '--task', question)
        self.cli('session-open', str(self.project), '--agent', 'claude', '--task', question)
        self.cli('session-open', str(self.project), '--agent', 'codex', '--task', 'Document a different issue')
        self.assertEqual(self.memory('check', 'show', check['id'])['runs'], [])

        self.mutate('edit', ident, '--summary', 'Retry failed imports seven times.',
                    '--body', '# Import retries\n\nRetry failed imports seven times.',
                    '--source', 'user:updated-retry-policy', '--reason', 'Replace the five-retry decision')
        current = self.memory('show', ident)
        fresh = self.cli('session-open', str(self.project), '--agent', 'codex', '--task', question)
        run, = self.memory('check', 'show', check['id'])['runs']
        self.assertEqual(run['session_id'], fresh['session_id'])
        self.assertEqual(run['startup']['status'], 'different_revision')
        self.assertEqual(run['startup']['observed_ref'], ident + '@' + current['revision'])
        self.assertIsNone(run['review'])

    def test_review_uses_the_last_matching_offer_and_preserves_startup(self):
        ident, _, corrected = self.corrected_retry_decision()
        question = 'What is the import retry count?'
        ref = ident + '@' + corrected['revision']
        check_id = self.memory('check', 'add', ref, '--question', question, '--agent', 'claude')['check']['id']
        session = self.cli('session-open', str(self.project), '--agent', 'claude', '--task', question)['session_id']
        edited = self.mutate('edit', ident, '--summary', 'Use seven import retries.',
                             '--body', '# Import retries\n\nUse seven import retries.',
                             '--source', 'user:revised-policy', '--reason', 'Update the retry policy')
        current = self.memory('show', ident)
        self.cli('session-context', session, '--task', question.upper())
        latest, = self.memory('check', 'show', check_id)['runs']
        self.assertEqual(latest['startup']['observed_ref'], ref)
        self.assertEqual(latest['question_context']['status'], 'different_revision')
        self.assertEqual(latest['question_context']['observed_ref'], ident + '@' + current['revision'])
        self.assertEqual(latest['question_context']['source_id'], current['note']['sources'][0]['id'])

        self.memory('check', 'review', check_id, '--session', session, '--used', 'no', '--corrected-again', 'yes')
        frozen, = self.memory('check', 'show', check_id)['runs']
        self.mutate('undo', edited['event'], '--reason', 'Return to the earlier retry policy')
        self.cli('session-context', session, '--task', question)
        self.assertEqual(self.memory('check', 'show', check_id)['runs'], [frozen])

    def test_memory_menu_can_cancel_or_save_a_check_after_showing_its_source(self):
        ident, _, note = self.corrected_retry_decision()
        question = 'What is the import retry count?'
        path = f'9\na\n{ident}\n{question}\nclaude\n'
        cancelled = self.terminal(['memory'], [(b'Choice > ', (path + 'n\nq\nq\n').encode())])
        self.assertIn(ident + '@' + note['revision'], cancelled)
        self.assertIn('sources/retry-review.md', cancelled)
        self.assertIn('Save this check?', cancelled)
        self.assertEqual(self.memory('check', 'list')['checks'], [])
        self.terminal(['memory'], [(b'Choice > ', (path + 'y\nq\nq\n').encode())])
        check, = self.memory('check', 'list')['checks']
        self.assertEqual((check['question'], check['agent']), (question, 'claude'))

    def test_preview_required_stale_rejected_archive_and_compensating_undo(self):
        args = ('edit', 'demo', '--summary', 'New decision.', '--body', '# Corrected decision\n\nUse CorrectDB.',
                '--source', 'user:review', '--reason', 'Correct false decision')
        before = self.memory('show', 'demo')
        self.memory(*args, '--apply', ok=False)
        plan = self.memory(*args)
        self.assertEqual(self.memory('show', 'demo')['revision'], before['revision'])
        changed = self.memory(*args, '--apply', '--preview', plan['preview'])
        self.memory(*args, '--apply', '--preview', plan['preview'], ok=False)
        archived = self.mutate('archive', 'demo', '--reason', 'Archive synthetic project')
        self.assertEqual(self.memory('list')['memories'], [])
        self.assertTrue(self.memory('show', 'demo')['note']['archived'])
        self.memory('undo', changed['event'], '--reason', 'Would overwrite archive', ok=False)
        undo = self.mutate('undo', archived['event'], '--reason', 'Restore active project')
        self.assertFalse(self.memory('show', 'demo')['note'].get('archived', False))
        self.assertNotEqual(undo['event'], archived['event'])
        created = self.memory('show', 'demo')['history'][0]['id']
        self.assertGreater(len(self.memory('show', 'demo')['history']), 3)
        self.memory('undo', created, '--reason', 'Creation already changed', ok=False)

    def test_pause_blocks_handoff_keeps_manual_correction_and_resume_retry(self):
        self.cli('session-open', str(self.project), '--session-id', '22222222222222222222222222222222')
        path = self.folder / 'handoff.json'
        self.cli('session-draft', '22222222222222222222222222222222', '--out', str(path))
        payload = json.loads(path.read_text())
        payload['source'].update(resource='test:handoff', title='Synthetic update', observed_at='2026-11-13T12:00:00Z')
        payload['project_state'] = 'An incoming update after resume.'
        path.write_text(json.dumps(payload))
        self.mutate('pause', '--reason', 'Review incoming knowledge')
        self.assertTrue(self.memory('status')['paused'])
        self.assertIn('paused', self.cli('session-close', '22222222222222222222222222222222', '--file', str(path), ok=False).lower())
        self.cli('session-context', '22222222222222222222222222222222')
        self.mutate('edit', 'demo', '--summary', 'Reviewed while paused.', '--body', '# Reviewed\n\nCorrectDB is agreed.',
                    '--source', 'user:review', '--reason', 'Manual correction during pause')
        self.mutate('resume', '--reason', 'Review complete')
        self.assertFalse(self.memory('status')['paused'])
        self.cli('session-draft', '22222222222222222222222222222222', '--out', str(self.folder / 'fresh.json'))
        payload = json.loads((self.folder / 'fresh.json').read_text())
        payload['source'].update(resource='test:handoff', title='Synthetic update', observed_at='2026-11-13T12:00:00Z')
        payload['project_state'] = 'An incoming update after resume.'
        (self.folder / 'fresh.json').write_text(json.dumps(payload))
        self.assertEqual(self.cli('session-close', '22222222222222222222222222222222', '--file', str(self.folder / 'fresh.json'))['status'], 'saved')

    def test_apply_rejects_changed_and_malformed_read_preconditions(self):
        shown = self.memory('show', 'demo')
        note = dict(shown['note'], id='captured-note', type='decision', title='Captured note',
                    summary='Captured decision', body='# Captured\n\nA source-derived decision.')
        payload = {'expected_revisions': {'demo': shown['revision']},
                   'changes': [{'expected_revision': 'new', 'note': note}]}
        self.mutate('edit', 'demo', '--summary', 'Project was corrected.', '--body', '# Current\n\nCorrectDB.',
                    '--source', 'user:review', '--reason', 'Correct before source apply')
        path = self.folder / 'source-apply.json'
        for expected in ({'demo': shown['revision']}, [], {'unknown-note': shown['revision']}):
            payload['expected_revisions'] = expected
            path.write_text(json.dumps(payload))
            self.cli('apply', str(path), '--actor', 'test', '--reason', 'Import reviewed source',
                     '--context', 'test:source', ok=False)
            self.memory('show', 'captured-note', ok=False)
        payload['expected_revisions'] = {'demo': self.memory('show', 'demo')['revision']}
        path.write_text(json.dumps(payload))
        self.cli('apply', str(path), '--actor', 'test', '--reason', 'Import reviewed source', '--context', 'test:source')
        self.assertEqual(self.memory('show', 'captured-note')['note']['summary'], 'Captured decision')

    def terminal(self, arguments, stages):
        command = [*self.entrypoint, '--root', str(self.vault), *arguments]
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
        entry = {'command': command, 'transport': 'PTY', 'outcome': 'running',
                 'input': [{'wait_for': needle.decode(), 'reply': reply.decode()} for needle, reply in stages]}
        self.entries.append(entry)
        output = bytearray()
        stage_output = bytearray()
        process = None
        try:
            process = subprocess.Popen(command, cwd=self.folder, env=dict(self.env, TERM='xterm-256color'),
                                       stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output.extend(data)
                    stage_output.extend(data)
                    visible = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][AB012]|\x1b[=>]', b'', stage_output)
                    if stages and stages[0][0] in visible:
                        _, reply = stages.pop(0)
                        os.write(master, reply)
                        stage_output.clear()
                if process.poll() is not None:
                    break
            process.wait(timeout=2)
            self.assertEqual(process.returncode, 0, output.decode(errors='replace'))
            self.assertFalse(stages, 'The terminal did not reach every expected interaction')
            entry.update(outcome='passed', returncode=process.returncode)
        except BaseException as exc:
            entry.update(outcome='failed', error=type(exc).__name__)
            raise
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
                entry.update(outcome='aborted', returncode=process.returncode)
            entry['terminal_output'] = output.decode(errors='replace')
            if slave is not None:
                os.close(slave)
            os.close(master)
            self.save()
        return entry['terminal_output']

    def test_terminal_menu_corrects_content_and_non_tty_default_lists(self):
        self.assertEqual(self.memory()['memories'][0]['id'], 'demo')
        script = ('3\ndemo\nCorrectDB chosen.\n# Corrected decision\n\nCorrectDB is agreed.\n.\n'
                  'user:terminal\nCorrect terminal mistake\ny\nq\n')
        output = self.terminal(['memory'], [('Choice > '.encode(), script.encode())])
        self.assertIn('Preview', output)
        self.assertEqual(self.memory('show', 'demo')['note']['summary'], 'CorrectDB chosen.')

    def test_curses_workspace_opens_memory_menu_and_returns(self):
        output = self.terminal([], [(b'm Memories', b'm'),
                                    [('Manage memories').encode(), b'q\n'],
                                    [b'm Memories', b'q']])
        self.assertIn('Manage memories', output)

    def test_summary_correction_accepts_explicit_complete_unchanged_body(self):
        shown = self.memory('show', 'demo')
        self.mutate('edit', 'demo', '--summary', 'Corrected summary of the existing complete body.',
                    '--body', shown['note']['body'], '--source', 'user:summary-review',
                    '--reason', 'The existing body already describes the correct decision')
        corrected = self.memory('show', 'demo')
        self.assertEqual(corrected['note']['body'], shown['note']['body'])
        self.assertEqual(corrected['note']['summary'], 'Corrected summary of the existing complete body.')

    def test_creation_undo_archives_without_deleting_history(self):
        shown = self.memory('show', 'demo')
        event = shown['history'][0]['id']
        self.mutate('undo', event, '--reason', 'Undo creation')
        self.assertTrue(self.memory('show', 'demo')['note']['archived'])
        self.assertEqual(len(self.memory('show', 'demo')['history']), 2)
        self.assertTrue(self.cli('check')['valid'])


if __name__ == '__main__':
    unittest.main()

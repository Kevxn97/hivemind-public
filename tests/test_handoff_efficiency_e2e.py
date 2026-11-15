"""Short handoffs retain revision, source, lifecycle and history guarantees.

Exercise the public CLI with synthetic sources: a reported decision, an inferred
question, exact replay, stale/foreign targets, rejected evidence, a linked-note
correction and no-change close. Generated instructions must invoke the complete
CLI from an unrelated working directory. Run via scripts/verify.py for evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HandoffEfficiencyE2E(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='hivemind-handoff-e2e-')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.vault = self.folder / 'vault'
        self.project = self.folder / 'project'
        self.project.mkdir()
        self.home = self.folder / 'home'
        self.home.mkdir()
        python = os.environ.get('HIVEMIND_TEST_PYTHON', sys.executable)
        self.entry = ([str(Path(python).with_name('hive'))] if os.environ.get('HIVEMIND_TEST_PYTHON')
                      else [python, '-B', str(ROOT / 'run.py')])
        self.env = {'HOME': str(self.home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.cli('init', str(self.vault), '--project', str(self.project), '--name', 'Demo', '--yes')
        (self.vault / 'sources').mkdir(exist_ok=True)
        self.source = self.vault / 'sources/decision.md'
        self.source.write_text('User decision: retry transient failures three times, then quarantine for manual review.\n')

    def process(self, command, ok=True, stdin=None):
        run = subprocess.run(command, cwd=self.folder, env=self.env, input=stdin,
                             text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode == 0, ok, run.stdout + run.stderr)
        return json.loads(run.stdout if ok else run.stderr)

    def cli(self, *args, ok=True, stdin=None):
        return self.process([*self.entry, '--root', str(self.vault), '--json', *args], ok, stdin)

    def opened(self):
        return self.cli('session-open', str(self.project), '--agent', 'codex')

    def reference(self):
        return 'demo@' + self.cli('show', 'demo')['revision']

    def record_args(self, session, expected=None, source='sources/decision.md'):
        return ['session-close', session['session_id'], '--record', '--expected', expected or self.reference(),
                '--source', source, '--source-title', 'Reviewed operator statement',
                '--observed-at', '2026-11-15T12:00:00Z']

    def snapshot(self):
        return {str(p.relative_to(self.vault)): p.read_bytes()
                for folder in ('wiki', '.history') for p in (self.vault / folder).rglob('*') if p.is_file()}

    def test_short_handoffs_replay_preserve_field_sources_and_close_without_change(self):
        first = self.opened()
        args = self.record_args(first) + ['--decision', 'Retry transient failures three times, then quarantine for manual review.',
                                         '--because', 'Avoid repeated processing.', '--agreement', 'agreed']
        saved = self.cli(*args)
        self.assertEqual(saved['status'], 'saved')
        before = self.snapshot()
        self.assertEqual(saved, self.cli(*args))
        self.assertEqual(before, self.snapshot())
        note = self.cli('show', 'demo')['note']
        source = next(s for s in note['sources'] if s['id'] == 'handoff-' + first['session_id'])
        self.assertEqual(source['sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        old_field = note['handoff']['field_sources']['decisions']
        second = self.opened()
        self.assertIn('quarantine', second['context']['text'])
        self.cli(*(self.record_args(second) + ['--question', 'Which production region is approved?', '--evidence', 'inferred']))
        updated = self.cli('show', 'demo')['note']
        self.assertEqual(updated['handoff']['field_sources']['decisions'], old_field)
        self.assertEqual(updated['handoff']['decisions'], note['handoff']['decisions'])
        self.assertEqual(updated['handoff']['field_sources']['open_questions']['evidence'], 'inferred')
        self.assertEqual(updated.get('verified', []), note.get('verified', []))
        third = self.opened()
        before = self.snapshot()
        self.assertEqual(self.cli('session-close', third['session_id'], '--no-change')['status'], 'no_change')
        self.assertEqual(self.snapshot(), before)
        state = (self.vault / '.state/sessions' / (first['session_id'] + '.json')).read_text()
        self.assertNotIn('Avoid repeated processing', state)
        self.assertTrue(self.cli('check')['valid'])

    def test_stale_foreign_missing_and_ambiguous_inputs_never_write(self):
        first, competing = self.opened(), self.opened()
        old = self.reference()
        self.cli(*(self.record_args(competing, old) + ['--question', 'Open hosted choice?']))
        before = self.snapshot()
        error = self.cli(*(self.record_args(first, old) + ['--question', 'Another question?']), ok=False)
        self.assertEqual(error['error_code'], 'REVISION_CONFLICT')
        self.assertEqual(self.snapshot(), before)
        for arguments in [
            ['session-close', first['session_id'], '--record', '--question', 'Missing provenance?'],
            self.record_args(first, 'missing@' + '0' * 64) + ['--question', 'Foreign?'],
            self.record_args(first),
            self.record_args(first) + ['--decision', 'Unreasoned decision'],
            ['session-close', first['session_id'], '--no-change', '--question', 'Must not be ignored'],
        ]:
            with self.subTest(arguments=arguments):
                self.cli(*arguments, ok=False)
                self.assertEqual(self.snapshot(), before)

    def test_missing_changed_and_linked_evidence_are_not_silently_accepted(self):
        session = self.opened()
        before = self.snapshot()
        for resource in ['sources/missing.md', 'sources/linked.md']:
            if 'linked' in resource:
                (self.vault / resource).symlink_to(self.source)
            self.cli(*(self.record_args(session, source=resource) + ['--question', 'What is approved?']), ok=False)
            self.assertEqual(self.snapshot(), before)
        args = self.record_args(session) + ['--question', 'What is approved?']
        saved = self.cli(*args)
        before = self.snapshot()
        self.source.write_text('Different observation after saving.\n')
        self.cli(*args, ok=False)
        self.assertEqual(self.snapshot(), before)
        evidence = self.cli('evidence', 'demo@' + saved['notes'][0]['revision'], '--source', 'handoff-' + session['session_id'])
        self.assertEqual(evidence['source_state'], 'changed')
        self.assertNotIn('Different observation', evidence.get('text', ''))

    def test_linked_correction_shows_new_source_and_keeps_exact_old_evidence(self):
        original = self.cli('show', 'demo')['note']
        source = {'id': 'storage-original', 'resource': 'sources/original.md', 'title': 'Original selection',
                  'observed_at': '2026-01-05T12:00:00Z'}
        raw = b'PostgreSQL was selected to match staging. No runtime check.\n'
        (self.vault / source['resource']).write_bytes(raw)
        source['sha256'] = hashlib.sha256(raw).hexdigest()
        note = {k: v for k, v in original.items() if k not in ('revision', 'path')}
        note.update(id='storage', type='decision', title='Storage', summary='Use PostgreSQL.',
                    body=raw.decode(), sources=[source], links=[{'relation': 'about', 'target': 'demo'}])
        path = self.folder / 'seed.json'
        path.write_text(json.dumps({'changes': [{'expected_revision': 'new', 'note': note}]}))
        self.cli('apply', str(path), '--actor', 'fixture', '--reason', 'Seed linked decision', '--context', 'test:fixture')
        old = self.cli('show', 'storage')
        correction = self.vault / 'sources/correction.md'
        correction.write_text('Operator correction: use SQLite locally without an external service. Hosted storage undecided.\n')
        args = ['memory', 'edit', 'storage', '--summary', 'Use SQLite locally; hosted storage undecided.',
                '--body', 'Use SQLite locally without an external service. Hosted storage is undecided. Reported correction; no runtime check.',
                '--source', 'sources/correction.md', '--reason', 'Operator corrected local scope']
        preview = self.cli(*args)
        correction.write_text(correction.read_text() + 'Additional observation.\n')
        self.cli(*args, '--apply', '--preview', preview['preview'], ok=False)
        self.assertEqual(old['revision'], self.cli('show', 'storage')['revision'])
        preview = self.cli(*args)
        self.cli(*args, '--apply', '--preview', preview['preview'])
        updated = self.cli('show', 'storage')['note']
        self.assertEqual(updated['sources'][0]['resource'], 'sources/correction.md')
        self.assertEqual(updated['sources'][1], source)
        self.assertEqual(updated['sources'][0]['sha256'], hashlib.sha256(correction.read_bytes()).hexdigest())
        fresh = self.opened()
        self.assertIn('Source ' + updated['sources'][0]['id'], fresh['context']['text'])
        self.assertNotIn('hive note ', self.cli('brief', str(self.project))['text'])
        evidence = self.cli('evidence', 'storage@' + old['revision'], '--source', source['id'])
        self.assertEqual(evidence['source_state'], 'hash_matched')
        self.assertEqual(evidence['text'], raw.decode())
        self.assertTrue(self.cli('check')['valid'])

    def test_alternate_local_paths_are_hashed_and_aliases_rejected(self):
        (self.vault / 'other').mkdir()
        session = self.opened()
        self.cli(*(self.record_args(session, source='other/../sources/decision.md') + ['--question', 'Open?']))
        saved = self.cli('show', 'demo')['note']
        source = next(s for s in saved['sources'] if s['id'] == 'handoff-' + session['session_id'])
        self.assertEqual(source.get('sha256'), hashlib.sha256(self.source.read_bytes()).hexdigest())
        (self.vault / 'alias').symlink_to(self.vault / 'sources', target_is_directory=True)
        before = self.snapshot()
        self.cli(*(self.record_args(self.opened(), source='alias/decision.md') + ['--question', 'Open?']), ok=False)
        self.assertEqual(self.snapshot(), before)

    def test_hook_uses_selected_project_and_current_refresh_revision(self):
        old = self.cli('show', 'demo')
        project = dict(old['note'], id='actual-project', cluster='projects/linked')
        decision = dict(old['note'], id='linked', cluster='projects/linked', type='decision',
                        links=[{'relation': 'about', 'target': 'actual-project'}])
        seed = self.folder / 'different-project-id.json'
        seed.write_text(json.dumps({'changes': [
            {'expected_revision': 'new', 'note': decision},
            {'expected_revision': 'new', 'note': project}]}))
        self.cli('apply', str(seed), '--actor', 'fixture', '--reason', 'Project ID differs from cluster', '--context', 'test:fixture')
        self.cli('project-unregister', str(self.project))
        self.cli('project-register', str(self.project), 'projects/linked')
        profile = self.home / 'codex'
        self.cli('connect', 'codex', '--agent-home', str(profile), '--apply')
        hooks = json.loads((profile / 'hooks.json').read_text())
        command = shlex.split(hooks['hooks']['SessionStart'][0]['hooks'][0]['command'])
        event = {'cwd': str(self.project), 'session_id': 'refresh-fixture', 'source': 'startup', 'hook_event_name': 'SessionStart'}
        def hook_text():
            return self.process(command, stdin=json.dumps(event))['hookSpecificOutput']['additionalContext']
        reference = 'actual-project@' + self.cli('show', 'actual-project')['revision']
        self.assertIn('Project write reference: ' + reference, hook_text())
        self.cli(*(self.record_args(self.opened(), expected=reference) + ['--question', 'A later open question?']))
        current = 'actual-project@' + self.cli('show', 'actual-project')['revision']
        self.assertNotEqual(reference, current)
        event['source'] = 'resume'
        self.assertIn('Project write reference: ' + current, hook_text())

    def test_generated_guidance_uses_full_cli_and_hook_supplies_write_reference(self):
        profile = self.home / 'codex'
        self.cli('connect', 'codex', '--agent-home', str(profile), '--apply')
        rules = (profile / 'AGENTS.md').read_text()
        prefix = shlex.split(re.search(r'Command prefix: `([^`]+)`', rules).group(1))
        run = subprocess.run([*prefix, 'memory', 'edit', '--help'], cwd=self.folder,
                             env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('--preview', run.stdout)
        self.assertIn('already supplied', rules)
        self.assertIn('--record', rules)
        self.assertIn('MUST use --record', rules)
        self.assertIn('Do not call session-draft for a simple handoff', rules)
        self.assertIn('--section handoff', rules)
        self.assertIn('Only close', rules)
        hooks = json.loads((profile / 'hooks.json').read_text())
        command = shlex.split(hooks['hooks']['SessionStart'][0]['hooks'][0]['command'])
        result = self.process(command, stdin=json.dumps({'cwd': str(self.project), 'session_id': 'efficiency-fixture',
                                                        'source': 'startup', 'hook_event_name': 'SessionStart'}))
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn(self.reference(), text)
        self.assertIn('Simple handoff: use session-close --record', text)
        opened = self.opened()
        commands = opened['next_commands']
        self.assertTrue(any('--record' in command and self.reference() in command for command in commands))
        full = self.cli('session-context', opened['session_id'], '--view', 'full')
        self.assertTrue(any('--record' in command for command in full['workflow']['next_commands']))
        self.assertLessEqual(len(text.split()), 500)
        self.assertIn('session-context', text[:1000])


if __name__ == '__main__':
    unittest.main()

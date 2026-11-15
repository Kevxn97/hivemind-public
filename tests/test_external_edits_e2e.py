"""Exercise recovery of a manually edited note through the actual CLI and hooks."""

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


class ExternalEditsE2E(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='hivemind-external-edits-')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.vault = self.folder / 'vault'
        self.home = self.folder / 'synthetic-home'
        self.project = self.folder / 'bench-project'
        self.home.mkdir()
        self.project.mkdir()
        self.env = {'HOME': str(self.home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT)}
        self.entrypoint = [sys.executable, '-B', str(ROOT / 'run.py')]
        self.core = [sys.executable, '-B', str(ROOT / 'hivemind/hive.py')]
        self.cli('init', str(self.vault), '--project', str(self.project), '--name', 'Bench', '--yes')
        self.note = self.vault / 'wiki/project/bench.md'
        self.original = self.note.read_bytes()
        self.metadata = ('--actor', 'fixture-editor', '--reason', 'Record reviewed external edit',
                         '--context', 'test:external-edit')

    def process(self, command, ok=True, payload=None):
        result = subprocess.run(command, cwd=self.folder, env=self.env, capture_output=True, text=True,
                                input=json.dumps(payload) if payload is not None else None, timeout=20)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout) if ok else json.loads(result.stderr)

    def cli(self, *args, ok=True):
        return self.process([*self.entrypoint, '--root', str(self.vault), '--json', *args], ok=ok)

    def connect(self, agent):
        config = ('hooks.json' if agent == 'codex' else 'settings.json')
        home = self.home / ('.' + agent)
        self.cli('connect', agent, '--agent-home', str(home), '--apply')
        value = json.loads((home / config).read_text())
        return shlex.split(value['hooks']['SessionStart'][0]['hooks'][0]['command'])

    def snapshot(self):
        return {str(path.relative_to(self.vault)): path.read_bytes() for path in self.vault.rglob('*')
                if path.is_file() and not path.is_symlink()}

    def adopt(self, *args, ok=True, note='bench', metadata=None):
        return self.cli('adopt', note, *(self.metadata if metadata is None else metadata), *args, ok=ok)

    def test_manual_edit_is_visible_in_both_hooks_and_adopt_preserves_exact_bytes(self):
        hooks = {agent: self.connect(agent) for agent in ('codex', 'claude')}
        before_history = self.cli('history', 'bench')
        modified = self.original.replace(b'\n', b'\r\n') + '\r\nUnjournaled umlaut ä after manual edit.\r\n'.encode()
        self.note.write_bytes(modified)
        before_preview = self.snapshot()
        self.assertIn('hive adopt bench', self.cli('check', ok=False)['error'])
        for agent, command in hooks.items():
            result = self.process(command, payload={'cwd': str(self.project), 'session_id': 'dirty-' + agent,
                                                    'source': 'startup', 'hook_event_name': 'SessionStart'})
            warning = result['hookSpecificOutput']['additionalContext']
            self.assertIn('hive adopt bench', warning)
            self.assertNotIn('Unjournaled umlaut', warning)
            self.assertNotIn('HiveMind session:', warning)
        for entrypoint in (self.entrypoint, self.core):
            report = self.process([*entrypoint, '--root', str(self.vault), 'doctor', '--home', str(self.home)]
                                  + (['--json'] if entrypoint == self.entrypoint else []))
            checks = {check['id']: check for check in report['checks']}
            self.assertEqual(checks['canonical']['state'], 'external_edits')
            self.assertIn('hive adopt bench', checks['canonical']['detail'])
            self.assertEqual(checks['codex.hooks']['state'], 'connected')
        preview = self.adopt()
        self.assertEqual(preview['status'], 'preview')
        self.assertIn('+Unjournaled umlaut ä after manual edit.', preview['diff'])
        self.assertEqual(preview['after_revision'], hashlib.sha256(modified).hexdigest())
        self.assertEqual(self.snapshot(), before_preview)
        applied = self.adopt('--apply', '--preview', preview['preview'])
        self.assertEqual(applied['status'], 'adopted')
        self.assertEqual(self.note.read_bytes(), modified)
        self.assertTrue(self.cli('check')['valid'])
        history = self.cli('history', 'bench')
        self.assertEqual(history[:-1], before_history)
        self.assertEqual(history[-1]['revision'], preview['after_revision'])
        self.assertEqual(history[-1]['previous_revision'], before_history[-1]['revision'])
        self.assertEqual(history[-1]['actor'], 'fixture-editor')
        self.assertEqual(history[-1]['context'], 'test:external-edit')
        event = json.loads((self.vault / '.history' / (applied['event'] + '.json')).read_text())
        self.assertEqual(event['changes'][0]['before'].encode(), self.original)
        self.assertEqual(event['changes'][0]['after'].encode(), modified)
        self.assertEqual(self.cli('show', 'bench')['revision'], preview['after_revision'])
        hits = self.cli('search', 'umlaut')
        self.assertEqual([(hit['id'], hit['revision']) for hit in hits], [('bench', preview['after_revision'])])
        result = self.process(hooks['codex'], payload={'cwd': str(self.project), 'session_id': 'clean-codex',
                                                       'source': 'startup', 'hook_event_name': 'SessionStart'})
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIsNotNone(re.search(r'HiveMind session: [0-9a-f]{32}', context))
        self.assertIn('Unjournaled umlaut', context)
        self.assertLessEqual(len(context.split()), 500)

    def test_apply_requires_a_fresh_preview_bound_to_bytes_and_provenance(self):
        self.note.write_bytes(self.original + b'\nManual change.\n')
        before = self.snapshot()
        self.assertIn('preview', self.adopt('--apply', ok=False)['error'].lower())
        preview = self.adopt()
        wrong_metadata = (*self.metadata[:-1], 'test:different-evidence')
        for token in ('0' * 64, preview['preview']):
            with self.subTest(token=token):
                self.assertIn('preview', self.adopt('--apply', '--preview', token, metadata=wrong_metadata,
                                                   ok=False)['error'].lower())
        self.assertEqual(self.snapshot(), before)
        self.note.write_bytes(self.original + b'\nNew manual change.\n')
        before = self.snapshot()
        self.assertIn('preview', self.adopt('--apply', '--preview', preview['preview'], ok=False)['error'].lower())
        self.assertEqual(self.snapshot(), before)

    def test_each_external_edit_requires_its_own_review(self):
        draft = self.folder / 'draft.json'
        self.cli('draft', 'bench', '--out', str(draft))
        payload = json.loads(draft.read_text())
        change = payload['changes'][0]
        change['expected_revision'] = 'new'
        change['note'].update(id='second-bench', cluster='projects/second-bench')
        draft.write_text(json.dumps(payload))
        self.cli('apply', str(draft), *self.metadata)
        second_note = self.vault / 'wiki/project/second-bench.md'
        self.note.write_bytes(self.original + b'\nFirst manual edit.\n')
        second_edit = second_note.read_bytes() + b'\nSecond manual edit.\n'
        second_note.write_bytes(second_edit)
        preview = self.adopt()
        self.adopt('--apply', '--preview', preview['preview'])
        self.assertEqual(len(self.cli('history', 'bench')), 2)
        self.assertEqual(len(self.cli('history', 'second-bench')), 1)
        self.assertEqual(second_note.read_bytes(), second_edit)
        self.assertIn('hive adopt second-bench', self.cli('check', ok=False)['error'])
        preview = self.adopt(note='second-bench')
        self.adopt('--apply', '--preview', preview['preview'], note='second-bench')
        self.assertTrue(self.cli('check')['valid'])

    def test_moved_new_invalid_and_symlinked_notes_cannot_be_adopted(self):
        self.assertIn('existing journaled note', self.adopt(note='untracked', ok=False)['error'])
        invalid = self.original.replace(b'title: "Bench"', b'title: null')
        self.assertNotEqual(invalid, self.original)
        self.note.write_bytes(invalid)
        self.assertIn('title', self.adopt(ok=False)['error'])
        self.note.write_bytes(self.original + b'\nManual change.\n')
        preview = self.adopt()
        moved = self.note.with_name('moved.md')
        self.note.rename(moved)
        self.assertIn('missing', self.adopt('--apply', '--preview', preview['preview'], ok=False)['error'].lower())
        outside = self.folder / 'outside.md'
        moved.rename(outside)
        self.note.symlink_to(outside)
        self.assertIn('symlink', self.adopt('--apply', '--preview', preview['preview'], ok=False)['error'].lower())
        self.note.unlink()
        self.note.parent.rename(self.folder / 'outside-dir')
        (self.folder / 'outside-dir' / self.note.name).write_bytes(outside.read_bytes())
        self.note.parent.symlink_to(self.folder / 'outside-dir', target_is_directory=True)
        self.assertIn('symlink', self.adopt('--apply', '--preview', preview['preview'], ok=False)['error'].lower())
        self.assertEqual(len(self.cli('history', 'bench')), 1)
        self.assertEqual(outside.read_bytes(), self.original + b'\nManual change.\n')

    def test_malformed_nested_note_metadata_returns_a_cli_error(self):
        modified = re.sub(rb'(?m)^sources: .*$', b'sources: [null]', self.original)
        self.assertNotEqual(modified, self.original)
        self.note.write_bytes(modified)
        before = self.snapshot()
        self.assertIn('metadata', self.adopt(ok=False)['error'])
        self.assertEqual(self.snapshot(), before)

    def test_adoption_does_not_rebuild_through_a_symlinked_route_directory(self):
        self.note.write_bytes(self.original + b'\nManual change.\n')
        preview = self.adopt()
        routes = self.vault / 'wiki/clusters/projects'
        outside = self.folder / 'outside-routes'
        routes.rename(outside)
        routes.symlink_to(outside, target_is_directory=True)
        before = {str(path.relative_to(outside)): path.stat().st_mtime_ns for path in outside.rglob('*.md')}
        self.assertIn('symlink', self.adopt('--apply', '--preview', preview['preview'], ok=False)['error'].lower())
        self.assertEqual(before, {str(path.relative_to(outside)): path.stat().st_mtime_ns
                                  for path in outside.rglob('*.md')})
        self.assertEqual(len(self.cli('history', 'bench')), 1)

    def test_pending_transaction_cannot_be_bypassed(self):
        self.note.write_bytes(self.original + b'\nManual change.\n')
        preview = self.adopt()
        (self.vault / '.pending.json').write_text('{}\n')
        before = self.snapshot()
        self.assertEqual(self.adopt('--apply', '--preview', preview['preview'], ok=False)['error_code'],
                         'RECOVERY_REQUIRED')
        self.assertEqual(self.snapshot(), before)

    def test_adoption_recovers_without_overwriting_a_later_editor_change(self):
        self.note.write_bytes(self.original + b'\nManual change.\n')
        preview = self.adopt()
        applied = self.adopt('--apply', '--preview', preview['preview'])
        journal = self.vault / '.history' / (applied['event'] + '.json')
        pending = self.vault / '.pending.json'
        journal.rename(pending)
        inspected = self.note.read_bytes()
        edited_again = inspected + b'\nAn editor saved a newer change.\n'
        self.note.write_bytes(edited_again)
        self.assertIn('Recovery conflict', self.cli('recover', ok=False)['error'])
        self.assertEqual(self.note.read_bytes(), edited_again)
        self.assertTrue(pending.is_file())
        self.assertFalse(journal.exists())
        self.note.write_bytes(inspected)
        self.assertTrue(self.cli('recover')['recovered'])
        self.assertFalse(pending.exists())
        self.assertTrue(journal.is_file())
        self.assertTrue(self.cli('check')['valid'])


if __name__ == '__main__':
    unittest.main()

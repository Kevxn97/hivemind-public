import os
"""User journeys across the public launcher, canonical writes and Claude hook."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1] / 'hivemind'
sys.path.insert(0, str(BASE))
import hive


class WorkflowCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        note = {'id': 'demo', 'type': 'project', 'cluster': 'projects/demo',
                'title': 'Demo-Projekt', 'summary': 'Quellenaufnahme mit geprüften Revisionen.',
                'body': '# Demo\n\nProjektstand für synthetische Übergaben.',
                'status': 'stable', 'evidence': 'reported', 'sensitivity': 'internal',
                'aliases': [], 'tags': [], 'sources': [{'id': 'fixture', 'title': 'Testquelle',
                'resource': 'test:fixture'}], 'verified': [], 'stale_after': '2100-01-01T00:00:00Z',
                'links': []}
        hive.apply(self.root, {'changes': [{'expected_revision': 'new', 'note': note}]},
                   'test', 'synthetic fixture', 'test:fixture')
        self.cli('project-register', str(self.repo), 'projects/demo')

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(BASE / 'launcher.py'), '--root', str(self.root),
                            *args, '--json'], capture_output=True, text=True, timeout=15)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return p

    def test_saved_handoff_reappears_in_fresh_claude_context(self):
        opened = self.cli('session-open', str(self.repo), '--agent', 'codex', '--task', 'Quellenaufnahme')
        sid = opened['session_id']
        path = self.root / 'handoff.json'
        self.cli('session-draft', sid, '--out', str(path))
        payload = json.loads(path.read_text())
        payload['source'].update(resource='test:decision-42', title='Explizite Testentscheidung',
                                 observed_at='2026-10-29T10:00:00Z')
        payload['project_state'] = 'Quellenaufnahme ist auf lokale Änderungen begrenzt.'
        payload['decisions'] = [{'decision': 'Zebramuster-Prüfung vor Übernahme',
                                 'reason': 'Verhindert doppelte Quittungen', 'status': 'agreed'}]
        path.write_text(json.dumps(payload))
        saved = self.cli('session-close', sid, '--file', str(path))
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved, self.cli('session-close', sid, '--file', str(path)))
        current = hive.load_notes(self.root)['demo']
        self.assertEqual(saved['notes'][0]['revision'], hive.digest(current[1]))
        hook = subprocess.run([sys.executable, str(BASE / 'hive.py'), '--root', str(self.root), 'brief', '--hook'],
                              input=json.dumps({'cwd': str(self.repo)}), capture_output=True, text=True, timeout=15)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        offered = json.loads(hook.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('Zebramuster', offered)
        self.assertIn('HiveMind-Sitzung:', offered)
        self.assertNotIn('HiveMind-Sitzung: ' + sid, offered)  # Old source IDs remain attributable.
        self.assertTrue(hive.check(self.root)['valid'])

    def test_task_budget_and_no_change_do_not_write_knowledge(self):
        before = len(hive.events(self.root))
        result = self.cli('brief', str(self.repo), '--task', 'Quellenaufnahme', '--budget', '180')
        self.assertLessEqual(len(result['text'].split()), 180)
        self.cli('brief', str(self.repo), '--budget', '0', ok=False)
        opened = self.cli('session-open', str(self.repo), '--agent', 'claude')
        saved = self.cli('session-close', opened['session_id'], '--no-change')
        self.assertEqual(saved['status'], 'no_change')
        self.assertEqual(self.cli('session-status', opened['session_id'])['status'], 'no_change')
        self.assertEqual(len(hive.events(self.root)), before)

    def test_feedback_and_evidence_views_are_reachable_through_launcher(self):
        feedback = self.cli('feedback', 'Quellenaufnahme', '--unhelpful', '--result', 'demo')
        self.assertFalse(feedback['helpful'])
        for args in [('why', 'demo'), ('impact', 'demo'), ('lessons', 'projects/demo'),
                     ('changes', 'projects/demo', '--limit', '3')]:
            value = self.cli(*args)
            self.assertIn('text', value)
            self.assertIn('items', value)
        p = subprocess.run([sys.executable, str(BASE / 'hive.py'), '--root', str(self.root),
                            'changes', 'projects/demo'], capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('items', json.loads(p.stdout))

    def test_default_brief_respects_disabled_legacy_routing(self):
        self.cli('project-unregister', str(self.repo))
        record, raw, _ = hive.load_notes(self.root)['demo']
        record['body'] += '\n\nLegacy folder: `' + str(self.repo) + '`.'
        hive.apply(self.root, {'changes': [{'expected_revision': hive.digest(raw), 'note': record}]},
                   'test', 'synthetic legacy path', 'test:fixture')
        (self.root / 'config').mkdir(exist_ok=True)
        (self.root / 'config/projects.json').write_text(json.dumps({'version': 1, 'legacy_fallback': False}))
        self.cli('brief', str(self.repo), ok=False)
        self.cli('brief', str(self.repo), '--task', 'Quellenaufnahme', ok=False)
        self.cli('project-register', str(self.repo), 'projects/demo')
        self.assertEqual(self.cli('brief', str(self.repo))['folder'], str(self.repo))


if __name__ == '__main__':
    unittest.main()

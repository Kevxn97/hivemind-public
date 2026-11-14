"""Public two-minute demo acceptance; all fixtures are synthetic and local.

Run via scripts/verify.py to retain outcome, source hashes and environment for
every run. Exercise the installed console entrypoint too when requested by CI.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def snapshot(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


class DemoE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hive-demo-acceptance-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        self.home = self.folder / 'home'
        self.home.mkdir()
        self.private = self.folder / 'selected private vault'
        self.private.mkdir()
        (self.private / 'private.txt').write_text('SYNTHETIC_PRIVATE_SENTINEL')
        self.env = {'HOME': str(self.home), 'HIVEMIND_HOME': str(self.private),
                    'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        python = os.environ.get('HIVEMIND_TEST_PYTHON', sys.executable)
        self.cli = ([str(Path(python).with_name('hive'))] if os.environ.get('HIVEMIND_TEST_PYTHON')
                    else [python, '-B', '-m', 'hivemind'])
        if not os.environ.get('HIVEMIND_TEST_PYTHON'):
            self.env['PYTHONPATH'] = str(ROOT)

    def call(self, *args, ok=True):
        result = subprocess.run([*self.cli, *map(str, args)], cwd=self.folder,
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout if ok else result.stderr)

    def test_demo_saves_switches_corrects_and_proves_next_context_is_current(self):
        private_before, source_before = snapshot(self.private), snapshot(ROOT / 'hivemind')
        destination = self.folder / 'demo with spaces'
        result = self.call('demo', '--directory', destination, '--json')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['scope'], 'synthetic_local_demo')
        self.assertFalse(result['real_agent_execution'])
        self.assertEqual([s['id'] for s in result['steps']],
                         ['store_decision', 'switch_session', 'correct_decision', 'switch_again', 'show_change'])
        self.assertTrue(all(s['outcome'] == 'passed' for s in result['steps']))
        self.assertIn('three retries', result['before']['context'])
        self.assertIn('five retries', result['after']['context'])
        self.assertNotIn('three retries', result['after']['context'])
        self.assertNotEqual(result['before']['revision'], result['after']['revision'])
        self.assertEqual(json.loads(Path(result['evidence']).read_text())['status'], 'passed')
        self.assertTrue(Path(result['transcript']).is_file())
        self.assertTrue(result['commands'])
        check = self.call('--root', result['vault'], 'check', '--json')
        self.assertTrue(check['valid'])
        self.assertEqual(check['events'], 3)
        self.assertEqual(private_before, snapshot(self.private))
        self.assertEqual(source_before, snapshot(ROOT / 'hivemind'))
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', Path(result['transcript']).read_text())

    def test_demo_refuses_existing_data_symlink_and_vault_destinations(self):
        existing = self.folder / 'existing'
        existing.mkdir()
        (existing / 'mine.txt').write_text('SYNTHETIC_DO_NOT_OVERWRITE')
        before = snapshot(existing)
        self.call('demo', '--directory', existing, '--json', ok=False)
        self.assertEqual(before, snapshot(existing))
        alias = self.folder / 'alias'
        alias.symlink_to(existing, target_is_directory=True)
        self.call('demo', '--directory', alias / 'demo', '--json', ok=False)
        self.assertEqual(before, snapshot(existing))
        self.call('demo', '--directory', self.private / 'demo', '--json', ok=False)
        self.assertFalse((self.private / 'demo').exists())

    def test_plain_demo_explains_result_and_evidence_without_json_noise(self):
        result = subprocess.run([*self.cli, 'demo', '--directory', str(self.folder / 'plain')],
                                cwd=self.folder, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('HiveMind demo', result.stdout)
        self.assertIn('five retries', result.stdout)
        self.assertIn('report.json', result.stdout)
        self.assertIn('synthetic', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()

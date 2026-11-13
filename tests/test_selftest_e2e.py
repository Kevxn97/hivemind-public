"""The public self-test through a fresh Python process, using synthetic inputs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

DRIVER = r'''
import json, os, sys
sys.dont_write_bytecode = True
try:
    from hivemind import selftest
except ModuleNotFoundError as exc:
    if exc.name != 'hivemind': raise
    import selftest
if os.environ.get('HIVE_SELFTEST_EXECUTABLE'):
    selftest.sys.executable = os.environ['HIVE_SELFTEST_EXECUTABLE']
print(json.dumps(selftest.run(os.environ.get('HIVE_SELFTEST_EVIDENCE'))))
'''

class SelfTestE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.selected = self.root / 'selected-vault'
        self.selected.mkdir()
        (self.selected / 'private.txt').write_text('PRIVATE_SELECTED_VAULT_SENTINEL')

    def run_flow(self, evidence=None, executable=None):
        env = dict(os.environ, HIVEMIND_HOME=str(self.selected), PYTHONDONTWRITEBYTECODE='1')
        env.pop('HIVE_SELFTEST_EVIDENCE', None)
        env.pop('HIVE_SELFTEST_EXECUTABLE', None)
        if evidence:
            env['HIVE_SELFTEST_EVIDENCE'] = str(evidence)
        if executable:
            env['HIVE_SELFTEST_EXECUTABLE'] = str(executable)
        result = subprocess.run([sys.executable, '-c', DRIVER], capture_output=True, text=True, env=env, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        saved = json.loads(Path(report['evidence_path']).read_text())
        self.assertEqual(saved, report)
        self.assertEqual((self.selected / 'private.txt').read_text(), 'PRIVATE_SELECTED_VAULT_SENTINEL')
        self.assertEqual([p.name for p in self.selected.iterdir()], ['private.txt'])
        self.assertNotIn(str(self.selected), json.dumps(report))
        self.assertNotIn('PRIVATE_SELECTED_VAULT_SENTINEL', json.dumps(report))
        return report

    def test_complete_local_handoff_and_lifecycle(self):
        report = self.run_flow(self.root / 'evidence')
        self.assertEqual(report['scope'], 'synthetic_local_hooks_and_handoff')
        self.assertEqual(report['outcome'], 'passed', json.dumps(report, indent=2))
        self.assertTrue(report['passed'])
        self.assertFalse(report['installer_checked'])
        self.assertTrue(report['source_sha256'])
        stages = {stage['id']: stage for stage in report['stages']}
        for name in ['initialize', 'codex_session_start', 'draft', 'save_handoff',
                     'claude_session_start', 'session_end', 'explicit_no_change', 'isolation']:
            self.assertEqual(stages[name]['outcome'], 'passed', stages)
        self.assertTrue(all(command['returncode'] == 0 for command in report['commands']))
        serialized = json.dumps(report)
        self.assertIn('selftest:decision', serialized)
        self.assertIn('Use three retries for the synthetic fixture.', serialized)
        self.assertIn('launcher.py', serialized)
        self.assertIn('hive.py', serialized)
        self.assertTrue((Path(report['evidence_path']).parent / 'fixture.json').is_file())
        self.assertTrue((Path(report['evidence_path']).parent / 'handoff.json').is_file())

    def test_failed_process_saves_replayable_evidence(self):
        executable = self.root / 'failing-python'
        executable.write_text('#!/bin/sh\nprintf "synthetic command failure\\n" >&2\nexit 17\n')
        executable.chmod(0o700)
        report = self.run_flow(self.root / 'failed-evidence', executable=executable)
        self.assertFalse(report['passed'])
        self.assertEqual(report['outcome'], 'failed')
        self.assertTrue(report['commands'])
        self.assertEqual(report['commands'][0]['returncode'], 17)
        self.assertIn('synthetic command failure', report['commands'][0]['stderr'])
        self.assertEqual(report['stages'][-1]['outcome'], 'failed')
        self.assertTrue(report['environment'])
        self.assertTrue(report['source_sha256'])

    def test_unusable_evidence_destination_still_saves_failure(self):
        blocked = self.root / 'evidence-file'
        blocked.write_text('preserve existing file')
        report = self.run_flow(blocked)
        self.addCleanup(__import__('shutil').rmtree, Path(report['evidence_path']).parent)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['stages'][-1]['id'], 'prepare')
        self.assertEqual(blocked.read_text(), 'preserve existing file')

    def test_default_evidence_survives_and_repeated_runs_are_independent(self):
        first = self.run_flow()
        self.addCleanup(__import__('shutil').rmtree, Path(first['evidence_path']).parent)
        second = self.run_flow()
        self.addCleanup(__import__('shutil').rmtree, Path(second['evidence_path']).parent)
        self.assertTrue(first['passed'], first)
        self.assertTrue(second['passed'], second)
        self.assertNotEqual(first['evidence_path'], second['evidence_path'])
        self.assertTrue(Path(first['evidence_path']).exists())
        self.assertTrue(Path(second['evidence_path']).exists())

if __name__ == '__main__':
    unittest.main()

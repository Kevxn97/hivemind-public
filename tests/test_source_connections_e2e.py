"""Synthetic CLI flows for host-owned source setup and selected capture.

Failure requirements cover preview writes, malformed/foreign/drifted config,
unsafe endpoints, changed previews, wrong projects, replay and paused writes.
All subprocesses use synthetic HOME/config directories and an empty PATH.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get('HIVEMIND_TEST_PYTHON', sys.executable)
INSTALLED = bool(os.environ.get('HIVEMIND_TEST_PYTHON'))
CLI = [str(Path(PYTHON).with_name('hive'))] if INSTALLED else [PYTHON, '-B', '-m', 'hivemind']


class SourceConnectionsE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        cls.output = Path(os.environ.get('HIVEMIND_E2E_EVIDENCE_DIR', '/private/tmp/hivemind-sources-evidence')) / stamp
        cls.output.mkdir(parents=True)
        cls.report = {'command': sys.orig_argv, 'environment': {'python': sys.version, 'platform': platform.platform(),
            'child_environment': 'Explicit HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, empty PATH; no inherited credentials.'},
            'starting_data': 'Fresh synthetic vault and registered project; selected fictional mail summary; no provider/host CLI calls.',
            'source_sha256': {str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in BASE.rglob('*.py')},
            'entrypoint': CLI, 'installed_mode': INSTALLED,
            'outcome': 'running', 'cases': []}
        cls.save()

    @classmethod
    def save(cls):
        (cls.output / 'evidence.json').write_text(json.dumps(cls.report, indent=2) + '\n')

    @classmethod
    def tearDownClass(cls):
        cls.report['outcome'] = 'passed' if all(c['outcome'] == 'passed' for c in cls.report['cases']) else 'failed'
        cls.save()
        print('Source connections evidence', cls.output / 'evidence.json')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hive selected source ')
        self.fixture = Path(self.temp.name).resolve()
        self.root = self.fixture / 'vault'
        self.project = self.fixture / 'project with spaces'
        self.project.mkdir()
        home = self.fixture / 'home'
        home.mkdir()
        self.env = {'HOME': str(home), 'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1',
                    'CODEX_HOME': str(home / '.codex'), 'CLAUDE_CONFIG_DIR': str(home / '.claude')}
        if not INSTALLED:
            self.env['PYTHONPATH'] = str(BASE)
        self.case = {'name': self.id(), 'outcome': 'running', 'commands': []}
        self.report['cases'].append(self.case)
        self.addCleanup(self.finish)
        self.call('init', '--yes', '--project', str(self.project), '--name', 'Sample Project', '--agent', 'none')
        self.selected = self.fixture / 'selected.json'
        self.selection = {'project': 'projects/sample-project', 'title': 'Pilot scope agreed',
            'summary': 'The fictional pilot covers one team.', 'source': {'kind': 'gmail',
            'resource': 'https://mail.google.com/mail/u/0/#inbox/synthetic-message',
            'title': 'Selected fictional pilot mail', 'observed_at': '2026-11-14T09:00:00Z'}}
        self.selected.write_text(json.dumps(self.selection))

    def finish(self):
        result = self._outcome.result
        errors = [str(error) for test, error in result.failures + result.errors if test is self or getattr(test, 'test_case', None) is self]
        self.case.update(outcome='failed' if errors else 'passed', failures=errors)
        self.save()
        self.temp.cleanup()

    def call(self, *args, succeeds=True, structured=True):
        command = [*CLI, '--root', str(self.root), *(['--json'] if structured else []), *args]
        result = subprocess.run(command, capture_output=True, text=True, cwd=self.fixture, env=self.env, timeout=20)
        self.case['commands'].append({'argv': command, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        self.save()
        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
        return json.loads(result.stdout) if succeeds and structured else result

    def snapshot(self):
        return {str(p.relative_to(self.fixture)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.fixture.rglob('*') if p.is_file()}

    def mcp(self, agent='codex', action='setup', *extra, succeeds=True):
        args = ['sources', action]
        if action == 'setup':
            args += ['mcp', '--url', 'https://mcp.example.test/service']
        return self.call(*args, '--agent', agent, '--project', str(self.project), '--name', 'sample', *extra, succeeds=succeeds)

    def test_guidance_and_templates_are_read_only_and_human_readable(self):
        before = self.snapshot()
        text = self.call('sources', structured=False).stdout
        self.assertIn('sources setup gmail', text)
        for agent in ('codex', 'claude'):
            for kind in ('gmail', 'calendar'):
                plan = self.call('sources', 'setup', kind, '--agent', agent)
                self.assertFalse(plan['applied'])
                self.assertEqual(plan['host_connection'], 'unverified')
                self.assertEqual(plan['capture_template']['source']['kind'], kind)
                self.assertIn('selected.json', plan['text'])
                self.assertTrue(plan['documentation'])
        self.assertEqual(before, self.snapshot())

    def test_project_config_preview_apply_repeat_and_owned_removal(self):
        for agent in ('codex', 'claude'):
            with self.subTest(agent=agent):
                path = self.project / ('.codex/config.toml' if agent == 'codex' else '.mcp.json')
                path.parent.mkdir(exist_ok=True)
                initial = '# Keep this comment\nmodel = "unchanged"\n' if agent == 'codex' else json.dumps({'extra': True, 'mcpServers': {'other': {'type': 'http', 'url': 'https://other.example.test/mcp'}}})
                path.write_text(initial)
                before = self.snapshot()
                plan = self.mcp(agent)
                self.assertEqual(plan['config_path'], str(path))
                self.assertEqual(before, self.snapshot())
                self.assertTrue(self.mcp(agent, 'setup', '--apply')['applied'])
                current = path.read_text()
                self.assertIn('https://mcp.example.test/service', current)
                self.assertIn('unchanged' if agent == 'codex' else 'other', current)
                before = self.snapshot()
                self.assertFalse(self.mcp(agent, 'setup', '--apply')['changed'])
                self.assertEqual(before, self.snapshot())
                self.mcp(agent, 'remove', '--apply')
                if agent == 'codex':
                    self.assertEqual(path.read_text(), initial)
                else:
                    self.assertEqual(json.loads(path.read_text()), json.loads(initial))

    def test_drift_foreign_and_malformed_config_refuse_without_mutation(self):
        for agent in ('codex', 'claude'):
            with self.subTest(agent=agent):
                self.mcp(agent, 'setup', '--apply')
                path = self.project / ('.codex/config.toml' if agent == 'codex' else '.mcp.json')
                path.write_text(path.read_text().replace('mcp.example.test', 'changed.example.test'))
                before = self.snapshot()
                self.mcp(agent, 'setup', '--apply', succeeds=False)
                self.mcp(agent, 'remove', '--apply', succeeds=False)
                self.assertEqual(before, self.snapshot())
                path.write_text('broken = [ SECRET_SENTINEL' if agent == 'codex' else '{SECRET_SENTINEL')
                result = self.mcp(agent, 'setup', '--apply', succeeds=False)
                self.assertNotIn('SECRET_SENTINEL', result.stderr)
        # A separately declared server is never adopted, even with equal URL.
        path = self.project / '.mcp.json'
        path.write_text(json.dumps({'mcpServers': {'unowned': {'type': 'http', 'url': 'https://mcp.example.test/service'}}}))
        before = self.snapshot()
        self.call('sources', 'setup', 'mcp', '--agent', 'claude', '--project', str(self.project), '--name', 'unowned', '--url', 'https://mcp.example.test/service', '--apply', succeeds=False)
        self.assertEqual(before, self.snapshot())

    def test_endpoint_and_unregistered_project_validation(self):
        for url in ('http://example.test/mcp', 'https://user:secret@example.test/mcp', 'https://example.test/mcp?token=SECRET', 'https://example.test/mcp#SECRET'):
            before = self.snapshot()
            result = self.call('sources', 'setup', 'mcp', '--agent', 'codex', '--project', str(self.project), '--name', 'sample', '--url', url, '--apply', succeeds=False)
            self.assertNotIn('SECRET', result.stderr)
            self.assertEqual(before, self.snapshot())
        other = self.fixture / 'unknown'
        other.mkdir()
        self.call('sources', 'setup', 'mcp', '--agent', 'codex', '--project', str(other), '--name', 'sample', '--url', 'https://example.test/mcp', '--apply', succeeds=False)

    def test_capture_preview_apply_journal_provenance_retrieval_and_repeat(self):
        before = self.snapshot()
        plan = self.call('sources', 'capture', '--file', str(self.selected))
        self.assertFalse(plan['applied'])
        self.assertEqual(plan['note']['evidence'], 'reported')
        self.assertEqual(before, self.snapshot())
        self.assertIn('Pilot scope agreed', plan['text'])
        self.call('sources', 'capture', '--file', str(self.selected), '--apply', succeeds=False)
        value = self.call('sources', 'capture', '--file', str(self.selected), '--apply', '--preview', plan['preview'])
        self.assertTrue(value['applied'])
        self.assertTrue(value['event'])
        note_path = self.root / value['note_path']
        self.assertIn('reported', note_path.read_text())
        self.assertIn(self.selection['source']['resource'], note_path.read_text())
        brief = self.call('brief', str(self.project), structured=False).stdout
        self.assertIn('fictional pilot', brief)
        plan = self.call('sources', 'capture', '--file', str(self.selected))
        self.assertEqual(plan['status'], 'already_captured')
        before = self.snapshot()
        self.assertFalse(self.call('sources', 'capture', '--file', str(self.selected), '--apply', '--preview', plan['preview'])['applied'])
        self.assertEqual(before, self.snapshot())

    def test_changed_file_or_project_invalidates_capture_preview(self):
        plan = self.call('sources', 'capture', '--file', str(self.selected))
        self.selected.write_text(json.dumps(self.selection) + '\n')
        before = self.snapshot()
        self.call('sources', 'capture', '--file', str(self.selected), '--apply', '--preview', plan['preview'], succeeds=False)
        self.assertEqual(before, self.snapshot())
        plan = self.call('sources', 'capture', '--file', str(self.selected))
        # Simulate an independently journalled project update through its normal draft/apply path.
        draft = self.fixture / 'project-draft.json'
        self.call('draft', 'sample-project', '--out', str(draft), structured=False)
        payload = json.loads(draft.read_text())
        payload['changes'][0]['note']['summary'] = 'Project scope revised by another editor.'
        draft.write_text(json.dumps(payload))
        self.call('apply', str(draft), '--actor', 'synthetic', '--reason', 'Concurrent fixture update', '--context', 'synthetic:test', structured=False)
        before = self.snapshot()
        self.call('sources', 'capture', '--file', str(self.selected), '--apply', '--preview', plan['preview'], succeeds=False)
        self.assertEqual(before, self.snapshot())

    def test_capture_rejects_unknown_fields_raw_batches_bad_project_and_symlinks(self):
        for extra in ({'messages': ['raw mailbox']}, {'project': 'projects/unknown'}, {'summary': 'x' * 281}):
            self.selected.write_text(json.dumps({**self.selection, **extra}))
            before = self.snapshot()
            self.call('sources', 'capture', '--file', str(self.selected), succeeds=False)
            self.assertEqual(before, self.snapshot())
        self.selected.unlink()
        self.selected.symlink_to(self.fixture / 'absent')
        self.call('sources', 'capture', '--file', str(self.selected), succeeds=False)

    def test_pause_keeps_preview_available_and_blocks_capture_and_source_writes(self):
        plan = self.call('sources', 'capture', '--file', str(self.selected))
        pause = self.call('memory', 'pause', '--reason', 'Synthetic source pause')
        self.call('memory', 'pause', '--reason', 'Synthetic source pause', '--apply', '--preview', pause['preview'])
        before = self.snapshot()
        self.call('sources', 'capture', '--file', str(self.selected))
        self.call('sources', 'capture', '--file', str(self.selected), '--apply', '--preview', plan['preview'], succeeds=False)
        self.mcp('codex', 'setup', '--apply', succeeds=False)
        self.assertEqual(before, self.snapshot())
        resume = self.call('memory', 'resume', '--reason', 'Synthetic source resume')
        self.call('memory', 'resume', '--reason', 'Synthetic source resume', '--apply', '--preview', resume['preview'])
        self.call('sources', 'capture', '--file', str(self.selected), '--apply', '--preview', plan['preview'])

    def test_host_config_symlink_is_preserved_and_unrelated_edits_survive_removal(self):
        outside = self.fixture / 'unrelated.json'
        outside.write_text('{"keep": true}')
        config = self.project / '.mcp.json'
        config.symlink_to(outside)
        before = self.snapshot()
        self.mcp('claude', 'setup', '--apply', succeeds=False)
        self.assertEqual(before, self.snapshot())
        config.unlink()
        self.mcp('claude', 'setup', '--apply')
        data = json.loads(config.read_text())
        data['unrelated_new_setting'] = {'preserve': True}
        config.write_text(json.dumps(data))
        self.mcp('claude', 'remove', '--apply')
        self.assertEqual(json.loads(config.read_text()), {'unrelated_new_setting': {'preserve': True}})

    def test_standard_system_aliases_accept_real_files_and_projects(self):
        def system_alias(path):
            for prefix, alias in (('/private/var/', '/var/'), ('/private/tmp/', '/tmp/')):
                if str(path).startswith(prefix) and Path(alias).resolve() == Path(prefix).resolve():
                    return alias + str(path)[len(prefix):]
            return str(path)
        selection_path = system_alias(self.selected)
        project_path = system_alias(self.project)
        plan = self.call('sources', 'capture', '--file', selection_path)
        self.call('sources', 'capture', '--file', selection_path, '--apply', '--preview', plan['preview'])
        self.call('sources', 'setup', 'mcp', '--agent', 'codex', '--project', project_path,
                  '--name', 'sample', '--url', 'https://mcp.example.test/service', '--apply')
        linked_file = self.fixture / 'linked.json'
        linked_file.symlink_to(self.selected)
        self.call('sources', 'capture', '--file', str(linked_file), succeeds=False)

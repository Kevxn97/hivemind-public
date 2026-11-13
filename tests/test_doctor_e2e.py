import os
"""Read-only diagnostics through a subprocess, with evidence for every run.

Failure cases: diagnostics must never repair/create state; missing integration
is distinct from malformed configuration; drift/delegation is not proof of a
broken runtime; unrelated hooks and private text must not leak; pending writes,
invalid journals, invalid mappings, and unfinished receipts remain actionable.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1] / 'hivemind'
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent / 'tests'))
import hive
from test_sessions import project


class DoctorE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        cls.out = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(BASE.parent / 'output'))) / 'doctor-e2e' / stamp
        cls.out.mkdir(parents=True)
        files = [*BASE.glob('*.py'), *BASE.glob('schemas/*.json'), *BASE.glob('integrations/*.md'), Path(__file__)]
        cls.report = {
            'command': list(sys.orig_argv),
            'environment': {'python': sys.version, 'platform': platform.platform()},
            'fixture': 'Temporary journaled synthetic store, synthetic home with copied public integration templates; no real settings or provider calls.',
            'source_sha256': {str(p.relative_to(BASE.parent)): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in files},
            'git_head': None,  # An exported source tree has no private Git history.
            'outcome': 'running', 'cases': []}
        cls.save()

    @classmethod
    def save(cls):
        (cls.out / 'evidence.json').write_text(json.dumps(cls.report, ensure_ascii=False, indent=2) + '\n')

    @classmethod
    def tearDownClass(cls):
        cls.report['outcome'] = 'passed' if all(c['outcome'] == 'passed' for c in cls.report['cases']) else 'failed'
        cls.report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        cls.save()
        print('Doctor evidence:', cls.out / 'evidence.json')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hive-doctor-e2e-')
        self.fixture = Path(self.temp.name).resolve()
        self.root, self.home = self.fixture / 'store', self.fixture / 'home'
        self.case = {'test': self.id(), 'outcome': 'running', 'commands': []}
        self.report['cases'].append(self.case)
        self.addCleanup(self.finish)
        self.home.mkdir()
        hive.apply(self.root, {'changes': [{'expected_revision': 'new', 'note': project()}]},
                   'test', 'Synthetic doctor fixture', 'test:doctor')
        (self.root / 'integrations').mkdir()
        for agent in ('codex', 'claude'):
            template = BASE / 'integrations' / (agent + '-global.md')
            shutil.copyfile(template, self.root / 'integrations' / template.name)

    def finish(self):
        result = self._outcome.result
        failures = [str(error) for test, error in result.failures + result.errors
                    if test is self or getattr(test, 'test_case', None) is self]
        self.case.update(outcome='failed' if failures else 'passed', failures=failures)
        self.save()
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def install(self, agent, end=True):
        folder = self.home / ('.' + agent)
        name = 'AGENTS.md' if agent == 'codex' else 'CLAUDE.md'
        self.write(folder / name, 'Private prefix SECRET_SENTINEL\n' + (self.root / 'integrations' / (agent + '-global.md')).read_text())
        script = self.root / 'integrations/codex_session_start.py' if agent == 'codex' else self.root / 'hive.py'
        self.write(script, '# synthetic adapter\n')
        self.write(self.root / 'hive.py', '# synthetic core entry\n')
        command = 'python3 ' + str(script) + ('' if agent == 'codex' else ' brief --hook')
        hooks = {'SessionStart': [{'hooks': [{'type': 'command', 'command': command}]}]}
        if end:
            hooks['SessionEnd'] = [{'hooks': [{'type': 'command', 'command': 'python3 ' + str(self.root / 'hive.py') + ' brief --hook --agent ' + agent}]}]
        self.write(folder / ('hooks.json' if agent == 'codex' else 'settings.json'), {'hooks': hooks})

    def snapshot(self):
        return {str(p.relative_to(self.fixture)): 'directory' if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.fixture.rglob('*'))}

    def doctor(self):
        before = self.snapshot()
        command = [sys.executable, '-B', str(BASE / 'doctor.py'), '--root', str(self.root), '--home', str(self.home)]
        p = subprocess.run(command, cwd=BASE, capture_output=True, text=True, timeout=20)
        self.case['commands'].append({'argv': command, 'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
        self.case['fixture_before_sha256'] = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
        self.case['unchanged'] = self.snapshot() == before
        self.save()
        self.assertEqual(self.snapshot(), before, 'doctor mutated the inspected store or home')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('SECRET_SENTINEL', p.stdout + p.stderr)
        value = json.loads(p.stdout)
        self.assertIn(value['status'], ('ok', 'attention', 'error'))
        self.assertEqual(sum(value['summary'].values()), len(value['checks']))
        return value, {c['id']: c for c in value['checks']}

    def test_complete_installation_is_healthy_and_read_only(self):
        self.install('codex'); self.install('claude')
        repo = self.fixture / 'repo'
        repo.mkdir()
        self.write(self.root / '.state/project-paths.json', {'version': 1, 'mappings': [{'path': str(repo), 'cluster': 'projects/demo'}]})
        value, checks = self.doctor()
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(checks['canonical']['status'], 'ok')
        self.assertEqual(checks['codex.rules']['status'], 'ok')
        self.assertEqual(checks['projects']['status'], 'ok')

    def test_fresh_store_has_no_created_state_or_lock_and_missing_integrations_are_not_errors(self):
        (self.root / '.hive.lock').unlink()
        value, checks = self.doctor()
        self.assertEqual(value['status'], 'attention')
        self.assertEqual(value['summary']['error'], 0)
        self.assertEqual(checks['codex.rules']['state'], 'not_installed')
        self.assertFalse((self.root / '.state').exists())
        self.assertFalse((self.root / '.hive.lock').exists())

    def test_rule_drift_delegation_and_missing_end_are_reported(self):
        self.install('codex', end=False); self.install('claude')
        self.write(self.home / '.codex/AGENTS.md', 'Use ' + str(self.root / 'START.md') + '\nSECRET_SENTINEL')
        claude = self.home / '.claude/CLAUDE.md'
        claude.write_text(claude.read_text().replace('session-close', 'old-close'))
        _, checks = self.doctor()
        self.assertEqual(checks['codex.rules']['state'], 'delegated')
        self.assertEqual(checks['claude.rules']['state'], 'drift')
        self.assertEqual(checks['codex.session_end']['status'], 'attention')

    def test_invalid_hook_json_and_shapes_are_errors_without_private_text(self):
        self.write(self.home / '.codex/hooks.json', '{SECRET_SENTINEL')
        self.write(self.home / '.claude/settings.json', {'hooks': []})
        _, checks = self.doctor()
        self.assertEqual(checks['codex.hooks']['status'], 'error')
        self.assertEqual(checks['claude.hooks']['status'], 'error')

    def test_unrelated_hooks_do_not_count_as_hivemind(self):
        self.write(self.home / '.codex/hooks.json', {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo SECRET_SENTINEL'}]}]}})
        _, checks = self.doctor()
        self.assertEqual(checks['codex.session_start']['state'], 'not_installed')
        self.assertEqual(checks['codex.session_start']['status'], 'attention')

    def test_hook_target_override_and_resume_exclusion_need_attention(self):
        self.install('codex'); self.install('claude')
        codex = self.home / '.codex/hooks.json'
        data = json.loads(codex.read_text())
        data['hooks']['SessionStart'][0]['hooks'][0]['command'] += ' --root ' + str(self.fixture / 'another-store')
        self.write(codex, data)
        claude = self.home / '.claude/settings.json'
        data = json.loads(claude.read_text())
        data['hooks']['SessionStart'][0]['matcher'] = 'startup'
        self.write(claude, data)
        _, checks = self.doctor()
        self.assertEqual(checks['codex.session_start']['status'], 'attention')
        self.assertIn('other_store', checks['codex.session_start']['adapter_states'])
        self.assertEqual(checks['claude.session_start']['status'], 'attention')
        self.assertIn('limited_events', checks['claude.session_start']['adapter_states'])

    def test_start_matcher_must_cover_clear_and_fork_too(self):
        for agent in ('codex', 'claude'):
            self.install(agent)
            path = self.home / ('.' + agent) / ('hooks.json' if agent == 'codex' else 'settings.json')
            data = json.loads(path.read_text())
            for missing in ('clear', 'fork'):
                with self.subTest(agent=agent, missing=missing):
                    sources = ['startup', 'resume', 'clear', 'compact', 'fork']
                    data['hooks']['SessionStart'][0]['matcher'] = '^(' + '|'.join(s for s in sources if s != missing) + ')$'
                    self.write(path, data)
                    _, checks = self.doctor()
                    self.assertEqual(checks[agent + '.session_start']['status'], 'attention')
                    self.assertIn('limited_events', checks[agent + '.session_start']['adapter_states'])

    def test_end_matcher_must_cover_actual_host_exit_reasons(self):
        for agent, matcher in [('codex', '^clear$'), ('claude', '^other$')]:
            with self.subTest(agent=agent):
                self.install(agent)
                path = self.home / ('.' + agent) / ('hooks.json' if agent == 'codex' else 'settings.json')
                data = json.loads(path.read_text())
                data['hooks']['SessionEnd'][0]['matcher'] = matcher
                self.write(path, data)
                _, checks = self.doctor()
                self.assertEqual(checks[agent + '.session_end']['status'], 'attention')
                self.assertIn('limited_events', checks[agent + '.session_end']['adapter_states'])
                data['hooks']['SessionEnd'][0]['matcher'] = '^other$' if agent == 'codex' else '*'
                self.write(path, data)
                _, checks = self.doctor()
                self.assertEqual(checks[agent + '.session_end']['status'], 'ok')

    def test_nonexistent_session_end_hook_cli_is_not_reported_as_configured(self):
        self.install('claude')
        path = self.home / '.claude/settings.json'
        data = json.loads(path.read_text())
        data['hooks']['SessionEnd'][0]['hooks'][0]['command'] = 'python3 ' + str(self.root / 'hive.py') + ' session-end --hook --agent claude'
        self.write(path, data)
        _, checks = self.doctor()
        self.assertEqual(checks['claude.session_end']['status'], 'attention')
        self.assertIn('unsupported', checks['claude.session_end']['adapter_states'])

    def test_pending_transaction_is_reported_without_recovery(self):
        self.write(self.root / '.pending.json', {'id': 'pending-fixture', 'changes': []})
        _, checks = self.doctor()
        self.assertEqual(checks['canonical']['state'], 'pending_transaction')
        self.assertEqual(checks['canonical']['status'], 'error')

    def test_unjournaled_change_is_not_silently_repaired(self):
        note = self.root / 'wiki/project/demo.md'
        note.write_text(note.read_text() + '\nUnjournaled fixture mutation.\n')
        _, checks = self.doctor()
        self.assertEqual(checks['canonical']['state'], 'invalid')
        self.assertEqual(checks['canonical']['status'], 'error')

    def test_stale_project_mapping_and_malformed_session_are_diagnosed(self):
        self.write(self.root / '.state/project-paths.json', {'version': 1, 'mappings': [{'path': str(self.fixture / 'missing-repo'), 'cluster': 'projects/missing'}]})
        self.write(self.root / '.state/sessions' / ('a' * 32 + '.json'), {'schema_version': 1, 'session_id': 'wrong', 'status': 'saved'})
        _, checks = self.doctor()
        self.assertEqual(checks['projects']['status'], 'attention')
        self.assertEqual(checks['projects']['missing_folders'], 1)
        self.assertEqual(checks['projects']['inactive_projects'], 1)
        self.assertEqual(checks['sessions']['status'], 'error')
        self.assertEqual(checks['sessions']['malformed'], 1)

    def test_unfinished_receipt_is_attention_not_invented_completion(self):
        ident = 'a' * 32
        receipt = {'schema_version': 1, 'session_id': ident, 'agent': 'codex', 'cluster': 'projects/demo',
                   'status': 'context_offered', 'opened_at': '2026-10-31T10:00:00Z', 'previous_context_at': None, 'offered': []}
        self.write(self.root / '.state/sessions' / (ident + '.json'), receipt)
        _, checks = self.doctor()
        self.assertEqual(checks['sessions']['status'], 'attention')
        self.assertEqual(checks['sessions']['unfinished'], 1)
        self.assertEqual(json.loads((self.root / '.state/sessions' / (ident + '.json')).read_text()), receipt)

    def test_ended_runtime_is_distinguished_from_open_receipt(self):
        for ident, ended in [('a' * 32, False), ('b' * 32, True)]:
            runtime = {'id': 'runtime-' + ident, 'source': 'startup'}
            if ended:
                runtime['ended_at'] = '2026-10-31T10:01:00Z'
            self.write(self.root / '.state/sessions' / (ident + '.json'), {
                'schema_version': 1, 'session_id': ident, 'agent': 'codex', 'cluster': 'projects/demo',
                'status': 'context_offered', 'opened_at': '2026-10-31T10:00:00Z', 'previous_context_at': None, 'offered': [], 'runtime': runtime})
        _, checks = self.doctor()
        self.assertEqual(checks['sessions']['unfinished'], 2)
        self.assertEqual(checks['sessions']['ended_without_close'], 1)
        self.assertEqual(checks['sessions']['open'], 1)

    def test_missing_store_is_reported_without_creating_it(self):
        self.root = self.fixture / 'missing-store'
        _, checks = self.doctor()
        self.assertEqual(checks['canonical']['status'], 'error')
        self.assertEqual(checks['canonical']['state'], 'missing_store')
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)

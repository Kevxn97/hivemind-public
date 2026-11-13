"""Synthetic subprocess coverage for portable agent connections.

Failure cases cover preview mutation, missing files, repeat operations, unrelated
settings, malformed JSON, managed drift, foreign ownership and hook execution.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
PACKAGED = (BASE / 'hivemind').is_dir()
CLI = [sys.executable, '-B', '-m', 'hivemind'] if PACKAGED else [sys.executable, '-B', str(BASE / 'connection_cli.py')]
INSPECT = ('from hivemind import connections' if PACKAGED else 'import connections')


class ConnectionsE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        cls.output = Path(os.environ.get('HIVEMIND_E2E_EVIDENCE_DIR', str(Path(tempfile.gettempdir()) / 'hivemind-connections-evidence'))) / stamp
        cls.output.mkdir(parents=True)
        cls.report = {'command': sys.orig_argv, 'environment': {'python': sys.version, 'platform': platform.platform()},
            'starting_data': 'Fresh temporary synthetic vault marker and agent homes with spaces. No provider calls or real host config.',
            'source_sha256': {str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in BASE.rglob('*.py')},
            'outcome': 'running', 'cases': []}
        cls.save()

    @classmethod
    def save(cls):
        (cls.output / 'evidence.json').write_text(json.dumps(cls.report, indent=2) + '\n')

    @classmethod
    def tearDownClass(cls):
        cls.report['outcome'] = 'passed' if all(c['outcome'] == 'passed' for c in cls.report['cases']) else 'failed'
        cls.save()
        print('Connections evidence', cls.output / 'evidence.json')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hive connection fixture ')
        self.fixture = Path(self.temp.name).resolve()
        self.root = self.fixture / 'vault with spaces'
        self.project = self.fixture / 'synthetic project'
        self.project.mkdir()
        self.home = self.fixture / 'agent home'
        self.case = {'name': self.id(), 'outcome': 'running', 'commands': []}
        self.report['cases'].append(self.case)
        self.addCleanup(self.finish)
        if PACKAGED:
            command = [*CLI, '--root', str(self.root), '--json', 'init', '--yes', '--project', str(self.project), '--name', 'Synthetic Connections Project', '--agent', 'none']
            result = subprocess.run(command, capture_output=True, text=True, cwd=BASE, timeout=15)
            self.case['commands'].append({'argv': command, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.root.mkdir()
            (self.root / '.hivemind.json').write_text(json.dumps({'version': 1, 'kind': 'hivemind-vault'}))

    def finish(self):
        result = self._outcome.result
        errors = [str(error) for test, error in result.failures + result.errors if test is self or getattr(test, 'test_case', None) is self]
        self.case.update(outcome='failed' if errors else 'passed', failures=errors)
        self.save()
        self.temp.cleanup()

    def snapshot(self):
        return {str(p.relative_to(self.fixture)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.fixture.rglob('*') if p.is_file()}

    def command(self, action, agent='codex', root=None, explicit_home=True):
        if action == 'inspect':
            return [sys.executable, '-B', '-c', INSPECT + '; import json,sys; print(json.dumps(connections.inspect(sys.argv[1], sys.argv[2], sys.argv[3] or None)))', str(root or self.root), agent, str(self.home) if explicit_home else '']
        global_options = ['--json'] if PACKAGED else []
        command = [*CLI, '--root', str(root or self.root), *global_options, action, agent]
        if explicit_home:
            command.extend(['--agent-home', str(self.home)])
        return command

    def call(self, action, agent='codex', apply=False, succeeds=True, root=None, explicit_home=True, env=None):
        command = self.command(action, agent, root, explicit_home)
        if apply:
            command.append('--apply')
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, cwd=BASE, env=env)
        self.case['commands'].append({'argv': command, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        self.save()
        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
        return json.loads(result.stdout) if succeeds else result

    def paths(self, agent):
        return self.home / ('AGENTS.md' if agent == 'codex' else 'CLAUDE.md'), self.home / ('hooks.json' if agent == 'codex' else 'settings.json')

    def test_preview_creates_nothing_and_missing_targets_install_and_remove(self):
        for agent in ('codex', 'claude'):
            with self.subTest(agent=agent):
                before = self.snapshot()
                value = self.call('connect', agent)
                self.assertFalse(value['applied'])
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.home.exists())
                self.call('connect', agent, apply=True)
                rules, config = self.paths(agent)
                self.assertIn('hivemind:connection:v1', rules.read_text())
                self.assertEqual(self.call('inspect', agent)['status'], 'connected')
                before = self.snapshot()
                self.call('connect', agent, apply=True)
                self.assertEqual(self.snapshot(), before)
                self.call('disconnect', agent, apply=True)
                self.assertFalse(rules.exists())
                self.assertFalse(config.exists())
                self.call('disconnect', agent, apply=True)
                self.assertEqual(self.call('inspect', agent)['status'], 'not_connected')
                self.home.rmdir()

    def test_both_events_execute_exact_installed_core_with_spaces(self):
        for agent in ('codex', 'claude'):
            with self.subTest(agent=agent):
                self.call('connect', agent, apply=True)
                _, config = self.paths(agent)
                data = json.loads(config.read_text())
                for event, timeout in [('SessionStart', 10), ('SessionEnd', 3)]:
                    entry = data['hooks'][event][0]['hooks'][0]
                    argv = shlex.split(entry['command'])
                    self.assertEqual(argv[0], sys.executable)
                    self.assertEqual(Path(argv[1]).name, 'hive.py')
                    self.assertEqual(argv[2:], ['--root', str(self.root), 'brief', '--hook', '--agent', agent, '--budget', '500'])
                    self.assertEqual(entry['timeout'], timeout)
                    payload = {'hook_event_name': event, 'reason': 'exit', 'cwd': str(self.project), 'session_id': 'connection-' + agent, 'source': 'startup', 'prompt': 'SECRET_PROMPT_SENTINEL', 'transcript_path': '/private/SECRET_TRANSCRIPT_SENTINEL'}
                    result = subprocess.run(argv, input=json.dumps(payload), text=True, capture_output=True, timeout=10, cwd=BASE)
                    self.case['commands'].append({'argv': argv, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr, 'stdin': 'Synthetic SessionStart/SessionEnd with cwd, session_id and privacy sentinels.'})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if event == 'SessionStart' or not PACKAGED:
                        self.assertIn('hookSpecificOutput', json.loads(result.stdout))
                    else:
                        self.assertEqual(json.loads(result.stdout)['matched'], 1)
                for path in self.root.rglob('*'):
                    if path.is_file():
                        self.assertNotIn(b'SECRET_PROMPT_SENTINEL', path.read_bytes())
                        self.assertNotIn(b'SECRET_TRANSCRIPT_SENTINEL', path.read_bytes())
                self.call('disconnect', agent, apply=True)

    def test_existing_text_settings_and_hooks_survive_repeat_roundtrip(self):
        self.home.mkdir()
        for agent in ('codex', 'claude'):
            rules, config = self.paths(agent)
            original_rules = 'Keep my instructions.\n'
            original = {'theme': 'dark', 'hooks': {'SessionStart': [{'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'echo unrelated'}]}], 'Stop': []}}
            rules.write_text(original_rules)
            config.write_text(json.dumps(original))
            self.call('connect', agent, apply=True)
            config_value = json.loads(config.read_text())
            config_value['additional'] = {'enabled': True}
            config.write_text(json.dumps(config_value))
            rules.write_text(rules.read_text() + '\nNew instructions.\n')
            self.call('connect', agent, apply=True)
            self.call('disconnect', agent, apply=True)
            self.assertEqual(rules.read_text(), original_rules + '\nNew instructions.\n')
            self.assertEqual(json.loads(config.read_text()), {**original, 'additional': {'enabled': True}})

    def test_malformed_config_and_managed_drift_refuse_without_changes(self):
        self.home.mkdir()
        config = self.home / 'hooks.json'
        config.write_text('{invalid SECRET_SENTINEL')
        before = self.snapshot()
        result = self.call('connect', apply=True, succeeds=False)
        self.assertNotIn('SECRET_SENTINEL', result.stdout + result.stderr)
        self.assertEqual(before, self.snapshot())
        config.unlink()
        self.call('connect', apply=True)
        rules = self.home / 'AGENTS.md'
        rules.write_text(rules.read_text().replace('500', '999'))
        before = self.snapshot()
        self.call('disconnect', apply=True, succeeds=False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.call('inspect')['status'], 'attention')

    def test_hook_drift_and_foreign_vault_refuse_without_changes(self):
        self.call('connect', apply=True)
        config = self.home / 'hooks.json'
        data = json.loads(config.read_text())
        data['hooks']['SessionEnd'][0]['hooks'][0]['timeout'] = 9
        config.write_text(json.dumps(data))
        before = self.snapshot()
        self.call('connect', apply=True, succeeds=False)
        self.assertEqual(self.snapshot(), before)
        other = self.fixture / 'another vault'
        other.mkdir()
        (other / '.hivemind.json').write_text(json.dumps({'version': 1, 'kind': 'hivemind-vault'}))
        before = self.snapshot()
        self.call('connect', apply=True, succeeds=False, root=other)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_marker_and_symlink_target_refuse(self):
        (self.root / '.hivemind.json').write_text('{}')
        self.call('connect', apply=True, succeeds=False)
        (self.root / '.hivemind.json').write_text(json.dumps({'version': 1, 'kind': 'hivemind-vault'}))
        self.home.mkdir()
        outside = self.fixture / 'outside'
        outside.write_text('Keep this')
        (self.home / 'AGENTS.md').symlink_to(outside)
        before = self.snapshot()
        self.call('connect', apply=True, succeeds=False)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_repeat_connection_serializes_ownership(self):
        command = [*self.command('connect'), '--apply']
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=BASE) for _ in range(6)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.case['commands'].append({'argv': command, 'returncode': process.returncode, 'stdout': stdout, 'stderr': stderr})
            self.assertEqual(process.returncode, 0, stdout + stderr)
        rules, config = self.paths('codex')
        self.assertEqual(rules.read_text().count('hivemind:connection:v1 BEGIN'), 1)
        self.assertEqual(len(json.loads(config.read_text())['hooks']['SessionStart']), 1)
        self.call('disconnect', apply=True)

    def test_missing_metadata_fields_and_interrupted_transaction_are_actionable(self):
        self.call('connect', apply=True)
        state = next((self.root / '.state/connections').glob('codex-*.json'))
        value = json.loads(state.read_text())
        del value['block']
        state.write_text(json.dumps(value))
        before = self.snapshot()
        result = self.call('connect', apply=True, succeeds=False)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(before, self.snapshot())
        value['transaction'] = 'pending'
        state.write_text(json.dumps(value))
        before = self.snapshot()
        self.assertEqual(self.call('inspect')['status'], 'attention')
        result = self.call('disconnect', apply=True, succeeds=False)
        self.assertIn('interrupted', result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_local_disabled_hooks_and_shadowed_rules_are_reported_read_only(self):
        self.call('connect', apply=True)
        (self.home / 'config.toml').write_text('[features]\nhooks = false\n# SECRET_SENTINEL\n')
        (self.home / 'AGENTS.override.md').write_text('SECRET_SENTINEL')
        before = self.snapshot()
        value = self.call('inspect')
        checks = {c['id']: c for c in value['checks']}
        self.assertEqual(value['status'], 'attention')
        self.assertEqual(checks['codex.hook_setting']['state'], 'disabled')
        self.assertEqual(checks['codex.rules_override']['state'], 'shadowed')
        self.assertTrue(all('detail' in check for check in value['checks']))
        self.assertNotIn('SECRET_SENTINEL', json.dumps(value))
        self.assertEqual(before, self.snapshot())
        (self.home / 'config.toml').write_text('SECRET_SENTINEL = [')
        before = self.snapshot()
        value = self.call('inspect')
        self.assertEqual({c['id']: c for c in value['checks']}['codex.hook_setting']['status'], 'error')
        self.assertNotIn('SECRET_SENTINEL', json.dumps(value))
        self.assertEqual(before, self.snapshot())

    def test_environment_selected_agent_home_and_default_hook_setting(self):
        for agent, variable in [('codex', 'CODEX_HOME'), ('claude', 'CLAUDE_CONFIG_DIR')]:
            environment = {**os.environ, variable: str(self.home)}
            self.call('connect', agent, apply=True, explicit_home=False, env=environment)
            value = self.call('inspect', agent, explicit_home=False, env=environment)
            self.assertEqual(value['agent_home'], str(self.home))
            if agent == 'codex':
                checks = {c['id']: c for c in value['checks']}
                self.assertEqual(checks['codex.hook_setting']['state'], 'enabled_default')
                self.assertEqual(checks['codex.host']['state'], 'unverified')
            self.call('disconnect', agent, apply=True, explicit_home=False, env=environment)

    def test_preview_exposes_owned_artifacts_and_useful_normal_commands(self):
        value = self.call('connect')
        proposal = value['proposed']
        self.assertIn('instructions', proposal)
        self.assertEqual(set(proposal['hooks']), {'SessionStart', 'SessionEnd'})
        rules = proposal['instructions']
        self.assertIn('brief /absolute/project/path', rules)
        self.assertNotIn('--hook', rules)
        self.assertIn('session-context', rules)
        self.assertIn('session-draft', rules)
        self.assertIn('--no-change', rules)
        self.assertNotIn('token', rules)

    def test_missing_vault_and_stale_installed_script_are_diagnostic(self):
        missing = self.fixture / 'missing vault'
        value = self.call('inspect', root=missing)
        self.assertEqual(value['status'], 'attention')
        self.assertEqual(value['checks'][0]['status'], 'error')
        self.assertFalse(missing.exists())
        self.call('connect', apply=True)
        state = next((self.root / '.state/connections').glob('codex-*.json'))
        ownership = json.loads(state.read_text())
        config_path = self.home / 'hooks.json'
        config = json.loads(config_path.read_text())
        for event in ('SessionStart', 'SessionEnd'):
            argv = shlex.split(ownership['groups'][event]['hooks'][0]['command'])
            argv[1] = str(self.fixture / 'removed install/hive.py')
            ownership['groups'][event]['hooks'][0]['command'] = shlex.join(argv)
            config['hooks'][event] = [ownership['groups'][event]]
        state.write_text(json.dumps(ownership))
        config_path.write_text(json.dumps(config))
        before = self.snapshot()
        value = self.call('inspect')
        self.assertEqual(value['status'], 'attention')
        self.assertEqual({c['id']: c for c in value['checks']}['codex.runtime_paths']['state'], 'stale')
        self.assertEqual(before, self.snapshot())
        self.call('connect', apply=True)
        self.assertEqual(self.call('inspect')['status'], 'connected')
        self.call('disconnect', apply=True)

    def test_partial_write_failure_rolls_back_connect_and_disconnect(self):
        self.home.mkdir()
        (self.home / 'AGENTS.md').write_text('Keep instructions.\n')
        (self.home / 'hooks.json').write_text('{"theme": "dark"}\n')
        script = INSPECT + """
import runpy
import sys
from pathlib import Path
original = connections._atomic
failed = False
def fail_once(path, content, mode=0o600):
    global failed
    if Path(path).name == 'hooks.json' and not failed:
        failed = True
        raise OSError('synthetic atomic write failure')
    return original(path, content, mode)
connections._atomic = fail_once
"""
        if PACKAGED:
            script += '\nfrom hivemind.cli import main\nraise SystemExit(main(sys.argv[1:]))\n'
        else:
            script += '\nsys.argv = [str(Path.cwd() / "connection_cli.py"), *sys.argv[1:]]\nrunpy.run_path(sys.argv[0], run_name="__main__")\n'
        for action in ('connect', 'disconnect'):
            before = self.snapshot()
            options = self.command(action)[len(CLI):]
            command = [sys.executable, '-B', '-c', script, *options, '--apply']
            result = subprocess.run(command, capture_output=True, text=True, cwd=BASE, timeout=15)
            self.case['commands'].append({'argv': command, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('synthetic atomic write failure', result.stderr)
            self.assertEqual(before, self.snapshot())
            if action == 'connect':
                self.call('connect', apply=True)

    def test_invalid_owned_hook_shape_is_refused_safely(self):
        self.call('connect', apply=True)
        state = next((self.root / '.state/connections').glob('codex-*.json'))
        ownership = json.loads(state.read_text())
        ownership['groups']['SessionStart'] = {'hooks': []}
        state.write_text(json.dumps(ownership))
        config_path = self.home / 'hooks.json'
        config = json.loads(config_path.read_text())
        config['hooks']['SessionStart'] = [{'hooks': []}]
        config_path.write_text(json.dumps(config))
        before = self.snapshot()
        result = self.call('connect', apply=True, succeeds=False)
        self.assertNotIn('Traceback', result.stderr)
        value = self.call('inspect')
        self.assertEqual(value['status'], 'attention')
        self.assertEqual(before, self.snapshot())

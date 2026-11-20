"""Public entrypoint acceptance. Run from source and an independently installed wheel.

The enclosing scripts/verify.py records all outcomes, including initial failures.
Only synthetic vaults, homes and projects are used; no provider is contacted.
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

REPO = Path(__file__).resolve().parents[1]


def snapshot(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


class PortableReleaseE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hivemind-public-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        self.project = self.folder / 'project with spaces'
        self.project.mkdir()
        self.home = self.folder / 'home'
        self.home.mkdir()
        self.vault = self.folder / 'vault one'
        self.other = self.folder / 'vault two'
        self.env = {'HOME': str(self.home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.python = os.environ.get('HIVEMIND_TEST_PYTHON', sys.executable)
        self.entrypoint = ([str(Path(self.python).with_name('hive'))] if os.environ.get('HIVEMIND_TEST_PYTHON')
                           else [self.python, '-B', '-m', 'hivemind'])
        if not os.environ.get('HIVEMIND_TEST_PYTHON'):
            self.env['PYTHONPATH'] = str(REPO)

    def cli(self, *args, ok=True, env=None):
        p = subprocess.run([*self.entrypoint, *map(str, args)],
                           cwd=self.folder, env=env or self.env, text=True,
                           capture_output=True, timeout=40)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        else:
            self.assertNotEqual(p.returncode, 0, p.stderr + p.stdout)
        return json.loads(p.stdout if ok else p.stderr)

    def init(self, vault, **kw):
        return self.cli('init', vault, '--project', self.project, '--name', 'Sample',
                        '--agent', 'none', '--yes', '--json', **kw)

    def test_fresh_setup_two_vaults_selection_and_unchanged_installation(self):
        before = snapshot(REPO / 'hivemind')
        a, b = self.init(self.vault), self.init(self.other)
        self.assertTrue(a['initialized'])
        self.assertTrue(b['initialized'])
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.home / '.claude').exists())
        untouched = snapshot(self.other)
        env = dict(self.env, HIVEMIND_HOME=str(self.vault))
        opened = self.cli('session-open', self.project, '--agent', 'codex', '--json', env=env)
        self.assertIn('session_id', opened)
        selected = self.cli('--root', self.other, 'session-open', self.project,
                            '--agent', 'claude', '--json', env=env)
        self.assertTrue((self.other / '.state/sessions' / (selected['session_id'] + '.json')).is_file())
        self.cli('--root', self.vault, 'session-close', opened['session_id'], '--no-change', '--json')
        self.assertEqual(before, snapshot(REPO / 'hivemind'))
        self.assertNotEqual(untouched, snapshot(self.other))
        self.assertFalse((self.home / '.local/share/hivemind').exists())
        self.assertTrue(self.cli('--root', self.vault, 'check', '--json')['valid'])

    def test_init_preserves_existing_and_refuses_unknown_content(self):
        self.init(self.vault)
        before = snapshot(self.vault)
        self.assertTrue(self.init(self.vault)['existing'])
        self.assertEqual(before, snapshot(self.vault))
        self.other.mkdir()
        (self.other / 'private.txt').write_text('SYNTHETIC_PRIVATE_SENTINEL')
        other = snapshot(self.other)
        self.init(self.other, ok=False)
        self.assertEqual(other, snapshot(self.other))

    def test_missing_default_is_actionable_without_creating_a_store(self):
        error = self.cli('session-open', self.project, '--json', ok=False)
        self.assertIn('init', json.dumps(error).lower())
        self.assertFalse((self.home / '.local/share/hivemind').exists())

    def test_generated_followup_commands_preserve_the_selected_vault(self):
        self.init(self.vault)
        brief = self.cli('--root', self.vault, 'brief', 'projects/sample', '--json')
        commands = [c for c in re.findall(r'`([^`]+)`', brief['text']) if ' show ID' in c]
        self.assertEqual(len(commands), 1)
        argv = shlex.split(commands[0].replace('show ID', 'show sample'))
        result = subprocess.run(argv, cwd=self.folder, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['note']['id'], 'sample')

    def test_personal_sync_automation_is_not_started_by_the_public_preview(self):
        self.init(self.vault)
        before = snapshot(self.vault)
        binaries = self.folder / 'synthetic bin'
        binaries.mkdir()
        invoked = self.folder / 'provider-was-invoked'
        stub = binaries / 'codex'
        stub.write_text('#!/bin/sh\ntouch ' + shlex.quote(str(invoked)) + '\nexit 99\n')
        stub.chmod(0o700)
        env = dict(self.env, PATH=str(binaries) + os.pathsep + os.defpath)
        error = self.cli('--root', self.vault, 'sync', '--json', ok=False, env=env)
        self.assertIn('disabled', json.dumps(error).lower())
        self.assertFalse(invoked.exists())
        self.assertEqual(before, snapshot(self.vault))

    def test_public_vault_cannot_install_legacy_background_automation(self):
        self.init(self.vault)
        before = snapshot(self.vault)
        # A recording runner prevents launchctl from ever reaching the real host,
        # including on the initial failing test before the preview gate exists.
        code = """
import json,sys
from pathlib import Path
from hivemind import background
def runner(*args, **kwargs):
    raise AssertionError('Synthetic runner reached: public preview should refuse before dispatch')
try:
    background.install(Path(sys.argv[1]), launchagents=Path(sys.argv[2]), runner=runner)
except ValueError as error:
    print(json.dumps({'error':str(error)}))
else:
    raise AssertionError('Unexpected public background installation')
"""
        result = subprocess.run([self.python, '-B', '-c', code, str(self.vault), str(self.folder / 'launchagents')],
                                env=self.env, cwd=self.folder, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('disabled', result.stdout)
        self.assertEqual(before, snapshot(self.vault))
        self.assertFalse((self.folder / 'launchagents').exists())

    def test_portable_agent_setup_and_read_only_doctor(self):
        self.init(self.vault)
        for agent in ('codex', 'claude'):
            agent_home = self.home / ('.' + agent)
            preview = self.cli('--root', self.vault, 'connect', agent, '--agent-home', agent_home, '--json')
            self.assertFalse(preview['applied'])
            self.assertFalse(agent_home.exists())
            configured = self.cli('--root', self.vault, 'connect', agent,
                                  '--agent-home', agent_home, '--apply', '--json')
            self.assertTrue(configured['applied'])
            config = json.loads((agent_home / ('hooks.json' if agent == 'codex' else 'settings.json')).read_text())
            session_id = None
            for event in ('SessionStart', 'SessionEnd'):
                payload = {'cwd': str(self.project), 'session_id': 'installed-' + agent,
                           'hook_event_name': event, 'source': 'startup', 'reason': 'other'}
                command = shlex.split(config['hooks'][event][0]['hooks'][0]['command'])
                result = subprocess.run(command, input=json.dumps(payload), cwd=self.folder,
                                        env=self.env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                if event == 'SessionStart':
                    offered = value['hookSpecificOutput']['additionalContext']
                    session_id = re.search(r'HiveMind session: ([0-9a-f]{32})', offered).group(1)
                    self.assertIn('agent-memory remember', offered[:1000])
                    if agent == 'codex':
                        stored = self.cli('--root', self.vault, 'agent-memory', 'remember', session_id,
                                          '--topic', 'SQLite migration', '--attempt', 'Copied before checkpoint',
                                          '--result', 'The copy missed the latest write.',
                                          '--when', 'Migrating a SQLite vault', '--json')
                        memory_id = stored['memory_id']
                        proof = self.vault / 'sources' / 'synthetic-migration.txt'
                        proof.parent.mkdir(exist_ok=True)
                        proof.write_text('Synthetic migration observation.\n', encoding='utf-8')
                        self.cli('--root', self.vault, 'agent-memory', 'share', session_id, memory_id,
                                 '--source', 'sources/synthetic-migration.txt', '--json')
                    else:
                        packet = self.cli('--root', self.vault, 'session-context', session_id,
                                          '--task', 'Check SQLite migration', '--budget', '500', '--json')
                        self.assertEqual(packet['memory_refs'], [memory_id])
                        self.assertIn('The copy missed the latest write.', packet['context']['text'])
                        rated = self.cli('--root', self.vault, 'session-feedback', session_id,
                                         '--outcome', 'helpful', '--used-memory', memory_id, '--json')
                        self.assertEqual(rated['feedback']['used_memories'], [memory_id])
                else:
                    self.assertEqual(value['matched'], 1)
            self.cli('--root', self.vault, 'session-close', session_id, '--no-change', '--json')
        before = snapshot(self.folder)
        report = self.cli('--root', self.vault, 'doctor', '--home', self.home, '--json')
        self.assertEqual(report['scope'], 'read_only_disk_snapshot')
        self.assertEqual(report['summary']['error'], 0)
        self.assertEqual(before, snapshot(self.folder))
        self.assertIn('unverified', json.dumps(report).lower())

    def test_paused_public_vault_keeps_agent_memory_readable_and_blocks_writes(self):
        self.init(self.vault)
        root = ('--root', self.vault)
        session = self.cli(*root, 'session-open', self.project, '--agent', 'codex', '--json')['session_id']
        remember = (*root, 'agent-memory', 'remember', session, '--topic', 'SQLite migration',
                    '--attempt', 'Copied before checkpoint', '--when', 'SQLite migration', '--result')
        ident = self.cli(*remember, 'Observation before the pause.', '--json')['memory_id']
        proof = self.vault / 'sources' / 'synthetic-pause.txt'
        proof.parent.mkdir(exist_ok=True)
        proof.write_text('Synthetic migration observation.\n', encoding='utf-8')
        pause = (*root, 'memory', 'pause', '--reason', 'Synthetic pause')
        preview = self.cli(*pause, '--json')['preview']
        self.cli(*pause, '--apply', '--preview', preview, '--json')
        packet = self.cli(*root, 'session-context', session, '--task', 'SQLite migration', '--json')
        self.assertEqual(packet['memory_refs'], [ident])
        remember_error = self.cli(*remember, 'Observation during the pause.', '--json', ok=False)
        share_error = self.cli(*root, 'agent-memory', 'share', session, ident,
                               '--source', 'sources/synthetic-pause.txt', '--json', ok=False)
        for error in (remember_error, share_error):
            self.assertEqual(error['error_code'], 'INVALID_STATE')
        resume = (*root, 'memory', 'resume', '--reason', 'Synthetic review finished')
        preview = self.cli(*resume, '--json')['preview']
        self.cli(*resume, '--apply', '--preview', preview, '--json')
        self.assertEqual(self.cli(*root, 'agent-memory', 'share', session, ident,
                                  '--source', 'sources/synthetic-pause.txt', '--json')['status'], 'proposed')

    def test_self_test_proves_handoff_without_touching_selected_vault(self):
        self.init(self.vault)
        before = snapshot(self.vault)
        result = self.cli('--root', self.vault, 'self-test', '--json',
                          '--evidence-dir', self.folder / 'evidence')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['scope'], 'synthetic_local_hooks_and_handoff')
        self.assertTrue(Path(result['evidence']).is_file())
        self.assertEqual(before, snapshot(self.vault))


if __name__ == '__main__':
    unittest.main()

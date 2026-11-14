"""First-run acceptance from an archive or an independently installed package.

The verification wrapper retains source state, outcome and environment. Each
case also records its commands and PTY transcript. All data and agent homes are
synthetic, with no inherited provider credentials.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import struct
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def snapshot(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob('*') if path.is_file() and '__pycache__' not in path.parts}


class QuickstartE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hive first use ')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.home = self.folder / 'home'
        self.home.mkdir()
        self.project = self.folder / 'Sample project'
        self.project.mkdir()
        self.vault = self.folder / 'selected vault'
        self.env = {'HOME': str(self.home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
                    'CODEX_HOME': str(self.home / '.codex'),
                    'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.python = os.environ.get('HIVEMIND_TEST_PYTHON', sys.executable)
        self.installed = bool(os.environ.get('HIVEMIND_TEST_PYTHON'))
        self.entry = ([str(Path(self.python).with_name('hive'))] if self.installed
                      else [self.python, '-B', str(ROOT / 'run.py')])
        self.commands = []
        self.addCleanup(self.save)

    def save(self):
        evidence = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', tempfile.gettempdir())) / 'quickstart'
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / (self.id().split('.')[-1] + '.json')).write_text(json.dumps({
            'fixture': 'Empty synthetic HOME, separate vault and sample project, no provider calls.',
            'environment': self.env, 'commands': self.commands}, indent=2) + '\n')

    def call(self, *args, ok=True, json_mode=True):
        command = [*self.entry, '--root', str(self.vault), *map(str, args)]
        if json_mode:
            command.append('--json')
        result = subprocess.run(command, cwd=self.folder, env=self.env, text=True,
                                capture_output=True, timeout=30)
        self.commands.append({'argv': command, 'cwd': str(self.folder), 'returncode': result.returncode,
                              'stdout': result.stdout, 'stderr': result.stderr})
        self.save()
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout) if ok and json_mode else result

    def setup_project(self, *extra):
        return self.call('setup', '--project', self.project, '--agent', 'none', '--yes', *extra)

    def terminal(self, replies):
        command = [*self.entry, '--root', str(self.vault)]
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        process = subprocess.Popen(command, cwd=self.folder, env=dict(self.env, TERM='xterm-256color'),
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        stage_output = bytearray()
        try:
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
                    if replies and replies[0][0].encode() in stage_output:
                        _, answer = replies.pop(0)
                        os.write(master, answer.encode())
                        stage_output.clear()
                if process.poll() is not None:
                    break
            process.wait(timeout=2)
            self.assertEqual(process.returncode, 0, output.decode(errors='replace'))
            self.assertFalse(replies, output.decode(errors='replace'))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            self.commands.append({'argv': command, 'transport': 'PTY', 'returncode': process.returncode,
                                  'transcript': output.decode(errors='replace')})
            self.save()
        return output.decode(errors='replace')

    def test_archive_launcher_runs_without_install_or_pythonpath(self):
        result = self.call('--version', json_mode=False)
        self.assertIn('HiveMind', result.stdout)
        self.assertFalse(self.vault.exists())

    def test_setup_stores_first_memory_and_offers_it_to_a_fresh_agent(self):
        before = snapshot(ROOT / 'hivemind')
        result = self.setup_project('--remember', 'Use SQLite for the synthetic pilot.')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['project']['cluster'], 'projects/sample-project')
        self.assertEqual(result['memory']['status'], 'captured')
        opened = self.call('session-open', self.project, '--agent', 'claude')
        self.assertIn('Use SQLite', opened['context']['text'])
        self.assertIn('user:setup', self.call('memory', 'show', result['memory']['note']['id'])['text'])
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.home / '.claude').exists())
        self.assertEqual(before, snapshot(ROOT / 'hivemind'))

    def test_setup_repeats_without_duplicate_knowledge_and_adds_another_project(self):
        self.setup_project('--remember', 'Synthetic first memory.')
        events = self.call('check')['events']
        repeated = self.setup_project('--remember', 'Synthetic first memory.')
        self.assertFalse(repeated['initialized'])
        self.assertEqual(self.call('check')['events'], events)
        other = self.folder / 'Another project'
        other.mkdir()
        added = self.call('setup', '--project', other, '--agent', 'none', '--yes')
        self.assertEqual(added['project']['cluster'], 'projects/another-project')
        self.assertEqual(len(self.call('projects')), 2)
        context = self.call('session-open', self.project, '--agent', 'codex')['context']['text']
        self.assertIn('Synthetic first memory', context)

    def test_project_name_conflict_and_invalid_inputs_preserve_existing_data(self):
        self.setup_project()
        canonical = snapshot(self.vault / 'wiki')
        other = self.folder / 'different' / self.project.name
        other.mkdir(parents=True)
        self.call('setup', '--project', other, '--agent', 'none', '--yes', ok=False)
        self.assertEqual(canonical, snapshot(self.vault / 'wiki'))
        self.call('setup', '--project', self.folder / 'missing', '--yes', ok=False)
        self.assertEqual(canonical, snapshot(self.vault / 'wiki'))

    def test_existing_unknown_vault_is_preserved_and_noninteractive_missing_inputs_are_actionable(self):
        result = self.call('setup', ok=False)
        self.assertIn('--yes', result.stderr)
        self.assertFalse(self.vault.exists())
        self.vault.mkdir()
        (self.vault / 'private.txt').write_text('SYNTHETIC_EXISTING_DATA')
        before = snapshot(self.vault)
        self.call('setup', '--project', self.project, '--agent', 'none', '--yes', ok=False)
        self.assertEqual(before, snapshot(self.vault))

    def test_connection_preview_and_explicit_connect_preserve_other_settings(self):
        result = self.call('setup', '--project', self.project, '--agent', 'codex', '--yes')
        self.assertEqual(result['connection']['status'], 'preview')
        self.assertFalse((self.home / '.codex').exists())
        home = self.home / '.codex'
        home.mkdir()
        (home / 'AGENTS.md').write_text('Existing synthetic instructions.\n')
        (home / 'hooks.json').write_text('{"keep": true}')
        connected = self.call('setup', '--project', self.project, '--agent', 'codex', '--yes', '--connect')
        self.assertTrue(connected['connection']['applied'])
        self.assertTrue((home / 'AGENTS.md').read_text().startswith('Existing synthetic instructions.'))
        self.assertTrue(json.loads((home / 'hooks.json').read_text())['keep'])

    def test_followup_commands_target_the_selected_public_vault_from_another_directory(self):
        result = self.setup_project()
        command = result['commands']['sources']
        invoked = subprocess.run(shlex.split(command), cwd=self.project, env=self.env,
                                 capture_output=True, text=True, timeout=20)
        self.commands.append({'argv': shlex.split(command), 'cwd': str(self.project),
                              'returncode': invoked.returncode, 'stdout': invoked.stdout, 'stderr': invoked.stderr})
        self.assertEqual(invoked.returncode, 0, invoked.stderr)
        self.assertIn('sources', invoked.stdout)
        self.assertFalse((self.home / '.local/share/hivemind').exists())

    def test_first_invocation_opens_guided_setup_and_saves_after_one_confirmation(self):
        script = f'{self.project}\nnone\nSynthetic guided memory.\ny\n'
        output = self.terminal([('Project folder', script)])
        self.assertIn('Ready', output)
        self.assertNotIn('"proposed"', output)
        context = self.call('session-open', self.project, '--agent', 'codex')['context']['text']
        self.assertIn('Synthetic guided memory', context)

    def test_declined_or_ended_setup_creates_no_vault(self):
        output = self.terminal([('Project folder', f'{self.project}\nnone\n\nn\n')])
        self.assertIn('cancelled', output.lower())
        self.assertFalse(self.vault.exists())

    def test_ending_input_during_agent_preview_keeps_the_confirmed_memory(self):
        output = self.terminal([('Project folder', f'{self.project}\ncodex\nSaved before connecting.\ny\n'),
                                ('Connect this agent now?', '\x04')])
        self.assertIn('Ready', output)
        self.assertFalse((self.home / '.codex').exists())
        context = self.call('session-open', self.project, '--agent', 'claude')['context']['text']
        self.assertIn('Saved before connecting', context)

    def test_terminal_opens_the_source_menu_and_returns_after_saving(self):
        self.setup_project()
        output = self.terminal([('Quellen', 's'), ('Choose a source', '4\n'),
                                ('Summary', 'A memory saved from the main terminal.\n'),
                                ('Title', '\n'), ('Source reference', '\n'),
                                ('Save this selection', 'y\n'),
                                ('Press Enter', '\n'), ('Quellen', 'q')])
        self.assertIn('Saved to the journal', output)
        context = self.call('session-open', self.project, '--agent', 'codex')['context']['text']
        self.assertIn('A memory saved from the main terminal', context)


if __name__ == '__main__':
    unittest.main()

"""Public English-first experience against a separate synthetic vault."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_terminal as vt


class EnglishUIE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hivemind-english-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'vault'
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / '.codex'),
                        CLAUDE_CONFIG_DIR=str(self.home / '.claude'), PYTHONDONTWRITEBYTECODE='1',
                        TERM='xterm-256color', LANG='en_US.UTF-8', LC_CTYPE='UTF-8')
        self.python = os.environ.get('HIVEMIND_TEST_PYTHON', sys.executable)
        self.entry = ([str(Path(self.python).with_name('hive'))] if os.environ.get('HIVEMIND_TEST_PYTHON')
                      else [self.python, '-B', str(ROOT / 'run.py')])
        initialized = subprocess.run([*self.entry, 'init', str(self.root), '--agent', 'none', '--yes'],
                                     cwd=self.base, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        notes = [vt.note('example-project', title='Example project', type='project',
                         cluster='projects/example-project',
                         links=[{'target': 'existing-note', 'relation': 'related', 'reason': 'Example link'}]),
                 vt.note('existing-note', title='Projektprüfung', cluster='projects/example-project',
                         body='Existing German content remains unchanged.')]
        vt.hive.apply(self.root, {'changes': [{'expected_revision': 'new', 'note': note} for note in notes]},
                      'english-ui-test', 'Synthetic example', 'synthetic://english-ui')
        self.before = {p.relative_to(self.root): p.read_bytes() for p in (self.root / 'wiki').rglob('*.md')}
        self.sessions = []
        self.addCleanup(self.close_sessions)
        vt.OUT = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', tempfile.gettempdir())) / 'english-ui'
        vt.OUT.mkdir(parents=True, exist_ok=True)

    def close_sessions(self):
        for session in self.sessions:
            session.close()

    def terminal(self):
        # Use the same CLI as a user, including an installed wheel if supplied.
        code = ('import os,sys;os.environ.update(' + repr(self.env) + ');'
                + ('sys.path.insert(0,' + repr(str(ROOT)) + ');' if self.python == sys.executable else '')
                + 'from hivemind.cli import main;raise SystemExit(main(["--root",sys.argv[1]]))')
        old_python = vt.sys.executable
        vt.sys.executable = self.python
        try:
            session = vt.Terminal(self.root, 'english-workspace', code=code)
        finally:
            vt.sys.executable = old_python
        self.sessions.append(session)
        return session

    def test_public_workspace_and_cli_are_in_english_without_translating_user_content(self):
        session = self.terminal()
        session.expect('Overview')
        session.expect('Knowledge')
        session.expect('Network')
        session.expect('Inbox')
        session.expect('Sources')
        session.send('2')
        session.expect('KNOWLEDGE')
        session.expect('Projektprüfung')
        session.send('3')
        session.expect('NETWORK')
        session.expect('LIVING KNOWLEDGE')
        session.send('5')
        session.expect('SOURCES')
        session.send('1')
        session.send('n')
        session.expect('CAPTURE A THOUGHT')
        session.send('\x1b')
        session.send('b')
        session.expect('Automation')
        session.send('\x1b')
        session.send('q')
        self.assertEqual(session.wait(), 0)
        for path, content in self.before.items():
            self.assertEqual((self.root / path).read_bytes(), content)
        plain = subprocess.run([*self.entry, '--root', str(self.root), '--plain'], cwd=self.base,
                               env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn('Background', plain.stdout)
        self.assertIn('Knowledge', plain.stdout)
        self.assertNotIn('Notizen', plain.stdout)


if __name__ == '__main__':
    unittest.main()

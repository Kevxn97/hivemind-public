"""Portable onboarding flows through a fresh Python process; stdlib only.

Run from the public checkout: python -m unittest discover -s tests -p 'test_onboarding_e2e.py'.
The transport driver calls the same parser/execute API used by the installed CLI.
It fakes only TTY detection so guided prompts can be exercised deterministically.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

DRIVER = r'''
import argparse, json, os, sys
from pathlib import Path
try:
    from hivemind import onboarding, hive, project_context
except ModuleNotFoundError as exc:
    if exc.name != 'hivemind': raise
    import onboarding, hive, project_context
if os.environ.get('HIVE_TEST_TTY') == '1':
    class Terminal:
        def isatty(self): return True
        def readline(self, *args): return sys.__stdin__.readline(*args)
    sys.stdin = Terminal()
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest='command', required=True)
onboarding.configure(sub)
args = parser.parse_args()
try:
    result = onboarding.execute(args, Path(os.environ['HIVE_TEST_DEFAULT']))
    notes = hive.load_notes(Path(result['vault']))
    hive.audit(Path(result['vault']), notes)
    result['test_notes'] = {key: value[0] for key, value in notes.items()}
    result['test_projects'] = project_context.list_projects(Path(result['vault']))
    print(json.dumps(result))
except (ValueError, OSError) as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(2)
'''

class OnboardingE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.default = self.root / 'default-vault'
        self.project = self.root / 'my-project'
        self.project.mkdir()

    def run_flow(self, *args, input='', tty=False, success=True):
        env = dict(os.environ, HIVE_TEST_DEFAULT=str(self.default), HIVE_TEST_TTY='1' if tty else '0')
        result = subprocess.run([sys.executable, '-c', DRIVER, 'init', *map(str, args)],
                                input=input, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr + result.stdout)
        return json.loads(result.stdout) if success else result

    def snapshot(self, vault):
        return {str(p.relative_to(vault)): p.read_bytes() for p in vault.rglob('*') if p.is_file()}

    def test_two_vaults_are_isolated_and_project_is_journaled(self):
        first = self.run_flow(self.root / 'one', '--project', self.project, '--name', 'My Project', '--yes', '--json')
        second = self.run_flow(self.root / 'two', '--yes', '--json')
        self.assertTrue(first['initialized'])
        self.assertFalse(first['existing'])
        self.assertEqual(first['project']['cluster'], 'projects/my-project')
        self.assertEqual(first['test_projects'][0]['folder'], str(self.project.resolve()))
        project = first['test_notes']['my-project']
        self.assertEqual(project['evidence'], 'reported')
        self.assertEqual(project['sources'][0]['resource'], 'user:onboarding')
        self.assertNotIn(str(self.project), json.dumps(project))
        self.assertTrue(list((Path(first['vault']) / '.history').glob('*.json')))
        self.assertEqual(second['test_notes'], {})
        self.assertEqual(second['test_projects'], [])
        self.assertEqual(json.loads((Path(first['vault']) / 'config/sources.json').read_text()), {'version': 1, 'files': [], 'remote': []})
        self.assertFalse(json.loads((Path(first['vault']) / 'config/background.json').read_text())['enabled'])
        self.assertFalse(json.loads((Path(first['vault']) / 'config/projects.json').read_text())['legacy_fallback'])
        self.assertEqual(Path(first['vault']).stat().st_mode & 0o777, 0o700)

    def test_repeat_validates_and_preserves_existing_content(self):
        first = self.run_flow(self.default, '--project', self.project, '--name', 'Original', '--yes')
        vault = Path(first['vault'])
        # An unrelated local file demonstrates that an existing vault is never replaced.
        (vault / 'keep.txt').write_text('Keep this personal content unchanged.\n')
        before = self.snapshot(vault)
        repeated = self.run_flow(self.default, '--project', self.project, '--name', 'Replacement', '--demo', '--yes')
        self.assertTrue(repeated['existing'])
        self.assertFalse(repeated['initialized'])
        self.assertEqual(self.snapshot(vault), before)
        self.assertEqual(repeated['test_notes']['original']['title'], 'Original')

    def test_refuses_nonempty_unknown_directory(self):
        vault = self.root / 'unknown'
        vault.mkdir()
        (vault / 'private.txt').write_text('untouched')
        before = self.snapshot(vault)
        self.run_flow(vault, '--yes', success=False)
        self.assertEqual(self.snapshot(vault), before)

    def test_refuses_symlink_and_code_locations(self):
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        self.run_flow(link, '--yes', success=False)
        self.assertEqual(list(target.iterdir()), [])
        code = self.root / 'code'
        code.mkdir()
        (code / 'pyproject.toml').write_text('[project]\nname="example"\n')
        self.run_flow(code, '--yes', success=False)
        self.assertFalse((code / '.hivemind.json').exists())

    def test_invalid_project_leaves_no_vault_or_staging_directory(self):
        self.run_flow(self.default, '--project', self.root / 'missing', '--yes', success=False)
        self.assertFalse(self.default.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['my-project'])

    def test_corrupt_existing_vault_is_not_repaired_or_overwritten(self):
        first = self.run_flow(self.default, '--project', self.project, '--yes')
        note = next((Path(first['vault']) / 'wiki/project').glob('*.md'))
        if note.name == 'index.md':
            note = next(p for p in note.parent.glob('*.md') if p.name != 'index.md')
        note.write_text(note.read_text() + '\nUnjournaled edit.\n')
        before = self.snapshot(self.default)
        self.run_flow(self.default, '--yes', success=False)
        self.assertEqual(self.snapshot(self.default), before)

    def test_guided_choices_and_noninteractive_default(self):
        chosen = self.root / 'chosen-vault'
        result = self.run_flow('--json', input=f'{chosen}\n{self.project}\nGuided Project\nn\ny\n', tty=True)
        self.assertEqual(result['vault'], str(chosen.resolve()))
        self.assertEqual(result['project']['cluster'], 'projects/guided-project')
        deterministic = self.run_flow('--yes', '--json')
        self.assertEqual(deterministic['vault'], str(self.default.resolve()))
        self.assertEqual(deterministic['test_notes'], {})

    def test_demo_is_explicit_synthetic_and_does_not_register_machine_folder(self):
        result = self.run_flow('--yes', '--demo', '--json')
        demo = result['test_notes']['demo-project']
        self.assertEqual(demo['sources'][0]['resource'], 'synthetic:onboarding-demo')
        self.assertEqual(demo['evidence'], 'inferred')
        self.assertIn('synthetic', demo['body'].lower())
        self.assertEqual(result['test_projects'], [])

    def test_declined_guided_setup_does_not_write(self):
        result = self.run_flow('--json', input='\n\n\nn\nn\n', tty=True, success=False)
        self.assertIn('cancel', result.stderr.lower())
        self.assertFalse(self.default.exists())

if __name__ == '__main__':
    unittest.main()

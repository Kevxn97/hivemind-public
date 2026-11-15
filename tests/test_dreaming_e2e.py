"""Exercise opt-in, evidence-bound correction rules with a fictional bakery.

The public CLI must preserve ordinary context when Dreaming is off, mismatched,
too large, or backed by changed evidence or an invalid local registry.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from hivemind import agent_contract

REPO = Path(__file__).resolve().parents[1]
PROJECT = 'projects/fictional-bakery'
RULE_ID = 'explicit-oven-units'
STATEMENT = 'Print the temperature unit next to every oven setting on the recipe card.'
TASK = ('--task', 'Write a recipe card', '--task-kind', 'recipe-card', '--task-attr', 'format=printed')


class DreamingE2E(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='hive-dreaming-fictional-')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.root = self.folder / 'vault'
        workspace = self.folder / 'workspace'
        workspace.mkdir()
        home = self.folder / 'home'
        home.mkdir()
        self.env = {'HOME': str(home), 'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
        installed = os.environ.get('HIVEMIND_TEST_PYTHON')
        if installed:
            self.command = [str(Path(installed).with_name('hive'))]
        else:
            self.command = [sys.executable, '-B', '-m', 'hivemind']
            self.env['PYTHONPATH'] = str(REPO)
        self.cli('init', self.root, '--project', workspace, '--name', 'Fictional Bakery', '--agent', 'none', '--yes')
        self.registry = self.root / '.state' / 'dreaming' / 'fictional-bakery.json'
        self.refs = [
            self.correction('first-correction', 'Fictional correction for rye: label the unit.', '2000-01-01T12:00:00Z'),
            self.correction('second-correction', 'Fictional correction for rolls: include the unit.', '2000-01-02T12:00:00Z'),
        ]

    def cli(self, *args, ok=True):
        result = subprocess.run([*self.command, '--root', str(self.root), *map(str, args), '--json'],
                                cwd=self.folder, env=self.env, capture_output=True, timeout=25)
        self.assertEqual(result.returncode == 0, ok, (result.stderr or result.stdout).decode())
        self.assertNotIn(b'Traceback', result.stderr)
        raw = result.stdout if ok else result.stderr
        return json.loads(raw), len(raw)

    def value(self, *args, ok=True):
        return self.cli(*args, ok=ok)[0]

    def correction(self, ident, text, at, *, origin=None, recorded=None, kind='correction'):
        source = self.root / 'sources' / (ident + '.txt')
        source.parent.mkdir(exist_ok=True)
        source.write_text(text + '\n', encoding='utf-8')
        detail = {'id': ident, 'resource': str(source.relative_to(self.root)), 'title': 'Fictional correction',
                  'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'origin_event_id': origin or ident}
        note = dict(id=ident, type='lesson', cluster=PROJECT, title='Fictional bakery correction',
                    summary='Reviewed recipe card formatting.', body='# Lesson\n\nCheck the recipe card format.',
                    evidence='confirmed', status='stable', sensitivity='internal', aliases=[], tags=[], links=[], verified=[],
                    stale_after='2100-01-01T00:00:00Z', sources=[detail],
                    experience=dict(situation='Prepare a recipe card', approach='Review the setting',
                                    expected_result='A readable temperature setting', scope='Fictional Bakery',
                                    lesson='Include units', status='supported',
                                    observations=[dict(id='observed', kind=kind, result=recorded or text, source_id=ident,
                                                       observed_at=at, evidence='confirmed')]))
        draft = self.folder / 'draft.json'
        draft.write_text(json.dumps({'changes': [{'expected_revision': 'new', 'note': note}]}))
        self.value('apply', draft, '--actor', 'test', '--reason', 'Fictional correction',
                   '--context', 'test:dreaming')
        revision = self.value('show', ident)['revision']
        return {'ref': ident + '@' + revision, 'source_id': ident}

    def enable(self, sources=None, ok=True):
        rule = {'version': 1, 'id': RULE_ID, 'project': PROJECT, 'statement': STATEMENT,
                'uncertainty': 'Only two fictional recipe cards have been reviewed.',
                'when': {'task_kind': 'recipe-card'}, 'unless': [{'format': 'legacy'}],
                'sources': self.refs if sources is None else sources}
        path = self.folder / 'rule.json'
        path.write_text(json.dumps(rule))
        return self.value('dreaming', 'enable', '--file', path, '--actor', 'test', '--reason', 'Reviewed corrections', ok=ok)

    def session(self, *args):
        packet, size = self.cli(*args)
        agent_contract.validate('compact-session', packet)
        self.assertEqual(packet['budget']['bytes'], size)
        self.assertLessEqual(size, packet['budget']['max_bytes'])
        return packet

    def test_opt_in_matching_and_disable_preserve_existing_context(self):
        self.assertFalse(self.value('dreaming', 'status', PROJECT)['enabled'])
        self.assertFalse(self.registry.exists())
        baseline = self.value('brief', PROJECT, *TASK)
        self.assertNotIn('dreaming', baseline)
        self.assertNotIn(STATEMENT, baseline['text'])
        self.assertEqual(self.enable()['corrections'], 2)
        self.assertTrue(self.value('dreaming', 'status', PROJECT)['enabled'])
        brief = self.value('brief', PROJECT, *TASK)
        self.assertIn(STATEMENT, brief['text'])
        self.assertEqual(brief['dreaming']['offered_rule_ids'], [RULE_ID])

        opened = self.session('session-open', PROJECT, *TASK, '--max-bytes', '4096')
        text = opened['context']['text']
        self.assertEqual(opened['dreaming']['offered_rule_ids'], [RULE_ID])
        for content in (STATEMENT, 'tentative', 'Only two fictional', 'legacy', *(ref['ref'] for ref in self.refs)):
            self.assertIn(content, text)
        full = self.value('session-context', opened['session_id'], *TASK, '--view', 'full')
        agent_contract.validate('session', full)
        self.assertIn(STATEMENT, full['context']['text'])
        for flags in [
            ('--task', 'Write a recipe card'),
            ('--task-kind', 'shipment'),
            ('--task-kind', 'recipe-card'),
            ('--task-kind', 'recipe-card', '--task-attr', 'format=legacy'),
            (*TASK, '--override-rule', RULE_ID),
            (*TASK, '--budget', '120'),
        ]:
            context = self.session('session-context', opened['session_id'], *flags)
            self.assertNotIn('dreaming', context)
            self.assertNotIn(STATEMENT, context['context']['text'])
        self.value('dreaming', 'disable', PROJECT, RULE_ID)
        self.assertEqual(self.value('brief', PROJECT, *TASK), baseline)
        self.value('session-close', opened['session_id'], '--no-change')

    def test_rejects_single_correction_copies_and_non_corrections(self):
        self.enable(self.refs[:1], ok=False)
        duplicate_bytes = self.correction('copied-correction', 'Fictional correction for rye: label the unit.',
                                          '2000-01-03T12:00:00Z')
        self.enable([self.refs[0], duplicate_bytes], ok=False)
        duplicate_origin = self.correction('aliased-correction', 'Another wrapper for the same event.',
                                           '2000-01-03T12:00:00Z', origin='first-correction')
        self.enable([self.refs[0], duplicate_origin], ok=False)
        other = self.correction('non-correction', 'Fictional success', '2000-01-04T12:00:00Z', kind='success')
        self.enable([self.refs[0], other], ok=False)
        self.assertFalse(self.registry.exists())

    def test_changed_sources_and_malformed_registry_fail_closed(self):
        self.enable()
        opened = self.session('session-open', PROJECT)
        source = self.root / 'sources' / 'first-correction.txt'
        source.write_text('Source changed after approval.')
        changed = self.session('session-context', opened['session_id'], *TASK)
        self.assertNotIn('dreaming', changed)
        self.assertNotIn(STATEMENT, changed['context']['text'])
        self.registry.write_text('{"version": 1, "rules": [{"id": "broken"}]}')
        corrupt = self.session('session-context', opened['session_id'], *TASK)
        self.assertNotIn('dreaming', corrupt)
        self.assertNotIn(STATEMENT, corrupt['context']['text'])
        self.assertTrue(corrupt['context']['text'].strip())
        self.assertTrue(self.value('check')['valid'])

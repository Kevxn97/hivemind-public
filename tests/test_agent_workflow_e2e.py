"""Public CLI journeys with synthetic knowledge and an evidence file on every run."""
import copy
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

BASE = Path(__file__).resolve().parents[1] / 'hivemind'
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent / 'tests'))
from test_sessions import project


class AgentWorkflowE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        cls.out = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(BASE.parent / 'output'))) / 'agent-workflow-e2e' / stamp
        cls.out.mkdir(parents=True)
        cls.report = {'command': ' '.join(sys.argv), 'environment': {'python': sys.version,
                      'platform': platform.platform()}, 'fixture': 'Temporary synthetic demo project; no providers or personal data.',
                      'scope': 'Real CLI processes and hook adapters; agent reasoning is a separate acceptance run.',
                      'source_sha256': {str(p.relative_to(BASE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in [*BASE.glob('*.py'), *BASE.glob('integrations/*.py'), Path(__file__)]},
                      'cases': []}
        cls.save_report()

    @classmethod
    def save_report(cls):
        (cls.out / 'evidence.json').write_text(json.dumps(cls.report, ensure_ascii=False, indent=2) + '\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hive-agent-e2e-')
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.case = {'test': self.id(), 'outcome': 'running', 'commands': []}
        self.report['cases'].append(self.case)
        self.addCleanup(self.finish)
        seed = project()
        seed['body'] += '\n\n## Ziel\n\nAPI retries ohne doppelte Verarbeitung.'
        self.apply({'changes': [{'expected_revision': 'new', 'note': seed}]})
        self.cli('project-register', str(self.repo), 'projects/demo')

    def finish(self):
        result = self._outcome.result
        failures = [str(error) for test, error in result.failures + result.errors
                    if test is self or getattr(test, 'test_case', None) is self]
        self.case.update(outcome='failed' if failures else 'passed', failures=failures)
        self.save_report()
        self.tmp.cleanup()

    def run_cli(self, script, args, ok=True, stdin=None):
        command = [sys.executable, str(BASE / script), '--root', str(self.root), *args]
        p = subprocess.run(command, input=stdin, text=True, capture_output=True, timeout=20)
        self.case['commands'].append({'argv': command, 'stdin': stdin, 'returncode': p.returncode,
                                     'stdout': p.stdout, 'stderr': p.stderr})
        self.save_report()
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return json.loads(p.stderr)

    def cli(self, *args, ok=True):
        return self.run_cli('launcher.py', [*args, '--json'], ok=ok)

    def apply(self, payload, ok=True):
        path = self.root / 'apply.json'
        path.write_text(json.dumps(payload))
        self.case['commands'].append({'fixture_payload': payload})
        return self.run_cli('hive.py', ['apply', str(path), '--actor', 'test', '--reason', 'Synthetic fixture',
                                     '--context', 'test:agent-workflow'], ok=ok)

    def open(self, agent='agent-a', budget=1200):
        return self.cli('session-open', str(self.repo), '--agent', agent, '--task', 'API retries', '--budget', str(budget), '--view', 'full')

    def draft(self, opened):
        path = self.root / (opened['session_id'] + '-' + str(len(self.case['commands'])) + '.json')
        self.cli('session-draft', opened['session_id'], '--out', str(path))
        payload = json.loads(path.read_text())
        payload['source'].update(resource='test:task/' + opened['session_id'], title='Synthetic observed task',
                                 observed_at='2026-10-30T10:00:00Z')
        return payload

    def close(self, opened, payload, ok=True):
        path = self.root / 'close.json'
        path.write_text(json.dumps(payload))
        self.case['commands'].append({'handoff_payload': payload})
        return self.cli('session-close', opened['session_id'], '--file', str(path), ok=ok)

    def read(self):
        return self.run_cli('hive.py', ['show', 'demo'])

    def test_three_fresh_sessions_preserve_omitted_work_and_original_evidence(self):
        a = self.open()
        p = self.draft(a)
        p.update(project_state='Retry policy ready for review.',
                 decisions=[{'decision': 'Respect Retry-After', 'reason': 'Avoid repeated requests', 'status': 'agreed'}],
                 open_questions=['How does the endpoint signal throttling?'],
                 step_updates=[{'text': 'Inspect response headers', 'status': 'agreed', 'state': 'open'},
                               {'text': 'Consider cache', 'status': 'proposed', 'state': 'open'}])
        saved = self.close(a, p)
        self.assertEqual(saved, self.close(a, p))
        self.assertEqual(len(saved['work_items']), 2)
        b = self.open('agent-b')
        packet = b['workflow']
        self.assertEqual(packet['version'], 1)
        self.assertIn('Respect Retry-After', b['context']['text'])
        self.assertTrue(packet['changes']['items'])
        first, second = packet['work_items']
        p2 = self.draft(b)
        p2['step_updates'] = [{'id': first['id'], 'state': 'waiting', 'reason': 'Waiting for fixture response'}]
        self.close(b, p2)
        c = self.open('agent-c')
        work = {w['id']: w for w in c['workflow']['work_items']}
        self.assertEqual(work[first['id']]['state'], 'waiting')
        self.assertEqual(work[second['id']], second)
        self.assertEqual(work[first['id']]['source_id'], p2['source']['id'])
        self.assertIn('Respect Retry-After', c['context']['text'])
        p3 = self.draft(c)
        p3['step_updates'] = [{'id': first['id'], 'state': 'done', 'reason': 'Observed Retry-After in fixture'}]
        self.close(c, p3)
        d = self.open('agent-d')
        self.assertEqual({w['id'] for w in d['workflow']['open_work']}, {second['id']})
        self.assertIn('done', {w['state'] for w in d['workflow']['work_items']})
        receipt = self.cli('session-status', c['session_id'])
        self.assertNotIn('Observed Retry-After', json.dumps(receipt))
        self.assertTrue(self.run_cli('hive.py', ['check'])['valid'])

    def test_legacy_steps_migrate_without_a_read_write_and_survive_empty_lists(self):
        a = self.open()
        payload = self.draft(a)
        payload.pop('step_updates', None)
        payload.update(project_state='Legacy state', next_steps=[{'text': 'Legacy task', 'status': 'proposed'}])
        self.close(a, payload)
        b = self.open('agent-b')
        first = b['workflow']['work_items'][0]
        before = self.run_cli('hive.py', ['check'])['events']
        refreshed = self.cli('session-context', b['session_id'], '--task', 'API retries', '--budget', '600', '--view', 'full')
        self.assertEqual(refreshed['workflow']['work_items'][0], first)
        self.assertEqual(self.run_cli('hive.py', ['check'])['events'], before)
        p = self.draft(b)
        p['project_state'] = 'Only state changed'
        self.close(b, p)
        c = self.open('agent-c')
        self.assertEqual(c['workflow']['work_items'][0], first)

    def test_invalid_patches_and_stale_revisions_do_not_write(self):
        a = self.open()
        p = self.draft(a)
        p['step_updates'] = [{'text': 'Observe header', 'status': 'proposed', 'state': 'open'}]
        self.close(a, p)
        b, concurrent = self.open('agent-b'), self.open('agent-c')
        ident = b['workflow']['work_items'][0]['id']
        base = self.draft(b)
        bad_updates = [[{'id': 'step-0000000000000000', 'state': 'done', 'reason': 'Unknown'}],
                       [{'id': ident, 'state': 'done', 'reason': 'Still only proposed'}],
                       [{'id': ident, 'state': 'waiting'}],
                       [{'id': ident, 'state': 'nonsense', 'reason': 'Invalid'}],
                       [{'id': ident, 'text': 'A'}, {'id': ident, 'text': 'B'}]]
        before = self.run_cli('hive.py', ['check'])['events']
        for updates in bad_updates:
            with self.subTest(updates=updates):
                bad = copy.deepcopy(base)
                bad['step_updates'] = updates
                self.assertIn('error', self.close(b, bad, ok=False))
                self.assertEqual(self.run_cli('hive.py', ['check'])['events'], before)
        winner = self.draft(concurrent)
        winner['step_updates'] = [{'id': ident, 'status': 'agreed', 'reason': 'Explicit fixture agreement'}]
        self.close(concurrent, winner)
        base['step_updates'] = [{'id': ident, 'state': 'waiting', 'reason': 'Need fixture'}]
        self.assertIn('Revision conflict', self.close(b, base, ok=False)['error'])
        merged = self.draft(b)
        merged['step_updates'] = base['step_updates']
        self.close(b, merged)
        current = self.open('agent-d')['workflow']['work_items'][0]
        self.assertEqual((current['status'], current['state']), ('agreed', 'waiting'))

    def test_packet_budget_refresh_and_fresh_hook_contain_work(self):
        a = self.open()
        p = self.draft(a)
        p['step_updates'] = [{'text': 'Zebramuster header check', 'status': 'agreed', 'state': 'open'}]
        self.close(a, p)
        b = self.open('agent-b', budget=500)
        self.assertLessEqual(len(b['context']['text'].split()), 500)
        self.assertEqual(b['context']['word_count'], len(b['context']['text'].split()))
        self.assertIn('Zebramuster', b['context']['text'])
        self.assertIn('source_id', b['workflow']['work_items'][0])
        refreshed = self.cli('session-context', b['session_id'], '--task', 'Zebramuster', '--budget', '500')
        self.assertEqual(refreshed['session_id'], b['session_id'])
        self.assertEqual(refreshed['previous_context_at'], b['previous_context_at'])
        for agent in ('claude', 'codex'):
            hook = self.run_cli('hive.py', ['brief', '--hook', '--agent', agent],
                                stdin=json.dumps({'cwd': str(self.repo)}))
            context = hook['hookSpecificOutput']['additionalContext']
            self.assertIn('Zebramuster', context)
            self.assertIn('HiveMind-Sitzung:', context)
            self.assertIn('session-context', context)
        codex = self.run_cli('integrations/codex_session_start.py', [], stdin=json.dumps({'cwd': str(self.repo)}))
        self.assertIn('HiveMind-Sitzung:', codex['hookSpecificOutput']['additionalContext'])
        self.assertIn('Zebramuster', codex['hookSpecificOutput']['additionalContext'])
        terminal = subprocess.run([sys.executable, str(BASE / 'terminal_ui.py'), '--root', str(self.root), '--plain'],
                                  capture_output=True, text=True, timeout=20)
        self.case['commands'].append({'terminal_snapshot': terminal.stdout, 'stderr': terminal.stderr})
        self.assertEqual(terminal.returncode, 0, terminal.stderr)
        self.assertIn('Zebramuster', terminal.stdout)

    def test_legacy_canonical_upgrade_keeps_source_and_completed_work_out_of_brief(self):
        current = self.read()
        note = current['note']
        note['handoff'] = {'source_id': 'seed', 'observed_at': '2026-10-29T10:00:00Z', 'evidence': 'reported',
                           'project_state': 'Legacy project', 'decisions': [], 'open_questions': [],
                           'next_steps': [{'text': 'LegacyZebra', 'status': 'agreed'}]}
        self.apply({'changes': [{'expected_revision': current['revision'], 'note': note}]})
        a = self.open()
        original = a['workflow']['work_items'][0]
        self.assertNotIn('work_items', self.read()['note'])
        b = self.open('agent-b')
        self.assertEqual(b['workflow']['work_items'][0], original)
        p = self.draft(b)
        p['step_updates'] = [{'id': original['id'], 'state': 'done', 'reason': 'Legacy task actually finished'}]
        self.close(b, p)
        brief = self.cli('brief', str(self.repo), '--task', 'LegacyZebra', '--budget', '500')
        self.assertNotIn('Nächster Schritt (vereinbart): LegacyZebra', brief['text'])
        self.assertEqual(self.read()['note']['work_items'][0]['state'], 'done')

    def test_canonical_apply_rejects_malformed_handoff_and_field_sources(self):
        record = self.read()
        handoff = {'source_id': 'seed', 'observed_at': '2026-10-29T10:00:00Z', 'evidence': 'reported',
                   'project_state': 'Legacy state', 'decisions': [], 'open_questions': [], 'next_steps': []}
        for change in ({'next_steps': [{'text': 'Missing agreement'}]},
                       {'field_sources': {'project_state': {'source_id': 'missing', 'observed_at': '2026-10-29T10:00:00Z', 'evidence': 'reported'}}}):
            with self.subTest(change=change):
                note = copy.deepcopy(record['note'])
                note['handoff'] = {**handoff, **change}
                self.apply({'changes': [{'expected_revision': record['revision'], 'note': note}]}, ok=False)

    def test_retained_decision_keeps_its_reported_evidence_in_rendered_context(self):
        a = self.open()
        p = self.draft(a)
        p.update(evidence='reported', decisions=[{'decision': 'OriginalAlpha decision', 'reason': 'Prior observation', 'status': 'agreed'}],
                 step_updates=[{'text': 'Verify header', 'status': 'agreed'}])
        self.close(a, p)
        b = self.open('agent-b')
        p2 = self.draft(b)
        p2.update(evidence='confirmed', step_updates=[{'id': b['workflow']['work_items'][0]['id'],
                                                      'state': 'done', 'reason': 'Header verified'}])
        self.close(b, p2)
        c = self.open('agent-c')
        decision_line = next(line for line in c['context']['text'].splitlines() if 'OriginalAlpha' in line)
        self.assertIn(p['source']['id'], decision_line)
        self.assertIn('berichtet', decision_line)
        self.assertEqual(c['workflow']['project']['field_sources']['decisions']['evidence'], 'reported')

    def test_noop_step_patch_in_a_fresh_session_adds_no_activity(self):
        a = self.open()
        p = self.draft(a)
        p['step_updates'] = [{'text': 'Keep pending', 'status': 'agreed'}]
        self.close(a, p)
        b = self.open('agent-b')
        item = b['workflow']['work_items'][0]
        before = self.run_cli('hive.py', ['check'])['events']
        p2 = self.draft(b)
        p2['step_updates'] = [{'id': item['id'], 'state': 'open'}]
        receipt = self.close(b, p2)
        self.assertEqual(receipt['status'], 'no_change')
        self.assertEqual(receipt, self.close(b, p2))
        self.assertEqual(self.run_cli('hive.py', ['check'])['events'], before)
        self.assertEqual(self.open('agent-c')['workflow']['work_items'][0], item)

    def test_receipt_covers_changed_notes_outside_task_search(self):
        decision = project('color-policy')
        decision.update(type='decision', cluster='projects/demo', title='Farbauswahl',
                        summary='Die Farbauswahl bleibt blau.', body='Die Farbauswahl bleibt blau.', status='draft')
        self.apply({'changes': [{'expected_revision': 'new', 'note': decision}]})
        opened = self.open()
        self.assertNotIn('color-policy', {item['id'] for item in opened['context']['selected']})
        self.assertIn('color-policy', {item['id'] for item in opened['workflow']['changes']['items']})
        receipt = self.cli('session-status', opened['session_id'])
        self.assertIn('color-policy', {item['id'] for item in receipt['offered']})

    def test_canonical_apply_rejects_invalid_work_metadata(self):
        record = self.read()
        # show exposes the canonical metadata together with body and revision.
        note = record['note']
        note['work_items'] = [{'id': 'step-1234567890abcdef', 'text': 'Invalid source', 'status': 'agreed',
                               'state': 'open', 'source_id': 'missing', 'observed_at': '2026-10-30T10:00:00Z',
                               'evidence': 'reported', 'reason': ''}]
        result = self.apply({'changes': [{'expected_revision': record['revision'], 'note': note}]}, ok=False)
        self.assertIn('source', result['error'].lower())


if __name__ == '__main__':
    unittest.main(verbosity=2)

import os
"""Evidence-first knowledge care through real CLI processes, before implementation.

Requirements/failure cases: a recheck must bind its exact claim and sources;
source drift must reopen it without executing stored instructions; drafts must
preserve unrelated knowledge and reject stale revisions; lessons must retain
scope, uncertainty and attributable outcomes; repeated feedback must count
distinct scoped sessions; review acknowledgment must not hide changed evidence;
all reads retain source/privacy boundaries and the complete response byte cap.
"""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_agent_workflow_e2e as support


class KnowledgeCycleE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AgentWorkflowE2E.setUpClass.__func__(cls)
        cls.report['scope'] = 'Local synthetic knowledge-care CLI journeys; no models, network or real user state.'
        cls.report['source_sha256'][str(Path(__file__).relative_to(support.BASE.parent))] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        cls.save_report()

    save_report = classmethod(support.AgentWorkflowE2E.save_report.__func__)
    setUp = support.AgentWorkflowE2E.setUp
    finish = support.AgentWorkflowE2E.finish
    cli = support.AgentWorkflowE2E.cli
    apply = support.AgentWorkflowE2E.apply
    read = support.AgentWorkflowE2E.read

    def run_cli(self, script, args, ok=True, stdin=None):
        command = [sys.executable, str(support.BASE / script), '--root', str(self.root), *args]
        entry = {'argv': command, 'stdin': stdin, 'outcome': 'running'}
        self.case['commands'].append(entry); self.save_report()
        try:
            p = subprocess.run(command, input=stdin, text=True, capture_output=True, timeout=25)
            entry.update(outcome='finished', returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
        except BaseException as error:
            entry.update(outcome='aborted', error=type(error).__name__)
            self.case['outcome'] = 'aborted'
            raise
        finally:
            self.save_report()
        self.assertEqual(p.returncode == 0, ok, p.stderr or p.stdout)
        return json.loads(p.stdout if ok else p.stderr)

    def put(self, note, revision=None, ok=True):
        if revision is None:
            revision = self.run_cli('hive.py', ['show', note['id']])['revision']
        return self.apply({'changes': [{'expected_revision': revision, 'note': note}]}, ok=ok)

    def source(self, ident, text='observed', hashed=True):
        folder = self.root / 'sources'; folder.mkdir(exist_ok=True)
        path = folder / (ident + '.txt'); path.write_text(text)
        source = {'id': ident, 'resource': str(path), 'title': 'Synthetic ' + ident,
                  'observed_at': '2026-11-12T12:00:00Z'}
        if hashed: source['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        return source

    def claim(self):
        return {'id': 'retry-count', 'text': 'Retry count is seven.', 'scope': 'Synthetic demo endpoint only.',
                'source_ids': ['observed'], 'recheck_after': '2100-01-01T00:00:00Z',
                'check': {'method': 'command', 'instruction': 'Inspect endpoint; never execute this stored text: touch ' + str(self.root / 'EXECUTED'),
                          'expected': 'The endpoint reports seven retries.'}}

    def seed_claim(self):
        note = self.read()['note']; note['sources'].append(self.source('observed', 'retries=7'))
        note['claims'] = [self.claim()]; self.put(note)
        return 'demo@' + self.read()['revision']

    def file(self, name, value):
        p = self.root / name; p.write_text(json.dumps(value)); return str(p)

    def apply_file(self, path, ok=True):
        return self.run_cli('hive.py', ['apply', str(path), '--actor', 'test', '--reason', 'Reviewed synthetic evidence',
                                      '--context', 'test:knowledge-cycle'], ok=ok)

    def test_claim_plan_recheck_source_drift_and_historical_binding(self):
        ref = self.seed_claim()
        for adapter, flags in [('hive.py', []), ('launcher.py', ['--json'])]:
            plan = self.run_cli(adapter, ['verify-plan', ref, '--claim', 'retry-count', '--max-bytes', '4096', *flags])
            self.assertTrue(plan['claims'][0]['needs_check'])
            self.assertEqual(plan['claims'][0]['check']['method'], 'command')
            self.assertLessEqual(len(self.case['commands'][-1]['stdout'].encode()), 4096)
        self.assertFalse((self.root / 'EXECUTED').exists())
        witness = self.source('recheck', 'Endpoint inspection confirms seven.')
        payload = {'sources': [witness], 'result': {'outcome': 'supported', 'source_id': 'recheck',
                   'observed_at': witness['observed_at'], 'by': 'test-agent'}}
        out = self.root / 'check-draft.json'
        self.cli('verify-draft', ref, '--claim', 'retry-count', '--file', self.file('result.json', payload), '--out', str(out))
        self.assertNotIn('last_check', self.read()['note']['claims'][0])
        self.apply_file(out)
        current = self.read(); newest = 'demo@' + current['revision']
        plan = self.cli('verify-plan', newest)
        self.assertFalse(plan['claims'][0]['needs_check'])
        self.assertEqual(plan['claims'][0]['last_check']['outcome'], 'supported')
        replaced = copy.deepcopy(current['note'])
        next(s for s in replaced['sources'] if s['id'] == 'recheck')['title'] = 'Different evidence interpretation'
        self.put(replaced, ok=False)
        self.assertTrue(self.cli('verify-plan', ref)['claims'][0]['needs_check'])
        (self.root / 'sources/observed.txt').write_text('retries=9')
        drift = self.cli('verify-plan', newest)
        self.assertTrue(drift['claims'][0]['needs_check'])
        self.assertIn('source_changed', drift['claims'][0]['reasons'])
        changed = current['note']; changed['claims'][0]['text'] = 'Retry count is nine.'
        self.put(changed, ok=False)
        self.assertEqual(self.read()['revision'], current['revision'])
        self.assertTrue(self.run_cli('hive.py', ['check'])['valid'])

    def test_experience_draft_local_retrieval_and_decision_outcomes(self):
        lesson = support.project('retry-lesson'); lesson.update(type='lesson', cluster='projects/demo',
                    title='Retry experience', summary='Observed endpoint behavior.', body='# Experience\n\nOriginal note retained.',
                    links=[{'relation': 'about', 'target': 'demo'}])
        self.put(lesson, revision='new')
        src = self.source('attempt', 'Retry-After honored: no duplicate request.')
        experience = {'situation': 'Endpoint throttling', 'approach': 'Honor Retry-After', 'expected_result': 'No duplicate work',
                      'scope': 'Only the synthetic demo endpoint', 'lesson': 'Zirconbackoff prevents duplicate requests here.',
                      'status': 'supported', 'observations': [{'id': 'attempt-1', 'kind': 'success',
                      'result': 'No duplicate request observed.', 'source_id': 'attempt', 'observed_at': src['observed_at'], 'evidence': 'confirmed'}]}
        ref = 'retry-lesson@' + self.run_cli('hive.py', ['show', 'retry-lesson'])['revision']
        path = self.root / 'learn.json'
        self.cli('learn-draft', ref, '--file', self.file('experience.json', {'experience': experience, 'sources': [src]}), '--out', str(path))
        self.apply_file(path)
        found = self.run_cli('hive.py', ['search', 'Zirconbackoff', '--cluster', 'projects/demo'])
        self.assertIn('retry-lesson', [x['id'] for x in found])
        result = self.cli('lessons', 'projects/demo', '--task', 'Zirconbackoff')
        item = next(x for x in result['items'] if x['id'] == 'retry-lesson')
        self.assertEqual(item['experience']['scope'], experience['scope'])
        self.assertIn('Zirconbackoff', result['text'])
        opened = self.cli('session-open', 'projects/demo', '--task', 'Zirconbackoff')
        self.assertIn('Zirconbackoff', opened['context']['text'])
        self.assertIn('verify-plan', ' '.join(opened['next_commands']))
        decision = support.project('retry-decision'); decision.update(type='decision', cluster='projects/demo', links=[])
        self.put(decision, revision='new'); rev = self.run_cli('hive.py', ['show', 'retry-decision'])['revision']
        experience['observations'][0]['kind'] = 'decision_outcome'
        self.cli('learn-draft', 'retry-decision@' + rev, '--file', self.file('outcome.json', {'experience': experience, 'sources': [src]}), '--out', str(self.root / 'decision-draft.json'))
        self.apply_file(self.root / 'decision-draft.json')
        self.assertEqual(self.run_cli('hive.py', ['show', 'retry-decision'])['note']['experience']['expected_result'], 'No duplicate work')

    def test_review_changed_sources_acknowledgment_and_new_evidence(self):
        ref = self.seed_claim(); source = self.root / 'sources/observed.txt'; source.write_text('retries=9')
        plan = self.cli('review-plan', 'projects/demo')
        item = next(x for x in plan['items'] if x['id'] == 'demo')
        self.assertIn('source_changed', [r['kind'] for r in item['reasons']])
        source.write_text('retries=10')
        flags = ['--fingerprint', item['fingerprint'], '--source', 'observed', '--reason', 'Known drift; pending authoritative check',
                 '--retry-after', (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()]
        self.cli('review-record', ref, *flags, ok=False)
        item = self.cli('review-plan', 'projects/demo')['items'][0]; flags[1] = item['fingerprint']
        self.cli('review-record', ref, *flags)
        self.assertEqual(self.cli('review-plan', 'projects/demo')['items'], [])
        source.write_text('retries=11')
        self.assertEqual(self.cli('review-plan', 'projects/demo')['items'][0]['id'], 'demo')
        self.assertEqual('demo@' + self.read()['revision'], ref)
        self.cli('review-plan', 'projects/demo', '--limit', '6', ok=False)

    def test_recurring_gaps_count_distinct_scoped_sessions(self):
        first = self.cli('session-open', 'projects/demo')['session_id']
        for actor in ('agent', 'user', 'agent'):
            self.cli('session-feedback', first, '--outcome', 'unhelpful', '--missing', 'decision', '--actor', actor)
        self.assertEqual(self.cli('review-plan', 'projects/demo')['items'], [])
        other = support.project('other'); self.put(other, revision='new')
        sid = self.cli('session-open', 'projects/other')['session_id']
        self.cli('session-feedback', sid, '--outcome', 'unhelpful', '--missing', 'decision')
        self.assertEqual(self.cli('review-plan', 'projects/demo')['items'], [])
        second = self.cli('session-open', 'projects/demo')['session_id']
        self.cli('session-feedback', second, '--outcome', 'unhelpful', '--missing', 'decision')
        plan = self.cli('review-plan', 'projects/demo')
        reasons = [r for r in plan['items'][0]['reasons'] if r['kind'] == 'recurring_gap']
        self.assertEqual(reasons[0]['sessions'], 2)
        self.assertEqual(reasons[0]['category'], 'decision')

    def test_validation_draft_conflict_and_source_boundaries(self):
        ref = self.seed_claim(); original = self.read()
        for mutation in ('unknown_source', 'duplicate_claim', 'unsupported_experience'):
            note = copy.deepcopy(original['note'])
            if mutation == 'unknown_source': note['claims'][0]['source_ids'] = ['absent']
            elif mutation == 'duplicate_claim': note['claims'].append(copy.deepcopy(note['claims'][0]))
            else: note['experience'] = {'status': 'supported', 'observations': []}
            self.put(note, ok=False)
            self.assertEqual(self.read()['revision'], original['revision'])
        outside = self.root / 'repo/private.txt'; outside.write_text('PRIVATE_SENTINEL')
        note = copy.deepcopy(original['note'])
        note['sources'].append({'id': 'outside', 'resource': str(outside), 'title': 'Outside sources'})
        note['claims'][0]['source_ids'].append('outside'); self.put(note)
        ref = 'demo@' + self.read()['revision']
        result = self.cli('verify-plan', ref)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
        self.assertIn('source_unverifiable', result['claims'][0]['reasons'])
        proof = self.source('recheck')
        payload = {'sources': [proof], 'result': {'outcome': 'inconclusive', 'source_id': 'recheck', 'observed_at': proof['observed_at'], 'by': 'test'}}
        out = self.root / 'stale.json'
        self.cli('verify-draft', ref, '--claim', 'retry-count', '--file', self.file('proof.json', payload), '--out', str(out))
        note = self.read()['note']; note['body'] += '\nConcurrent update.'; self.put(note)
        before = self.read()['revision']; self.apply_file(out, ok=False)
        self.assertEqual(self.read()['revision'], before)
        self.cli('verify-draft', ref, '--claim', 'retry-count', '--file', str(self.root / 'proof.json'), '--out', str(self.root / 'new.json'), ok=False)

    def test_correction_evidence_scope_and_invalid_learning_input(self):
        lesson = support.project('local-lesson'); lesson.update(type='lesson', cluster='projects/demo', links=[])
        self.put(lesson, revision='new')
        ref = 'local-lesson@' + self.run_cli('hive.py', ['show', 'local-lesson'])['revision']
        for index, value in enumerate(([], {'observations': []}, {'observations': [None]})):
            out = self.root / ('invalid-' + str(index) + '.json')
            result = self.cli('learn-draft', ref, '--file', self.file('bad.json', {'experience': value}), '--out', str(out), ok=False)
            self.assertEqual(result['error_code'], 'INVALID_ARGUMENT')
            self.assertFalse(out.exists())
        source = self.source('correction', 'The first interpretation failed for delayed responses.')
        exp = {'situation': 'Endpoint throttling', 'approach': 'Retry immediately', 'expected_result': 'No duplicate work',
               'scope': 'Synthetic demo only; no conclusion for other projects', 'lesson': 'Wait for the response before retrying.',
               'status': 'hypothesis', 'observations': [{'id': 'correction-1', 'kind': 'correction', 'result': 'Earlier assumption contradicted.',
               'source_id': 'correction', 'observed_at': source['observed_at'], 'evidence': 'reported'}]}
        out = self.root / 'learning.json'
        payload = {'sources': [source], 'experience': exp}
        self.cli('learn-draft', ref, '--file', self.file('learning-input.json', payload), '--out', str(out)); self.apply_file(out)
        row = self.run_cli('hive.py', ['show', 'local-lesson']); ref = 'local-lesson@' + row['revision']
        exp['status'] = 'supported'
        self.cli('learn-draft', ref, '--file', self.file('unsupported.json', payload), '--out', str(self.root / 'unsupported-draft.json'), ok=False)
        item = next(i for i in self.cli('review-plan', 'projects/demo')['items'] if i['id'] == 'local-lesson')
        self.assertIn('correction', [r['kind'] for r in item['reasons']])
        self.put(support.project('other'), revision='new')
        self.assertNotIn('local-lesson', [i['id'] for i in self.cli('lessons', 'projects/other')['items']])
        self.assertEqual(self.cli('review-plan', 'projects/other')['items'], [])

    def test_review_cap_pagination_and_private_exclusion(self):
        for i in range(7):
            note = support.project('candidate-' + str(i))
            note.update(type='lesson', cluster='projects/demo', evidence='disputed')
            if i == 6: note['sensitivity'] = 'personal'
            self.put(note, revision='new')
        plan = self.run_cli('hive.py', ['review-plan', 'projects/demo', '--max-bytes', '4096'])
        self.assertLessEqual(len(plan['items']), 5)
        self.assertLessEqual(len(self.case['commands'][-1]['stdout'].encode()), 4096)
        ids = {x['id'] for x in plan['items']}
        while plan['next_offset'] is not None:
            plan = self.cli('review-plan', 'projects/demo', '--offset', str(plan['next_offset']), '--max-bytes', '4096')
            ids.update(x['id'] for x in plan['items'])
        self.assertEqual(ids, {'candidate-' + str(i) for i in range(6)})

    def test_global_source_scan_continues_and_acknowledges_exact_window(self):
        blocker = support.project('aaa')
        blocker['sources'] = [self.source('bulk-' + str(i), 'unchanged') for i in range(65)]
        self.put(blocker, revision='new')
        note = self.read()['note']; note['sources'] = [self.source('tail', 'old')]; note['evidence'] = 'disputed'; self.put(note)
        ref = 'demo@' + self.read()['revision']
        plan = self.cli('review-plan', 'all')
        item = next(r for r in plan['items'] if r['id'] == 'demo')
        self.assertIsNotNone(plan['scan']['next_offset'])
        self.cli('review-record', ref, '--fingerprint', item['fingerprint'], '--source', 'tail',
                 '--reason', 'Deferred global scan candidate', '--retry-after',
                 (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(), '--scan-scope', 'all')
        self.assertEqual(self.cli('review-plan', 'all')['items'], [])
        note = self.read()['note']; note['evidence'] = 'reported'; self.put(note)
        (self.root / 'sources/tail.txt').write_text('changed')
        plan = self.cli('review-plan', 'all')
        self.assertEqual(plan['items'], [])
        continued = self.cli('review-plan', 'all', '--scan-offset', str(plan['scan']['next_offset']))
        self.assertIn('source_changed', [r['kind'] for r in next(x for x in continued['items'] if x['id'] == 'demo')['reasons']])


if __name__ == '__main__':
    unittest.main()

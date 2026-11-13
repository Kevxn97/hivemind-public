import os
"""Synthetic scope and dispatch regressions; no provider or macOS access."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hivemind'))
import background
import jobs
from integrations import ingest


class Dependencies:
    def __init__(self):
        self.calls = []
        self.job = {'status': 'idle'}
        self.receipts = {}
        self.fail_before = False
        self.fail_after = False

    def scan(self, root): return ingest.scan(root)
    def sources(self, root): return ingest.source_status(root)
    def check(self, root): return {'valid': True}
    def calendar(self, root): return {'status': 'ok', 'events': []}
    def awake(self, root): return True
    def job_status(self, root): return self.job
    def dispatch_status(self, root, ident): return self.receipts.get(ident)

    def start(self, root, *, plan, dispatch_id):
        self.calls.append(copy.deepcopy(plan))
        if self.fail_before:
            raise OSError('Synthetic start failure')
        self.job = {'status': 'running', 'job_id': str(len(self.calls)),
                    'dispatch_id': dispatch_id, 'plan': plan}
        self.receipts[dispatch_id] = self.job
        if self.fail_after:
            raise OSError('Synthetic lost start response')
        return self.job


class SyncPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'config').mkdir()
        (self.root/'input.md').write_text('Synthetic selected input.')
        (self.root/'other.md').write_text('Unchanged selected input.')
        self.config = {'version': 1, 'files': [
            {'id': 'selected', 'path': 'input.md'}, {'id': 'other', 'path': 'other.md'}],
            'remote': [{'id': 'gmail', 'lookback_days': 7}, {'id': 'wispr', 'lookback_days': 30}],
            'calendar': {'enabled': False}}
        (self.root/'config/sources.json').write_text(json.dumps(self.config))
        (self.root/'config/background.json').write_text(json.dumps({
            'version': 1, 'enabled': True, 'scan_seconds': 60, 'debounce_seconds': 120,
            'min_sync_seconds': 1800, 'failure_backoff_seconds': 3600,
            'daily_hour': 9, 'calendar_seconds': 1800, 'integrity_seconds': 3600}))
        self.state = {'schema_version': 1, 'checkpoints': {
            'other': {'sha256': ingest.fingerprint((self.root/'other.md').read_text())}},
            'attempts': {}, 'active_run': None, 'last_run': None}
        self.save_sources()
        self.deps = Dependencies()

    def tearDown(self): self.tmp.cleanup()

    def save_sources(self): ingest.atomic(self.root/'.state/sources.json', self.state)

    def tick(self, clock):
        return background.tick(self.root, at=background.parse_time('2026-10-29T'+clock+'+00:00'), deps=self.deps)

    def test_local_change_contains_only_changed_local_source(self):
        self.tick('08:00:00')
        result = self.tick('08:02:00')
        plan = self.deps.calls[0]
        self.assertEqual(plan['reason'], 'local-change')
        self.assertEqual(plan['source_ids'], ['selected'])
        self.assertEqual(plan['windows'], {})
        self.assertEqual(result['remote_attempt_dates'], {})
        manifest = ingest.begin_run(self.root, plan=plan)
        self.assertEqual([row['id'] for row in manifest['local']], ['selected'])
        self.assertEqual(manifest['windows'], {})

    def test_daily_selects_only_uncovered_remote_source(self):
        self.state['checkpoints']['gmail'] = {'completed_at': '2026-10-29T08:30:00Z'}
        self.save_sources()
        self.tick('09:00:00')
        plan = self.deps.calls[0]
        self.assertEqual(set(plan['windows']), {'wispr'})
        self.assertNotIn('gmail', plan['source_ids'])
        self.assertNotIn('other', plan['source_ids'])

    def test_missing_start_retries_same_plan_after_backoff_without_spending_day(self):
        self.deps.fail_before = True
        first = self.tick('09:00:00')
        self.assertEqual(first['dispatch']['status'], 'prepared')
        self.assertEqual(first['remote_attempt_dates'], {})
        self.assertNotIn('attempted_fingerprint', first['local'])
        self.tick('09:59:00')
        self.assertEqual(len(self.deps.calls), 1)
        self.deps.fail_before = False
        later = self.tick('10:01:00')
        self.assertEqual(len(self.deps.calls), 2)
        self.assertEqual(self.deps.calls[0], self.deps.calls[1])
        self.assertEqual(later['dispatch']['status'], 'started')
        self.assertEqual(later['remote_attempt_dates']['gmail'], '2026-10-29')

    def test_accepted_start_that_never_reaches_provider_reopens_its_markers(self):
        first=self.tick('09:00:00')
        self.assertEqual(first['dispatch']['status'],'started')
        self.deps.job.update(status='failed',start_failed=True,phase='dispatching')
        recovered=self.tick('09:01:00')
        self.assertEqual(recovered['dispatch']['status'],'prepared')
        self.assertEqual(recovered['remote_attempt_dates'],{})
        self.assertNotIn('attempted_fingerprint',recovered['local'])
        self.tick('09:59:00')
        self.assertEqual(len(self.deps.calls),1)
        self.tick('10:02:00')
        self.assertEqual(len(self.deps.calls),2)
        self.assertEqual(self.deps.calls[0],self.deps.calls[1])

    def test_missing_start_preserves_independent_coverage_without_replay(self):
        first=self.tick('09:00:00')
        self.deps.job.update(status='failed',start_failed=True,phase='dispatching')
        self.deps.job={'status':'complete','job_id':'manual'}
        for ident in ('gmail','wispr'):
            self.state['checkpoints'][ident]={'completed_at':'2026-10-29T09:30:00Z'}
        self.state['checkpoints']['selected']={'sha256':ingest.fingerprint((self.root/'input.md').read_text())}
        self.save_sources()
        recovered=self.tick('09:31:00')
        self.assertEqual(recovered['dispatch']['status'],'superseded')
        self.assertEqual(recovered['remote_attempt_dates'],{'gmail':'2026-10-29','wispr':'2026-10-29'})
        self.tick('14:00:00')
        self.assertEqual(len(self.deps.calls),1)
        self.assertEqual(self.deps.job['job_id'],'manual')

    def test_unstarted_plan_is_cancelled_when_source_selection_changes(self):
        self.deps.fail_before=True
        first=self.tick('09:00:00')
        old_id=first['dispatch']['id']
        self.config['remote']=[row for row in self.config['remote'] if row['id']=='gmail']
        (self.root/'config/sources.json').write_text(json.dumps(self.config))
        cancelled=self.tick('09:01:00')
        self.assertEqual(cancelled['dispatch']['status'],'cancelled')
        self.deps.fail_before=False
        self.tick('10:02:00')
        self.assertEqual(len(self.deps.calls),2)
        self.assertNotEqual(self.deps.calls[-1]['dispatch_id'],old_id)
        self.assertEqual(set(self.deps.calls[-1]['windows']),{'gmail'})
        self.assertNotIn('wispr',self.deps.calls[-1]['source_ids'])

    def test_lost_response_reconciles_completed_job_even_after_manual_replacement(self):
        self.deps.fail_after = True
        first = self.tick('09:00:00')
        accepted = self.deps.receipts[first['dispatch']['id']]
        accepted.update(status='complete')
        self.deps.job = {'status': 'complete', 'job_id': 'newer-manual'}
        recovered = self.tick('14:00:00')
        self.assertEqual(len(self.deps.calls), 1)
        self.assertEqual(recovered['dispatch']['status'], 'finished')
        self.assertEqual(recovered['daily_attempt_date'], '2026-10-29')

    def test_provider_failure_is_not_a_missing_start_retry(self):
        first = self.tick('09:00:00')
        self.deps.job.update(status='partial')
        self.tick('09:01:00')
        self.tick('14:00:00')
        self.assertEqual(len(self.deps.calls), 1)
        self.assertEqual(background.read(self.root)['dispatch']['status'], 'finished')

    def test_plans_cannot_widen_local_scope_or_add_unknown_source(self):
        with self.assertRaisesRegex(ValueError, 'remote sources'):
            ingest.prepare_plan(self.root, reason='local-change', source_ids=['gmail'])
        with self.assertRaisesRegex(ValueError, 'unconfigured'):
            ingest.prepare_plan(self.root, source_ids=['unknown'])
        plan = ingest.prepare_plan(self.root, source_ids=['selected'])
        plan['windows']['gmail'] = {'start': '2026-10-01T00:00:00Z', 'end': plan['created_at']}
        with self.assertRaisesRegex(ValueError, 'windows'):
            ingest.begin_run(self.root, plan=plan)
        self.assertIsNone(ingest.source_status(self.root)['active_run'])

    def test_manual_default_preserves_all_configured_sources(self):
        manifest = ingest.begin_run(self.root)
        self.assertEqual({row['id'] for row in manifest['local']}, {'selected', 'other'})
        self.assertEqual(set(manifest['windows']), {'gmail', 'wispr'})

    def test_completed_dispatch_is_idempotent_and_receipt_survives_new_job(self):
        ident = 'a'*32
        plan = ingest.prepare_plan(self.root, source_ids=['selected'], dispatch_id=ident)
        accepted = {'status': 'complete', 'job_id': 'b'*32, 'dispatch_id': ident, 'plan': plan}
        jobs.write(self.root, accepted)
        jobs.write(self.root, {'status': 'complete', 'job_id': 'c'*32})
        with patch.object(jobs.subprocess, 'Popen') as spawn:
            self.assertEqual(jobs.start(self.root, plan=plan, dispatch_id=ident)['job_id'], accepted['job_id'])
            spawn.assert_not_called()
        self.assertEqual(jobs.dispatch_status(self.root, ident)['status'], 'complete')

    def test_unspawned_job_reservation_recovers_as_missing_start(self):
        ident = 'e'*32
        plan = ingest.prepare_plan(self.root, source_ids=['selected'], dispatch_id=ident)
        jobs.write(self.root, {'status':'running','phase':'dispatching','job_id':'f'*32,
                              'dispatch_id':ident,'plan':plan,'started_at':'2000-01-01T00:00:00Z'})
        recovered=jobs.dispatch_status(self.root,ident)
        self.assertTrue(recovered['start_failed'])
        self.assertEqual(recovered['status'],'failed')
        self.assertIsNone(ingest.source_status(self.root)['active_run'])

    def test_preparing_crash_is_retryable_with_or_without_source_reservation(self):
        for reserved in (False,True):
            with self.subTest(reserved=reserved):
                ident='e'*32;owner='f'*32
                plan=ingest.prepare_plan(self.root,source_ids=['selected'],dispatch_id=ident)
                if reserved:ingest.begin_run(self.root,owner=owner,plan=plan)
                jobs.write(self.root,{'status':'running','phase':'preparing','job_id':owner,
                                     'dispatch_id':ident,'plan':plan,'started_at':'2000-01-01T00:00:00Z'})
                recovered=jobs.dispatch_status(self.root,ident)
                self.assertTrue(recovered['start_failed'])
                self.assertIsNone(ingest.source_status(self.root)['active_run'])

    def test_ambiguous_guard_spawn_is_not_silently_replayed(self):
        ident='e'*32
        plan=ingest.prepare_plan(self.root,source_ids=['selected'],dispatch_id=ident)
        jobs.write(self.root,{'status':'running','phase':'curating','job_id':'f'*32,
                             'dispatch_id':ident,'plan':plan,'started_at':'2000-01-01T00:00:00Z'})
        recovered=jobs.dispatch_status(self.root,ident)
        self.assertFalse(recovered['start_failed'])

    def test_process_creation_failure_remains_retryable_without_a_provider_run(self):
        ident = 'd'*32
        plan = ingest.prepare_plan(self.root, source_ids=['selected'], dispatch_id=ident)
        with patch.object(jobs.shutil, 'which', return_value='/synthetic/codex'), \
             patch.object(jobs.subprocess, 'Popen', side_effect=OSError('Synthetic spawn failure')) as spawn:
            first = jobs.start(self.root, plan=plan, dispatch_id=ident)
            second = jobs.start(self.root, plan=plan, dispatch_id=ident)
        self.assertTrue(first['start_failed'])
        self.assertTrue(second['start_failed'])
        self.assertEqual(spawn.call_count, 2)
        self.assertIsNone(ingest.source_status(self.root)['active_run'])


if __name__ == '__main__': unittest.main()

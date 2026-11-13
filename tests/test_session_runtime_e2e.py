import os
"""Runtime identity and explicit usefulness through public CLI, synthetic only."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

import test_agent_workflow_e2e as harness
BASE = harness.BASE


class SessionRuntimeE2E(unittest.TestCase):
    # Share the subprocess fixture/evidence recorder, not its test cases.
    setUp = harness.AgentWorkflowE2E.setUp
    finish = harness.AgentWorkflowE2E.finish
    save_report = classmethod(harness.AgentWorkflowE2E.save_report.__func__)
    run_cli = harness.AgentWorkflowE2E.run_cli
    cli = harness.AgentWorkflowE2E.cli
    apply = harness.AgentWorkflowE2E.apply
    draft = harness.AgentWorkflowE2E.draft
    close = harness.AgentWorkflowE2E.close
    read = harness.AgentWorkflowE2E.read

    @classmethod
    def setUpClass(cls):
        harness.AgentWorkflowE2E.setUpClass.__func__(cls)
        cls.report['scope'] = 'Runtime binding, lifecycle, feedback and stable CLI errors; synthetic local data.'
        cls.report['source_sha256'][str(Path(__file__).relative_to(BASE.parent))] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        cls.report['source_sha256'].update({str(p.relative_to(BASE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in BASE.glob('schemas/*.json')})
        cls.save_report()

    def bound(self, runtime='thread-a', source='startup', agent='codex', target=None):
        return self.cli('session-open', target or str(self.repo), '--agent', agent,
                        '--runtime-id', runtime, '--runtime-source', source)

    def hook(self, runtime='thread-a', source='startup', agent='codex', event='SessionStart'):
        args = [] if agent == 'codex' else ['brief', '--hook', '--agent', agent]
        script = 'integrations/codex_session_start.py' if agent == 'codex' else 'hive.py'
        payload = {'cwd': str(self.repo), 'session_id': runtime, 'source': source,
                   'hook_event_name': event, 'reason': 'other', 'transcript_path': '/private/DO_NOT_READ',
                   'prompt': 'DO_NOT_STORE_TRANSCRIPT'}
        return self.run_cli(script, args, stdin=json.dumps(payload))

    def test_resume_compact_and_repeated_start_reuse_until_explicit_close(self):
        first = self.bound()
        for source in ['startup', 'resume', 'compact']:
            again = self.bound(source=source)
            self.assertEqual(again['session_id'], first['session_id'])
            self.assertEqual(again['runtime']['last_source'], source)
        saved = self.cli('session-close', first['session_id'], '--no-change')
        next_one = self.bound(source='resume')
        self.assertNotEqual(next_one['session_id'], first['session_id'])
        self.assertEqual(next_one['previous_session_id'], first['session_id'])
        self.assertEqual(self.cli('session-status', first['session_id']), saved)
        self.assertEqual(len(list((self.root / '.state/sessions').glob('*.json'))), 2)

    def test_runtime_namespace_project_and_fork_are_separate(self):
        first = self.bound()
        other_agent = self.bound(agent='claude')
        forked = self.bound(runtime='thread-fork', source='fork')
        from test_sessions import project
        self.apply({'changes': [{'expected_revision': 'new', 'note': project('other')}]})
        other_project = self.bound(target='projects/other')
        self.assertEqual(len({r['session_id'] for r in [first, other_agent, forked, other_project]}), 4)
        self.assertEqual(self.bound()['session_id'], first['session_id'])
        self.assertNotIn('previous_session_id', forked)

    def test_concurrent_start_creates_one_receipt(self):
        argv = [sys.executable, str(BASE / 'hive.py'), '--root', str(self.root), 'session-open',
                str(self.repo), '--runtime-id', 'thread-race', '--runtime-source', 'startup']
        def run(_):
            return subprocess.run(argv, capture_output=True, text=True, timeout=30)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            processes = list(pool.map(run, range(4)))
        for p in processes:
            self.case['commands'].append({'argv': argv, 'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len({json.loads(p.stdout)['session_id'] for p in processes}), 1)

    def test_hooks_keep_runtime_identity_and_end_never_invents_a_handoff(self):
        for agent in ['codex', 'claude']:
            first = self.hook(agent=agent)
            sid = re.search(r'HiveMind-Sitzung: ([a-f0-9]{32})', first['hookSpecificOutput']['additionalContext']).group(1)
            # Hosts may turn long hook output into a short preview plus a file.
            # Keep continuity instructions usable inside that preview.
            text = first['hookSpecificOutput']['additionalContext']
            self.assertTrue(text.startswith('HiveMind-Sitzung: ' + sid))
            self.assertIn('session-context', text[:1000])
            self.assertIn('session-close --no-change', text[:1000])
            resumed = self.hook(agent=agent, source='compact')
            self.assertIn('HiveMind-Sitzung: ' + sid, resumed['hookSpecificOutput']['additionalContext'])
            before = self.run_cli('hive.py', ['check'])['events']
            self.hook(agent=agent, event='SessionEnd')
            ended = self.cli('session-status', sid)
            self.assertEqual(ended['status'], 'context_offered')
            self.assertIn('ended_at', ended['runtime'])
            self.assertNotIn('notes', ended)
            self.assertEqual(self.run_cli('hive.py', ['check'])['events'], before)
            self.hook(agent=agent, source='resume')
            self.assertNotIn('ended_at', self.cli('session-status', sid)['runtime'])
            text = (self.root / '.state/sessions' / (sid + '.json')).read_text()
            self.assertNotIn('DO_NOT_', text)

    def test_feedback_is_explicit_revision_bound_idempotent_and_private(self):
        first = self.bound()
        item = first['offered'][0]
        ref = item['id'] + '@' + item['revision']
        self.cli('session-close', first['session_id'], '--no-change')
        args = ('session-feedback', first['session_id'], '--outcome', 'helpful', '--used', ref,
                '--cited', ref, '--actor', 'user', '--missing', 'none')
        feedback = self.cli(*args)
        self.assertEqual(feedback, self.cli(*args))
        self.assertEqual(feedback['feedback']['used'], [item])
        self.assertEqual(feedback['feedback']['cited'], [item])
        before = (self.root / '.state/sessions' / (first['session_id'] + '.json')).read_bytes()
        for bad in [('session-feedback', first['session_id'], '--outcome', 'helpful', '--used', 'demo@' + '0'*64),
                    ('session-feedback', first['session_id'], '--outcome', 'not_used', '--used', ref),
                    ('session-feedback', first['session_id'], '--outcome', 'helpful', '--cited', ref)]:
            result = self.cli(*bad, ok=False)
            self.assertEqual(result['error_code'], 'INVALID_ARGUMENT')
        self.assertEqual(before, (self.root / '.state/sessions' / (first['session_id'] + '.json')).read_bytes())
        metrics = self.cli('session-metrics', '--cluster', 'projects/demo')
        self.assertEqual(metrics['sessions'], 1)
        self.assertEqual(metrics['feedback']['user']['helpful'], 1)
        self.assertEqual(metrics['feedback']['agent']['helpful'], 0)
        self.assertEqual(metrics['used_revisions'], 1)
        self.assertNotIn('read', metrics)

    def test_refresh_preserves_previously_offered_revisions(self):
        opened = self.bound()
        previous = opened['offered']
        record = self.read()
        record['note']['body'] += '\n\nNew independently observed revision.'
        self.apply({'changes': [{'expected_revision': record['revision'], 'note': record['note']}]})
        refreshed = self.cli('session-context', opened['session_id'], '--task', 'API retries')
        accumulated = self.cli('session-status', opened['session_id'])
        for item in previous:
            self.assertIn(item, accumulated['offered'])
        ref = previous[0]['id'] + '@' + previous[0]['revision']
        self.cli('session-feedback', opened['session_id'], '--outcome', 'helpful', '--used', ref)

    def test_versioned_errors_and_invalid_runtime_do_not_create_sessions(self):
        cases = [(['session-status', 'a'*32], 'SESSION_NOT_FOUND'),
                 (['session-open', str(self.repo), '--runtime-id', '../bad'], 'INVALID_ARGUMENT'),
                 (['session-open', str(self.repo), '--runtime-source', 'resume'], 'INVALID_ARGUMENT'),
                 (['session-open', str(self.repo), '--budget', 'bad'], 'INVALID_ARGUMENT')]
        for command, code in cases:
            with self.subTest(command=command):
                result = self.cli(*command, ok=False)
                self.assertEqual(result['contract_version'], 1)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error_code'], code)
        self.assertEqual(list((self.root / '.state/sessions').glob('*.json')), [])

    def test_handoff_conflict_recovery_and_closed_errors_have_distinct_codes(self):
        opened = self.bound()
        invalid = self.draft(opened)
        invalid.update(project_state='Synthetic state', evidence='not-an-evidence-value')
        self.assertEqual(self.close(opened, invalid, ok=False)['error_code'], 'INVALID_HANDOFF')
        invalid['evidence'] = 'reported'
        invalid['expected_revision'] = '0' * 64
        self.assertEqual(self.close(opened, invalid, ok=False)['error_code'], 'REVISION_CONFLICT')
        pending = self.root / '.pending.json'
        pending.write_text('{"id":"synthetic-pending"}')
        self.assertEqual(self.cli('session-context', opened['session_id'], ok=False)['error_code'], 'RECOVERY_REQUIRED')
        pending.unlink()
        self.cli('session-close', opened['session_id'], '--no-change')
        self.assertEqual(self.cli('session-context', opened['session_id'], ok=False)['error_code'], 'SESSION_CLOSED')

    def test_doctor_cli_is_readonly_even_without_python_caches(self):
        code = self.root / 'fresh-code'
        code.mkdir()
        for file in BASE.glob('*.py'):
            shutil.copy2(file, code / file.name)
        shutil.copytree(BASE / 'schemas', code / 'schemas')
        shutil.copytree(BASE / 'integrations', code / 'integrations', ignore=shutil.ignore_patterns('__pycache__', 'backups'))
        home = self.root / 'empty-home'
        home.mkdir()
        def snapshot():
            return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob('*') if p.is_file()}
        before = snapshot()
        for script in ['hive.py', 'launcher.py']:
            args = ['doctor', '--home', str(home)] + (['--json'] if script == 'launcher.py' else [])
            report = self.run_cli(str(code / script), args)
            self.assertEqual(report['scope'], 'read_only_disk_snapshot')
            self.assertEqual(report['contract_version'], 1)
            self.assertEqual(before, snapshot())

    def test_schema_export_is_consumable_offline_and_end_is_idempotent(self):
        import agent_contract
        schema = self.cli('contract', '--schema', 'receipt')['schema']
        opened = self.bound()
        ended = self.cli('session-end', opened['session_id'])
        agent_contract.validate_schema(schema, ended)
        self.assertEqual(ended, self.cli('session-end', opened['session_id']))
        closed = self.cli('session-close', opened['session_id'], '--no-change')
        self.assertEqual(closed, self.cli('session-end', opened['session_id']))

    def test_malformed_receipts_return_stable_errors_without_leaking_contents(self):
        opened = self.bound()
        path = self.root / '.state/sessions' / (opened['session_id'] + '.json')
        original = path.read_text()
        record = json.loads(original)
        record['status'] = 'read'
        for broken in [json.dumps(record), '[]', '{PRIVATE_BROKEN_RECEIPT']:
            path.write_text(broken)
            result = self.cli('session-status', opened['session_id'], ok=False)
            self.assertEqual(result['error_code'], 'INVALID_STATE')
            self.assertNotIn('PRIVATE_BROKEN', json.dumps(result))
            self.assertEqual(path.read_text(), broken)
        path.write_text(original)

    def test_doctor_uses_the_same_receipt_contract(self):
        opened = self.bound()
        path = self.root / '.state/sessions' / (opened['session_id'] + '.json')
        record = json.loads(path.read_text())
        record['feedback'] = [{'outcome': 'helpful', 'transcript': 'PRIVATE_INVALID_FEEDBACK'}]
        path.write_text(json.dumps(record))
        result = self.cli('doctor', '--home', str(self.root / 'fake-home'))
        check = next(item for item in result['checks'] if item['id'] == 'sessions')
        self.assertEqual(check['malformed'], 1)
        self.assertNotIn('PRIVATE_INVALID', json.dumps(result))


if __name__ == '__main__':
    unittest.main(verbosity=2)

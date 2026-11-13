#!/usr/bin/env python3
"""Subprocess E2E, synthetic data only; real jobs/ingest/hive, fake Codex provider.

Failure cases fixed before implementation: duplicate starts, successful process
without receipts, malformed receipt, provider failure, interrupted worker, partial
coverage, and restarting after failure. A process exit is never sync evidence.
The fake executable represents the provider boundary, not a live model test.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parents[1] / 'hivemind'
sys.path.insert(0, str(BASE))
from integrations import ingest


class JobsE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.children = []
        (self.root/'config').mkdir()
        (self.root/'config/sources.json').write_text(json.dumps({
            'version': 1, 'files': [], 'remote': [
                {'id': 'gmail', 'lookback_days': 7}],
            'calendar': {'enabled': False}}))
        binary = self.root/'bin'
        binary.mkdir()
        fake = binary/'codex'
        fake.write_text("""#!/usr/bin/env python3
import json,os,pathlib,re,sys,time
root=pathlib.Path.cwd()
mode=(root/'mode').read_text() if (root/'mode').exists() else 'complete'
if mode=='hold':
 with (root/'agent-starts').open('a') as proof:proof.write('started\\n')
 (root/'agent-live').write_text('Synthetic agent holds the writer lease.')
 while not (root/'release-agent').exists():time.sleep(.02)
 (root/'agent-live').unlink()
time.sleep(.6)
if mode=='process_failure':sys.exit(7)
if mode=='missing':sys.exit(0)
active=json.loads((root/'.state/sources.json').read_text())['active_run']
folder=root/'.state/runs'/active
manifest=json.loads((folder/'manifest.json').read_text())
if mode=='malformed':
 (folder/'receipts.json').write_text('{}')
 sys.exit(0)
rows=[]
for source in manifest['local']:
 rows.append({'id':source['id'],'kind':'local','status':'complete','sha256':source['sha256'],'disposition':'irrelevant','note_ids':[],'reason':'Synthetic fixture without durable knowledge.'})
for ident,window in manifest['windows'].items():
 row={'id':ident,'kind':'remote','status':'complete','window':window,'coverage':{'complete':True,'pages_complete':True,'permission':'granted','continuation':None,'page_count':1},'items':[],'counts':{'examined':0},'reason':'Synthetic empty provider response.'}
 if mode=='partial':row={'id':ident,'kind':'remote','status':'blocked','reason':'Synthetic provider unavailable.'}
 rows.append(row)
(folder/'receipts.json').write_text(json.dumps({'schema_version':1,'run_id':active,'sources':rows}))
""")
        fake.chmod(0o700)
        self.env = {**os.environ, 'PATH': str(binary)+os.pathsep+os.environ.get('PATH', '')}

    def tearDown(self):
        (self.root/'release-agent').touch()
        (self.root/'release-abort').touch()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        self.temp.cleanup()

    def wait_for(self, path, timeout=10):
        limit = time.monotonic()+timeout
        while time.monotonic() < limit:
            if path.exists():return
            time.sleep(.02)
        self.fail('Synthetic synchronization marker did not appear: '+path.name)

    def worker_process(self, *, timeout=None, delayed_abort=False):
        job_id = uuid.uuid4().hex
        (self.root/'.state').mkdir(exist_ok=True)
        (self.root/'.state/job.json').write_text(json.dumps({
            'status': 'running', 'job_id': job_id,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'message': 'Synthetic worker fixture'}))
        if delayed_abort:
            script = '''import pathlib,sys,time
sys.path.insert(0,sys.argv[1])
import jobs
root=pathlib.Path(sys.argv[2]); original=jobs.ingest.abort_run
def delayed(*args,**kwargs):
 (root/'abort-entered').touch()
 while not (root/'release-abort').exists():time.sleep(.02)
 return original(*args,**kwargs)
jobs.ingest.abort_run=delayed
sys.exit(jobs.worker(root,sys.argv[3],sys.argv[4]))'''
            command = [sys.executable, '-c', script, str(BASE), str(self.root),
                       job_id, str(self.root/'bin/codex')]
        else:
            command = [sys.executable, str(BASE/'jobs.py'), '--root', str(self.root),
                       '--worker', job_id, '--codex', str(self.root/'bin/codex')]
            if timeout is not None:command += ['--agent-timeout', str(timeout)]
        child = subprocess.Popen(command, env=self.env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.children.append(child)
        # Publish only the PID of the actual Popen this test created.
        state = json.loads((self.root/'.state/job.json').read_text())
        state['pid'] = child.pid
        (self.root/'.state/job.json').write_text(json.dumps(state))
        return child, job_id

    def cli(self, command):
        p = subprocess.run([sys.executable, str(BASE/'jobs.py'), '--root',
                            str(self.root), command], env=self.env,
                           text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def done(self):
        limit = time.monotonic()+20
        while time.monotonic() < limit:
            result = self.cli('status')
            if result['status'] != 'running':
                return result
            time.sleep(.08)
        self.fail('Synthetic job did not terminate')

    def test_complete_and_single_active_process(self):
        first = self.cli('start')
        second = self.cli('start')
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(self.done()['status'], 'complete')
        self.assertTrue((self.root/'.state/sources.json').exists())
        state = json.loads((self.root/'.state/sources.json').read_text())
        self.assertIn('gmail', state['checkpoints'])
        self.assertIsNone(state['active_run'])

    def test_scoped_local_plan_uses_worker_without_remote_calls(self):
        (self.root/'INBOX').mkdir()
        (self.root/'INBOX/thought.md').write_text('Synthetic selected thought.')
        # This fake fails every remote receipt; a scoped local run must still complete.
        (self.root/'mode').write_text('partial')
        script = """import json,pathlib,sys
sys.path.insert(0,sys.argv[1]);import jobs
from integrations import ingest
root=pathlib.Path(sys.argv[2]);ident='a'*32
plan=ingest.prepare_plan(root,reason='local-change',source_ids=['inbox:thought.md'],dispatch_id=ident)
print(json.dumps(jobs.start(root,plan=plan,dispatch_id=ident)))"""
        proc=subprocess.run([sys.executable,'-c',script,str(BASE),str(self.root)],
                            env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(proc.returncode,0,proc.stderr)
        started=json.loads(proc.stdout)
        completed=self.done()
        self.assertEqual(completed['status'],'complete')
        self.assertEqual(completed['dispatch_id'],'a'*32)
        manifest=json.loads((self.root/'.state/runs'/completed['run_id']/'manifest.json').read_text())
        self.assertEqual(manifest['windows'],{})
        self.assertEqual([row['id'] for row in manifest['local']],['inbox:thought.md'])
        self.assertNotIn('gmail',json.loads((self.root/'.state/sources.json').read_text())['checkpoints'])
        replay=subprocess.run([sys.executable,'-c',script,str(BASE),str(self.root)],
                              env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(replay.returncode,0,replay.stderr)
        self.assertEqual(json.loads(replay.stdout)['job_id'],started['job_id'])

    def test_exit_zero_without_receipts_is_failure_and_retry_works(self):
        (self.root/'mode').write_text('missing')
        self.cli('start')
        self.assertEqual(self.done()['status'], 'failed')
        state = json.loads((self.root/'.state/sources.json').read_text())
        self.assertEqual(state['checkpoints'], {})
        self.assertIsNone(state['active_run'])
        (self.root/'mode').write_text('complete')
        self.cli('start')
        self.assertEqual(self.done()['status'], 'complete')

    def test_provider_exit_and_invalid_receipts_never_ack(self):
        for mode in ('process_failure', 'malformed'):
            with self.subTest(mode=mode):
                (self.root/'mode').write_text(mode)
                self.cli('start')
                self.assertEqual(self.done()['status'], 'failed')
                state = json.loads((self.root/'.state/sources.json').read_text())
                self.assertEqual(state['checkpoints'], {})
                self.assertIsNone(state['active_run'])

    def test_partial_sources_are_independent(self):
        (self.root/'INBOX').mkdir()
        (self.root/'INBOX/thought.md').write_text('Synthetic temporary example.')
        (self.root/'mode').write_text('partial')
        self.cli('start')
        self.assertEqual(self.done()['status'], 'partial')
        state = json.loads((self.root/'.state/sources.json').read_text())
        self.assertIn('inbox:thought.md', state['checkpoints'])
        self.assertNotIn('gmail', state['checkpoints'])

    def test_stranded_worker_recovers_explicitly(self):
        (self.root/'.state').mkdir()
        (self.root/'.state/job.json').write_text(json.dumps({
            'status': 'running', 'job_id': 'synthetic-stranded',
            'started_at': '2000-01-01T00:00:00Z', 'pid': 99999999,
            'message': 'Synthetic interrupted worker'}))
        state = self.cli('status')
        self.assertEqual(state['status'], 'failed')
        self.cli('start')
        self.assertEqual(self.done()['status'], 'complete')

    def test_reservation_gap_recovers_by_owner_and_preserves_foreign_run(self):
        (self.root/'.state').mkdir()
        job_id = uuid.uuid4().hex
        manifest = ingest.begin_run(self.root, owner=job_id)
        value = {'status':'running', 'job_id':job_id, 'pid':99999999,
                 'started_at':'2000-01-01T00:00:00Z'}
        (self.root/'.state/job.json').write_text(json.dumps(value))
        self.assertEqual(self.cli('status')['status'], 'failed')
        self.assertIsNone(ingest.source_status(self.root)['active_run'])
        foreign = ingest.begin_run(self.root, owner=uuid.uuid4().hex)
        (self.root/'.state/job.json').write_text(json.dumps({**value, 'run_id':foreign['id']}))
        self.assertEqual(self.cli('status')['status'], 'failed')
        self.assertEqual(ingest.source_status(self.root)['active_run'], foreign['id'])
        ingest.abort_run(self.root, foreign['id'], 'Synthetic fixture cleanup.')

    def test_sigkill_worker_preserves_agent_lease_until_agent_exits(self):
        (self.root/'mode').write_text('hold')
        child, job_id = self.worker_process()
        self.wait_for(self.root/'agent-live')
        os.kill(child.pid, signal.SIGKILL)  # Only our own known Popen process.
        child.wait(timeout=5)
        self.assertEqual(self.cli('status')['status'], 'running')
        self.assertEqual(self.cli('start')['job_id'], job_id)
        self.assertEqual((self.root/'agent-starts').read_text().splitlines(), ['started'])
        self.assertTrue(self.root.joinpath('agent-live').exists())
        (self.root/'release-agent').touch()
        self.assertEqual(self.done()['status'], 'failed')
        self.assertIsNone(ingest.source_status(self.root)['active_run'])
        (self.root/'mode').write_text('complete')
        retry = self.cli('start')
        self.assertNotEqual(retry['job_id'], job_id)
        self.assertEqual(self.done()['status'], 'complete')
        self.assertIsNone(ingest.source_status(self.root)['active_run'])

    def test_failed_status_is_published_after_abort_cleanup(self):
        (self.root/'mode').write_text('process_failure')
        child, job_id = self.worker_process(delayed_abort=True)
        self.wait_for(self.root/'abort-entered')
        state = self.cli('status')
        self.assertEqual(state['status'], 'running')
        self.assertEqual(self.cli('start')['job_id'], job_id)
        self.assertIsNotNone(ingest.source_status(self.root)['active_run'])
        (self.root/'release-abort').touch()
        child.wait(timeout=10)
        self.assertEqual(self.done()['status'], 'failed')
        self.assertIsNone(ingest.source_status(self.root)['active_run'])

    def test_guard_deadline_ends_agent_and_releases_reservation(self):
        (self.root/'mode').write_text('hold')
        child, job_id = self.worker_process(timeout=.8)
        self.wait_for(self.root/'agent-live')
        os.kill(child.pid, signal.SIGKILL)  # Deadline belongs to the independent guard.
        child.wait(timeout=10)
        self.assertEqual(self.cli('start')['job_id'], job_id)
        self.assertEqual(self.done()['status'], 'failed')
        self.assertIsNone(ingest.source_status(self.root)['active_run'])
        # The guard killed the exact process it launched; no delayed receipt appears.
        (self.root/'release-agent').touch()
        time.sleep(.8)
        state = json.loads((self.root/'.state/job.json').read_text())
        self.assertFalse((self.root/'.state/runs'/state['run_id']/'receipts.json').exists())


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(BASE.parent.parent/'hivemind-public-evidence')))/'jobs-e2e'/stamp
    out.mkdir(parents=True)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(JobsE2E))
    log = stream.getvalue()
    (out/'test.log').write_text(log)
    names = ['jobs.py', 'integrations/ingest.py', 'integrations/calendar.py',
             'hive.py', '../scripts/verify_jobs.py']
    evidence = {
        'command': 'python3 scripts/verify_jobs.py', 'at': stamp,
        'environment': {'python': sys.version, 'platform': platform.platform()},
        'fixtures': 'Temporary empty canonical store; synthetic capture; fake Codex executable in child PATH; calendar disabled. Owner gap/foreign reservation fixtures; actual test-owned worker Popen SIGKILL while fake agent waits for release; delayed abort barrier; internal short agent deadline. No live providers.',
        'source_sha256': {name: hashlib.sha256((BASE/name).read_bytes()).hexdigest()
                          if (BASE/name).exists() else 'MISSING' for name in names},
        'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'success': result.wasSuccessful()}
    (out/'evidence.json').write_text(json.dumps(evidence, indent=2))
    print(log)
    print(out)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())

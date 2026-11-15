#!/usr/bin/env python3
"""Subprocess E2E: actual policy/state/locks/plist; synthetic job/calendar boundaries.
Failure cases declared before implementation: missing/malformed config must not
start Codex; unchanged checkpoints never dispatch; local changes are debounced
and throttled; missed daily run catches up once; failed/partial results back off
without retry loops; active work survives daemon stop; launchd install is idempotent.
No provider, Calendar helper, real launchctl, global configuration or user daemon.
"""
import hashlib, io, json, os, platform, signal, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timezone
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]/'hivemind'
HELPER=r'''
import json,pathlib,sys
sys.path.insert(0,sys.argv[1]);import background
from integrations import ingest
root=pathlib.Path(sys.argv[2]); action=json.loads(sys.argv[3])
class D:
 def scan(self,r):return ingest.scan(r)
 def job_status(self,r):
  p=r/'.state/fake-job.json';return json.loads(p.read_text()) if p.exists() else {'status':'idle'}
 def start(self,r,plan=None,dispatch_id=None):
  p=r/'.state/starts.json'; rows=json.loads(p.read_text()) if p.exists() else [];rows.append(action['at']);ingest.atomic(p,rows)
  job={'status':'running','job_id':str(len(rows)),'dispatch_id':dispatch_id,'plan':plan,'started_at':action['at']};ingest.atomic(r/'.state/fake-job.json',job);return job
 def check(self,r):
  p=r/'.state/checks.json';ingest.atomic(p,{'count':(json.loads(p.read_text())['count'] if p.exists() else 0)+1});return {'valid':True}
 def calendar(self,r):
  p=r/'.state/calendar-calls.json';ingest.atomic(p,{'count':(json.loads(p.read_text())['count'] if p.exists() else 0)+1});return {'status':'ok','exported_at':action['at'],'events':[]}
 def awake(self,r):return not action.get('asleep')
if action.get('job'):
 previous=json.loads((root/'.state/fake-job.json').read_text()) if (root/'.state/fake-job.json').exists() else {}
 ingest.atomic(root/'.state/fake-job.json',{**previous,**action['job']})
if action.get('run'):
 background.run(root,deps=D());sys.exit(0)
print(json.dumps(background.tick(root,at=background.parse_time(action['at']),deps=D())))
'''
class BackgroundE2E(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'config').mkdir();(self.root/'.state').mkdir(mode=0o700)
  self.source=self.root/'input.txt';self.source.write_text('Synthetic selected input.\n')
  self.config={'version':1,'enabled':True,'scan_seconds':60,'debounce_seconds':120,'min_sync_seconds':1800,'failure_backoff_seconds':3600,'daily_hour':9,'calendar_seconds':1800,'integrity_seconds':3600}
  self.write_config();(self.root/'config/sources.json').write_text(json.dumps({'version':1,'files':[{'id':'test','path':str(self.source)}],'remote':[{'id':'gmail','lookback_days':7}]}))
  self.checkpoint();self.children=[]
 def tearDown(self):
  for p in self.children:
   if p.poll() is None:p.terminate()
   try:p.wait(timeout=3)
   except subprocess.TimeoutExpired:p.kill();p.wait()
  self.tmp.cleanup()
 def write_config(self):(self.root/'config/background.json').write_text(json.dumps(self.config))
 def checkpoint(self):
  sha=hashlib.sha256(self.source.read_bytes()).hexdigest();(self.root/'.state/sources.json').write_text(json.dumps({'schema_version':1,'checkpoints':{'test':{'sha256':sha}},'attempts':{},'active_run':None,'last_run':None}))
 def tick(self,at='2026-10-20T08:00:00+02:00',job=None,asleep=False):
  data={'at':at,'asleep':asleep}
  if job:data['job']=job
  p=subprocess.run([sys.executable,'-c',HELPER,str(BASE),str(self.root),json.dumps(data)],capture_output=True,text=True,timeout=10)
  self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
 def starts(self):
  p=self.root/'.state/starts.json';return json.loads(p.read_text()) if p.exists() else []
 def test_missing_and_bad_config_fail_closed_then_recover(self):
  (self.root/'config/background.json').unlink();self.assertEqual(self.tick()['status'],'degraded');self.assertEqual(self.starts(),[])
  (self.root/'config/background.json').write_text('{}');self.assertEqual(self.tick()['status'],'degraded');self.write_config();self.assertEqual(self.tick()['status'],'running')
 def test_unchanged_sources_and_calendar_are_bounded(self):
  self.tick();self.tick('2026-10-20T08:01:00+02:00');self.assertEqual(self.starts(),[])
  calls=json.loads((self.root/'.state/calendar-calls.json').read_text());self.assertEqual(calls['count'],1)
 def test_changed_local_debounce_and_throttle(self):
  self.source.write_text('Changed synthetic input.');self.tick();self.tick('2026-10-20T08:01:00+02:00');self.assertEqual(self.starts(),[])
  self.tick('2026-10-20T08:02:00+02:00');self.assertEqual(len(self.starts()),1)
  self.tick('2026-10-20T08:03:00+02:00',{'status':'complete','job_id':'1'});self.source.write_text('Another change.');self.tick('2026-10-20T08:04:00+02:00');self.tick('2026-10-20T08:06:00+02:00');self.assertEqual(len(self.starts()),1)
  self.tick('2026-10-20T08:32:00+02:00');self.assertEqual(len(self.starts()),2)
 def test_daily_catchup_once_per_local_day_after_restart(self):
  self.tick('2026-10-20T12:00:00+02:00');self.assertEqual(len(self.starts()),1)
  self.tick('2026-10-20T12:01:00+02:00',{'status':'complete','job_id':'1'});self.tick('2026-10-20T23:00:00+02:00');self.assertEqual(len(self.starts()),1)
  self.tick('2026-10-21T08:59:00+02:00');self.assertEqual(len(self.starts()),1);self.tick('2026-10-21T09:00:00+02:00');self.assertEqual(len(self.starts()),2)
 def test_dark_wake_defers_daily_run_without_spending_the_day(self):
  # 24./25.10.2026: daily runs began in 15-second maintenance wakes and died when the Mac slept again.
  state=self.tick('2026-10-20T09:28:00+02:00',asleep=True);self.assertEqual(self.starts(),[]);self.assertIn('maintenance',state['message'])
  self.tick('2026-10-20T10:40:00+02:00');self.assertEqual(len(self.starts()),1)
 def test_failure_no_blind_retry_and_new_input_backoff(self):
  self.source.write_text('Changed.');self.tick();self.tick('2026-10-20T08:02:00+02:00');self.tick('2026-10-20T08:03:00+02:00',{'status':'partial','job_id':'1'});self.tick('2026-10-20T08:59:00+02:00');self.assertEqual(len(self.starts()),1)
  # Daily catches up only after one-hour backoff; same failing local fingerprint
  # is never repeatedly retried merely because its checkpoint remains pending.
  self.tick('2026-10-20T09:00:00+02:00');self.assertEqual(len(self.starts()),1)
  self.tick('2026-10-20T09:04:00+02:00');self.assertEqual(len(self.starts()),2)
  self.tick('2026-10-20T09:05:00+02:00',{'status':'failed','job_id':'2'});self.tick('2026-10-20T12:00:00+02:00');self.assertEqual(len(self.starts()),2)
  self.source.write_text('New changed input.');self.tick('2026-10-20T12:01:00+02:00');self.tick('2026-10-20T12:03:00+02:00');self.assertEqual(len(self.starts()),3)
 def test_active_manual_job_prevents_start(self):
  self.tick('2026-10-20T12:00:00+02:00',{'status':'running','job_id':'manual'});self.assertEqual(self.starts(),[])
  self.tick('2026-10-20T12:01:00+02:00',{'status':'complete','job_id':'manual'});self.assertEqual(len(self.starts()),1)
 def test_state_permissions_and_no_raw_content(self):
  self.tick();p=self.root/'.state/background.json';self.assertEqual(p.stat().st_mode&0o777,0o600);self.assertEqual((self.root/'.state').stat().st_mode&0o777,0o700);self.assertNotIn('Synthetic selected input',p.read_text())
 def test_single_daemon_and_stop_does_not_stop_job(self):
  self.config['enabled']=False;self.write_config();cmd=[sys.executable,'-c',HELPER,str(BASE),str(self.root),json.dumps({'at':'2026-10-20T08:00:00+02:00','run':True})]
  p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True);self.children.append(p)
  limit=time.time()+5
  while time.time()<limit:
   state=self.root/'.state/background.json'
   if state.exists():break
   time.sleep(.03)
  self.assertTrue(state.exists());second=subprocess.run(cmd,capture_output=True,text=True,timeout=3);self.assertEqual(second.returncode,0)
  # The daemon does not own or signal an independent job when launchd stops it.
  job=self.root/'.state/fake-job.json';job.write_text(json.dumps({'status':'running','job_id':'independent'}));p.terminate();p.wait(timeout=3);self.assertEqual(json.loads(job.read_text())['status'],'running')
 def test_missing_or_empty_source_configuration_never_dispatches(self):
  p=self.root/'config/sources.json';p.unlink();self.assertEqual(self.tick('2026-10-20T12:00:00+02:00')['status'],'degraded');self.assertEqual(self.starts(),[])
  p.write_text(json.dumps({'version':1,'files':[],'remote':[]}));self.tick('2026-10-20T12:01:00+02:00');self.assertEqual(self.starts(),[])
 def test_existing_remote_coverage_today_does_not_repeat_daily(self):
  p=self.root/'.state/sources.json';s=json.loads(p.read_text());s['checkpoints']['gmail']={'completed_at':'2026-10-20T08:30:00Z'};p.write_text(json.dumps(s))
  self.tick('2026-10-20T12:00:00+02:00');self.assertEqual(self.starts(),[])
 def test_current_calendar_snapshot_does_not_refresh_on_start(self):
  (self.root/'.state/agenda.json').write_text(json.dumps({'status':'ok','exported_at':'2026-10-20T05:55:00Z','events':[]}))
  self.tick();self.assertFalse((self.root/'.state/calendar-calls.json').exists())
 def test_empty_capture_does_not_start_model(self):
  folder=self.root/'INBOX';folder.mkdir();(folder/'empty.md').write_text('# Gedanken\n\n')
  self.tick();self.tick('2026-10-20T08:03:00+02:00');self.assertEqual(self.starts(),[])
 def test_invalid_integrity_pauses_dispatch(self):
  script=HELPER.replace("return {'valid':True}","return {'valid':False}")
  p=subprocess.run([sys.executable,'-c',script,str(BASE),str(self.root),json.dumps({'at':'2026-10-20T12:00:00+02:00'})],capture_output=True,text=True,timeout=10)
  self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)['status'],'degraded');self.assertEqual(self.starts(),[])
 def test_launchd_install_and_uninstall_with_fake_runner(self):
  script=r'''import json,pathlib,sys,subprocess,plistlib
sys.path.insert(0,sys.argv[1]);import background
root=pathlib.Path(sys.argv[2]);calls=[];loaded=[False]
def run(args,**kw):
 calls.append(args)
 if args[1]=='print':return subprocess.CompletedProcess(args,0 if loaded[0] else 113,'','')
 if args[1]=='bootstrap':loaded[0]=True
 if args[1]=='bootout':loaded[0]=False
 return subprocess.CompletedProcess(args,0,'','')
p=root/'fakeLaunchAgents'
a=background.install(root,launchagents=p,runner=run,uid=501)
b=background.install(root,launchagents=p,runner=run,uid=501)
pl=plistlib.loads(next(p.glob('*.plist')).read_bytes());removed=background.uninstall(root,launchagents=p,runner=run,uid=501)
print(json.dumps({'a':a,'b':b,'calls':calls,'plist':pl,'removed':removed,'left':len(list(p.glob('*.plist')))}))'''
  p=subprocess.run([sys.executable,'-c',script,str(BASE),str(self.root)],capture_output=True,text=True,timeout=10);self.assertEqual(p.returncode,0,p.stderr);r=json.loads(p.stdout)
  self.assertEqual(r['left'],0);self.assertEqual(sum(call[1]=='bootstrap' for call in r['calls']),1);self.assertEqual(sum(call[1]=='bootout' for call in r['calls']),1);self.assertEqual(r['plist']['ProgramArguments'][0],sys.executable);self.assertEqual(r['plist']['WorkingDirectory'],str(self.root.resolve()));self.assertTrue(r['plist']['KeepAlive']);self.assertNotIn('http',str(r['plist']));self.assertEqual(r['plist']['EnvironmentVariables']['HOME'],str(Path.home()))
if __name__=='__main__':
 stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');out=Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(BASE.parent.parent/'hivemind-public-evidence')))/'background-e2e'/stamp;out.mkdir(parents=True)
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(BackgroundE2E);stream=io.StringIO();result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite);log=stream.getvalue();(out/'test.log').write_text(log)
 files=['background.py','config/background.json','../scripts/verify_background.py'];e={'at':stamp,'passed':result.wasSuccessful(),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'boundary':'Real subprocess scheduler, locks, config, state and launchd plist; fake jobs, Calendar and launchctl; no live providers.','command':f'{sys.executable} scripts/verify_background.py','environment':{'python':sys.version,'platform':platform.platform()},'sha256':{f:hashlib.sha256((BASE/f).read_bytes()).hexdigest() for f in files if (BASE/f).exists()}}
 (out/'evidence.json').write_text(json.dumps(e,indent=2));print(log);print(str(out/'evidence.json'));sys.exit(not result.wasSuccessful())

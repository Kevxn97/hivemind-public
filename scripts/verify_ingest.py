#!/usr/bin/env python3
"""Synthetic CLI E2E; no provider calls, personal data or permission dialogs."""
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1] / 'hivemind'
COMMANDS = []

class IngestE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hive-ingest-fixture-')
        self.root = Path(self.temp.name).resolve()
        (self.root/'config').mkdir()
        (self.root/'input.md').write_text('# Selected\nSynthetic local evidence.\n')
        self.config = {'version':1,'files':[{'id':'selected','path':'input.md','reason':'Synthetic explicit selection'}], 'remote':[{'id':'gmail','title':'Synthetic mail','lookback_days':7}]}
        self.save_config()
    def tearDown(self):
        self.temp.cleanup()
    def save_config(self):
        (self.root/'config/sources.json').write_text(json.dumps(self.config))
    def cli(self,*args,ok=True):
        command=[sys.executable,str(BASE/'integrations/ingest.py'),'--root',str(self.root),*args]
        proc=subprocess.run(command,capture_output=True,text=True)
        COMMANDS.append({'command':['python3','integrations/ingest.py','--root','SYNTHETIC_ROOT',*args[:2]],'returncode':proc.returncode})
        if ok: self.assertEqual(proc.returncode,0,proc.stderr+proc.stdout)
        else: self.assertNotEqual(proc.returncode,0,proc.stdout)
        return json.loads(proc.stdout or proc.stderr)
    def begin(self): return self.cli('begin')
    def receipt(self,run):
        return {'schema_version':1,'run_id':run['id'],'sources':[{'id':'selected','kind':'local','status':'complete','sha256':run['local'][0]['sha256'],'disposition':'irrelevant','note_ids':[],'reason':'Synthetic document carries no durable knowledge.'},{'id':'gmail','kind':'remote','status':'complete','window':run['windows']['gmail'],'coverage':{'complete':True,'pages_complete':True,'permission':'granted','continuation':None,'page_count':1},'items':[],'counts':{'examined':0},'reason':'No matching synthetic results.'}]}
    def finish(self,run,receipt):
        path=self.root/'receipts.json';path.write_text(json.dumps(receipt))
        return self.cli('finish',run['id'],'--receipts',str(path))
    def put_note(self,source):
        note={'id':'synthetic-note','type':'lesson','cluster':'knowledge','title':'Synthetic note','summary':'Synthetic summary','body':'# Synthetic evidence\n\nNo private data.','status':'stable','evidence':'reported','sensitivity':'personal','aliases':[],'tags':[],'sources':[source],'verified':[],'stale_after':'2030-01-01T00:00:00Z','links':[]}
        payload=self.root/'payload.json';payload.write_text(json.dumps({'changes':[{'expected_revision':'new','note':note}]}))
        proc=subprocess.run([sys.executable,str(BASE/'hive.py'),'--root',str(self.root),'apply',str(payload),'--actor','synthetic-e2e','--reason','Synthetic fixture','--context','test:fixture'],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
    def test_capture_scan_read_finish_and_idempotent_rejection(self):
        cap=self.cli('capture','--text','A synthetic idea.','--title','Synthetic capture')
        self.assertEqual(self.cli('read',cap['id'])['content'],'# Synthetic capture\n\nA synthetic idea.\n')
        self.assertEqual(self.cli('scan')['pending_count'],2)
        run=self.begin();receipt=self.receipt(run)
        item=next(x for x in run['local'] if x['id']==cap['id'])
        receipt['sources'].append({'id':cap['id'],'kind':'local','status':'complete','sha256':item['sha256'],'disposition':'irrelevant','note_ids':[],'reason':'Synthetic test-only thought.'})
        self.assertEqual(self.finish(run,receipt)['status'],'complete')
        self.assertEqual(self.cli('scan')['pending_count'],0)
        self.assertTrue(self.cli('inbox')['items'][0]['processed'])
        self.cli('finish',run['id'],'--receipts',str(self.root/'receipts.json'),ok=False)
    def test_remote_checkpoint_tracks_covered_window_not_finish_time(self):
        from datetime import datetime, timedelta
        run=self.begin()
        run['windows']['gmail']={'start':'2026-10-01T12:00:00Z','end':'2026-10-08T12:00:00Z'}
        # Synthetic aged manifest models a provider run that finishes much later.
        manifest=self.root/'.state/runs'/run['id']/'manifest.json';manifest.write_text(json.dumps(run))
        state=self.finish(run,self.receipt(run))
        self.assertEqual(state['checkpoints']['gmail']['completed_at'],run['windows']['gmail']['end'])
        self.assertIsNotNone(state['checkpoints']['gmail']['acknowledged_at'])
        next_run=self.begin()
        self.assertEqual(next_run['windows']['gmail']['start'],'2026-10-07T12:00:00Z')
        self.cli('abort',next_run['id'],'--reason','Synthetic aged-run fixture complete')
    def test_stale_hash_and_independent_checkpoint(self):
        run=self.begin();receipt=self.receipt(run)
        (self.root/'input.md').write_text('Changed while curating.')
        state=self.finish(run,receipt)
        self.assertEqual(state['status'],'partial')
        self.assertNotIn('selected',state['checkpoints'])
        self.assertIn('gmail',state['checkpoints'])
        self.assertEqual(self.cli('scan')['pending_count'],1)
    def test_curated_note_and_citation_required(self):
        for source in (None,{'id':'selected','resource':'wrong:path','title':'Wrong citation'},{'id':'selected','resource':str(self.root/'input.md'),'title':'Missing fingerprint'}):
            with self.subTest(source=source):
                run=self.begin();receipt=self.receipt(run)
                receipt['sources'][0].update(disposition='curated',note_ids=['synthetic-note'])
                if source:self.put_note(source)
                state=self.finish(run,receipt)
                self.assertNotIn('selected',state['checkpoints'])
                if source:
                    # New store for each canonical fixture; do not directly edit notes.
                    self.tearDown();self.setUp()
        run=self.begin();receipt=self.receipt(run)
        self.put_note({'id':'selected','resource':str(self.root/'input.md'),'sha256':run['local'][0]['sha256'],'title':'Selected synthetic input'})
        receipt['sources'][0].update(disposition='curated',note_ids=['synthetic-note'])
        self.assertEqual(self.finish(run,receipt)['status'],'complete')
    def test_remote_incomplete_permission_pages_and_count(self):
        cases=[{'coverage':{'complete':False,'pages_complete':True,'permission':'granted','continuation':None,'page_count':1}},{'coverage':{'complete':True,'pages_complete':False,'permission':'granted','continuation':'next','page_count':1}},{'coverage':{'complete':True,'pages_complete':True,'permission':'denied','continuation':None,'page_count':1}},{'counts':{'examined':1}}]
        for patch in cases:
            with self.subTest(patch=patch):
                run=self.begin();receipt=self.receipt(run);receipt['sources'][1].update(patch)
                state=self.finish(run,receipt)
                self.assertNotIn('gmail',state['checkpoints'])
                self.assertIn('selected',state['checkpoints'])
    def test_malformed_and_single_active_run(self):
        run=self.begin()
        self.cli('begin',ok=False)
        path=self.root/'bad.json';path.write_text('{"sources":[]}')
        self.cli('finish',run['id'],'--receipts',str(path),ok=False)
        self.assertEqual(self.cli('status')['active_run'],run['id'])
        self.assertEqual(self.finish(run,self.receipt(run))['status'],'complete')
        def start(_):
            proc=subprocess.run([sys.executable,str(BASE/'integrations/ingest.py'),'--root',str(self.root),'begin'],capture_output=True,text=True)
            return proc.returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:codes=list(pool.map(start,range(4)))
        self.assertEqual(codes.count(0),1)
    def test_bounds_and_self_inputs(self):
        self.cli('capture','--text',' ',ok=False)
        self.cli('capture','--text','x'*24001,ok=False)
        self.config['files'][0]['path']='wiki/concept/local.md';self.save_config()
        self.cli('scan',ok=False)
    def test_canonical_tamper_prevents_all_acknowledgements(self):
        run=self.begin();receipt=self.receipt(run)
        self.put_note({'id':'test','resource':'test:fixture','title':'Synthetic source'})
        note=self.root/'wiki/lesson/synthetic-note.md';note.write_text(note.read_text()+'\nUnjournaled tamper.\n')
        result=self.finish(run,receipt)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['checkpoints'],{})
    def test_wispr_requires_two_streams_and_versions(self):
        self.config['remote']=[{'id':'wispr','title':'Synthetic Wispr','lookback_days':30}];self.save_config()
        run=self.begin()
        coverage={'complete':True,'pages_complete':True,'permission':'granted','continuation':None,'page_count':2,'modified_feed_complete':True,'streams':{'meetings':{'complete':True,'pages_complete':True,'continuation':None,'page_count':1},'scratchpads':{'complete':True,'pages_complete':True,'continuation':None,'page_count':1}}}
        local={'id':'selected','kind':'local','status':'complete','sha256':run['local'][0]['sha256'],'disposition':'irrelevant','note_ids':[],'reason':'Synthetic test fixture.'}
        remote={'id':'wispr','kind':'remote','status':'complete','window':run['windows']['wispr'],'coverage':copy.deepcopy(coverage),'items':[{'id':'synthetic-meeting','kind':'meeting','modified_at':run['started_at'],'resource':'wispr:synthetic-meeting','disposition':'irrelevant','note_ids':[],'reason':'Synthetic fixture outside durable scope.'}],'counts':{'examined':1},'reason':'Both fully paginated streams examined.'}
        remote['coverage']['streams'].pop('scratchpads')
        state=self.finish(run,{'schema_version':1,'run_id':run['id'],'sources':[local,remote]})
        self.assertNotIn('wispr',state['checkpoints'])
        run=self.begin();remote.update(window=run['windows']['wispr'],coverage=coverage)
        local['sha256']=run['local'][0]['sha256']
        state=self.finish(run,{'schema_version':1,'run_id':run['id'],'sources':[local,remote]})
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state['checkpoints']['wispr']['item_versions']['meeting:synthetic-meeting'],remote['items'][0]['modified_at'])
        next_run=self.begin()
        self.assertEqual(next_run['windows']['wispr']['mode'],'incremental')
        self.assertIsNotNone(next_run['windows']['wispr']['modified_since'])
        self.cli('abort',next_run['id'],'--reason','Synthetic test complete')
    def test_wispr_curated_and_unchanged_require_exact_cited_version(self):
        self.config['remote']=[{'id':'wispr','title':'Synthetic Wispr','lookback_days':30}];self.save_config()
        old_version='2026-10-01T10:00:00Z';new_version='2026-10-02T10:00:00Z'
        coverage={'complete':True,'pages_complete':True,'permission':'granted','continuation':None,'page_count':2,'modified_feed_complete':True,'streams':{'meetings':{'complete':True,'pages_complete':True,'continuation':None,'page_count':1},'scratchpads':{'complete':True,'pages_complete':True,'continuation':None,'page_count':1}}}
        def receipt(run,version,disposition):
            return {'schema_version':1,'run_id':run['id'],'sources':[{'id':'selected','kind':'local','status':'complete','sha256':run['local'][0]['sha256'],'disposition':'irrelevant','note_ids':[],'reason':'Synthetic local input.'},{'id':'wispr','kind':'remote','status':'complete','window':run['windows']['wispr'],'coverage':coverage,'items':[{'id':'meeting','resource':'wispr:meeting','kind':'meeting','modified_at':version,'disposition':disposition,'note_ids':['synthetic-note'] if disposition!='irrelevant' else [],'reason':'Synthetic versioned item.'}],'counts':{'examined':1},'reason':'Both streams and modified feed complete.'}]}
        self.put_note({'id':'wispr-meeting','resource':'wispr:meeting','title':'Synthetic meeting','modified_at':old_version})
        run=self.begin();first=self.finish(run,receipt(run,old_version,'curated'))
        self.assertEqual(first['status'],'complete')
        checkpoint=copy.deepcopy(first['checkpoints']['wispr'])
        run=self.begin();stale=self.finish(run,receipt(run,new_version,'curated'))
        self.assertEqual(stale['status'],'partial')
        self.assertEqual(stale['checkpoints']['wispr'],checkpoint)
        self.assertIn('citation',stale['attempts']['wispr']['detail'])
        # A legacy citation without a version must be explicitly upgraded before reuse.
        self.tearDown();self.setUp()
        self.config['remote']=[{'id':'wispr','title':'Synthetic Wispr','lookback_days':30}];self.save_config()
        run=self.begin();first=self.finish(run,receipt(run,old_version,'irrelevant'))
        checkpoint=copy.deepcopy(first['checkpoints']['wispr'])
        self.put_note({'id':'wispr-meeting','resource':'wispr:meeting','title':'Legacy unversioned synthetic meeting'})
        run=self.begin();missing=self.finish(run,receipt(run,old_version,'unchanged'))
        self.assertEqual(missing['status'],'partial')
        self.assertEqual(missing['checkpoints']['wispr'],checkpoint)
        self.assertIn('citation',missing['attempts']['wispr']['detail'])
    def test_begin_owner_is_persisted_with_manifest_and_validated(self):
        owner='0123456789abcdef0123456789abcdef'
        script="import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from integrations.ingest import begin_run;print(json.dumps(begin_run(Path(sys.argv[2]),owner=sys.argv[3])))"
        command=[sys.executable,'-c',script,str(BASE),str(self.root),owner]
        proc=subprocess.run(command,capture_output=True,text=True)
        COMMANDS.append({'command':['python3','-c','begin_run(SYNTHETIC_ROOT,owner=UUID32HEX)'],'returncode':proc.returncode})
        self.assertEqual(proc.returncode,0,proc.stderr)
        run=json.loads(proc.stdout)
        manifest=json.loads((self.root/'.state/runs'/run['id']/'manifest.json').read_text())
        self.assertEqual(manifest['owner'],owner)
        self.assertEqual(self.cli('status')['active_run'],manifest['id'])
        self.cli('abort',run['id'],'--reason','Synthetic owner fixture complete')
        invalid=subprocess.run(command[:-1]+['not-a-job-owner'],capture_output=True,text=True)
        COMMANDS.append({'command':['python3','-c','begin_run(SYNTHETIC_ROOT,owner=INVALID)'],'returncode':invalid.returncode})
        self.assertNotEqual(invalid.returncode,0)
        self.assertIsNone(self.cli('status')['active_run'])
    def test_remote_curated_requires_citation_and_no_raw_payload(self):
        run=self.begin();receipt=self.receipt(run)
        receipt['sources'][1]['items']=[{'id':'message','resource':'gmail:message','disposition':'curated','note_ids':['synthetic-note'],'reason':'Synthetic durable fact.'}]
        receipt['sources'][1]['counts']={'examined':1}
        state=self.finish(run,receipt);self.assertNotIn('gmail',state['checkpoints'])
        self.put_note({'id':'mail-message','resource':'gmail:message','title':'Synthetic message'})
        run=self.begin();receipt['run_id']=run['id'];receipt['sources'][1]['window']=run['windows']['gmail']
        receipt['sources'][1]['items'][0]['body']='Raw remote archive disallowed'
        self.assertNotIn('gmail',self.finish(run,receipt)['checkpoints'])
        run=self.begin();receipt['run_id']=run['id'];receipt['sources'][1]['window']=run['windows']['gmail'];receipt['sources'][1]['items'][0].pop('body')
        state=self.finish(run,receipt);self.assertEqual(state['status'],'complete')
    def test_calendar_validation_retains_success_without_launching(self):
        sys.path.insert(0,str(BASE))
        from integrations import calendar
        unavailable=calendar.save_result(self.root,{'status':'needs_permission'})
        self.assertEqual(unavailable['status'],'unavailable')
        for key in ('exported_at','range_start','range_end_exclusive','system_time_zone'):
            self.assertIsNone(unavailable[key])
        good={'status':'ok','exported_at':'2026-10-20T12:00:00Z','range_start':'2026-10-20T00:00:00Z','range_end_exclusive':'2026-11-03T00:00:00Z','system_time_zone':'Europe/Berlin','events':[{'key':'synthetic','title':'Synthetic event','start':'2026-10-21T10:00:00Z','end':'2026-10-21T11:00:00Z','all_day':False,'calendar':'Synthetic','cancelled':False,'description':'Must not be retained'}]}
        first=calendar.save_result(self.root,good)
        self.assertNotIn('description',first['events'][0])
        after=calendar.save_result(self.root,{'status':'needs_permission'})
        self.assertEqual(after['status'],'stale');self.assertEqual(after['events'],first['events'])
        malformed=copy.deepcopy(good);malformed['events'][0]['all_day']='false'
        after=calendar.save_result(self.root,malformed)
        self.assertEqual(after['status'],'stale')
        self.assertEqual((self.root/'.state/agenda.json').stat().st_mode & 0o777,0o600)
        all_day=copy.deepcopy(good);all_day['events'][0].update(all_day=True,start_date='2026-10-21',end_date_exclusive='2026-10-22')
        script="import json,sys;sys.path.insert(0,sys.argv[1]);from integrations import calendar;print(json.dumps(calendar.normalize(json.loads(sys.argv[2]))))"
        proc=subprocess.run([sys.executable,'-c',script,str(BASE),json.dumps(all_day)],cwd=BASE/'integrations',capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(IngestE2E)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    evidence={'kind':'synthetic CLI E2E','status':'passed' if result.wasSuccessful() else 'failed','tests':result.testsRun,'failures':[(str(t),s) for t,s in result.failures+result.errors],'environment':{'python':sys.version.split()[0],'platform':sys.platform,'providers':'none; Calendar save_result only; no app launch'},'fixture':'Temporary store; selected markdown, INBOX capture, canonical note through hive.apply, synthetic gmail receipts; no private content','reproduce':'python3 scripts/verify_ingest.py','commands':COMMANDS,'scenarios':unittest.defaultTestLoader.getTestCaseNames(IngestE2E),'code_sha256':{str(p.relative_to(BASE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [BASE/'integrations/ingest.py',BASE/'integrations/calendar.py',BASE/'integrations/CalendarReader.swift',BASE/'config/sources.json',BASE/'hive.py',Path(__file__)] if p.exists()}}
    out=Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(BASE.parent.parent/'hivemind-public-evidence')))/'verification/ingest-e2e.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(evidence,indent=2)+'\n')
    print('Evidence:',out)
    sys.exit(0 if result.wasSuccessful() else 1)

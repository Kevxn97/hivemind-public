#!/usr/bin/env python3
"""Terminal-only CLI subprocess E2E, synthetic canonical store; no live providers."""
import os
from pathlib import Path
import datetime as dt,hashlib,json,os,subprocess,sys,tempfile,unittest,io
BASE=Path(__file__).resolve().parents[1]/'hivemind';sys.path.insert(0,str(BASE))
import hive
class CliE2E(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  (self.root/'config').mkdir();(self.root/'config/sources.json').write_text(json.dumps({'version':1,'files':[],'remote':[]}))
  note={'id':'fixture','type':'lesson','cluster':'knowledge','title':'Ein testbarer Gedanke','summary':'Synthetisches Wissen','body':'Inhalt mit Quelle.','status':'stable','evidence':'confirmed','sensitivity':'internal','aliases':[],'tags':[],'sources':[{'id':'f','resource':'test:fixture','title':'Testquelle'}],'verified':[],'stale_after':'2100-01-01T00:00:00Z','links':[]}
  hive.apply(self.root,{'changes':[{'expected_revision':'new','note':note}]},'test','fixture','test:fixture')
 def tearDown(self):self.temp.cleanup()
 def cli(self,*args):return subprocess.run([sys.executable,str(BASE/'launcher.py'),'--root',str(self.root),*args],capture_output=True,text=True,timeout=10)
 def test_background_pause_and_failure_are_visible_even_when_process_lives(self):
  states=[({'status':'paused','message':'Automatik ausgeschaltet'},'pausiert'),({'status':'degraded','message':'Konfiguration beschädigt'},'gestört'),({'status':'running','message':'Quellenpause nach Fehler','backoff_until':'2100-01-01T00:00:00Z'},'Quellenpause')]
  for state,expected in states:
   code="import sys;sys.path.insert(0,sys.argv[1]);import launcher,background;background.status=lambda root:"+repr({'daemon_running':True,**state})+";print(launcher.plain_status(launcher.snapshot(__import__('pathlib').Path(sys.argv[2]))))"
   p=subprocess.run([sys.executable,'-c',code,str(BASE),str(self.root)],capture_output=True,text=True,timeout=10)
   self.assertEqual(p.returncode,0,p.stderr);self.assertIn(expected,p.stdout)
 def test_noninteractive_default_never_opens_browser_or_server(self):
  p=self.cli('--plain');self.assertEqual(p.returncode,0,p.stderr);self.assertIn('HiveMind',p.stdout);self.assertNotIn('http://',p.stdout);self.assertFalse((self.root/'.state/launcher.json').exists())
 def test_status_is_machine_readable_without_starting_service(self):
  p=self.cli('--status','--json');self.assertEqual(p.returncode,0,p.stderr);d=json.loads(p.stdout);self.assertEqual(d['knowledge']['notes'],1);self.assertIn('background',d);self.assertFalse((self.root/'.state/launcher.json').exists())
 def test_search_detail_and_connections_use_canonical_knowledge(self):
  p=self.cli('--search','testbarer','--json');self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)[0]['id'],'fixture')
  p=self.cli('--note','fixture','--json');self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)['note']['sources'][0]['resource'],'test:fixture')
  p=self.cli('--neighbors','fixture','--json');self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout),[])
 def test_capture_then_check_and_blank_text_rejected(self):
  p=self.cli('--capture','Ein bewusster Gedanke','--title','Idee','--json');self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(len(list((self.root/'INBOX').glob('*.md'))),1)
  p=self.cli('--check','--json');self.assertEqual(p.returncode,0,p.stderr);self.assertTrue(json.loads(p.stdout)['knowledge']['valid'])
  p=self.cli('--capture','   ');self.assertNotEqual(p.returncode,0)
 def test_brief_shows_project_context_for_a_folder(self):
  repo=self.root/'repo';(repo/'src').mkdir(parents=True)
  project={'id':'demo','type':'project','cluster':'projects/demo','title':'Demo-Projekt','summary':'Synthetisches Projekt','body':f'Einstieg: `{repo}`','status':'stable','evidence':'confirmed','sensitivity':'internal','aliases':[],'tags':[],'sources':[{'id':'f','resource':'test:fixture','title':'Testquelle'}],'verified':[],'stale_after':'2100-01-01T00:00:00Z','links':[]}
  hive.apply(self.root,{'changes':[{'expected_revision':'new','note':project}]},'test','fixture','test:fixture')
  p=self.cli('brief',str(repo/'src'));self.assertEqual(p.returncode,0,p.stderr);self.assertIn('Demo-Projekt',p.stdout);self.assertRaises(ValueError,json.loads,p.stdout)
  p=self.cli('brief',str(repo),'--json');self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)['cluster'],'projects/demo')
  p=self.cli('brief',str(self.root/'config'));self.assertNotEqual(p.returncode,0);self.assertEqual(p.stdout,'')
 def test_search_without_hits_is_recorded_locally(self):
  p=self.cli('search','Zauberwort');self.assertEqual(p.returncode,0,p.stderr)
  self.assertEqual(json.loads((self.root/'.state/search-misses.jsonl').read_text().splitlines()[-1])['query'],'Zauberwort')
 def test_retired_web_entry_refuses_to_start(self):
  p=self.cli('--serve');self.assertNotEqual(p.returncode,0);self.assertIn('Terminal',p.stderr+p.stdout)
 def test_positional_shortcuts_and_legacy_graph_are_terminal_only(self):
  p=self.cli('search','testbarer','--json');self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)[0]['id'],'fixture')
  p=self.cli('--graph','--plain');self.assertEqual(p.returncode,0,p.stderr);self.assertNotIn('http://',p.stdout)
if __name__=='__main__':
 out=Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(BASE.parent.parent/'hivemind-public-evidence')))/'terminal-cli'/dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');out.mkdir(parents=True)
 stream=io.StringIO();r=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CliE2E));log=stream.getvalue();(out/'test.log').write_text(log)
 e={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'command':'python3 scripts/verify_launcher.py','kind':'Synthetic CLI subprocess E2E, temporary journaled note and capture; no daemon install, no providers','source_sha256':{f:hashlib.sha256((BASE/f).read_bytes()).hexdigest() for f in ['launcher.py','../scripts/verify_launcher.py']},'tests':r.testsRun,'result':'PASS' if r.wasSuccessful() else 'FAIL','failures':len(r.failures),'errors':len(r.errors)};(out/'evidence.json').write_text(json.dumps(e,indent=2)+'\n');print(log);print(out);raise SystemExit(0 if r.wasSuccessful() else 1)

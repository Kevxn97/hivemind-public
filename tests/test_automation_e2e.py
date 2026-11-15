"""Opt-in automation journeys, specified before enabling public automation.

Requirements: fresh and upgraded vaults default off; previews/cancel/EOF never
mutate; stale consent fails; disabled sources leave the dispatch plan; pause
blocks new provider work; services belong to one vault and never a legacy label;
weekly local review is bounded and retains canonical history. Real subprocess
flows use synthetic sources/profiles and an executable provider double only.
The shared harness persists commands, outcomes and source hashes on every run.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

import test_source_connections_e2e as support
import test_easy_sources_e2e as easy_support


class AutomationE2E(unittest.TestCase):
    setUpClass = classmethod(support.SourceConnectionsE2E.setUpClass.__func__)
    tearDownClass = classmethod(support.SourceConnectionsE2E.tearDownClass.__func__)
    save = classmethod(support.SourceConnectionsE2E.save.__func__)
    setUp = support.SourceConnectionsE2E.setUp
    finish = support.SourceConnectionsE2E.finish
    call = support.SourceConnectionsE2E.call
    snapshot = support.SourceConnectionsE2E.snapshot
    terminal = easy_support.EasySourcesE2E.terminal

    def apply(self, *args):
        preview = self.call('automation', *args)
        self.assertEqual(preview['status'], 'preview')
        return self.call('automation', *args, '--apply', '--preview', preview['preview'])

    def add_file(self):
        path = self.project / 'selected.txt'
        path.write_text('Synthetic input for a bounded sync.\n')
        self.apply('source', 'add', 'selected', '--path', str(path), '--project', 'Sample Project')
        return path

    def configure_provider_double(self):
        profile = self.fixture / 'separate profile'
        profile.mkdir()
        binary = self.fixture / 'synthetic-codex'
        # Exercise the real guard/worker/receipt path without a model or account.
        binary.write_text('#!' + support.PYTHON + '\n' + '''
import json,os,pathlib,sys
root=pathlib.Path(sys.argv[sys.argv.index('--cd')+1])
(root/'.state/provider-invocation.json').write_text(json.dumps({'argv':sys.argv[1:],'profile':os.environ.get('CODEX_HOME')}))
run=json.loads((root/'.state/sources.json').read_text())['active_run']
folder=root/'.state/runs'/run
manifest=json.loads((folder/'manifest.json').read_text())
receipts=[{'id':row['id'],'kind':'local','status':'complete','sha256':row['sha256'],
 'disposition':'irrelevant','note_ids':[],'reason':'Synthetic fixture; no durable knowledge.'} for row in manifest['local']]
(folder/'receipts.json').write_text(json.dumps({'schema_version':1,'run_id':run,'sources':receipts}))
''')
        binary.chmod(0o700)
        self.apply('provider', '--codex', str(binary), '--agent-home', str(profile))
        return profile

    def test_defaults_readonly_previews_and_stale_consent(self):
        before = self.snapshot()
        state = self.call('automation', 'status')
        self.assertFalse(state['sync']['enabled'])
        self.assertFalse(state['maintenance']['enabled'])
        self.assertFalse(state['background']['enabled'])
        self.assertEqual(before, self.snapshot())
        plan = self.call('automation', 'enable', 'maintenance')
        self.assertEqual(before, self.snapshot())
        self.apply('schedule', '--daily-hour', '11')
        before = self.snapshot()
        self.call('automation', 'enable', 'maintenance', '--apply', '--preview', plan['preview'], succeeds=False)
        self.assertEqual(before, self.snapshot())
        self.call('automation', 'run', 'sync', succeeds=False)
        self.assertFalse((self.root / '.state/job.json').exists())
        plain=self.call('--plain',structured=False).stdout
        self.assertRegex(plain,r'Sync\s+off')

    def test_prepared_run_pins_selected_source_scope(self):
        self.add_file()
        self.apply('source','add','gmail','--remote','gmail','--scope','Only fictional pilot mail','--project','Sample Project')
        code='from hivemind.integrations import ingest;import json,sys;from pathlib import Path;print(json.dumps(ingest.begin_run(Path(sys.argv[1]))))'
        command=[support.PYTHON,'-B','-c',code,str(self.root)]
        result=subprocess.run(command,env=self.env,cwd=self.fixture,text=True,capture_output=True,timeout=15)
        self.case['commands'].append({'argv':command,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr});self.save()
        self.assertEqual(result.returncode,0,result.stderr)
        manifest=json.loads(result.stdout)
        self.assertEqual(manifest['source_config']['gmail']['scope'],'Only fictional pilot mail')
        self.assertEqual(manifest['source_config']['selected']['project'],'projects/sample-project')
        self.apply('source','remove','gmail')
        persisted=json.loads((self.root/'.state/runs'/manifest['id']/'manifest.json').read_text())
        self.assertEqual(manifest['source_config'],persisted['source_config'])

    def test_selected_sources_enable_disable_remove_and_remote_scope(self):
        path = self.add_file()
        self.apply('source', 'add', 'gmail', '--remote', 'gmail', '--scope', 'Only fictional pilot mail', '--project', 'Sample Project')
        state = self.call('automation', 'status')
        self.assertEqual(len(state['sources']), 2)
        self.assertIn(str(path), json.dumps(state))
        self.apply('source', 'disable', 'selected')
        self.assertFalse(next(r for r in self.call('automation', 'status')['sources'] if r['id']=='selected')['enabled'])
        report = self.call('--source-status')
        self.assertNotIn('selected', [r['id'] for r in report['sources']])
        self.apply('source', 'enable', 'selected')
        self.apply('source', 'remove', 'gmail')
        self.assertEqual([r['id'] for r in self.call('automation', 'status')['sources']], ['selected'])
        before = self.snapshot()
        self.call('automation', 'source', 'add', 'bad', '--path', str(self.root/'wiki/index.md'), '--project', 'Sample Project', succeeds=False)
        self.assertEqual(before, self.snapshot())

    def test_actual_bounded_sync_profile_pause_and_disabled_provider(self):
        self.add_file()
        profile = self.configure_provider_double()
        self.apply('enable', 'sync')
        plan = self.call('automation', 'run', 'sync')
        self.assertFalse((self.root/'.state/provider-invocation.json').exists())
        job = self.call('automation', 'run', 'sync', '--apply', '--preview', plan['preview'])
        deadline = time.monotonic()+12
        while job['status']=='running' and time.monotonic()<deadline:
            time.sleep(.1)
            job = self.call('automation', 'status')['job']
        self.assertEqual(job['status'], 'complete', job)
        invocation = json.loads((self.root/'.state/provider-invocation.json').read_text())
        self.assertEqual(invocation['profile'], str(profile))
        self.assertIn('--no-daemon', invocation['argv'])
        self.assertNotIn('dangerously', json.dumps(invocation))
        self.assertNotIn('.agents/skills', invocation['argv'][-1])
        self.assertTrue(self.call('check')['valid'])
        pause = self.call('memory', 'pause', '--reason', 'Synthetic stop')
        self.call('memory', 'pause', '--reason', 'Synthetic stop', '--apply', '--preview', pause['preview'])
        before = self.snapshot()
        self.call('automation', 'run', 'sync', succeeds=False)
        self.assertEqual(before, self.snapshot())
        self.apply('disable', 'sync')
        self.assertFalse(self.call('automation','status')['sync']['enabled'])

    def test_weekly_review_enable_run_disable_keeps_canonical_bytes(self):
        self.apply('enable', 'maintenance')
        self.apply('schedule', '--maintenance-day', '2', '--maintenance-hour', '10')
        before = {k:v for k,v in self.snapshot().items() if k.startswith(('vault/wiki/','vault/.history/'))}
        result = self.apply('run', 'maintenance')
        self.assertEqual(result['status'], 'complete')
        self.assertLessEqual(len(result['review']['items']), 5)
        self.assertFalse(result['canonical_changes'])
        self.assertTrue(Path(result['report']).is_file())
        after = {k:v for k,v in self.snapshot().items() if k.startswith(('vault/wiki/','vault/.history/'))}
        self.assertEqual(before, after)
        self.apply('disable', 'maintenance')
        self.call('automation','run','maintenance',succeeds=False)

    def test_terminal_toggle_cancel_and_eof(self):
        self.env['TERM']='xterm-256color'
        before = self.snapshot()
        self.terminal(['automation','enable','maintenance'], [('Apply this change', 'n\n')])
        self.assertEqual(before,self.snapshot())
        output=self.terminal(['automation'], [('Automation','\x04')])
        self.assertNotIn('Choose an action',output)
        # Opening the native TUI may create reconstructible runtime locks only.
        self.assertEqual(json.loads((self.root/'config/background.json').read_text())['enabled'],False)
        output = self.terminal(['automation','enable','maintenance'], [('Apply this change','y\n')])
        self.assertIn('Applied',output)
        self.assertTrue(self.call('automation','status')['maintenance']['enabled'])

    def test_invalid_schedule_field_keeps_menu_open_without_saving(self):
        self.env['TERM']='xterm-256color'
        before=self.call('automation','status')['schedule']
        output=self.terminal(['automation'], [
            ('Source sync','6'),
            ('Daily source sync','morgen\n'),
            ('Weekday','\n\n'),
            ('Invalid input','\n'),
            ('Source sync','0'),
        ])
        self.assertNotIn('INVALID_ARGUMENT',output)
        self.assertEqual(self.call('automation','status')['schedule'],before)

    def test_source_menu_accepts_capitalization_and_rejects_unknown_kind(self):
        self.env['TERM']='xterm-256color'
        for kind,final in [('Outlook',[('Invalid input','\n'),('Source sync','0')]),
                           ('Gmail',[('Apply','y'),('Saved','\n'),('Selected sources','0'),('Source sync','0')])]:
            with self.subTest(kind=kind):
                self.terminal(['automation'],[
                    ('Source sync','1'),('Selected sources','r'),
                    ('gmail, calendar',kind+'\n'),('Name this source','\n'),
                    ('What can HiveMind read','Only the fictional pilot.\n'),
                    ('Initial lookback','\n'),*final])
                sources=self.call('automation','status')['sources']
                if kind=='Outlook':self.assertEqual(sources,[])
                else:
                    self.assertEqual(sources[0]['connector'],'gmail')
                    self.assertEqual(sources[0]['id'],'gmail')

    def test_service_ownership_lifecycle_and_weekly_catchup_in_subprocess(self):
        self.apply('enable','maintenance')
        code = r'''
import json,sys,subprocess
from pathlib import Path
from datetime import datetime
from hivemind import service_control, background
root=Path(sys.argv[1]); folder=Path(sys.argv[2]); folder.mkdir()
legacy=folder/'local.hivemind.background.plist';legacy.write_text('SYNTHETIC_PRIVATE_SERVICE')
calls=[]
def runner(args,**kwargs):
 calls.append(args)
 return subprocess.CompletedProcess(args, 1 if 'print' in args or 'is-active' in args else 0,'','')
for platform in ('darwin','linux'):
 spec=service_control.spec(root,directory=folder,platform=platform)
 assert spec['label']!='local.hivemind.background'
 assert spec['label']!=service_control.spec(root.parent/'other',directory=folder,platform=platform)['label']
 service_control.install(root,directory=folder,platform=platform,runner=runner)
 assert Path(spec['path']).is_file()
 service_control.install(root,directory=folder,platform=platform,runner=runner)
 service_control.uninstall(root,directory=folder,platform=platform,runner=runner)
 assert not Path(spec['path']).exists()
 assert legacy.read_text()=='SYNTHETIC_PRIVATE_SERVICE'
cfg=json.loads((root/'config/background.json').read_text());cfg['enabled']=True
(root/'config/background.json').write_text(json.dumps(cfg))
a=background.tick(root,at=datetime.fromisoformat('2026-11-15T10:00:00+01:00'))
report=root/'.state/maintenance.json'; assert report.exists()
first=report.read_bytes()
background.tick(root,at=datetime.fromisoformat('2026-11-15T11:00:00+01:00'))
assert first==report.read_bytes()
print(json.dumps({'service_calls':calls,'weekly':a}))
'''
        command=[support.PYTHON,'-B','-c',code,str(self.root),str(self.fixture/'services')]
        result=subprocess.run(command,env=self.env,cwd=self.fixture,text=True,capture_output=True,timeout=20)
        self.case['commands'].append({'argv':command,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
        self.save()
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':
    unittest.main()

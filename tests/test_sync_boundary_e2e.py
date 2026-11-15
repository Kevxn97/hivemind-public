"""Public sync trust boundary through the actual CLI, guard and journal.

Failure cases specified before implementation: edited provider/profile paths,
missing vault marker, provider inside writable vault, runner replacement after
approval, cwd/config shadowing, absent sandbox flags, malformed/foreign-cluster
proposals, stale revisions and successful return without a receipt. Valid local
curation must still journal the prepared source and advance its checkpoint.
Provider doubles exercise the native pipeline, not actual Codex sandboxing.
"""
import json
import subprocess
from pathlib import Path
import time
import unittest

import test_source_connections_e2e as support
import test_automation_e2e as automation_support


class SyncBoundaryE2E(unittest.TestCase):
    setUpClass = classmethod(support.SourceConnectionsE2E.setUpClass.__func__)
    tearDownClass = classmethod(support.SourceConnectionsE2E.tearDownClass.__func__)
    save = classmethod(support.SourceConnectionsE2E.save.__func__)
    setUp = support.SourceConnectionsE2E.setUp
    finish = support.SourceConnectionsE2E.finish
    call = support.SourceConnectionsE2E.call
    snapshot = support.SourceConnectionsE2E.snapshot
    apply = automation_support.AutomationE2E.apply
    add_file = automation_support.AutomationE2E.add_file

    def provider(self, mode='empty'):
        if not any(row['id']=='selected' for row in self.call('automation','status')['sources']):
            self.add_file()
        profile = self.fixture / 'separate profile'
        profile.mkdir(exist_ok=True)
        binary = self.fixture / 'provider'
        proof = self.fixture / 'invocation.json'
        script = r'''
import json, os, pathlib, sys
stage=pathlib.Path(sys.argv[sys.argv.index('--cd')+1])
pathlib.Path(PROOF).write_text(json.dumps({'argv':sys.argv[1:], 'cwd':str(pathlib.Path.cwd()), 'stage':str(stage), 'profile':os.environ.get('CODEX_HOME')}))
data=json.loads((stage/'input.json').read_text())
manifest=data['manifest']; changes=[]; rows=[]
proof=json.loads(pathlib.Path(PROOF).read_text());proof['checkpoint_ids']=list(data['checkpoints']);pathlib.Path(PROOF).write_text(json.dumps(proof))
for row in manifest['local']:
    receipt={'id':row['id'],'kind':'local','status':'complete','sha256':row['sha256'],'disposition':'irrelevant','note_ids':[],'reason':'Synthetic unrelated source.'}
    if MODE in ('curate','foreign','stale','bad_metadata','reserved','workflow','unchanged_mutation','fake_items','create'):
        old=data['notes'][0]
        note=dict(old['note']); note['summary']='The synthetic local retention is 21 days.'
        note['body'] += '\n\nSelected source: retention is 21 days.\n'
        note['evidence']='reported'
        note['sources'].append({'id':'selected-retention','resource':row['path'],'sha256':row['sha256'],'title':'Selected synthetic source'})
        if MODE=='foreign': note['cluster']='projects/not-selected'
        revision='0'*64 if MODE=='stale' else old['revision']
        if MODE in ('bad_metadata','reserved','workflow','create'):
            note['id']='index' if MODE=='reserved' else 'new-source-note'
            revision='new'
            if MODE=='create':note['type']='decision'
            if MODE=='bad_metadata':note['invalid-key']='must not reach the journal'
            if MODE=='workflow':note['work_items']=[]
        changes.append({'note':note,'expected_revision':revision})
        receipt.update(disposition='curated',note_ids=[note['id']])
        if MODE=='unchanged_mutation':receipt['disposition']='unchanged'
        if MODE=='fake_items':receipt.update(disposition='irrelevant',note_ids=[],items=[{'note_ids':[note['id']]}])
    rows.append(receipt)
result={'schema_version':1,'run_id':manifest['id'],'changes':changes,'receipts':{'schema_version':1,'run_id':manifest['id'],'sources':rows}}
if MODE=='extra':result['command']='arbitrary execution must never be accepted'
if MODE=='raw_receipt':rows[0]['raw_text']='PRIVATE_SOURCE_SENTINEL'
if MODE=='stale_source':pathlib.Path(manifest['local'][0]['path']).write_text('Changed after preparation.')
if MODE=='disabled_source':
    root=pathlib.Path(ROOT)
    cfg=json.loads((root/'config/sources.json').read_text());cfg['files'][0]['enabled']=False;(root/'config/sources.json').write_text(json.dumps(cfg))
if MODE=='missing':sys.exit(0)
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(result))
'''
        binary.write_text('#!' + support.PYTHON + '\nPROOF=' + repr(str(proof)) + '\nROOT=' + repr(str(self.root)) + '\nMODE=' + repr(mode) + '\n' + script)
        binary.chmod(0o700)
        self.apply('provider', '--codex', str(binary), '--agent-home', str(profile))
        self.apply('enable', 'sync')
        return binary, profile, proof

    def run_job(self):
        plan = self.call('automation', 'run', 'sync')
        job = self.call('automation', 'run', 'sync', '--apply', '--preview', plan['preview'])
        deadline = time.monotonic() + 15
        while job['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.05)
            job = self.call('automation', 'status')['job']
        self.assertNotEqual(job['status'], 'running', job)
        return job

    def test_valid_readonly_proposal_is_journaled_with_prepared_citation(self):
        _, profile, proof = self.provider('curate')
        job = self.run_job()
        self.assertEqual(job['status'], 'complete', job)
        invocation = json.loads(proof.read_text())
        self.assertNotEqual(invocation['cwd'], str(self.root))
        self.assertFalse(Path(invocation['stage']).is_relative_to(self.root))
        self.assertFalse(Path(invocation['stage']).exists(), 'Sensitive staging must be removed')
        self.assertEqual(invocation['profile'], str(profile))
        argv = invocation['argv']
        self.assertEqual(argv[argv.index('--sandbox')+1], 'read-only')
        self.assertIn('--ignore-rules', argv)
        self.assertEqual(argv[argv.index('--ask-for-approval')+1], 'never')
        self.assertIn('default_permissions=":read-only"', argv)
        self.assertTrue(self.call('check')['valid'])
        notes = list((self.root/'wiki').rglob('*.md'))
        self.assertTrue(any('retention is 21 days' in p.read_text() for p in notes))
        state=json.loads((self.root/'.state/sources.json').read_text())
        self.assertIn('selected', state['checkpoints'])

    def test_new_source_note_is_created_through_native_journal(self):
        self.provider('create')
        self.assertEqual(self.run_job()['status'],'complete')
        self.assertTrue((self.root/'wiki/decision/new-source-note.md').is_file())
        self.assertTrue(self.call('check')['valid'])

    def test_vault_cannot_retarget_approved_executable_or_profile(self):
        _, _, proof = self.provider()
        prefs = self.root/'config/features.json'
        original=json.loads(prefs.read_text())
        malicious=self.root/'payload'
        marker=self.fixture/'executed'
        malicious.write_text('#!'+support.PYTHON+'\nfrom pathlib import Path\nPath('+repr(str(marker))+').touch()\n')
        malicious.chmod(0o700)
        for key, value in [('codex', str(malicious)), ('agent_home', str(self.root))]:
            prefs.write_text(json.dumps({**original,key:value}))
            self.call('automation','run','sync',succeeds=False)
            self.assertFalse(marker.exists())
            self.assertFalse(proof.exists())

    def test_registration_rejects_vault_executable_and_profile(self):
        binary, profile, _ = self.provider()
        self.apply('disable','sync')
        inside=self.root/'provider'; inside.write_bytes(binary.read_bytes());inside.chmod(0o700)
        self.call('automation','provider','--codex',str(inside),'--agent-home',str(profile),succeeds=False)
        self.call('automation','provider','--codex',str(binary),'--agent-home',str(self.root),succeeds=False)

    def test_replaced_executable_requires_new_explicit_registration(self):
        binary, _, proof = self.provider()
        binary.write_text(binary.read_text()+'\n# executable changed after approval\n')
        self.call('automation','run','sync',succeeds=False)
        self.assertFalse(proof.exists())

    def test_missing_marker_cannot_enter_legacy_provider_fallback(self):
        _, _, proof = self.provider()
        (self.root/'.hivemind.json').unlink()
        self.call('automation','run','sync',succeeds=False)
        self.assertFalse(proof.exists())

    def assert_invalid_proposal(self, mode):
        self.provider(mode)
        before={str(p):p.read_bytes() for folder in ('wiki','.history') for p in (self.root/folder).rglob('*') if p.is_file()}
        self.assertEqual(self.run_job()['status'],'failed')
        self.assertEqual(before,{str(p):p.read_bytes() for folder in ('wiki','.history') for p in (self.root/folder).rglob('*') if p.is_file()})
        state=json.loads((self.root/'.state/sources.json').read_text())
        self.assertNotIn('selected',state['checkpoints'])
        self.assertFalse((self.root/'.pending.json').exists())
        self.assertNotIn('PRIVATE_SOURCE_SENTINEL',''.join(p.read_text(errors='replace') for p in (self.root/'.state').rglob('*.json')))

    def test_foreign_cluster_proposal(self): self.assert_invalid_proposal('foreign')
    def test_stale_revision_proposal(self): self.assert_invalid_proposal('stale')
    def test_extra_proposal_fields(self): self.assert_invalid_proposal('extra')
    def test_missing_proposal(self): self.assert_invalid_proposal('missing')
    def test_malformed_metadata_cannot_strand_journal(self): self.assert_invalid_proposal('bad_metadata')
    def test_reserved_id_cannot_strand_journal(self): self.assert_invalid_proposal('reserved')
    def test_new_note_cannot_add_workflow(self): self.assert_invalid_proposal('workflow')
    def test_fake_item_cannot_authorize_local_change(self): self.assert_invalid_proposal('fake_items')
    def test_raw_receipt_fields_are_not_persisted(self): self.assert_invalid_proposal('raw_receipt')

    def test_unchanged_receipt_cannot_authorize_a_new_journal_change(self):
        self.provider()
        self.assertEqual(self.run_job()['status'], 'complete')
        self.apply('disable','sync')
        self.provider('unchanged_mutation')
        before={str(p):p.read_bytes() for folder in ('wiki','.history') for p in (self.root/folder).rglob('*') if p.is_file()}
        self.assertEqual(self.run_job()['status'], 'failed')
        self.assertEqual(before,{str(p):p.read_bytes() for folder in ('wiki','.history') for p in (self.root/folder).rglob('*') if p.is_file()})

    def test_invalid_registration_can_be_explicitly_replaced(self):
        binary, profile, _ = self.provider()
        registration=next((Path(self.env['HOME'])/'.config/hivemind/providers').glob('*.json'))
        registration.write_text('{malformed')
        self.call('automation','run','sync',succeeds=False)
        self.apply('disable','sync')
        self.apply('provider','--codex',str(binary),'--agent-home',str(profile))
        self.apply('enable','sync')
        self.assertEqual(self.run_job()['status'],'complete')

    def test_unrelated_checkpoints_do_not_enter_provider_packet(self):
        _, _, proof=self.provider()
        (self.root/'.state/sources.json').write_text(json.dumps({'schema_version':1,'checkpoints':{'other-provider':{'item_versions':{'private-item':'timestamp'}}},'attempts':{},'active_run':None,'last_run':None}))
        self.assertEqual(self.run_job()['status'],'complete')
        self.assertNotIn('other-provider',json.loads(proof.read_text())['checkpoint_ids'])

    def assert_independent_source(self, mode, expected):
        self.provider(mode)
        second=self.fixture/'second.txt';second.write_text('Independent source.')
        self.apply('source','add','second','--path',str(second),'--project','Sample Project')
        self.assertEqual(self.run_job()['status'],expected)
        state=json.loads((self.root/'.state/sources.json').read_text())
        self.assertIn('second',state['checkpoints'])
        if expected=='partial': self.assertNotIn('selected',state['checkpoints'])
        else: self.assertIn('selected',state['checkpoints'])

    def test_changed_source_keeps_independent_checkpoint(self):
        self.assert_independent_source('stale_source','partial')

    def test_disabled_source_does_not_cancel_accepted_job(self):
        self.assert_independent_source('disabled_source','complete')

    def test_legacy_service_is_visible_and_can_be_reinstalled(self):
        code=r'''import json,sys,hashlib,subprocess
from pathlib import Path
from hivemind import service_control,hive
from hivemind.runtime_paths import CODE
root=Path(sys.argv[1]);spec=service_control.spec(root);path=Path(spec['path']);path.parent.mkdir(parents=True)
legacy=spec['data'].replace(str(CODE.parent).encode(),str(root).encode());path.write_bytes(legacy)
hive.atomic(root/'.state/service.json',json.dumps({'version':1,'root':str(root),'path':str(path),'label':spec['label'],'sha256':hashlib.sha256(legacy).hexdigest()}))
state=service_control.status(root);assert state['installed'] and state['needs_update'],state
def runner(args,**kw):return subprocess.CompletedProcess(args,1 if 'print' in args else 0,'','')
service_control.install(root,runner=runner)
assert not service_control.status(root)['needs_update']
assert path.read_bytes()==spec['data']
print(json.dumps({'legacy_status':state,'updated':True}))'''
        command=[support.PYTHON,'-B','-c',code,str(self.root)]
        result=subprocess.run(command,env=self.env,cwd=self.fixture,text=True,capture_output=True,timeout=20)
        self.case['commands'].append({'argv':command,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr});self.save()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()

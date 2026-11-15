"""Bounded local inputs and verified sync receipts, Python standard library only.

No provider calls are made here. The sync agent reads remote sources with existing
connected tools, then submits a coverage receipt. Receipts attest provider results;
this validator checks their consistency, citations and canonical history, not the
truth of an external provider's response. Raw remote messages are never archived.

Receipt JSON (all prepared sources exactly once):
{"schema_version":1,"run_id":"RUN_ID","sources":[
 {"id":"local-id","kind":"local","status":"complete","sha256":"HASH",
  "disposition":"curated","note_ids":["existing-canonical-note"],"reason":"..."},
 {"id":"gmail","kind":"remote","status":"complete",
  "window":{"start":"...Z","end":"...Z"},
  "coverage":{"complete":true,"pages_complete":true,"permission":"granted",
              "continuation":null,"page_count":1},
  "items":[{"id":"message-id","resource":"gmail:message-id",
            "disposition":"curated","note_ids":["note-id"],"reason":"..."}],
  "counts":{"examined":1},"reason":"..."}]}

Every source status may instead be blocked/incomplete/failed with a reason; its
checkpoint remains unchanged. Complete local sources need the prepared AND current
hash; curated citations need resource==prepared path and sha256==prepared hash.
Local `irrelevant` needs a reason. Local `unchanged` additionally needs a previous
checkpoint with the same hash. Every remote item needs curated/unchanged citations
or an explicit irrelevant reason. Complete remote coverage requires all pages,
permission, no continuation and an exact examined-item count. No raw item text.
For source `wispr`, window adds mode (initial/incremental) and modified_since
(null first run = fully paginate modified metadata feed, initial content cutoff
is start). Coverage also requires modified_feed_complete:true and streams:
{meetings:{complete:true,pages_complete:true,continuation:null,page_count:1},
 scratchpads:{complete:true,pages_complete:true,continuation:null,page_count:1}}.
Wispr items add kind (meeting/scratchpad) and modified_at (timezone timestamp).
For curated AND unchanged Wispr items, each cited canonical note must contain
a source with the exact resource AND modified_at of the receipt item, e.g.
{"id":"wispr-meeting","resource":"wispr:meeting-id","title":"Meeting",
 "modified_at":"2026-10-20T10:00:00Z"}. Existing unversioned citations must be
explicitly updated through hive.apply before they can acknowledge a version.
Only IDs and modified timestamps are retained; no transcript archive.
Gmail starts at last completed_at minus one day (initial seven days); Wispr
starts at last completed_at minus one day (initial content cutoff thirty days).
A malformed envelope is rejected without consuming the active run. A well-formed
run becomes complete/partial/failed after independent source validation. Repeat
finish is rejected. `abort RUN_ID --reason TEXT` ends a stranded run without ack.
"""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
if __package__ and __package__.startswith('hivemind'):
    from ..runtime_paths import default_vault
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from runtime_paths import default_vault
import re
import sys
import tempfile
import uuid

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
if __package__ and __package__.startswith('hivemind'):
    from .. import hive
else:
    import hive
if __package__ and __package__.startswith('hivemind'):
    from ..integrations import calendar as calendar_adapter
else:
    from integrations import calendar as calendar_adapter

MAX_CONTENT=24000
ID=re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{0,180}')
RUN_ID=re.compile(r'\d{8}T\d{12}Z-[a-f0-9]{12}')


def now():return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def fingerprint(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()
def text(value,label,maximum=2000):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:raise ValueError('Invalid '+label)
    return value

def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.ingest-')
    try:
        with os.fdopen(fd,'w') as handle:
            json.dump(value,handle,ensure_ascii=False,indent=2);handle.write('\n');handle.flush();os.fsync(handle.fileno())
        os.chmod(name,0o600);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

@contextlib.contextmanager
def locked(root):
    state=Path(root)/'.state';state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state/'ingest.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        yield

def _state(root):
    path=Path(root)/'.state/sources.json'
    if not path.exists():return {'schema_version':1,'checkpoints':{},'attempts':{},'active_run':None,'last_run':None}
    state=json.loads(path.read_text())
    if not isinstance(state,dict) or state.get('schema_version')!=1 or not isinstance(state.get('checkpoints'),dict) or not isinstance(state.get('attempts'),dict):raise ValueError('Malformed source state; refusing checkpoint reset')
    return state

def config(root):
    root=Path(root).resolve();path=root/'config/sources.json'
    if not path.exists():return {'version':1,'files':[],'remote':[]}
    payload=json.loads(path.read_text())
    if not isinstance(payload,dict) or payload.get('version')!=1:raise ValueError('Malformed source configuration')
    seen=set()
    for kind in ('files','remote'):
        rows=payload.get(kind,[])
        if not isinstance(rows,list) or len(rows)>200:raise ValueError('Invalid bounded source list')
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not ID.fullmatch(row['id']) or row['id'] in seen or row['id'].startswith('inbox:'):raise ValueError('Invalid or duplicate source ID')
            seen.add(row['id'])
            if 'enabled' in row and type(row['enabled']) is not bool:raise ValueError('Invalid source enabled state')
            if kind=='files':
                source_path=Path(text(row.get('path'),'source path')).expanduser()
                source_path=(root/source_path).resolve() if not source_path.is_absolute() else source_path.resolve()
                # Canonical knowledge is output, never its own selected sync input.
                for canonical in (root/'wiki',):
                    if source_path.is_relative_to(canonical.resolve()):raise ValueError('Canonical HiveMind notes cannot be sync inputs')
                if source_path.is_relative_to((root/'.state').resolve()):raise ValueError('Operational state cannot be sync input')
                if row.get('heading') is not None and not re.fullmatch(r'#{1,6} .+',row['heading']):raise ValueError('Invalid selected heading')
            else:
                days=row.get('lookback_days',7)
                if type(days)!=int or not 1<=days<=31:raise ValueError('Remote initial window must be 1..31 days')
    return {**payload, **{kind:[row for row in payload.get(kind,[]) if row.get('enabled',True)] for kind in ('files','remote')}}

def _selected(root):
    root=Path(root).resolve();result=[dict(row) for row in config(root).get('files',[])]
    folder=root/'INBOX'
    if folder.is_symlink():raise ValueError('INBOX must not be a symlink')
    paths=sorted(folder.glob('*')) if folder.exists() else []
    if len(paths)>500:raise ValueError('INBOX exceeds 500 entries; curate existing inputs first')
    for path in paths:
        if path.suffix.lower() in ('.md','.txt') and path.name!='README.md':
            result.append({'id':'inbox:'+path.name,'path':str(path),'title':path.stem,'inbox':True,'reason':'Deliberately captured thought'})
    return result

def _read(root,source):
    root=Path(root).resolve();path=Path(source['path']).expanduser()
    if not path.is_absolute():path=root/path
    if source.get('inbox') and (path.is_symlink() or path.resolve().parent!=(root/'INBOX').resolve()):raise ValueError('Inbox symlinks and nested paths are unsupported')
    path=path.resolve()
    if not path.is_file():raise ValueError('Selected file is missing')
    if path.stat().st_size>1_000_000:raise ValueError('Source exceeds 1 MB; select a smaller document')
    content=path.read_text(encoding='utf-8');heading=source.get('heading');line=1
    if heading:
        lines=content.splitlines(keepends=True);matches=[i for i,value in enumerate(lines) if value.rstrip('\r\n')==heading]
        if len(matches)!=1:raise ValueError('Selected heading missing or ambiguous; no whole-file fallback')
        start=matches[0];level=len(heading)-len(heading.lstrip('#'));end=len(lines)
        for i in range(start+1,len(lines)):
            match=re.match(r'^(#{1,6}) ',lines[i])
            if match and len(match.group(1))<=level:end=i;break
        content=''.join(lines[start:end]);line=start+1
    if len(content)>MAX_CONTENT:raise ValueError('Selected content exceeds 24000 characters; select narrower input')
    return {'id':source['id'],'path':str(path),'heading':heading,'start_line':line,'content':content,'sha256':fingerprint(content)}

def read_source(root,source_id):
    for source in _selected(root):
        if source['id']==source_id:return _read(root,source)
    raise ValueError('Unknown source ID; use scan')

def _scan(root,state):
    items=[]
    for source in _selected(root):
        item={key:value for key,value in source.items() if key!='inbox'};item.setdefault('title',Path(source['path']).stem)
        try:
            doc=_read(root,source);item.update(path=doc['path'],status='ready',sha256=doc['sha256'],changed=state['checkpoints'].get(source['id'],{}).get('sha256')!=doc['sha256'])
        except (OSError,ValueError) as exc:item.update(status='error',changed=True,error=str(exc))
        items.append(item)
    return {'sources':items,'pending_count':sum(row['changed'] for row in items)}

def scan(root):
    with locked(root):return _scan(root,_state(root))

def capture(root,text,title=''):
    if not isinstance(text,str) or not text.strip() or len(text)>MAX_CONTENT:raise ValueError('Capture needs 1..24000 characters')
    if not isinstance(title,str) or len(title)>160 or '\n' in title or '\r' in title:raise ValueError('Capture title must be one line, at most 160 characters')
    title=title.strip();content=('# '+title+'\n\n' if title else '')+text.strip()+'\n'
    if len(content)>MAX_CONTENT:raise ValueError('Capture including title exceeds 24000 characters')
    root=Path(root).resolve()
    with locked(root):
        folder=root/'INBOX'
        if folder.is_symlink():raise ValueError('INBOX must not be a symlink')
        folder.mkdir(mode=0o700,exist_ok=True)
        if len(list(folder.iterdir()))>=500:raise ValueError('INBOX is full; curate existing captures first')
        stamp=datetime.now(timezone.utc);name=stamp.strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]+'.md';path=folder/name
        with path.open('x',encoding='utf-8') as out:os.chmod(path,0o600);out.write(content)
        return {'id':'inbox:'+name,'path':str(path),'title':title or 'New thought','created_at':stamp.isoformat()}

def inbox(root):
    root=Path(root).resolve()
    with locked(root):
        state=_state(root);items=[]
        for source in _selected(root):
            if not source.get('inbox'):continue
            try:
                doc=_read(root,source);path=Path(doc['path']);content=doc['content'];first=content.splitlines()[0] if content else ''
                items.append({'id':source['id'],'title':first[2:] if first.startswith('# ') else path.stem,'created_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),'preview':content[:280],'processed':state['checkpoints'].get(source['id'],{}).get('sha256')==doc['sha256']})
            except (OSError,ValueError) as exc:items.append({'id':source['id'],'title':source['title'],'created_at':None,'preview':'','processed':False,'error':str(exc)})
        return sorted(items,key=lambda item:item.get('created_at') or '',reverse=True)

def refresh_calendar(root):return calendar_adapter.refresh(root)

def validate_plan(root, plan):
    """Plans can narrow configured sources, never widen them or invent windows."""
    fields={'schema_version','reason','source_ids','windows','created_at','dispatch_id'}
    if not isinstance(plan,dict) or plan.get('schema_version')!=1 or set(plan)!=fields:
        raise ValueError('Invalid sync plan')
    if plan.get('reason') not in {'manual','daily','local-change'}:
        raise ValueError('Invalid sync reason')
    selected={row['id'] for row in _selected(root)}
    remote={row['id']:row for row in config(root).get('remote',[])}
    ids=plan.get('source_ids')
    if not isinstance(ids,list) or len(ids)>700 or not all(isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
        raise ValueError('Sync plan requires unique source IDs')
    if set(ids)-selected-set(remote):raise ValueError('Sync plan names an unconfigured source')
    if plan['reason']=='local-change' and set(ids)&set(remote):
        raise ValueError('Local-change plans cannot read remote sources')
    dispatch=plan.get('dispatch_id')
    if dispatch is not None and (not isinstance(dispatch,str) or not re.fullmatch(r'[0-9a-f]{32}',dispatch)):
        raise ValueError('Invalid dispatch ID')
    created=hive.timestamp(text(plan.get('created_at'),'plan creation time',80))
    windows=plan.get('windows')
    if not isinstance(windows,dict) or set(windows)!=(set(ids)&set(remote)):
        raise ValueError('Sync plan windows must match selected remote sources')
    for ident,window in windows.items():
        window_fields={'start','end','mode','modified_since'} if ident=='wispr' else {'start','end'}
        if not isinstance(window,dict) or set(window)!=window_fields:raise ValueError('Invalid sync window')
        begin=hive.timestamp(text(window.get('start'),'window start',80))
        end=hive.timestamp(text(window.get('end'),'window end',80))
        if begin>end or end!=created:raise ValueError('Invalid sync window boundaries')
        if ident=='wispr':
            if window.get('mode') not in {'initial','incremental'}:raise ValueError('Invalid Wispr mode')
            if window.get('modified_since')!=(window['start'] if window['mode']=='incremental' else None):
                raise ValueError('Invalid Wispr modified window')
    return json.loads(json.dumps(plan))


def _prepare_plan(root,state,reason='manual',source_ids=None,at=None,dispatch_id=None):
    at=at or datetime.now(timezone.utc)
    if at.tzinfo is None:raise ValueError('Sync plan time needs timezone')
    created=at.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    configured=config(root)
    if source_ids is None:
        source_ids=[row['id'] for row in _selected(root)]+[row['id'] for row in configured.get('remote',[])]
    windows={}
    for source in configured.get('remote',[]):
        if source['id'] not in source_ids:continue
        previous=state['checkpoints'].get(source['id'],{}).get('completed_at')
        start=hive.timestamp(previous)-timedelta(days=1) if previous else at-timedelta(days=source.get('lookback_days',7))
        window={'start':start.isoformat(timespec='seconds').replace('+00:00','Z'),'end':created}
        if source['id']=='wispr':window.update(mode='incremental' if previous else 'initial',modified_since=window['start'] if previous else None)
        windows[source['id']]=window
    return validate_plan(root,{'schema_version':1,'reason':reason,'source_ids':list(source_ids),
        'windows':windows,'created_at':created,'dispatch_id':dispatch_id})


def prepare_plan(root,*,reason='manual',source_ids=None,at=None,dispatch_id=None):
    root=Path(root).resolve()
    with locked(root):return _prepare_plan(root,_state(root),reason,source_ids,at,dispatch_id)


def begin_run(root,owner=None,plan=None):
    """Reserve exactly one plan. Correlation IDs are metadata, never authority."""
    if owner is not None and (not isinstance(owner,str) or not re.fullmatch(r'[0-9a-f]{32}',owner)):
        raise ValueError('Owner must be a lowercase UUID32hex job ID')
    root=Path(root).resolve()
    with locked(root):
        state=_state(root)
        if state.get('active_run'):raise ValueError('Sync already active: '+state['active_run'])
        plan=validate_plan(root,plan) if plan is not None else _prepare_plan(root,state)
        at=datetime.now(timezone.utc);stamp=at.strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:12]
        local=[row for row in _scan(root,state)['sources'] if row['id'] in plan['source_ids']]
        result={'schema_version':1,'id':stamp,'started_at':now(),'local':local,
                'windows':plan['windows'],'owner':owner,'plan':plan,'dispatch_id':plan.get('dispatch_id')}
        selected=_selected(root)+config(root).get('remote',[])
        result['source_config']={row['id']:row for row in selected if row['id'] in plan['source_ids']}
        atomic(root/'.state/runs'/stamp/'manifest.json',result)
        state['active_run']=stamp;atomic(root/'.state/sources.json',state)
        return result

def _citations(notes,note_ids,resource,sha256=None,modified_at=None):
    if not isinstance(note_ids,list) or not note_ids or len(note_ids)!=len(set(note_ids)) or len(note_ids)>200:raise ValueError('Curated item needs unique canonical note_ids')
    for ident in note_ids:
        if not isinstance(ident,str) or ident not in notes:raise ValueError('Curated canonical note is missing: '+str(ident))
        note=notes[ident][0]
        if not any(source.get('resource')==resource and (sha256 is None or source.get('sha256')==sha256) and (modified_at is None or source.get('modified_at')==modified_at) for source in note.get('sources',[])):raise ValueError('Canonical note lacks exact source citation: '+ident)

def _disposition(receipt,notes,resource,sha256=None,previous=None,modified_at=None):
    disposition=receipt.get('disposition');text(receipt.get('reason'),'disposition reason')
    if disposition=='curated':_citations(notes,receipt.get('note_ids'),resource,sha256,modified_at)
    elif disposition=='irrelevant':
        if receipt.get('note_ids',[])!=[]:raise ValueError('Irrelevant item must not claim curated notes')
    elif disposition=='unchanged':
        if sha256 is not None:
            if not previous or previous.get('sha256')!=sha256:raise ValueError('Unchanged requires previous exact checkpoint')
        else:_citations(notes,receipt.get('note_ids'),resource,modified_at=modified_at)
    else:raise ValueError('Explicit curated/irrelevant/unchanged disposition required')

def _validate_receipt(receipt,prepared,kind,notes,root,state):
    if receipt.get('kind')!=kind:raise ValueError('Receipt source kind mismatch')
    status=receipt.get('status')
    if status not in ('complete','incomplete','blocked','failed'):raise ValueError('Invalid source receipt status')
    text(receipt.get('reason'),'source reason')
    if status!='complete':raise ValueError(status+': '+receipt['reason'])
    if kind=='local':
        if prepared.get('status')!='ready':raise ValueError('Prepared local source was unavailable')
        sha=prepared['sha256']
        if receipt.get('sha256')!=sha:raise ValueError('Receipt hash differs from prepared source')
        current=read_source(root,prepared['id'])
        if current['sha256']!=sha or current['path']!=prepared['path']:raise ValueError('Local source changed after preparation')
        _disposition(receipt,notes,prepared['path'],sha,state['checkpoints'].get(prepared['id']))
        return {'sha256':sha,'disposition':receipt['disposition'],'note_ids':receipt.get('note_ids',[])}
    if receipt.get('window')!=prepared:raise ValueError('Remote window differs from prepared bounds')
    coverage=receipt.get('coverage')
    if not isinstance(coverage,dict) or coverage.get('complete') is not True or coverage.get('pages_complete') is not True or coverage.get('permission')!='granted' or coverage.get('continuation') is not None or type(coverage.get('page_count'))!=int or coverage['page_count']<1:raise ValueError('Incomplete remote pages, permission or coverage')
    items=receipt.get('items');counts=receipt.get('counts')
    if not isinstance(items,list) or len(items)>5000 or not isinstance(counts,dict) or type(counts.get('examined'))!=int or counts['examined']!=len(items):raise ValueError('Remote examined count must match every disposition item')
    wispr=receipt['id']=='wispr'
    versions=dict(state['checkpoints'].get('wispr',{}).get('item_versions',{})) if wispr else {}
    if wispr:
        if coverage.get('modified_feed_complete') is not True:raise ValueError('Wispr modified feed was not fully paginated')
        streams=coverage.get('streams')
        if not isinstance(streams,dict) or set(streams)!={'meetings','scratchpads'}:raise ValueError('Wispr requires both meetings and scratchpads coverage')
        for stream in streams.values():
            if not isinstance(stream,dict) or stream.get('complete') is not True or stream.get('pages_complete') is not True or stream.get('continuation') is not None or type(stream.get('page_count'))!=int or stream['page_count']<1:raise ValueError('Incomplete Wispr stream pagination')
    seen=set();curated=0
    for item in items:
        if not isinstance(item,dict) or set(item)-({'id','resource','disposition','note_ids','reason','kind','modified_at'} if wispr else {'id','resource','disposition','note_ids','reason'}):raise ValueError('Remote receipt may contain only IDs, references and dispositions; no raw content')
        ident=text(item.get('id'),'remote item ID',2048)
        if wispr:
            if item.get('kind') not in ('meeting','scratchpad'):raise ValueError('Wispr item needs meeting/scratchpad kind')
            modified=text(item.get('modified_at'),'Wispr modified timestamp',100);hive.timestamp(modified)
            ident=item['kind']+':'+ident
            if item.get('disposition')=='unchanged' and versions.get(ident)!=modified:raise ValueError('Wispr unchanged item needs prior exact ID and modified timestamp')
            versions[ident]=modified
        if ident in seen:raise ValueError('Duplicate remote item ID')
        seen.add(ident);resource=text(item.get('resource'),'remote source resource',2048)
        _disposition(item,notes,resource,modified_at=item['modified_at'] if wispr else None)
        curated+=item.get('disposition')=='curated'
    result={'window':prepared,'counts':{'examined':len(items),'curated':curated},'coverage':coverage}
    if wispr:result['item_versions']=versions
    return result

def finish_run(root,run_id,receipts):
    root=Path(root).resolve()
    if not isinstance(run_id,str) or not RUN_ID.fullmatch(run_id):raise ValueError('Invalid run ID')
    if not isinstance(receipts,dict) or set(receipts)!={'schema_version','run_id','sources'} or receipts.get('schema_version')!=1 or receipts.get('run_id')!=run_id or not isinstance(receipts.get('sources'),list):raise ValueError('Malformed receipt envelope')
    with locked(root):
        state=_state(root)
        if state.get('active_run')!=run_id:raise ValueError('Run is not active or already finished')
        manifest=json.loads((root/'.state/runs'/run_id/'manifest.json').read_text())
        expected={item['id']:('local',item) for item in manifest['local']}
        expected.update({ident:('remote',window) for ident,window in manifest['windows'].items()})
        rows={}
        for receipt in receipts['sources']:
            if not isinstance(receipt,dict) or not isinstance(receipt.get('id'),str) or receipt['id'] in rows or receipt['id'] not in expected:raise ValueError('Unknown or duplicate source receipt')
            rows[receipt['id']]=receipt
        if set(rows)!=set(expected):raise ValueError('Every prepared source requires an explicit receipt')
        at=now();outcomes={};integrity_error=None
        with hive.locked(root):
            try:
                hive.require_clean(root);notes=hive.load_notes(root)
                for note,_,_ in notes.values():hive.validate(note,{i:r[0]['type'] for i,r in notes.items()})
                hive.audit(root,notes)
            except (OSError,ValueError,KeyError,TypeError) as exc:integrity_error=str(exc);notes={}
            for ident,(kind,prepared) in expected.items():
                try:
                    if integrity_error:raise ValueError('Canonical integrity failed: '+integrity_error)
                    checkpoint=_validate_receipt(rows[ident],prepared,kind,notes,root,state)
                    state['checkpoints'][ident]={**checkpoint,'completed_at':prepared['end'] if kind=='remote' else at,'acknowledged_at':at,'run_id':run_id}
                    outcome={'status':'complete','attempted_at':at,'detail':rows[ident]['reason']}
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    reported=rows[ident].get('status')
                    outcome={'status':reported if reported in ('blocked','incomplete','failed') else 'failed','attempted_at':at,'detail':str(exc)}
                outcomes[ident]=outcome;state['attempts'][ident]=outcome
            successes=sum(item['status']=='complete' for item in outcomes.values())
            status='failed' if integrity_error or (outcomes and not successes) else ('complete' if successes==len(outcomes) else 'partial')
            summary={'id':run_id,'run_id':run_id,'status':status,'started_at':manifest['started_at'],'finished_at':at,'sources':outcomes,'canonical_integrity':not bool(integrity_error)}
            if integrity_error:summary['error']=integrity_error
            atomic(root/'.state/runs'/run_id/'result.json',summary)
            state['last_run']=summary;state['active_run']=None;atomic(root/'.state/sources.json',state)
        return {**state,'status':status}

def abort_run(root,run_id,reason):
    text(reason,'abort reason')
    if not RUN_ID.fullmatch(run_id):raise ValueError('Invalid run ID')
    with locked(root):
        state=_state(root)
        if state.get('active_run')!=run_id:raise ValueError('Run is not active')
        manifest=json.loads((Path(root)/'.state/runs'/run_id/'manifest.json').read_text())
        result={'id':run_id,'run_id':run_id,'status':'failed','started_at':manifest['started_at'],'finished_at':now(),'error':reason,'sources':{}}
        atomic(Path(root)/'.state/runs'/run_id/'result.json',result)
        state['last_run']=result;state['active_run']=None;atomic(Path(root)/'.state/sources.json',state)
        return result

def source_status(root):
    """Return {sources:[{id,title,status,last_complete_at,attempted_at,detail,counts}],
    last_run:object|null,active_run:string|null}. Local statuses are pending,
    complete, not_run or unavailable; a blocked/incomplete/failed last attempt
    is visible until a later valid completion. Remote complete means the exact
    last covered window, not a guarantee about newer provider data. The native
    Calendar is exposed through .state/agenda.json separately. last_complete_at
    is the covered window end for remote sources; acknowledged_at in checkpoint
    records when validation completed. Local completion is validation time.
    """
    with locked(root):
        state=_state(root);rows=[]
        for source in _scan(root,state)['sources']+config(root).get('remote',[]):
            ident=source['id'];attempt=state['attempts'].get(ident,{});checkpoint=state['checkpoints'].get(ident,{})
            status='pending' if source.get('changed') else ('complete' if checkpoint else 'not_run')
            if source.get('status')=='error':status='unavailable'
            elif attempt.get('status') and attempt['status']!='complete':status=attempt['status']
            rows.append({'id':ident,'title':source.get('title',ident),'status':status,'last_complete_at':checkpoint.get('completed_at'),'attempted_at':attempt.get('attempted_at'),'detail':source.get('error') or attempt.get('detail') or source.get('reason','Not synced yet'),'counts':checkpoint.get('counts')})
        return {'sources':rows,'last_run':state.get('last_run'),'active_run':state.get('active_run')}

def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root',type=Path,default=default_vault());sub=parser.add_subparsers(dest='command',required=True)
    for command in ('scan','inbox','status','begin','calendar'):sub.add_parser(command)
    sub.add_parser('read').add_argument('id')
    p=sub.add_parser('capture');p.add_argument('--text',required=True);p.add_argument('--title',default='')
    p=sub.add_parser('finish');p.add_argument('run_id');p.add_argument('--receipts',type=Path,required=True)
    p=sub.add_parser('abort');p.add_argument('run_id');p.add_argument('--reason',required=True)
    args=parser.parse_args();root=args.root.resolve()
    try:
        if args.command=='scan':result=scan(root)
        elif args.command=='read':result=read_source(root,args.id)
        elif args.command=='capture':result=capture(root,args.text,args.title)
        elif args.command=='inbox':result={'items':inbox(root)}
        elif args.command=='status':result=source_status(root)
        elif args.command=='begin':result=begin_run(root)
        elif args.command=='calendar':result=refresh_calendar(root)
        elif args.command=='abort':result=abort_run(root,args.run_id,args.reason)
        elif args.command=='finish':
            if args.receipts.stat().st_size>2_000_000:raise ValueError('Receipts exceed 2 MB')
            result=finish_run(root,args.run_id,json.loads(args.receipts.read_text()))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'status':'error','error':str(exc)},ensure_ascii=False));return 1

if __name__=='__main__':sys.exit(main())

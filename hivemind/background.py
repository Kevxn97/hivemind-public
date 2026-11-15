#!/usr/bin/env python3
"""Headless, bounded HiveMind scheduler: no HTTP listener, web UI or browser.

The macOS per-user LaunchAgent only scans selected local inputs, refreshes the
already-authorized Calendar helper, and dispatches the existing bounded Codex job.
No new model/login/approval settings. Raw provider output is discarded by jobs.py.
A 09:00 local daily run catches up after login/resume, once per day, and only while the
Mac is fully awake (a maintenance dark wake falls asleep again within seconds); local changes
wait for 120 quiet seconds and are limited to one automatic sync per 30 minutes.
A failed/partial input is not blindly retried; new input or the next day's run
may proceed after at least one hour. Stopping this daemon never stops its jobs.
"""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
if __package__:
    from .runtime_paths import default_vault
else:
    from runtime_paths import default_vault
import plistlib
import signal
import subprocess
import sys
import time
import uuid

if __package__:
    from . import hive
else:
    import hive
if __package__:
    from . import jobs
else:
    import jobs
if __package__:
    from .integrations import ingest
else:
    from integrations import ingest

BASE=Path(__file__).resolve().parent
LABEL='local.hivemind.background'
BOUNDS={'scan_seconds':(10,3600),'debounce_seconds':(30,3600),
        'min_sync_seconds':(1800,86400),'failure_backoff_seconds':(3600,86400),
        'daily_hour':(0,23),'calendar_seconds':(1800,86400),'integrity_seconds':(300,86400)}


def parse_time(value):
    at=datetime.fromisoformat(value.replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('Timestamp needs timezone')
    return at


def stamp(at):return at.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def current():return datetime.now().astimezone()


def private_folder(root):
    folder=Path(root)/'.state'
    if folder.is_symlink():raise ValueError('Operational state must not be a symlink')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700);folder.chmod(0o700)
    return folder


def config(root):
    path=Path(root)/'config/background.json'
    if not path.is_file():raise ValueError('config/background.json is missing; automation remains off.')
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or value.get('version')!=1 or type(value.get('enabled'))!=bool:
        raise ValueError('Invalid background configuration; automation remains off.')
    for key,(low,high) in BOUNDS.items():
        if type(value.get(key))!=int or not low<=value[key]<=high:
            raise ValueError('Invalid background configuration: '+key)
    return value


def read(root):
    path=Path(root)/'.state/background.json'
    if not path.exists():return {'schema_version':1,'status':'inactive','local':{}}
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or value.get('schema_version')!=1 or not isinstance(value.get('local'),dict):
        raise ValueError('Invalid background status; existing data is preserved.')
    return value


def save(root,value):
    private_folder(root);ingest.atomic(Path(root)/'.state/background.json',value);return value


def lock_handle(root,name):
    path=private_folder(root)/name
    if path.is_symlink():raise ValueError('Operational lock must not be a symlink')
    return os.fdopen(os.open(path,os.O_CREAT|os.O_RDWR,0o600),'a')


def daemon_active(root):
    if not (Path(root)/'.state/background-daemon.lock').exists():return False
    with lock_handle(root,'background-daemon.lock') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
    return False


def agent_path(launchagents=None):
    return (Path(launchagents) if launchagents is not None else Path.home()/'Library/LaunchAgents')/(LABEL+'.plist')


def status(root):
    root=Path(root).resolve()
    try:value=read(root)
    except (OSError,ValueError) as exc:value={'schema_version':1,'status':'degraded','error':str(exc)[:500]}
    if (root/'.hivemind.json').is_file():
        if __package__:
            from . import service_control
        else:
            sys.path.insert(0,str(BASE.parent))
            from hivemind import service_control
        return {**value,'daemon_running':daemon_active(root),**service_control.status(root)}
    return {**value,'daemon_running':daemon_active(root),'installed':agent_path().is_file(),'label':LABEL}


def system_awake():
    """Full wake only. A dark (maintenance) wake lacks the Graphics capability and sleeps
    again within seconds, which strands a long Codex run (observed 24. and 25.10.2026)."""
    try:report=subprocess.run(['/usr/bin/pmset','-g','systemstate'],capture_output=True,text=True,timeout=5).stdout
    except (OSError,subprocess.SubprocessError):return True
    line=next((line for line in report.splitlines() if 'Capabilities' in line),'')
    return 'Graphics' in line if line else True


class Dependencies:
    def scan(self,root):return ingest.scan(root)
    def sources(self,root):return ingest.source_status(root)
    def job_status(self,root):return jobs.status(root)
    def start(self,root,**kwargs):return jobs.start(root,**kwargs)
    def dispatch_status(self,root,dispatch_id):return jobs.dispatch_status(root,dispatch_id)
    def check(self,root):return hive.check(root)
    def calendar(self,root):return ingest.refresh_calendar(root)
    def awake(self,root):return system_awake()


def elapsed(at,value):
    if not value:return float('inf')
    return max(0,(at-parse_time(value)).total_seconds())


def wiki_signature(root):
    """Metadata only: hourly/changed integrity checks avoid loading the wiki each tick."""
    rows=[]
    for folder in (Path(root)/'wiki',Path(root)/'.history'):
        if folder.exists():
            for path in sorted(folder.rglob('*')):
                if path.is_file():
                    info=path.stat();rows.append((str(path.relative_to(root)),info.st_mtime_ns,info.st_size))
    pending=Path(root)/'.pending.json'
    if pending.exists():
        info=pending.stat();rows.append(('.pending.json',info.st_mtime_ns,info.st_size))
    return hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()


def covered_sources(root,deps,at):
    """Return only configured remote sources with complete coverage today."""
    configured=ingest.config(root).get('remote',[])
    report=deps.sources(root) if hasattr(deps,'sources') else ingest.source_status(root)
    rows={row['id']:row for row in report.get('sources',[])}
    result=set()
    for source in configured:
        row=rows.get(source['id'],{});completed=row.get('last_complete_at')
        if completed and row.get('status')=='complete' and parse_time(completed).astimezone(at.tzinfo).date()==at.date():
            result.add(source['id'])
    return result


def covered_today(root,deps,at):
    configured={row['id'] for row in ingest.config(root).get('remote',[])}
    return bool(configured) and configured<=covered_sources(root,deps,at)


def accept_dispatch(state,intent,job):
    """Spend a source/day only after a matching job was durably accepted."""
    if 'spent_markers' not in intent:
        intent['spent_markers']={'local':state['local'].get('attempted_fingerprint'),
                                 'daily':state.get('daily_attempt_date'),
                                 'remote':{ident:state.get('remote_attempt_dates',{}).get(ident)
                                           for ident in intent['plan']['windows']}}
    intent.update(status='started' if job.get('status')=='running' else 'finished',job_id=job.get('job_id'))
    if intent.get('fingerprint'):state['local']['attempted_fingerprint']=intent['fingerprint']
    for ident in intent['plan']['windows']:
        state.setdefault('remote_attempt_dates',{})[ident]=intent['day']
    if intent['plan']['windows']:state['daily_attempt_date']=intent['day']
    state['job']={key:job[key] for key in ('status','job_id','dispatch_id','started_at','finished_at','phase') if key in job}


def reopen_dispatch(state,intent,job,covered,at,cfg):
    """Undo only our reservations after proof that no provider could have started."""
    previous=intent.pop('spent_markers',None)
    def restore(mapping,key,value):
        if value is None:mapping.pop(key,None)
        else:mapping[key]=value
    if previous is not None:
        if intent.get('fingerprint') and state['local'].get('attempted_fingerprint')==intent['fingerprint']:
            restore(state['local'],'attempted_fingerprint',previous.get('local'))
        for ident in previous.get('remote',{}):
            if ident not in covered and state['remote_attempt_dates'].get(ident)==intent['day']:
                restore(state['remote_attempt_dates'],ident,previous['remote'][ident])
        if state.get('daily_attempt_date')==intent['day'] and not set(previous.get('remote',{}))<=covered:
            restore(state,'daily_attempt_date',previous.get('daily'))
    intent['status']='prepared'
    if intent.get('failed_start_job_id')!=job.get('job_id'):
        intent['failed_start_job_id']=job.get('job_id')
        intent['retry_after']=stamp(at+timedelta(seconds=cfg['failure_backoff_seconds']))
        if not state.get('backoff_until') or parse_time(state['backoff_until'])<parse_time(intent['retry_after']):
            state['backoff_until']=intent['retry_after']


def narrow_unstarted_plan(root,intent,report,deps):
    """Independent completed work can satisfy a missing start without replaying it."""
    local={row['id']:row for row in report.get('sources',[])}
    source_report=deps.sources(root) if hasattr(deps,'sources') else ingest.source_status(root)
    coverage={row['id']:row.get('last_complete_at') for row in source_report.get('sources',[])}
    plan=intent['plan'];remaining=[]
    configured=set(local)|{row['id'] for row in ingest.config(root).get('remote',[])}
    if set(plan['source_ids'])-configured:
        intent.update(status='cancelled',message='Unstarted job superseded by a changed source selection.')
        return
    for ident in plan['source_ids']:
        if ident in plan['windows']:
            end=coverage.get(ident)
            if end and parse_time(end)>=parse_time(plan['windows'][ident]['end']):continue
        elif ident in local and local[ident].get('status')=='ready' and not local[ident].get('changed'):
            continue
        remaining.append(ident)
    if not remaining:
        intent.update(status='superseded',message='Prepared job covered by independently completed source work.')
    elif remaining!=plan['source_ids']:
        intent['plan']=ingest.validate_plan(root,{**plan,'source_ids':remaining,
                          'windows':{ident:window for ident,window in plan['windows'].items() if ident in remaining}})


def launch_dispatch(root,state,intent,at,deps,cfg):
    """Persist retry intent before start; a missing child can be retried after backoff."""
    intent.update(last_attempt_at=stamp(at),retry_after=stamp(at+timedelta(seconds=cfg['failure_backoff_seconds'])),
                  attempts=intent.get('attempts',0)+1)
    state.update(last_auto_start_at=stamp(at),last_auto_reason=intent['plan']['reason'])
    save(root,state)
    try:
        launched=deps.start(root,plan=intent['plan'],dispatch_id=intent['id'])
    except (OSError,ValueError,RuntimeError):
        state.update(status='degraded',backoff_until=intent['retry_after'],
                     message='Job start not confirmed; the prepared sync will be checked again after source backoff.')
        return
    if launched.get('dispatch_id')!=intent['id']:
        # A manual job won the jobs lock. Preserve our unspent intent.
        state['message']='Another sync is running; the prepared job is waiting.'
        return
    if launched.get('start_failed'):
        state.update(status='degraded',backoff_until=intent['retry_after'],
                     message='Job could not start; a targeted retry follows source backoff.')
        return
    accept_dispatch(state,intent,launched)
    state['message']='Bounded sync started.'
    if launched.get('status') in ('failed','partial'):
        state['backoff_until']=intent['retry_after']
        state['last_failure_at']=stamp(at)
        state['failure_reason']='Sync incomplete; no blind retry.'


def meaningful(root,row):
    if not row['id'].startswith('inbox:'):return True
    # Ignore old title-only/whitespace templates. This reads only already bounded
    # selected Inbox files; text never enters daemon state or logs.
    content=ingest.read_source(root,row['id'])['content']
    return any(line.strip() and not line.lstrip().startswith('#') for line in content.splitlines())


def _tick(root,state,cfg,at,deps):
    state.update(status='running',updated_at=stamp(at),message='Background active; no sync needed.')
    state.pop('error',None)
    if not cfg['enabled']:
        state.update(status='paused',message='Automation is disabled in config/background.json.')
        return save(root,state)
    if (root/'.hivemind.json').is_file():
        if __package__:
            from . import feature_controls, automation, maintenance
        else:
            sys.path.insert(0,str(BASE.parent))
            from hivemind import feature_controls, automation, maintenance
        if automation.status(root)['paused']:
            state.update(status='paused',message='Incoming updates paused; no new sync or review starts.')
            return save(root,state)
        preferences=feature_controls.settings(root)
        if preferences['maintenance_enabled']:
            review=maintenance.run(root,at=at,scheduled=True)
            state['maintenance']={'finished_at':review['finished_at'],'items':len(review['review']['items'])}
        if not preferences['sync_enabled']:
            state.update(message='Background active; source sync disabled. Weekly review follows its schedule.')
            return save(root,state)
    if not (root/'config/sources.json').is_file():
        raise ValueError('config/sources.json is missing; automatic sync remains off.')
    source_config=ingest.config(root)
    # One serialized tick refreshes local hashes and observes existing independent work.
    report=deps.scan(root);state['last_scan_at']=stamp(at)
    changed=[(row['id'],row['sha256']) for row in report.get('sources',[]) if row.get('status')=='ready' and row.get('changed') and meaningful(root,row)]
    failures=sum(row.get('status')=='error' for row in report.get('sources',[]))
    fingerprint=hashlib.sha256(json.dumps(sorted(changed),separators=(',',':')).encode()).hexdigest() if changed else None
    local=state['local']
    if local.get('pending_fingerprint')!=fingerprint:
        local.update(pending_fingerprint=fingerprint,changed_since=stamp(at) if changed else None)
    local.update(pending_count=len(changed),unavailable_count=failures)
    job=deps.job_status(root)
    state['job']={key:job[key] for key in ('status','job_id','started_at','finished_at','phase') if key in job}
    terminal=':'.join(str(job.get(key,'')) for key in ('job_id','status','finished_at'))
    if job.get('status') in ('failed','partial') and state.get('observed_terminal')!=terminal:
        state['backoff_until']=stamp(at+timedelta(seconds=cfg['failure_backoff_seconds']))
        state['last_failure_at']=stamp(at)
        state['failure_reason']='Sync incomplete; a source or provider limit may apply. No blind retry.'
    if job.get('status') in ('complete','failed','partial'):state['observed_terminal']=terminal
    if 'remote_attempt_dates' not in state:
        state['remote_attempt_dates']={row['id']:state['daily_attempt_date'] for row in source_config.get('remote',[]) if state.get('daily_attempt_date')}
    covered=covered_sources(root,deps,at)
    for ident in covered:state['remote_attempt_dates'][ident]=at.date().isoformat()
    if covered and covered=={row['id'] for row in source_config.get('remote',[])}:
        state['daily_attempt_date']=at.date().isoformat()
    intent=state.get('dispatch')
    if intent and intent.get('status') in ('prepared','started'):
        accepted=deps.dispatch_status(root,intent['id']) if hasattr(deps,'dispatch_status') else (job if job.get('dispatch_id')==intent['id'] else None)
        if accepted and accepted.get('start_failed'):
            reopen_dispatch(state,intent,accepted,covered,at,cfg)
        elif accepted:
            accept_dispatch(state,intent,accepted)
        if intent.get('status')=='prepared':narrow_unstarted_plan(root,intent,report,deps)
    # Refresh existing read-only Calendar no more than every 30 min. It never
    # requests new access, compiles or relocates the compatibility application.
    if not state.get('calendar_attempted_at'):
        agenda_path=root/'.state/agenda.json'
        if agenda_path.is_file():
            try:
                previous=json.loads(agenda_path.read_text());exported=previous.get('exported_at')
                valid_until=previous.get('range_end_exclusive')
                if (previous.get('status')=='ok' and exported and
                    0<=(at-parse_time(exported)).total_seconds()<cfg['calendar_seconds'] and
                    (not valid_until or at<parse_time(valid_until))):
                    state['calendar_attempted_at']=exported
                    state['calendar']={'status':'ok','exported_at':exported,'event_count':len(previous.get('events',[]))}
            except (OSError,ValueError,TypeError):pass
    if (source_config.get('calendar',{}).get('enabled',not (root/'.hivemind.json').exists())
            and elapsed(at,state.get('calendar_attempted_at'))>=cfg['calendar_seconds']):
        state['calendar_attempted_at']=stamp(at)
        try:
            calendar=deps.calendar(root)
            state['calendar']={'status':calendar.get('status','unavailable'),'exported_at':calendar.get('exported_at'),'event_count':len(calendar.get('events',[]))}
        except (OSError,ValueError,RuntimeError):state['calendar']={'status':'unavailable'}
    signature=wiki_signature(root)
    need_check=(state.get('integrity',{}).get('signature')!=signature or elapsed(at,state.get('integrity',{}).get('checked_at'))>=cfg['integrity_seconds'])
    if need_check:
        try:
            proof=deps.check(root)
            if proof.get('valid') is not True:raise ValueError('Knowledge integrity check did not pass')
            state['integrity']={'valid':True,'checked_at':stamp(at),'signature':signature}
        except (OSError,ValueError,RuntimeError):
            state['integrity']={'valid':False,'checked_at':stamp(at),'signature':signature}
    daily_ids=[row['id'] for row in source_config.get('remote',[])
               if at.hour>=cfg['daily_hour'] and state['remote_attempt_dates'].get(row['id'])!=at.date().isoformat()]
    daily=bool(daily_ids)
    retry=bool(intent and intent.get('status')=='prepared')
    local_due=(bool(changed) and fingerprint!=local.get('attempted_fingerprint') and elapsed(at,local.get('changed_since'))>=cfg['debounce_seconds'])
    if job.get('status')=='running':state['message']='A sync is already running; the background service will not start another.'
    elif not state.get('integrity',{}).get('valid'):
        state.update(status='degraded',message='Check knowledge integrity; automatic sync is paused.')
    elif state.get('backoff_until') and at<parse_time(state['backoff_until']):
        if retry:state['status']='degraded'
        state['message']=('Job start not confirmed; retry no earlier than ' if retry else 'Source backoff after incomplete sync; no automatic retry before ')+state['backoff_until']
    elif elapsed(at,state.get('last_auto_start_at'))<cfg['min_sync_seconds']:
        state['message']='Automation is waiting for the minimum 30-minute interval.'
    elif (daily or local_due or retry) and hasattr(deps,'awake') and not deps.awake(root):
        # Neither the day nor the input counts as attempted; the next full wake dispatches.
        state['message']='The Mac is briefly awake for maintenance; sync waits until it is fully awake.'
    elif retry and at<parse_time(intent['retry_after']):
        state.update(status='degraded',message='Unconfirmed job start is waiting for bounded recovery.')
    elif daily or local_due or retry:
        # Fresh integrity immediately before dispatch; jobs enforces its own writer lease.
        if not need_check:
            try:
                proof=deps.check(root)
                if proof.get('valid') is not True:raise ValueError('Knowledge integrity check did not pass')
                state['integrity']={'valid':True,'checked_at':stamp(at),'signature':signature}
            except (OSError,ValueError,RuntimeError):
                state['integrity']={'valid':False,'checked_at':stamp(at),'signature':signature}
                state.update(status='degraded',message='Check knowledge integrity; automatic sync is paused.')
                return save(root,state)
        if not retry:
            dispatch_id=uuid.uuid4().hex
            source_ids=[ident for ident,_ in changed]+daily_ids
            plan=ingest.prepare_plan(root,reason='daily' if daily else 'local-change',
                                     source_ids=source_ids,at=at,dispatch_id=dispatch_id)
            intent={'id':dispatch_id,'status':'prepared','plan':plan,'day':at.date().isoformat(),
                    'fingerprint':fingerprint,'prepared_at':stamp(at),'attempts':0}
            state['dispatch']=intent
        launch_dispatch(root,state,intent,at,deps,cfg)
    elif failures:
        state.update(status='degraded',message='At least one selected local source is unavailable.')
    state['daily_hour']=cfg['daily_hour'];state['scan_seconds']=cfg['scan_seconds']
    return save(root,state)


def tick(root,*,at=None,deps=None):
    root=Path(root).resolve();at=at or current();deps=deps or Dependencies()
    if at.tzinfo is None:raise ValueError('Scheduler time needs timezone')
    with lock_handle(root,'background-tick.lock') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        state=read(root)
        try:return _tick(root,state,config(root),at,deps)
        except (OSError,ValueError,RuntimeError) as exc:
            state.update(status='degraded',updated_at=stamp(at),error=str(exc)[:500],message='Background is waiting for a local error to be resolved; no extra sync was started.')
            return save(root,state)


def run(root,*,deps=None):
    root=Path(root).resolve();stopping=False
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
    with lock_handle(root,'background-daemon.lock') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        previous={sig:signal.signal(sig,stop) for sig in (signal.SIGTERM,signal.SIGINT)}
        try:
            while not stopping:
                try:tick(root,deps=deps)
                except (OSError,ValueError,RuntimeError):pass  # Preserve corrupt files; retry boundedly, never reset them.
                try:seconds=config(root)['scan_seconds']
                except (OSError,ValueError):seconds=60
                deadline=time.monotonic()+seconds
                while not stopping and time.monotonic()<deadline:time.sleep(min(1,max(.01,deadline-time.monotonic())))
            try:
                value=read(root);value.update(status='stopped',updated_at=stamp(current()),message='Background stopped; independently running syncs remain active.');save(root,value)
            except (OSError,ValueError):pass
        finally:
            for sig,handler in previous.items():signal.signal(sig,handler)
    return 0


def _launch(runner,args):
    return runner(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=20,check=False)


def install(root,*,launchagents=None,runner=None,uid=None):
    if (Path(root) / '.hivemind.json').exists():
        if not config(root)['enabled']:
            raise ValueError('Background scheduling is disabled. Enable background with hive automation.')
        if __package__:
            from . import service_control
        else:
            sys.path.insert(0,str(BASE.parent))
            from hivemind import service_control
        return service_control.install(root,directory=launchagents,runner=runner)
    root=Path(root).resolve();config(root);runner=runner or subprocess.run;uid=os.getuid() if uid is None else uid
    path=agent_path(launchagents);path.parent.mkdir(parents=True,exist_ok=True)
    executable=Path(sys.executable).absolute()
    payload={'Label':LABEL,'ProgramArguments':[str(executable),str(Path(__file__).resolve()),'--root',str(root),'run'],'WorkingDirectory':str(root),
             'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'ProcessType':'Background',
             'EnvironmentVariables':{'HOME':str(Path.home()),'PATH':str(Path.home()/'.local/bin')+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'},
             'StandardOutPath':'/dev/null','StandardErrorPath':'/dev/null'}
    data=plistlib.dumps(payload,sort_keys=True);domain='gui/'+str(uid);target=domain+'/'+LABEL
    loaded=_launch(runner,['/bin/launchctl','print',target]).returncode==0
    same=path.exists() and path.read_bytes()==data
    if loaded and not same:_launch(runner,['/bin/launchctl','bootout',target]);loaded=False
    if not same:
        if path.is_symlink():raise ValueError('LaunchAgent plist must not be a symlink')
        temporary=path.with_suffix('.plist.tmp');temporary.write_bytes(data);temporary.chmod(0o600);os.replace(temporary,path)
    enabled=_launch(runner,['/bin/launchctl','enable',target])
    if enabled.returncode!=0:raise RuntimeError('launchctl could not enable the user background service.')
    if not loaded:
        result=_launch(runner,['/bin/launchctl','bootstrap',domain,str(path)])
        if result.returncode!=0:raise RuntimeError('launchctl could not start the user background service.')
    return {'installed':True,'running':True,'label':LABEL,'plist':str(path),'domain':domain}


def uninstall(root,*,launchagents=None,runner=None,uid=None):
    if (Path(root)/'.hivemind.json').is_file():
        if __package__:
            from . import service_control
        else:
            sys.path.insert(0,str(BASE.parent))
            from hivemind import service_control
        cfg=config(root);cfg['enabled']=False
        ingest.atomic(Path(root)/'config/background.json',cfg)
        return service_control.uninstall(root,directory=launchagents,runner=runner)
    runner=runner or subprocess.run;uid=os.getuid() if uid is None else uid;path=agent_path(launchagents);target='gui/'+str(uid)+'/'+LABEL
    result=_launch(runner,['/bin/launchctl','bootout',target])
    if result.returncode!=0:
        loaded=_launch(runner,['/bin/launchctl','print',target])
        if loaded.returncode==0:raise RuntimeError('Background service could not be stopped; the plist is preserved.')
    if path.is_symlink():raise ValueError('LaunchAgent plist must not be a symlink')
    path.unlink(missing_ok=True)
    return {'installed':False,'running':False,'label':LABEL,'message':'Background service removed; running syncs remain active independently.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=default_vault());parser.add_argument('command',choices=('run','tick','status','install','uninstall'));args=parser.parse_args()
    try:
        if args.command=='run':return run(args.root)
        value={'tick':tick,'status':status,'install':install,'uninstall':uninstall}[args.command](args.root)
        print(json.dumps(value,ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'status':'error','error':str(exc)[:500]},ensure_ascii=False),file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())

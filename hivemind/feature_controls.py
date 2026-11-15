"""Guided opt-in source sync, background startup and weekly knowledge review."""
import argparse
import copy
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import automation, background, hive, jobs, maintenance, project_context, service_control, provider_trust
from .integrations import ingest
from .runtime_paths import require_vault

DEFAULTS={'version':1,'sync_enabled':False,'maintenance_enabled':False,
          'maintenance_day':6,'maintenance_hour':9,'codex':None,'agent_home':None}
FILES=('config/features.json','config/background.json','config/sources.json')
DAYS=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')


def settings(root):
    path=Path(root)/FILES[0]
    value=json.loads(path.read_text()) if path.exists() else copy.deepcopy(DEFAULTS)
    if not isinstance(value,dict) or value.get('version')!=1:
        raise ValueError('Invalid feature settings; automation stays disabled.')
    for key in ('sync_enabled','maintenance_enabled'):
        if type(value.get(key)) is not bool:
            raise ValueError('Invalid feature setting: '+key)
    for key,maximum in (('maintenance_day',6),('maintenance_hour',23)):
        if type(value.get(key)) is not int or not 0<=value[key]<=maximum:
            raise ValueError('Invalid feature schedule: '+key)
    for key in ('codex','agent_home'):
        if value.get(key) is not None and (not isinstance(value[key],str) or not Path(value[key]).is_absolute()):
            raise ValueError('Invalid provider path: '+key)
    return value


def require_sync(root):
    prefs=settings(root)
    if not prefs['sync_enabled']:
        raise ValueError('Source sync is disabled. Configure a provider and selected sources, then enable sync with hive automation.')
    automation.require_running(root)
    provider_ready(prefs)
    return provider_trust.require(root, prefs)


def provider_ready(prefs):
    binary=prefs.get('codex');profile=prefs.get('agent_home')
    if not binary or not Path(binary).is_file() or not os.access(binary,os.X_OK):
        raise ValueError('Choose an installed Codex CLI with hive automation provider --codex PATH --agent-home PATH.')
    if not profile or not Path(profile).is_dir():
        raise ValueError('Choose an existing Codex profile directory. Log in and connect sources in that profile first.')


def _sources(root):
    value=json.loads((Path(root)/FILES[2]).read_text())
    ingest.config(root)  # Validate active and disabled rows before editing them.
    return value


def status(root):
    prefs=settings(root);cfg=background.config(root);sources=_sources(root)
    rows=[dict(row,kind=kind,enabled=row.get('enabled',True)) for kind in ('files','remote') for row in sources[kind]]
    service=service_control.status(root)
    paused=automation.status(root)['paused']
    provider_error=None
    try:registered=provider_trust.read(root)
    except ValueError as exc:registered=None;provider_error=str(exc)
    result={'status':'ok','vault':str(root),'paused':paused,
        'sync':{'enabled':prefs['sync_enabled'],'codex':prefs['codex'],'agent_home':prefs['agent_home'],
                'host_access':'unverified','provider_registered':bool(registered),'provider_error':provider_error,
                'execution_policy':'read-only proposals; native validation and journal apply'},
        'background':{**service,'enabled':cfg['enabled'],'daemon_running':background.daemon_active(root)},
        'maintenance':{'enabled':prefs['maintenance_enabled'],'day':prefs['maintenance_day'],
                       'hour':prefs['maintenance_hour'],'last_run':maintenance.read(root)},
        'schedule':{'daily_hour':cfg['daily_hour'],'scan_seconds':cfg['scan_seconds'],
                    'debounce_seconds':cfg['debounce_seconds'],'min_sync_seconds':cfg['min_sync_seconds']},
        'sources':rows,'job':jobs.read(root)}
    state=lambda on:'ON' if on else 'OFF'
    result['text']='\n'.join(['HiveMind automation',str(root),'',
        'Incoming updates  '+('PAUSED' if paused else 'running'),
        'Source sync       '+state(prefs['sync_enabled']),
        'Background        '+state(cfg['enabled'])+(' · running' if result['background']['daemon_running'] else ' · not running'),
        'Weekly review     '+state(prefs['maintenance_enabled']),
        f"Daily sources     {cfg['daily_hour']:02d}:00 local · changes after {cfg['debounce_seconds']} quiet seconds",
        f"Weekly review     {DAYS[prefs['maintenance_day']]} {prefs['maintenance_hour']:02d}:00 local · up to 5 items",
        'Codex profile     '+(prefs['agent_home'] or 'not selected'),
        'Selected sources  '+str(sum(row['enabled'] for row in rows)),
        *['  '+row['id']+' · '+state(row['enabled'])+' · '+row.get('path',row.get('scope','')) for row in rows],
        '', 'Background scheduling requires a running service or hive automation serve.',
        'Turning a feature off prevents new runs. Already running sync jobs finish independently.',
        'Connector login and access belong to the chosen Codex profile.',
        *(['Provider approval needs attention: '+provider_error] if provider_error else []),
        *(['Background service needs an update. Preview hive automation enable background, then apply it.'] if service.get('needs_update') else [])])
    return result


def configure(sub):
    parser=sub.add_parser('automation',help='Guided source sync, background startup and weekly review')
    actions=parser.add_subparsers(dest='automation_action')
    for action in ('status','setup','serve','review'):
        actions.add_parser(action)
    changing=[]
    for action in ('enable','disable'):
        p=actions.add_parser(action);p.add_argument('feature',choices=('sync','background','maintenance'));changing.append(p)
    p=actions.add_parser('provider',help='Select an existing Codex executable and profile; never copies credentials')
    p.add_argument('--codex',required=True);p.add_argument('--agent-home',required=True);changing.append(p)
    p=actions.add_parser('schedule')
    p.add_argument('--daily-hour',type=int);p.add_argument('--maintenance-day',type=int,help='0 Monday .. 6 Sunday')
    p.add_argument('--maintenance-hour',type=int);changing.append(p)
    p=actions.add_parser('run');p.add_argument('feature',choices=('sync','maintenance'));changing.append(p)
    p=actions.add_parser('source');sources=p.add_subparsers(dest='source_action',required=True)
    p=sources.add_parser('add');p.add_argument('source_id')
    kind=p.add_mutually_exclusive_group(required=True);kind.add_argument('--path');kind.add_argument('--remote',choices=('gmail','calendar','wispr','mcp'))
    p.add_argument('--project',required=True);p.add_argument('--scope');p.add_argument('--heading')
    p.add_argument('--lookback-days',type=int,default=7);changing.append(p)
    for action in ('enable','disable','remove'):
        p=sources.add_parser(action);p.add_argument('source_id');changing.append(p)
    for p in changing:
        p.add_argument('--apply',action='store_true');p.add_argument('--preview')
    return parser


def _snapshot(root):
    paths=[*FILES,'.state/automation.json']
    result={name:(Path(root)/name).read_text() if (Path(root)/name).exists() else None for name in paths}
    result['provider_registration']=provider_trust.snapshot(root)
    for name in paths:
        path=Path(root)/name
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('Configuration and state must not be symlinks')
    return result


@contextlib.contextmanager
def _control_lock(root):
    path=Path(root)/'.state/features.lock'
    if path.is_symlink():raise ValueError('Feature lock must not be a symlink')
    with os.fdopen(os.open(path,os.O_CREAT|os.O_RDWR,0o600),'a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        yield


def _plan(root,args):
    before=_snapshot(root);prefs=settings(root);cfg=background.config(root);sources=_sources(root)
    values={FILES[0]:prefs,FILES[1]:cfg,FILES[2]:sources};changed=None
    action=args.automation_action;feature=getattr(args,'feature',None)
    if action in ('enable','disable'):
        on=action=='enable'
        if feature=='background':
            service_control.spec(root)  # Fail before any change on unsupported systems.
            cfg['enabled']=on;changed=FILES[1]
        else:
            prefs[feature+'_enabled']=on;changed=FILES[0]
        if on and feature=='sync':
            provider_trust.require(root,prefs)
            if not ingest._selected(root) and not ingest.config(root).get('remote'):
                raise ValueError('Select at least one source before enabling sync.')
        detail=f"{action.title()} {feature} for this vault."
        if feature=='sync' and on:
            detail+='\nSelected content is sent to the configured Codex model for curation; account usage or costs may apply.\nProfile '+prefs['agent_home']
        if feature=='background':
            detail+='\nUser service '+service_control.spec(root)['path']
        if feature=='maintenance':
            detail+='\nLocal evidence review of up to five items; no model call or automatic factual rewrite.'
    elif action=='provider':
        if prefs['sync_enabled']:
            raise ValueError('Disable sync before changing its provider profile.')
        prefs['codex']=str(Path(args.codex).expanduser().absolute())
        prefs['agent_home']=str(Path(args.agent_home).expanduser().resolve());provider_ready(prefs)
        registration=provider_trust.candidate(root,prefs)
        prefs['codex']=registration['codex']
        prefs['agent_home']=registration['agent_home']
        changed=FILES[0]
        detail='Use Codex '+prefs['codex']+'\nProfile '+prefs['agent_home']+'\nExisting model and login are retained. Sync commands run read-only without escalation; proposals are validated before journal writes. Connector permissions remain in this profile.'
    elif action=='schedule':
        if args.daily_hour is not None:
            if not 0<=args.daily_hour<=23:raise ValueError('Daily hour must be 0..23')
            cfg['daily_hour']=args.daily_hour
        for key,maximum in (('maintenance_day',6),('maintenance_hour',23)):
            value=getattr(args,key)
            if value is not None:
                if not 0<=value<=maximum:raise ValueError(key+' is out of range')
                prefs[key]=value
        detail=f"Daily source check at {cfg['daily_hour']:02d}:00; weekly review on {DAYS[prefs['maintenance_day']]} at {prefs['maintenance_hour']:02d}:00 (local time)."
        changed='schedule'
    elif action=='source':
        ident=args.source_id
        if not ingest.ID.fullmatch(ident) or ident.startswith('inbox:'):
            raise ValueError('Choose a simple, unique source ID')
        rows=[(kind,row) for kind in ('files','remote') for row in sources[kind] if row['id']==ident]
        if args.source_action=='add':
            if rows:raise ValueError('Source ID already exists; disable/remove it before replacing it.')
            project=project_context.resolve_project(root,args.project)
            if project is None:
                projects=project_context.list_projects(root)
                matches=[p for p in projects if p['title']==args.project] if projects and 'title' in projects[0] else []
                if len(matches)==1:project={'cluster':matches[0]['cluster']}
                else:
                    from .source_connections import _choose_project
                    project={'cluster':_choose_project(root,args.project,False)}
            row={'id':ident,'title':ident,'enabled':True,'project':project['cluster']}
            if args.path:
                path=Path(args.path).expanduser().absolute()
                if path.is_symlink() or not path.is_file():raise ValueError('Choose one existing local text file, not a link or directory.')
                path=path.resolve()
                if any(path.is_relative_to(root/p) for p in ('wiki','.history','.state','config')):
                    raise ValueError('Vault knowledge, state and configuration cannot be selected input.')
                row['path']=str(path)
                if args.heading:row['heading']=args.heading
                ingest._read(root,row)  # Bounded readable text and exact optional heading.
                sources['files'].append(row)
            else:
                if not args.scope or not args.scope.strip() or len(args.scope)>500:
                    raise ValueError('Describe the remote selection with --scope (1..500 characters).')
                if not 1<=args.lookback_days<=31:raise ValueError('Lookback must be 1..31 days')
                if args.remote=='wispr' and ident!='wispr':raise ValueError('Use source ID wispr for its modified-feed contract.')
                row.update(connector=args.remote,scope=args.scope.strip(),lookback_days=args.lookback_days)
                sources['remote'].append(row)
            detail='Select '+ident+' for '+project['cluster']+'\n'+row.get('path',row.get('scope',''))+'\nRemote login remains in the selected agent profile.'
        else:
            if not rows:raise ValueError('Unknown source ID')
            kind,row=rows[0]
            if args.source_action=='remove':sources[kind].remove(row)
            else:row['enabled']=args.source_action=='enable'
            detail=args.source_action.title()+' source '+ident+'. Existing knowledge and checkpoints are retained.'
        changed=FILES[2]
    elif action=='run':
        automation.require_running(root)
        if feature=='sync':
            require_sync(root)
            plan=ingest._prepare_plan(root,ingest._state(root))
            if not plan['source_ids']:raise ValueError('No enabled sources to sync')
            detail='Run one bounded sync with Codex for '+', '.join(plan['source_ids'])+'.\nConfigured model usage may apply. Profile '+prefs['agent_home']
        else:
            if not prefs['maintenance_enabled']:raise ValueError('Weekly review is disabled. Enable maintenance first.')
            detail='Prepare a local review of up to five notes; no model calls or canonical changes.'
    else:
        raise ValueError('Unknown automation action')
    params={k:v for k,v in vars(args).items() if k not in {'apply','preview','json'}}
    token=hive.digest(json.dumps({'vault':str(root),'before':before,'parameters':params,'provider':registration if action=='provider' else None},sort_keys=True,default=str))
    return {'status':'preview','applied':False,'preview':token,'text':detail,
            'changes':values if changed=='schedule' else {changed:values[changed]} if changed else {} }


def execute(args,root):
    root=require_vault(Path(root).resolve())
    action=getattr(args,'automation_action',None)
    interactive=sys.stdin.isatty() and not getattr(args,'json',False)
    if action is None or action=='setup':
        return menu(root) if interactive else status(root)
    if action=='status':return status(root)
    if action=='review':
        result=maintenance.read(root)
        if result is None:return {'status':'empty','text':'No review yet. Enable weekly review, then run it once in hive automation.'}
        result['text']='Weekly review\n\n'+'\n'.join(row['title']+'\n  '+', '.join(r['kind'] for r in row['reasons'])+'\n  '+row['ref'] for row in result['review']['items'])
        return result
    if action=='serve':
        if not background.config(root)['enabled']:
            raise ValueError('Background scheduling is disabled. Enable background in hive automation.')
        background.run(root)
        return {'status':'stopped','text':'Background scheduler stopped.'}
    result=_plan(root,args)
    if not args.apply and interactive:
        print('\n'+result['text']+'\n')
        try:confirmed=_ask('Apply this change [y/N]').lower() in ('y','yes')
        except (EOFError,KeyboardInterrupt):confirmed=False
        if not confirmed:return {'status':'cancelled','applied':False,'text':'Not changed.'}
        args.apply=True;args.preview=result['preview']
    if not args.apply:return result
    with _control_lock(root):
        current=_plan(root,args)
        if args.preview!=current['preview']:raise ValueError('Settings changed or preview missing; preview this action again.')
        if action=='provider':
            provider_trust.register(root,provider_trust.candidate(root,current['changes'][FILES[0]]))
        for path,value in current['changes'].items():
            # Schedule changes write only actually different files.
            text=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
            if not (root/path).exists() or json.loads((root/path).read_text())!=value:
                hive.atomic(root/path,text)
        if action in ('enable','disable') and args.feature=='background':
            try:
                service=service_control.install(root) if action=='enable' else service_control.uninstall(root)
            except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
                cfg=background.config(root);cfg['enabled']=False
                hive.atomic(root/FILES[1],json.dumps(cfg,indent=2)+'\n')
                raise
            current['service']=service
    if action=='run':
        return jobs.start(root) if args.feature=='sync' else maintenance.run(root)
    current.update(status='applied',applied=True,text='Applied.\n'+current['text'])
    return current


def _ask(label,default=''):
    return input(label+(' ['+default+']' if default else '')+' > ').strip() or default


def menu(root):
    from . import terminal_ui
    terminal_ui.run(root,initial_view='automation')
    return {'status':'closed','text':'Automatik-Einstellungen geschlossen.'}

"""Optional user-configured local Calendar helper; disabled unless explicitly selected."""
import argparse
from datetime import datetime, date
import fcntl
import json
import os
from pathlib import Path
import sys
if __package__ and __package__.startswith('hivemind'):
    from ..runtime_paths import default_vault
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from runtime_paths import default_vault
import plistlib
import re
import subprocess
import tempfile

COMPATIBILITY_APP = Path(os.environ['HIVEMIND_CALENDAR_APP']).expanduser() if os.environ.get('HIVEMIND_CALENDAR_APP') else None
BUNDLE_ID = os.environ.get('HIVEMIND_CALENDAR_BUNDLE_ID')


def _at(value):
    if not isinstance(value, str):
        raise ValueError('Calendar timestamp must be text')
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('Calendar timestamp needs timezone')
    return at


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.agenda-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write('\n');handle.flush();os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):os.unlink(name)


def normalize(payload):
    if not isinstance(payload, dict) or payload.get('status') != 'ok':
        raise ValueError('Calendar access unavailable: '+str(payload.get('status', 'malformed'))+(': '+str(payload['error'])[:500] if payload.get('error') else '') if isinstance(payload,dict) else 'Malformed Calendar response')
    start,end = _at(payload.get('range_start')),_at(payload.get('range_end_exclusive'))
    _at(payload.get('exported_at'))
    if not start < end:
        raise ValueError('Calendar range must be positive')
    if not isinstance(payload.get('system_time_zone'), str) or not payload['system_time_zone']:
        raise ValueError('Missing Calendar timezone')
    events=payload.get('events')
    if not isinstance(events,list) or len(events)>20000:
        raise ValueError('Malformed or excessive Calendar events')
    safe=[];seen=set()
    for event in events:
        if not isinstance(event,dict):raise ValueError('Malformed Calendar event')
        for key in ('key','title','calendar','start','end'):
            if not isinstance(event.get(key),str) or (key!='title' and not event[key]):
                raise ValueError('Malformed Calendar event '+key)
        for key in ('all_day','cancelled'):
            if not isinstance(event.get(key),bool):raise ValueError('Malformed Calendar event '+key)
        event_start,event_end=_at(event['start']),_at(event['end'])
        if event_end < event_start:raise ValueError('Calendar event ends before start')
        if event['key'] in seen:raise ValueError('Duplicate Calendar occurrence key')
        seen.add(event['key'])
        row={key:event[key] for key in ('key','title','calendar','start','end','all_day','cancelled')}
        for key in ('event_id','item_id','calendar_id','time_zone','occurrence','detached','recurring'):
            if key in event and isinstance(event[key],(str,bool,type(None))):row[key]=event[key]
        if event['all_day']:
            first,last=event.get('start_date'),event.get('end_date_exclusive')
            if not isinstance(first,str) or not isinstance(last,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',first) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',last):raise ValueError('Missing or malformed all-day dates')
            if date.fromisoformat(first) >= date.fromisoformat(last):
                raise ValueError('Invalid all-day exclusive range')
            row.update(start_date=first,end_date_exclusive=last)
        safe.append(row)
    return {key:payload[key] for key in ('status','exported_at','range_start','range_end_exclusive','system_time_zone')} | {'events':safe,'compatibility_app':str(COMPATIBILITY_APP) if COMPATIBILITY_APP is not None else None}


def save_result(root,payload):
    """Validate and save an operational snapshot; preserve successful data on failure."""
    root=Path(root);state=root/'.state';state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state/'calendar.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        path=state/'agenda.json'
        try:
            result=normalize(payload)
        except (ValueError,TypeError,KeyError) as exc:
            previous=json.loads(path.read_text()) if path.exists() else {}
            result={**previous,'status':'stale' if previous.get('exported_at') else 'unavailable','error':str(exc),'compatibility_app':str(COMPATIBILITY_APP) if COMPATIBILITY_APP is not None else None}
            result.setdefault('events',[])
            for key in ('exported_at','range_start','range_end_exclusive','system_time_zone'):result.setdefault(key,None)
            # The original export timestamp describes successful data, never this failure.
            result['attempted_at']=datetime.now().astimezone().isoformat()
        _atomic(path,result)
        return result


def refresh(root):
    """No authorize operation, compilation, relocation or TCC dialog is possible here."""
    root=Path(root)
    try:
        if COMPATIBILITY_APP is None or not BUNDLE_ID:
            raise ValueError('Calendar helper is not configured; set HIVEMIND_CALENDAR_APP and HIVEMIND_CALENDAR_BUNDLE_ID explicitly')
        binary=COMPATIBILITY_APP/'Contents/MacOS/CalendarReader'
        if not binary.is_file():raise ValueError('Existing authorized Calendar helper is unavailable; no automatic rebuild')
        info=plistlib.loads((COMPATIBILITY_APP/'Contents/Info.plist').read_bytes())
        if info.get('CFBundleIdentifier') != BUNDLE_ID or info.get('CFBundleExecutable') != 'CalendarReader':
            raise ValueError('Calendar compatibility app identity mismatch')
        runtime=root/'.state';runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,name=tempfile.mkstemp(prefix='.calendar-response-',suffix='.json',dir=runtime);os.close(fd)
        response=Path(name)
        try:
            subprocess.run(['/usr/bin/open','-W','-n',str(COMPATIBILITY_APP),'--args','events','--output',str(response)],check=True,timeout=120,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            if response.stat().st_size>10_000_000:raise ValueError('Excessive Calendar response')
            payload=json.loads(response.read_text())
        finally:
            response.unlink(missing_ok=True)
        return save_result(root,payload)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        return save_result(root,{'status':'error','error':str(exc)})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=default_vault())
    args=parser.parse_args()
    print(json.dumps(refresh(args.root),ensure_ascii=False,indent=2))

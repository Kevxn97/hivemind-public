"""Bounded weekly local evidence review; never invent verification or edit notes."""
import datetime as dt
import json
from pathlib import Path

from . import automation, curation, hive, project_context


def read(root):
    path=Path(root)/'.state/maintenance.json'
    return json.loads(path.read_text()) if path.exists() else None


def due_slot(at, day, hour):
    scheduled=at.replace(hour=hour,minute=0,second=0,microsecond=0)-dt.timedelta(days=(at.weekday()-day)%7)
    if scheduled>at:
        scheduled-=dt.timedelta(days=7)
    return scheduled.date().isoformat()


def run(root, *, at=None, scheduled=False):
    from . import feature_controls
    prefs=feature_controls.settings(root)
    if not prefs['maintenance_enabled']:
        raise ValueError('Weekly review is disabled. Enable maintenance in hive automation.')
    automation.require_running(root)
    at=at or dt.datetime.now().astimezone()
    slot=due_slot(at,prefs['maintenance_day'],prefs['maintenance_hour'])
    previous=read(root)
    if scheduled and previous and previous.get('slot')==slot:
        return previous
    with hive.locked(root,shared=True):
        hive.require_clean(root)
        records=hive.load_notes(root)
        hive.audit(root,records)
    if any(note['sensitivity']!='personal' and hive.active(note) for note,_,_ in records.values()):
        scan=previous.get('review',{}).get('scan',{}).get('next_offset') if previous else None
        # New sources may have been removed since the last bounded scan.
        count=sum(len(note['sources']) for note,_,_ in records.values() if note['sensitivity']!='personal' and hive.active(note))
        review=curation.plan(root,'all',limit=5,max_bytes=24000,scan_offset=scan if scan is not None and scan<=count else 0)
    else:
        review={'items':[],'total_candidates':0,'scan':{'next_offset':None}}
    path=Path(root)/'.state/maintenance.json'
    result={'status':'complete','slot':slot,'finished_at':at.isoformat(),
            'canonical_changes':False,'review':review,'report':str(path),
            'text':f"Local review ready — {len(review['items'])} items. Inspect with hive automation review.\n"
                   'Sources and claims keep their existing evidence status; corrections use the journaled memory workflow.'}
    hive.atomic(path,json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result

"""Canonical project work: explicit deltas, stable IDs and per-item evidence."""
import copy
import hashlib
import json
import re

STATES = {'open', 'waiting', 'done', 'dropped'}
LABELS = {'open': 'Offen', 'waiting': 'Wartet', 'done': 'Erledigt', 'dropped': 'Verworfen'}
ID = re.compile(r'^step-[0-9a-f]{16}$')
FIELDS = {'id', 'text', 'status', 'state', 'source_id', 'observed_at', 'evidence', 'reason'}
PATCH_FIELDS = {'id', 'text', 'status', 'state', 'reason'}


def _text(value, name, limit=800, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError('Invalid work item ' + name)
    if '<!-- HIVEMIND:' in value:
        raise ValueError('Work items cannot contain managed-section markers')


def validate(note):
    if __package__:
        from . import hive
    else:
        import hive
    handoff = note.get('handoff')
    if handoff is not None:
        if not isinstance(handoff, dict):
            raise ValueError('Invalid handoff metadata')
        sources = {s['id'] for s in note['sources']}
        def provenance(value):
            if not isinstance(value, dict) or value.get('source_id') not in sources or value.get('evidence') not in hive.EVIDENCE:
                raise ValueError('Invalid handoff source/evidence')
            hive.timestamp(value.get('observed_at', ''))
        provenance(handoff)
        _text(handoff.get('project_state'), 'project_state', 1600, empty=True)
        for field in ('decisions', 'next_steps', 'open_questions'):
            values = handoff.get(field)
            if not isinstance(values, list) or len(values) > 8:
                raise ValueError('Invalid handoff ' + field)
            for value in values:
                if field == 'open_questions':
                    _text(value, field, 500)
                    continue
                fields = {'decision', 'reason', 'status'} if field == 'decisions' else {'text', 'status'}
                if not isinstance(value, dict) or set(value) != fields or value['status'] not in {'agreed', 'proposed'}:
                    raise ValueError('Invalid handoff ' + field + ' item')
                for key in fields - {'status'}:
                    _text(value[key], key)
        field_sources = handoff.get('field_sources', {})
        if not isinstance(field_sources, dict) or set(field_sources) - {'project_state', 'decisions', 'open_questions'}:
            raise ValueError('Invalid handoff field_sources')
        for value in field_sources.values():
            provenance(value)
    if 'work_items' not in note:
        return
    rows = note['work_items']
    if note['type'] != 'project' or not isinstance(rows, list) or len(rows) > 64:
        raise ValueError('Project work_items must contain at most 64 items; curate completed history explicitly')
    sources = {s['id'] for s in note['sources']}
    seen = set()
    for item in rows:
        if not isinstance(item, dict) or set(item) != FIELDS:
            raise ValueError('Invalid canonical work item fields')
        ident = item['id']
        if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in seen:
            raise ValueError('Invalid or duplicate work item ID')
        seen.add(ident)
        _text(item['text'], 'text')
        _text(item['reason'], 'reason', empty=True)
        if item['status'] not in {'agreed', 'proposed'} or item['state'] not in STATES:
            raise ValueError('Invalid work item status/state')
        if item['state'] == 'done' and item['status'] != 'agreed':
            raise ValueError('A proposed work item cannot be completed without agreement')
        if item['source_id'] not in sources:
            raise ValueError('Work item source is missing')
        if item['evidence'] not in hive.EVIDENCE:
            raise ValueError('Invalid work item evidence')
        hive.timestamp(item['observed_at'])


def _ident(*parts):
    value = json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()
    return 'step-' + hashlib.sha256(value).hexdigest()[:16]


def read(note):
    """Legacy steps get deterministic IDs in memory; retrieval never migrates files."""
    validate(note)
    if 'work_items' in note:
        return copy.deepcopy(note['work_items'])
    handoff = note.get('handoff') or {}
    rows = []
    for item in handoff.get('next_steps', []):
        ident = _ident(note['id'], item['text'], item['status'])
        if any(row['id'] == ident for row in rows):
            continue
        rows.append({'id': ident, 'text': item['text'], 'status': item['status'], 'state': 'open',
                     'source_id': handoff['source_id'], 'observed_at': handoff['observed_at'],
                     'evidence': handoff['evidence'], 'reason': ''})
    if rows:
        validate({**note, 'work_items': rows})
    return rows


def validate_updates(updates):
    if not isinstance(updates, list) or len(updates) > 8:
        raise ValueError('step_updates must contain at most eight explicit changes')
    seen = set()
    for item in updates:
        if not isinstance(item, dict) or not item or set(item) - PATCH_FIELDS:
            raise ValueError('Invalid step_updates fields')
        ident = item.get('id')
        if 'id' in item:
            if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in seen:
                raise ValueError('Invalid or duplicate step update ID')
            seen.add(ident)
            if not (set(item) - {'id', 'reason'}):
                raise ValueError('Step update has no changed fields')
        elif not {'text', 'status'} <= set(item):
            raise ValueError('New steps require text and agreed/proposed status')
        for field in ('text', 'reason'):
            if field in item:
                _text(item[field], field)
        if 'status' in item and item['status'] not in {'agreed', 'proposed'}:
            raise ValueError('Invalid step agreement status')
        if 'state' in item and item['state'] not in STATES:
            raise ValueError('Invalid step lifecycle state')
        if ident is None and item.get('state', 'open') != 'open':
            raise ValueError('New steps start open; update an existing ID to change its lifecycle')


def merge(note, payload):
    rows = read(note)
    updates = payload.get('step_updates', [])
    validate_updates(updates)
    attribution = {'source_id': payload['source']['id'], 'observed_at': payload['source']['observed_at'],
                   'evidence': payload['evidence']}
    # Legacy declarations add work, never replace or reopen existing work.
    for item in payload['next_steps']:
        matching = next((r for r in rows if r['text'] == item['text']), None)
        if matching:
            if matching['status'] != item['status']:
                raise ValueError('Use step_updates with the existing ID and reason to change agreement')
            continue
        rows.append({'id': _ident(note['id'], item['text'], item['status']), **item,
                     'state': 'open', **attribution, 'reason': ''})
    for index, patch in enumerate(updates):
        ident = patch.get('id')
        if ident is None:
            rows.append({'id': _ident(note['id'], payload['session_id'], index),
                         'state': 'open', 'reason': '', **patch, **attribution})
            continue
        current = next((item for item in rows if item['id'] == ident), None)
        if current is None:
            raise ValueError('Unknown work item ID: ' + ident)
        changed = any(current[key] != value for key, value in patch.items() if key != 'reason')
        if not changed:
            continue
        if any(key in patch and patch[key] != current[key] for key in ('state', 'status')) and not patch.get('reason'):
            raise ValueError('Changing step state or agreement requires an attributable reason')
        current.update(patch)
        current['reason'] = patch.get('reason', '')
        current.update(attribution)
    return rows


def line(item):
    agreement = 'vereinbart' if item['status'] == 'agreed' else 'vorgeschlagen'
    return f"{item['id']} · {LABELS[item['state']]} · {agreement}: {item['text']}"

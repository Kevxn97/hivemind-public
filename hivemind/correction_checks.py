"""Observe an explicitly marked correction in actual local session offers."""
import json
from pathlib import Path
import re
import sys

if __package__:
    from . import evidence, hive, insights, sessions
else:
    import evidence
    import hive
    import insights
    import sessions


INDEX = Path('.state/correction-checks/index.json')
CHECK_ID = re.compile(r'[0-9a-f]{16}')
AGENTS = {'codex', 'claude'}


def _read(root, path):
    hive.reject_symlinks(root, path)
    if not path.exists():
        return None
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('Invalid local correction check data')
    return value


def _write(root, path, value):
    hive.reject_symlinks(root, path)
    hive.atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def _index(root):
    state = _read(root, root / INDEX)
    if state is None:
        return {}
    checks = state.get('checks')
    if state.get('version') != 1 or not isinstance(checks, dict):
        raise ValueError('Invalid correction check index')
    for key, row in checks.items():
        if (not CHECK_ID.fullmatch(key) or not isinstance(row, dict) or row.get('id') != key
                or not all(isinstance(row.get(k), str) for k in
                           ('ref', 'source_id', 'question', 'agent', 'cluster', 'armed_at'))
                or not evidence.REFERENCE.fullmatch(row['ref']) or row['agent'] not in AGENTS
                or not row['question'].isprintable() or type(row.get('active')) is not bool):
            raise ValueError('Invalid stored correction check')
        hive.timestamp(row['armed_at'])
    return checks


def _check(checks, ident):
    if ident not in checks:
        raise ValueError('Unknown correction check ID')
    return checks[ident]


def _question_key(value):
    return ' '.join(value.casefold().split())


def _correction(root, reference):
    if not evidence.REFERENCE.fullmatch(reference):
        raise ValueError('Use the exact active NOTE_ID@REVISION from memory show')
    ident, revision = reference.split('@')
    records, history = insights._read(root)
    note, raw, _ = records.get(ident, ({}, '', None))
    if (not note or hive.digest(raw) != revision or note['sensitivity'] != 'internal'
            or not hive.active(note) or not hive.cluster_for(note).startswith('projects/')):
        raise ValueError('Choose a current active project memory at its exact revision')
    entry = next(((event, change) for event in reversed(history)
                  for change in event['changes'] if change['id'] == ident), None)
    if entry is None or entry[1]['before'] is None:
        raise ValueError('Choose a journaled content correction to an existing memory')
    event, change = entry
    before = hive.decode(change['before'])
    source = note['sources'][0]
    if (change['after'] != raw
            or all(before[key] == note[key] for key in ('summary', 'body'))
            or not re.fullmatch(r'correction-[a-f0-9]{16}', source['id'])
            or source['resource'] != event['context']
            or source['title'] != 'User correction · ' + event['reason']):
        raise ValueError('Choose a revision saved by a sourced memory edit that corrected its content')
    return hive.cluster_for(note), source['id']


def add(root, reference, question, agent):
    root = Path(root).resolve()
    question = question.strip()
    if not question or len(question) > 500 or not question.isprintable() or agent not in AGENTS:
        raise ValueError('Choose Codex or Claude and enter a single-line question of 1..500 characters')
    ident = hive.digest(json.dumps([reference, agent, _question_key(question)], ensure_ascii=False))[:16]
    with sessions._locked(root):
        cluster, source_id = _correction(root, reference)
        checks = _index(root)
        if ident not in checks or not checks[ident]['active']:
            checks[ident] = {'id': ident, 'ref': reference, 'cluster': cluster, 'source_id': source_id,
                             'question': question, 'agent': agent, 'active': True, 'armed_at': sessions._now()}
            _write(root, root / INDEX, {'version': 1, 'checks': checks})
        check = checks[ident]
    return {'check': check, 'text': f"Watching {reference} for {agent}. Check {ident}.\n{check['question']}"}


def list_checks(root):
    root = Path(root).resolve()
    with sessions._locked(root):
        rows = list(_index(root).values())
    lines = [f"{c['id']} · {c['agent']} · {'active' if c['active'] else 'stopped'} · {c['question']}" for c in rows]
    return {'checks': rows, 'text': '\n'.join(lines) or 'No correction checks yet.'}


def stop(root, ident):
    root = Path(root).resolve()
    with sessions._locked(root):
        checks = _index(root)
        check = _check(checks, ident)
        if check['active']:
            check['active'] = False
            _write(root, root / INDEX, {'version': 1, 'checks': checks})
    return {'check': check, 'text': f'Check {ident} stopped. Earlier observations are available.'}


def _run_path(root, ident, session_id):
    if not CHECK_ID.fullmatch(ident) or not sessions.SESSION_ID.fullmatch(session_id):
        raise ValueError('Invalid correction check or session ID')
    return (root / INDEX).parent / ident / (session_id + '.json')


def _read_run(root, ident, session_id):
    row = _read(root, _run_path(root, ident, session_id))
    if row is not None and (row.get('check_id') != ident or row.get('session_id') != session_id
                            or not {'agent', 'opened_at', 'startup', 'question_context', 'review'} <= row.keys()):
        raise ValueError('Invalid correction check observation')
    return row


def show(root, ident):
    root = Path(root).resolve()
    with sessions._locked(root):
        check = _check(_index(root), ident)
        folder = (root / INDEX).parent / ident
        hive.reject_symlinks(root, folder)
        runs = sorted((_read_run(root, ident, path.stem) for path in folder.glob('*.json')),
                      key=lambda row: row['opened_at'], reverse=True)
    state = 'active' if check['active'] else 'stopped'
    lines = [f"Check {ident} · {check['agent']} · {state}", check['question'],
             f"Expected {check['ref']} · source {check['source_id']}",
             f"Inspect with hive evidence {check['ref']} --source {check['source_id']}"]
    for run in runs:
        lines.append(f"Session {run['session_id']} · {run['opened_at']}")
        for field, label in (('startup', 'Startup'), ('question_context', 'Latest matching follow-up')):
            observed = run[field]
            if observed:
                lines.append(f"  {label}: {observed['status']} · {observed['observed_ref'] or '—'} · source {observed['source_id'] or '—'}")
        report = run['review']
        lines.append('  Awaiting your answer review.' if report is None else
                     f"  Your review: used {'yes' if report['used'] else 'no'}, corrected again {'yes' if report['corrected_again'] else 'no'}.")
    if not runs:
        lines.append('Waiting for a matching fresh session.')
    return {'check': check, 'runs': runs, 'text': '\n'.join(lines)}


def review(root, ident, session_id, used, corrected_again):
    root = Path(root).resolve()
    if type(used) is not bool or type(corrected_again) is not bool:
        raise ValueError('Provide both observed answers, yes or no')
    with sessions._locked(root):
        _check(_index(root), ident)
        run = _read_run(root, ident, session_id)
        if run is None:
            raise ValueError('This check has no observation for that session')
        report = {'used': used, 'corrected_again': corrected_again}
        existing = run['review']
        if existing is None:
            run['review'] = {**report, 'actor': 'user', 'at': sessions._now()}
            _write(root, _run_path(root, ident, session_id), run)
        elif any(existing[key] != value for key, value in report.items()):
            raise ValueError('A different answer review is already recorded for that session')
    return {'run': run, 'review': run['review'], 'text': f'Answer review recorded for session {session_id}.'}


def observe(root, session, task, stage, blocks, rendered):
    """Called under the session lock, after the ordinary receipt has been saved."""
    try:
        if not (root / INDEX).exists():
            return
        checks = _index(root)
        question = _question_key(task)
        delivered = {b['ref'].split('@')[0]: b for b in blocks if b['ref'] in rendered}
        for check in checks.values():
            if (not check['active'] or check['cluster'] != session['cluster'] or check['agent'] != session['agent']
                    or hive.timestamp(session['opened_at']) <= hive.timestamp(check['armed_at'])):
                continue
            matches = question == _question_key(check['question'])
            if not matches and (question or stage != 'startup'):
                continue
            block = delivered.get(check['ref'].split('@')[0])
            if block is None:
                status = 'not_offered'
            elif block['ref'] != check['ref']:
                status = 'different_revision'
            elif block['source_id'] != check['source_id']:
                status = 'different_source'
            else:
                status = 'offered'
            observed = {'status': status, 'observed_ref': block['ref'] if block else None,
                        'source_id': block['source_id'] if block else None}
            ident, sid = check['id'], session['session_id']
            run = _read_run(root, ident, sid) or {
                'check_id': ident, 'session_id': sid, 'agent': session['agent'],
                'opened_at': session['opened_at'], 'startup': None, 'question_context': None, 'review': None}
            fields = []
            if stage == 'startup' and run['startup'] is None:
                fields.append('startup')
            if matches and run['question_context'] != observed:
                fields.append('question_context')
            if fields and run['review'] is None:
                for field in fields:
                    run[field] = observed
                _write(root, _run_path(root, ident, sid), run)
    except (OSError, ValueError, TypeError, RecursionError):
        # Optional observation state must never prevent normal context delivery.
        print('HiveMind could not record a correction check. Inspect hive memory check list or show.', file=sys.stderr)

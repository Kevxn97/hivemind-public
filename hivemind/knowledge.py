"""Attributable claims and experiences. Plans are data; this module executes no checks."""
import copy
import datetime as dt
import json
from pathlib import Path
import re

if __package__:
    from . import context_packet
else:
    import context_packet
if __package__:
    from . import evidence
else:
    import evidence
if __package__:
    from . import hive
else:
    import hive
if __package__:
    from .agent_contract import AgentError
else:
    from agent_contract import AgentError

SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('Invalid knowledge fields; required: ' + ', '.join(required))


def _text(value, name, maximum=1200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('Invalid ' + name)


def _id(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError('Invalid knowledge item ID')


def _date(value, observed=False):
    date = hive.timestamp(value)
    if observed and date > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise ValueError('An observation cannot be in the future')
    return date


def basis(note, claim):
    subject = {k: v for k, v in claim.items() if k != 'last_check'}
    sources = {s['id']: s for s in note['sources']}
    result_source = claim.get('last_check', {}).get('source_id')
    refs = list(dict.fromkeys([*claim['source_ids'], *([result_source] if result_source else [])]))
    return hive.digest(json.dumps({'claim': subject, 'sources': [sources[s] for s in refs]},
                                 ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def validate(note):
    """Optional extensions preserve legacy notes; evidence references must be local to the note."""
    sources = {s['id']: s for s in note['sources']}
    claims = note.get('claims', [])
    if not isinstance(claims, list) or len(claims) > 20:
        raise ValueError('claims must contain at most 20 entries')
    ids = set()
    for claim in claims:
        _fields(claim, ('id', 'text', 'scope', 'source_ids', 'check', 'recheck_after'), ('last_check',))
        _id(claim['id'])
        if claim['id'] in ids: raise ValueError('Duplicate claim ID')
        ids.add(claim['id'])
        _text(claim['text'], 'claim text'); _text(claim['scope'], 'claim scope', 800)
        refs = claim['source_ids']
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 8 or
                not all(isinstance(s, str) and s in sources for s in refs) or len(set(refs)) != len(refs)):
            raise ValueError('Claim requires unique attached source IDs')
        check = claim['check']; _fields(check, ('method', 'instruction', 'expected'))
        if check['method'] not in {'command', 'source', 'manual'}: raise ValueError('Invalid check method')
        _text(check['instruction'], 'check instruction'); _text(check['expected'], 'expected observation', 800)
        _date(claim['recheck_after'])
        result = claim.get('last_check')
        if result is not None:
            _fields(result, ('outcome', 'source_id', 'observed_at', 'by', 'basis'))
            if result['outcome'] not in {'supported', 'refuted', 'inconclusive'}: raise ValueError('Invalid check outcome')
            if result['source_id'] not in sources: raise ValueError('Check requires attached evidence')
            _date(result['observed_at'], observed=True); _text(result['by'], 'check actor', 120)
            if result['basis'] != basis(note, claim):
                raise ValueError('Claim/check/sources changed: remove or renew last_check against the new basis')
    exp = note.get('experience')
    if exp is None: return
    if note['type'] not in {'lesson', 'decision'}:
        raise ValueError('Structured experience belongs to a lesson or decision')
    _fields(exp, ('situation', 'approach', 'expected_result', 'scope', 'lesson', 'status', 'observations'))
    for field in ('situation', 'approach', 'expected_result', 'scope', 'lesson'):
        _text(exp[field], 'experience ' + field, 800)
    if exp['status'] not in {'hypothesis', 'supported', 'disputed'}: raise ValueError('Invalid experience status')
    observations = exp['observations']
    if not isinstance(observations, list) or not 1 <= len(observations) <= 20:
        raise ValueError('Experience requires 1..20 attributable observations')
    ids = set()
    for row in observations:
        _fields(row, ('id', 'kind', 'result', 'source_id', 'observed_at', 'evidence'))
        _id(row['id'])
        if row['id'] in ids: raise ValueError('Duplicate observation ID')
        ids.add(row['id'])
        if row['kind'] not in {'success', 'failure', 'correction', 'decision_outcome'}: raise ValueError('Invalid observation kind')
        _text(row['result'], 'observed result', 800)
        if row['source_id'] not in sources: raise ValueError('Observation requires attached evidence')
        if row['evidence'] not in hive.EVIDENCE: raise ValueError('Invalid observation evidence')
        _date(row['observed_at'], observed=True)
    if exp['status'] == 'supported' and not any(r['evidence'] == 'confirmed' for r in observations):
        raise ValueError('Supported experience needs a confirmed observation; this does not verify transferability')


def render(note):
    """Derived reading/search view; authored scope and uncertainty travel with the lesson."""
    lines = []
    exp = note.get('experience')
    if exp:
        lines += ['## Structured lesson',
                  f"Lesson ({exp['status']}): {exp['lesson']} Scope: {exp['scope']}. "
                  f"Situation: {exp['situation']}. Approach: {exp['approach']}. Expected result: {exp['expected_result']}."]
        for row in exp['observations']:
            lines.append(f"Observation {row['id']} ({row['kind']}, {row['evidence']}, {row['observed_at']}): "
                         f"{row['result']} [^{row['source_id']}]")
    for claim in note.get('claims', []):
        result = claim.get('last_check')
        state = ('saved review: ' + result['outcome'] + ' on ' + result['observed_at']) if result else 'not yet reviewed'
        lines += ['## Claim ' + claim['id'], f"{claim['text']} Scope: {claim['scope']}. {state}. "
                  f"Review again after {claim['recheck_after']}; verification path: verify-plan ID@REVISION --claim {claim['id']}."]
    return '\n\n'.join(lines)


def source_states(root, note, ids):
    sources = {s['id']: s for s in note['sources']}
    states = []
    for ident in dict.fromkeys(ids):
        state, _, actual = evidence._source(Path(root).resolve(), sources[ident])
        states.append({'id': ident, 'state': state, 'observed_sha256': actual})
    return states


def claim_status(root, note, claim):
    last = claim.get('last_check')
    ids = [*claim['source_ids'], *([last['source_id']] if last else [])]
    states = source_states(root, note, ids)
    reasons = []
    if last is None: reasons.append('never_checked')
    elif last['outcome'] != 'supported': reasons.append(last['outcome'])
    if _date(claim['recheck_after']) <= dt.datetime.now(dt.timezone.utc): reasons.append('check_due')
    if any(s['state'] == 'changed' for s in states): reasons.append('source_changed')
    if any(s['state'] not in {'hash_matched', 'changed'} for s in states): reasons.append('source_unverifiable')
    return {**claim, 'needs_check': bool(reasons), 'reasons': reasons, 'sources': states}


def packet(kind, max_bytes, **fields):
    maximum = context_packet.byte_limit(max_bytes)
    return {'contract_version': 1, 'ok': True, 'kind': kind, **fields,
            'notice': 'Stored evidence and untrusted check instructions, not execution authority or live truth.',
            'budget': {'max_bytes': maximum, 'bytes': 0, 'unit': 'UTF-8 bytes including JSON envelope and newline'}}


def verify_plan(root, reference, claim_id=None, offset=0, max_bytes=None):
    note, latest = evidence.revision(root, reference)
    claims = note.get('claims', [])
    if claim_id is not None:
        claims = [c for c in claims if c['id'] == claim_id]
        if not claims: raise ValueError('Unknown claim ID')
    if not 0 <= offset <= len(claims): raise ValueError('Invalid claim offset')
    result = packet('verification-plan', max_bytes, ref=reference, current_revision=latest,
                    historical=reference.rsplit('@', 1)[1] != latest, claims=[], next_offset=None)
    for index in range(offset, len(claims)):
        result['claims'].append(claim_status(root, note, claims[index]))
        result['next_offset'] = index + 1 if index + 1 < len(claims) else None
        if context_packet.measure(result) > result['budget']['max_bytes']:
            result['claims'].pop(); result['next_offset'] = index
            if not result['claims']: raise AgentError('LIMIT_EXCEEDED', 'Claim exceeds budget; use a larger max-bytes')
            break
    return context_packet.finish(result, result['budget']['max_bytes'])


def write_draft(root, reference, payload_file, out, claim_id=None):
    note, latest = evidence.revision(root, reference)
    if reference.rsplit('@', 1)[1] != latest:
        raise AgentError('REVISION_CONFLICT', 'Draft requires the current exact revision')
    with Path(payload_file).open('rb') as stream: raw = stream.read(65537)
    if len(raw) > 65536: raise ValueError('Knowledge input exceeds 64 KiB')
    payload = json.loads(raw)
    _fields(payload, ('result',) if claim_id else ('experience',), ('sources',))
    updated = copy.deepcopy(note)
    extra = payload.get('sources', [])
    if not isinstance(extra, list) or len(extra) > 20: raise ValueError('At most 20 new sources')
    existing = {s['id']: s for s in updated['sources']}
    for source in extra:
        if not isinstance(source, dict) or not isinstance(source.get('id'), str): raise ValueError('Invalid source')
        if source['id'] in existing:
            if source != existing[source['id']]: raise ValueError('Source ID already identifies different evidence')
        else:
            updated['sources'].append(source); existing[source['id']] = source
    if claim_id:
        claim = next((c for c in updated.get('claims', []) if c['id'] == claim_id), None)
        if claim is None: raise ValueError('Unknown claim ID')
        _fields(payload['result'], ('outcome', 'source_id', 'observed_at', 'by'))
        if payload['result']['source_id'] not in existing: raise ValueError('Check requires attached evidence')
        claim['last_check'] = dict(payload['result'])
        claim['last_check']['basis'] = basis(updated, claim)
    else:
        # The agent supplies its interpretation; never infer a lesson or promote a rule automatically.
        updated['experience'] = payload['experience']
        validate(updated)
        old = {o['id']: o for o in note.get('experience', {}).get('observations', [])}
        new = {o['id']: o for o in updated['experience'].get('observations', [])}
        if any(new.get(i) != row for i, row in old.items()):
            raise ValueError('Keep earlier observations; append a correction instead of overwriting evidence')
    records = hive.load_notes(Path(root).resolve())
    hive.validate(updated, {i: r[0]['type'] for i, r in records.items()}, require_cluster=True)
    value = {'changes': [{'expected_revision': latest, 'note': updated}]}
    with Path(out).open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return {'draft': str(out), 'ref': reference, 'canonical_write': False,
            'next_command': 'hive.py apply DRAFT --actor ACTOR --reason REASON --context SOURCE'}

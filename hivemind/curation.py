"""Bounded, local care candidates and evidence-bound deferrals; no semantic auto-writes."""
import datetime as dt
import json
from pathlib import Path

if __package__:
    from . import context_packet
else:
    import context_packet
if __package__:
    from .agent_contract import AgentError
else:
    from agent_contract import AgentError
if __package__:
    from . import evidence
else:
    import evidence
if __package__:
    from . import hive
else:
    import hive
if __package__:
    from . import insights
else:
    import insights
if __package__:
    from . import knowledge
else:
    import knowledge
if __package__:
    from . import sessions
else:
    import sessions


def _state(root):
    path = Path(root) / '.state/knowledge-reviews.json'
    if not path.exists(): return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict): raise AgentError('INVALID_STATE', 'Invalid knowledge review state')
    for ident, row in value.items():
        if not isinstance(row, dict) or not {'fingerprint', 'retry_after', 'ref', 'source_id', 'reason', 'at'} <= row.keys():
            raise AgentError('INVALID_STATE', 'Invalid knowledge review receipt')
        hive.timestamp(row['retry_after'])
    return value


def _feedback(root, scope):
    paths = sorted((Path(root) / '.state/sessions').glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    gaps, used = {}, {}
    for path in paths[:500]:
        row = sessions._read(root, path.stem)
        if scope and row['cluster'] != scope: continue
        latest = {report['actor']: report for report in row.get('feedback', [])}
        for report in latest.values():
            for ref in report['used']:
                used.setdefault(ref['id'], set()).add(row['session_id'])
            if report['outcome'] == 'unhelpful' and report['missing'] != 'none':
                gaps.setdefault((row['cluster'], report['missing']), set()).add(row['session_id'])
    return gaps, used, max(0, len(paths) - 500)


def _candidates(root, records, scope, scan_offset=0):
    gaps, usage, omitted_receipts = _feedback(root, scope)
    visible = {i: r for i, r in insights._allowed(records).items() if not scope or hive.in_cluster(r[0], scope)}
    leads = {}
    for ident, (note, _, _) in sorted(visible.items(), key=lambda r: (r[0] != hive.cluster_for(r[1][0]).split('/')[-1], r[0])):
        if note['type'] == 'project': leads.setdefault(hive.cluster_for(note), ident)
    ordered_sources = [(ident, source['id']) for ident, (note, _, _) in sorted(visible.items())
                       for source in reversed(note['sources'])]
    if not 0 <= scan_offset <= len(ordered_sources): raise ValueError('Invalid source scan offset')
    selected_sources = set(ordered_sources[scan_offset:scan_offset + 64])
    next_scan = scan_offset + 64 if scan_offset + 64 < len(ordered_sources) else None
    result = []
    for ident, (note, raw, _) in sorted(visible.items()):
        reasons, source_fingerprints, priority = [], [], 10
        if note['evidence'] == 'disputed': reasons.append({'kind': 'disputed'}); priority = 0
        if hive.freshness(note) == 'stale': reasons.append({'kind': 'review_due'}); priority = min(priority, 4)
        for source in reversed(note['sources']):
            if (ident, source['id']) not in selected_sources: continue
            # The evidence reader hashes only bounded attached local text; no external source import.
            state, _, actual = evidence._source(Path(root).resolve(), source)
            source_fingerprints.append({'id': source['id'], 'state': state, 'sha256': actual})
            if state == 'changed':
                reasons.append({'kind': 'source_changed', 'source_id': source['id']}); priority = min(priority, 1)
            elif state == 'missing':
                reasons.append({'kind': 'source_missing', 'source_id': source['id']}); priority = min(priority, 2)
        for claim in note.get('claims', []):
            check = claim.get('last_check')
            if not check or check['outcome'] != 'supported' or hive.timestamp(claim['recheck_after']) <= dt.datetime.now(dt.timezone.utc):
                reasons.append({'kind': 'claim_check', 'claim_id': claim['id'], 'outcome': check['outcome'] if check else 'never_checked'})
                priority = min(priority, 0 if check and check['outcome'] == 'refuted' else 3)
        exp = note.get('experience')
        if exp:
            for row in exp['observations']:
                if row['kind'] in {'correction', 'failure', 'decision_outcome'}:
                    reasons.append({'kind': row['kind'], 'observation_id': row['id'], 'source_id': row['source_id']})
                    priority = min(priority, 1 if row['kind'] == 'correction' else 3)
            if exp['status'] in {'hypothesis', 'disputed'}:
                reasons.append({'kind': 'experience_' + exp['status']}); priority = min(priority, 2)
        gap_evidence = []
        for (cluster, category), ids in sorted(gaps.items()):
            if ident == leads.get(cluster) and len(ids) >= 2:
                reasons.append({'kind': 'recurring_gap', 'category': category, 'sessions': len(ids)})
                gap_evidence.append([category, sorted(ids)]); priority = min(priority, 2)
        if not reasons: continue
        ref = ident + '@' + hive.digest(raw)
        fingerprint = hive.digest(json.dumps({'ref': ref, 'reasons': reasons, 'sources': source_fingerprints,
                                             'gaps': gap_evidence}, sort_keys=True, separators=(',', ':')))
        result.append((priority, -len(usage.get(ident, [])), ident,
                       {'id': ident, 'ref': ref, 'title': note['title'][:120], 'cluster': hive.cluster_for(note),
                        'fingerprint': fingerprint, 'reasons': reasons, 'reported_use_sessions': len(usage.get(ident, [])),
                        'source_states': source_fingerprints,
                        'review_scope': scope or 'all', 'scan_offset': scan_offset,
                        'next_commands': ['evidence ' + ref, 'verify-plan ' + ref, 'hive.py draft ' + ident + ' --out DRAFT']}))
    return [r[3] for r in sorted(result)], omitted_receipts, {'offset': scan_offset, 'next_offset': next_scan,
            'total_sources': len(ordered_sources), 'sources_probed': len(selected_sources)}


def plan(root, target='.', limit=5, offset=0, max_bytes=None, scan_offset=0):
    insights._limit(limit)
    scope = insights._scope(root, target)
    records, _ = insights._read(root)
    if not any(hive.in_cluster(r[0], scope) if scope else True for r in insights._allowed(records).values()):
        raise ValueError('Unknown or empty review scope')
    candidates, receipts, scan = _candidates(root, records, scope, scan_offset)
    state = _state(root); now = dt.datetime.now(dt.timezone.utc)
    items = [r for r in candidates if not (r['fingerprint'] in state
                                          and hive.timestamp(state[r['fingerprint']]['retry_after']) > now)]
    if not 0 <= offset <= len(items): raise ValueError('Invalid review offset')
    result = knowledge.packet('review-plan', max_bytes, cluster=scope, items=[], total_candidates=len(items),
                              suppressed=len(candidates) - len(items), next_offset=None,
                              scan={'receipt_limit': 500, 'receipts_omitted': receipts, 'source_probe_limit': 64, **scan},
                              instructions=['Read the exact note and sources; check claim scope and verification path.',
                                            'Keep conflicting evidence explicit. Suggest only evidenced corrections, merges or useful links.',
                                            'Use draft/apply for canonical edits. Unchanged or deferred candidates can be recorded with evidence and a retry date.'])
    for index in range(offset, min(len(items), offset + limit)):
        result['items'].append(items[index]); result['next_offset'] = index + 1 if index + 1 < len(items) else None
        if context_packet.measure(result) > result['budget']['max_bytes']:
            result['items'].pop(); result['next_offset'] = index
            if not result['items']: raise AgentError('LIMIT_EXCEEDED', 'Candidate exceeds budget; increase max-bytes')
            break
    return context_packet.finish(result, result['budget']['max_bytes'])


def record(root, reference, fingerprint, source_id, reason, retry_after, scan_scope=None, scan_offset=0):
    knowledge._text(reason, 'review reason', 280)
    until = hive.timestamp(retry_after); now = dt.datetime.now(dt.timezone.utc)
    if not now < until <= now + dt.timedelta(days=90): raise ValueError('Retry date must be in the next 90 days')
    match = evidence.REFERENCE.fullmatch(reference)
    if not match: raise ValueError('Use exact NOTE_ID@REVISION')
    root = Path(root).resolve()
    with hive.locked(root):
        hive.require_clean(root); records = hive.load_notes(root); hive.audit(root, records)
        for note, _, _ in records.values(): hive.validate(note, {i: r[0]['type'] for i, r in records.items()})
        row = records.get(match[1])
        if not row or row[0]['sensitivity'] == 'personal': raise ValueError('Unavailable review note')
        if hive.digest(row[1]) != match[2]: raise AgentError('REVISION_CONFLICT', 'Review note changed')
        if source_id not in {s['id'] for s in row[0]['sources']}: raise ValueError('Review requires an attached source')
        scope = hive.cluster_for(row[0]) if scan_scope is None else None if scan_scope == 'all' else scan_scope
        if scope: hive.validate_scope(scope)
        candidates, _, _ = _candidates(root, records, scope, scan_offset)
        current = next((c for c in candidates if c['ref'] == reference), None)
        if not current or current['fingerprint'] != fingerprint:
            raise AgentError('REVISION_CONFLICT', 'Review evidence changed; obtain a fresh plan')
        state = _state(root)
        receipt = {'ref': reference, 'fingerprint': fingerprint, 'source_id': source_id, 'reason': reason,
                   'retry_after': retry_after, 'at': now.isoformat(), 'scan_scope': scope or 'all', 'scan_offset': scan_offset}
        state[fingerprint] = receipt
        state = dict(sorted(state.items(), key=lambda r: r[1]['at'])[-512:])
        path = root / '.state/knowledge-reviews.json'; path.parent.mkdir(exist_ok=True)
        hive.atomic(path, json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    return {'recorded': receipt, 'canonical_write': False,
            'notice': 'A deferred review, not verified knowledge. Changed evidence reopens the candidate.'}

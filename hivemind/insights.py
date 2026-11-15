"""Bounded local decision views over recorded knowledge; never invent graph edges."""
from collections import deque
import datetime as dt
from pathlib import Path
import re

if __package__:
    from . import hive
else:
    import hive


def _limit(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise ValueError('Choose a limit between 1 and 5')
    return value


def _read(root):
    root = Path(root).resolve()
    with hive.locked(root, shared=(root / '.hive.lock').exists()):
        hive.require_clean(root)
        records = hive.load_notes(root)
        types = {i: r[0]['type'] for i, r in records.items()}
        for note, _, _ in records.values():
            hive.validate(note, types)
        hive.audit(root, records)
        history = hive.events(root)
    return records, history


def _allowed(records, include_history=False):
    return {i: r for i, r in records.items()
            if r[0]['sensitivity'] != 'personal' and (include_history or hive.active(r[0]))}


def _scope(root, target):
    target = str(target)
    if target == 'all':
        return None
    if target.startswith('projects/'):
        hive.validate_scope(target)
        return target
    if __package__:
        from . import project_context
    else:
        import project_context
    result = project_context.resolve_project(root, target)
    if result is None:
        raise ValueError('No unique project. Register a folder or use projects/<id>.')
    return result['cluster']


def _excerpt(body, words=120):
    # Prefer an explicitly authored rationale, never synthesize one from links.
    blocks = re.split(r'(?m)(?=^#{1,6} )', body)
    chosen = next((b for b in blocks if re.match(r'#{1,6}\s+.*(?:Begründung|Rationale|Warum|Reason|Why)', b, re.I)), body)
    text = chosen.strip()
    return ' '.join(text.split()[:words]) + (' …' if len(text.split()) > words else '')


def _item(record, **extra):
    note, raw, _ = record
    result = {'id': note['id'], 'title': note['title'], 'type': note['type'],
            'cluster': hive.cluster_for(note), 'summary': note['summary'],
            'revision': hive.digest(raw), 'status': note['status'], 'evidence': note['evidence'],
            'freshness': hive.freshness(note), 'stale_after': note['stale_after'],
            'sources': note['sources'], 'verified': note.get('verified', []), **extra}
    handoff = note.get('handoff')
    if isinstance(handoff, dict):
        result['handoff_evidence'] = handoff.get('evidence', 'reported')
        result['handoff_observed_at'] = handoff.get('observed_at')
        result['handoff_source_id'] = handoff.get('source_id')
    if note.get('experience'):
        result['experience'] = note['experience']
    return result


def _edge(origin, edge, direction):
    return {'origin': origin, 'relation': edge['relation'], 'target': edge['target'],
            'reason': edge.get('reason', ''), 'direction': direction}


def _path_text(path):
    labels = {'depends_on': 'depends on', 'related': 'linked to', 'about': 'about',
              'supersedes': 'supersedes', 'contradicts': 'contradicts'}
    return ' / '.join(f"{e['origin']} —{labels[e['relation']]}→ {e['target']}" +
                      (f" ({e['reason']})" if e['reason'] else '') for e in path)


def _render(title, introduction, items):
    lines = [title, '', introduction, '']
    for item in items:
        status = {'stable': 'stable', 'draft': 'draft', 'deprecated': 'deprecated'}[item['status']]
        evidence = hive.EVIDENCE_LABELS[item['evidence']]
        freshness = 'review due' if item['freshness'] == 'stale' else 'within review period'
        lines += [item['title'] + ' [' + item['id'] + ']',
                  f"Note: {status} · {evidence} · {freshness} · Revision {item['revision'][:12]}"]
        if 'handoff_evidence' in item:
            lines.append('Current handoff: ' + hive.EVIDENCE_LABELS.get(item['handoff_evidence'], 'unclassified') +
                         ' · observed ' + str(item.get('handoff_observed_at') or 'undated') +
                         ' · source ' + str(item.get('handoff_source_id') or 'unassigned') +
                         '. The note status does not verify this handoff.')
        if item.get('reasons'):
            lines.append(' / '.join(item['reasons']))
        if item.get('path'):
            lines.append('Documented path: ' + _path_text(item['path']))
        if item.get('excerpt'):
            lines.append(item['excerpt'])
        else:
            lines.append(item['summary'])
        if item.get('event'):
            event = item['event']
            lines.append('Change ' + event['at'] + ': ' + event['reason'])
            lines.append('Event: ' + event['id'] + ' · ' + event['actor'])
            lines.append('Context: ' + event['context'])
        cited = set(re.findall(r'\[\^([^\]]+)\]', item.get('excerpt', '')))
        if item.get('handoff_source_id'):
            cited.add(item['handoff_source_id'])
        ordered_sources = sorted(item['sources'], key=lambda source:
                                 (source['id'] != item.get('handoff_source_id'), source['id'] not in cited))
        for source in ordered_sources[:3]:
            lines.append('Source: ' + source['title'] + ' · ' + source['resource'])
        if len(item['sources']) > 3:
            lines.append(f"{len(item['sources']) - 3} more sources via hive note {item['id']}.")
        lines.append('')
    if not items:
        lines.append('No matching documented entries.')
    lines.append('Stored claims and relationships; not proof of the current live state.')
    return '\n'.join(lines) + '\n'


def explain(root, ident, limit=5):
    """Show authored rationale and direct typed links, with exact revisions and sources."""
    _limit(limit)
    records, _ = _read(root)
    visible = _allowed(records, include_history=True)
    if ident not in visible:
        raise ValueError('Note missing or marked as personal: ' + ident)
    selected = _item(visible[ident], excerpt=_excerpt(visible[ident][0]['body']), path=[])
    links = []
    for origin, (note, _, _) in visible.items():
        for edge in note['links']:
            if origin == ident and edge['target'] in visible:
                links.append((edge['target'], _edge(origin, edge, 'outgoing')))
            elif edge['target'] == ident:
                links.append((origin, _edge(origin, edge, 'incoming')))
    priority = {'contradicts': 0, 'supersedes': 1, 'depends_on': 2, 'about': 3, 'related': 4}
    links.sort(key=lambda row: (priority[row[1]['relation']], not row[1]['reason'], row[0]))
    items, seen = [selected], {ident}
    for other, edge in links:
        if other in seen or len(items) >= limit:
            continue
        seen.add(other)
        items.append(_item(visible[other], path=[edge], excerpt=_excerpt(visible[other][0]['body'], 70)))
    title = 'WHY / ' + selected['title']
    introduction = 'Documented text and links. A link alone does not prove why a decision was made.'
    return {'kind': 'explain', 'title': title, 'subject': selected['id'], 'items': items,
            'text': _render(title, introduction, items)}


def impact(root, ident, limit=5, depth=4):
    """Follow only incoming dependencies; report direct contradiction/replacement separately."""
    _limit(limit)
    if not isinstance(depth, int) or not 1 <= depth <= 4:
        raise ValueError('Choose a depth between 1 and 4')
    records, _ = _read(root)
    all_visible = _allowed(records, include_history=True)
    if ident not in all_visible:
        raise ValueError('Note missing or marked as personal: ' + ident)
    visible = _allowed(records)
    visible[ident] = all_visible[ident]
    incoming = {}
    for origin, (note, _, _) in visible.items():
        for edge in note['links']:
            if edge['relation'] == 'depends_on' and edge['target'] in visible:
                incoming.setdefault(edge['target'], []).append((origin, edge))
    queue, seen, items = deque([(ident, [])]), {ident}, []
    while queue and len(items) < limit:
        current, path = queue.popleft()
        if len(path) >= depth:
            continue
        for origin, edge in sorted(incoming.get(current, []), key=lambda row: row[0]):
            if origin in seen:
                continue
            seen.add(origin)
            next_path = path + [_edge(origin, edge, 'incoming')]
            items.append(_item(visible[origin], path=next_path,
                               reasons=['Depends directly or indirectly on the selected note.']))
            queue.append((origin, next_path))
            if len(items) >= limit:
                break
    # Conflict/replacement is evidence to inspect, not a transitive dependency.
    for origin, (note, _, _) in sorted(visible.items()):
        for edge in note['links']:
            if edge['relation'] not in {'contradicts', 'supersedes'}:
                continue
            other = edge['target'] if origin == ident else origin if edge['target'] == ident else None
            if other not in all_visible or other in seen or len(items) >= limit:
                continue
            seen.add(other)
            label = ('Contradiction documented; check its resolution.' if edge['relation'] == 'contradicts' else
                     'This note supersedes the selected note.' if edge['target'] == ident else
                     'This note is superseded by the selected note.')
            items.append(_item(all_visible[other], path=[_edge(origin, edge, 'outgoing' if origin == ident else 'incoming')], reasons=[label]))
    title = 'IMPACT / ' + all_visible[ident][0]['title']
    introduction = 'Possible impact: these notes depend on, contradict, or supersede the selected note. No state is changed automatically.'
    return {'kind': 'impact', 'title': title, 'subject': ident, 'items': items, 'depth': depth,
            'text': _render(title, introduction, items)}


def lessons(root, target='.', task='', limit=5):
    """Find cross-project lessons over up to two recorded links; mark search-only candidates."""
    _limit(limit)
    scope = _scope(root, target)
    if scope is None:
        raise ValueError('Lessons require a specific project.')
    records, _ = _read(root)
    visible = _allowed(records)
    starts = sorted(i for i, r in visible.items() if hive.cluster_for(r[0]) == scope)
    if not starts:
        raise ValueError('Unknown or empty project: ' + scope)
    neighbors = {}
    for origin, (note, _, _) in visible.items():
        for edge in note['links']:
            other = edge['target']
            if other in visible and edge['relation'] in {'related', 'about', 'depends_on'}:
                neighbors.setdefault(origin, []).append((other, _edge(origin, edge, 'outgoing')))
                neighbors.setdefault(other, []).append((origin, _edge(origin, edge, 'incoming')))
    queue = deque((i, []) for i in starts)
    seen = set(starts)
    candidates = {i: [] for i in starts if visible[i][0]['type'] == 'lesson' or visible[i][0].get('experience')}
    while queue:
        current, path = queue.popleft()
        if len(path) >= 2:
            continue
        for other, edge in sorted(neighbors.get(current, []), key=lambda row: (row[0], row[1]['relation'])):
            if other in seen:
                continue
            seen.add(other)
            next_path = path + [edge]
            queue.append((other, next_path))
            if visible[other][0]['type'] == 'lesson' and hive.cluster_for(visible[other][0]) != scope:
                candidates[other] = next_path
    hits = [row['id'] for row in hive.search(Path(root).resolve(), task, limit=100)] if task.strip() else []
    for ident in hits:
        if ident in visible and visible[ident][0]['type'] == 'lesson' and hive.cluster_for(visible[ident][0]) != scope:
            candidates.setdefault(ident, [])
    order = sorted(candidates, key=lambda i: (i not in hits if task.strip() else False, not candidates[i], len(candidates[i]), i))
    if __package__:
        from . import knowledge
    else:
        import knowledge
    items = [_item(visible[i], path=candidates[i], excerpt=_excerpt(knowledge.render(visible[i][0]) + '\n\n' + visible[i][0]['body']),
                   reasons=['Lesson from this project.' if i in starts else 'Documented connection to the project.' if candidates[i] else 'Search candidate; no saved project connection.'])
             for i in order[:limit]]
    title = 'LESSONS / ' + scope
    return {'kind': 'lessons', 'title': title, 'cluster': scope, 'task': task, 'items': items,
            'text': _render(title, 'Check applicability against the source; no new relationship was saved.', items)}


def changes(root, target='.', since=None, limit=5, session_id=None):
    """At most five changed/contested/due/open records; read-only, no review-date reset."""
    _limit(limit)
    if since is not None and session_id is not None:
        raise ValueError('Choose either since or session_id')
    scope = _scope(root, target)
    if session_id is not None and scope is None:
        raise ValueError('A session comparison needs its project scope')
    records, history = _read(root)
    visible = {i: r for i, r in _allowed(records, include_history=True).items() if scope is None or hive.cluster_for(r[0]) == scope}
    if scope is not None and not visible:
        raise ValueError('Unknown or empty project: ' + scope)
    baseline = 'explicit' if since else 'recent_window'
    if since is None and scope:
        if __package__:
            from . import sessions
        else:
            import sessions
        latest = sessions.context_baseline(root, scope)
        session = sessions.status(root, session_id or latest['session_id']) if session_id or latest else None
        if session:
            if session['cluster'] != scope:
                raise ValueError('Session belongs to a different project')
            # Opening a new session must not erase the previous visit's baseline.
            # An explicit session ID always asks what preceded that session.
            if session_id or session['status'] == 'context_offered':
                since = session.get('previous_context_at')
                if since:
                    baseline = 'previous_context_offered'
            else:
                since, baseline = session['opened_at'], 'last_context_offered'
    if since is None:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec='seconds')
    boundary = hive.timestamp(since)
    recent = {}
    for event in history:
        if hive.timestamp(event['at']) <= boundary:
            continue
        for change in event['changes']:
            if change['id'] in visible:
                recent[change['id']] = {key: event[key] for key in ('id', 'at', 'actor', 'reason', 'context')}
    candidates = []
    for ident, record in visible.items():
        note = record[0]
        if not hive.active(note) and ident not in recent:
            continue
        reasons, priority = [], 9
        if not hive.active(note):
            reasons.append('Archived or marked deprecated since the baseline.')
        if hive.active(note) and note['evidence'] == 'disputed':
            reasons.append('Resolve disputed evidence.'); priority = 0
        if hive.active(note) and note['type'] == 'decision' and note['status'] == 'draft':
            reasons.append('Decision is still a draft.'); priority = min(priority, 1)
        if hive.active(note) and hive.freshness(note) == 'stale':
            reasons.append('Review overdue.'); priority = min(priority, 2)
        if ident in recent:
            reasons.append('Changed since the baseline.'); priority = min(priority, 3)
        # Explicit authored heading is a cue to inspect, never a fabricated blocking task.
        if hive.active(note) and re.search(r'^#{1,6}\s+.*(?:offene Fragen|offene Punkte|nächste Schritte|open questions|open items|next steps)', note['body'], re.I | re.M):
            reasons.append('Contains a section with next steps or open questions.'); priority = min(priority, 4)
        if reasons:
            candidates.append((priority, ident, _item(record, reasons=reasons, event=recent.get(ident))))
    candidates.sort(key=lambda row: (row[0], -(hive.timestamp(row[2]['event']['at']).timestamp() if row[2]['event'] else 0), row[1]))
    items = [row[2] for row in candidates[:limit]]
    title = 'CHANGES / ' + (scope or 'All work contexts')
    label = {'explicit': 'Selected baseline', 'recent_window': 'No prior session baseline: last seven days',
             'last_context_offered': 'Since project context was last offered',
             'previous_context_offered': 'Since the project context before this session'}[baseline]
    return {'kind': 'changes', 'title': title, 'cluster': scope, 'since': since, 'baseline': baseline,
            'total_candidates': len(candidates), 'items': items,
            'text': _render(title, label + ': ' + since + '. Also includes open and due entries.', items)}

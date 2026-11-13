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
        raise ValueError('Kein eindeutiges Projekt. Ordner registrieren oder projects/<id> verwenden.')
    return result['cluster']


def _excerpt(body, words=120):
    # Prefer an explicitly authored rationale, never synthesize one from links.
    blocks = re.split(r'(?m)(?=^#{1,6} )', body)
    chosen = next((b for b in blocks if re.match(r'#{1,6}\s+.*(?:Begründung|Rationale|Warum)', b, re.I)), body)
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
    labels = {'depends_on': 'hängt ab von', 'related': 'verknüpft mit', 'about': 'bezieht sich auf',
              'supersedes': 'ersetzt', 'contradicts': 'widerspricht'}
    return ' / '.join(f"{e['origin']} —{labels[e['relation']]}→ {e['target']}" +
                      (f" ({e['reason']})" if e['reason'] else '') for e in path)


def _render(title, introduction, items):
    lines = [title, '', introduction, '']
    for item in items:
        status = {'stable': 'angenommen', 'draft': 'Entwurf', 'deprecated': 'überholt'}[item['status']]
        evidence = hive.GERMAN_EVIDENCE[item['evidence']]
        freshness = 'Prüfung fällig' if item['freshness'] == 'stale' else 'im Prüfzeitraum'
        lines += [item['title'] + ' [' + item['id'] + ']',
                  f"Notiz: {status} · {evidence} · {freshness} · Revision {item['revision'][:12]}"]
        if 'handoff_evidence' in item:
            lines.append('Aktuelle Übergabe: ' + hive.GERMAN_EVIDENCE.get(item['handoff_evidence'], 'nicht eingeordnet') +
                         ' · Beobachtung ' + str(item.get('handoff_observed_at') or 'nicht datiert') +
                         ' · Quelle ' + str(item.get('handoff_source_id') or 'nicht zugeordnet') +
                         '. Der Notizstatus verifiziert diese Übergabe nicht.')
        if item.get('reasons'):
            lines.append(' / '.join(item['reasons']))
        if item.get('path'):
            lines.append('Belegter Pfad: ' + _path_text(item['path']))
        if item.get('excerpt'):
            lines.append(item['excerpt'])
        else:
            lines.append(item['summary'])
        if item.get('event'):
            event = item['event']
            lines.append('Änderung ' + event['at'] + ': ' + event['reason'])
            lines.append('Ereignis: ' + event['id'] + ' · ' + event['actor'])
            lines.append('Kontext: ' + event['context'])
        cited = set(re.findall(r'\[\^([^\]]+)\]', item.get('excerpt', '')))
        if item.get('handoff_source_id'):
            cited.add(item['handoff_source_id'])
        ordered_sources = sorted(item['sources'], key=lambda source:
                                 (source['id'] != item.get('handoff_source_id'), source['id'] not in cited))
        for source in ordered_sources[:3]:
            lines.append('Quelle: ' + source['title'] + ' · ' + source['resource'])
        if len(item['sources']) > 3:
            lines.append(f"Weitere {len(item['sources']) - 3} Quellen über hive note {item['id']}.")
        lines.append('')
    if not items:
        lines.append('Keine passenden belegten Einträge.')
    lines.append('Gespeicherte Aussagen und Beziehungen; kein Nachweis des aktuellen Live-Zustands.')
    return '\n'.join(lines) + '\n'


def explain(root, ident, limit=5):
    """Show authored rationale and direct typed links, with exact revisions and sources."""
    _limit(limit)
    records, _ = _read(root)
    visible = _allowed(records, include_history=True)
    if ident not in visible:
        raise ValueError('Notiz fehlt oder ist als persönlich markiert: ' + ident)
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
    title = 'WARUM / ' + selected['title']
    introduction = 'Dokumentierter Text und Verknüpfungen. Eine Verknüpfung allein beweist keine Entscheidungsursache.'
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
        raise ValueError('Notiz fehlt oder ist als persönlich markiert: ' + ident)
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
                               reasons=['Hängt direkt oder mittelbar von der ausgewählten Notiz ab.']))
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
            label = ('Dokumentierter Widerspruch; Auflösung prüfen.' if edge['relation'] == 'contradicts' else
                     'Diese Notiz ersetzt die ausgewählte Notiz.' if edge['target'] == ident else
                     'Diese Notiz wird durch die ausgewählte Notiz ersetzt.')
            items.append(_item(all_visible[other], path=[_edge(origin, edge, 'outgoing' if origin == ident else 'incoming')], reasons=[label]))
    title = 'AUSWIRKUNG / ' + all_visible[ident][0]['title']
    introduction = 'Mögliche Betroffenheit: Diese Notizen hängen von der ausgewählten Notiz ab oder stehen mit ihr im Widerspruch bzw. ersetzen sie. Keine automatische Zustandsänderung.'
    return {'kind': 'impact', 'title': title, 'subject': ident, 'items': items, 'depth': depth,
            'text': _render(title, introduction, items)}


def lessons(root, target='.', task='', limit=5):
    """Find cross-project lessons over up to two recorded links; mark search-only candidates."""
    _limit(limit)
    scope = _scope(root, target)
    if scope is None:
        raise ValueError('Erfahrungen benötigen ein konkretes Projekt.')
    records, _ = _read(root)
    visible = _allowed(records)
    starts = sorted(i for i, r in visible.items() if hive.cluster_for(r[0]) == scope)
    if not starts:
        raise ValueError('Unbekanntes oder leeres Projekt: ' + scope)
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
                   reasons=['Erfahrung aus diesem Projekt.' if i in starts else 'Belegte Verbindung zum Projekt.' if candidates[i] else 'Suchkandidat; keine gespeicherte Projektverbindung.'])
             for i in order[:limit]]
    title = 'ERFAHRUNGEN / ' + scope
    return {'kind': 'lessons', 'title': title, 'cluster': scope, 'task': task, 'items': items,
            'text': _render(title, 'Übertragbarkeit anhand der Quelle prüfen; keine neue Beziehung wurde gespeichert.', items)}


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
        raise ValueError('Unbekanntes oder leeres Projekt: ' + scope)
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
            reasons.append('Seit dem Vergleichsstand archiviert oder als überholt markiert.')
        if hive.active(note) and note['evidence'] == 'disputed':
            reasons.append('Widersprüchliche Evidenz klären.'); priority = 0
        if hive.active(note) and note['type'] == 'decision' and note['status'] == 'draft':
            reasons.append('Entscheidung ist noch ein Entwurf.'); priority = min(priority, 1)
        if hive.active(note) and hive.freshness(note) == 'stale':
            reasons.append('Prüftermin überschritten.'); priority = min(priority, 2)
        if ident in recent:
            reasons.append('Seit dem Vergleichsstand geändert.'); priority = min(priority, 3)
        # Explicit authored heading is a cue to inspect, never a fabricated blocking task.
        if hive.active(note) and re.search(r'^#{1,6}\s+.*(?:offene Fragen|offene Punkte|nächste Schritte)', note['body'], re.I | re.M):
            reasons.append('Enthält einen Abschnitt mit nächsten Schritten oder offenen Fragen.'); priority = min(priority, 4)
        if reasons:
            candidates.append((priority, ident, _item(record, reasons=reasons, event=recent.get(ident))))
    candidates.sort(key=lambda row: (row[0], -(hive.timestamp(row[2]['event']['at']).timestamp() if row[2]['event'] else 0), row[1]))
    items = [row[2] for row in candidates[:limit]]
    title = 'ÄNDERUNGEN / ' + (scope or 'Alle Arbeitskontexte')
    label = {'explicit': 'Gewählter Vergleichsstand', 'recent_window': 'Ohne bisherigen Sitzungsstand: letzte sieben Tage',
             'last_context_offered': 'Seit zuletzt angebotenem Projektkontext',
             'previous_context_offered': 'Seit dem Projektkontext vor dieser Sitzung'}[baseline]
    return {'kind': 'changes', 'title': title, 'cluster': scope, 'since': since, 'baseline': baseline,
            'total_candidates': len(candidates), 'items': items,
            'text': _render(title, label + ': ' + since + '. Zusätzlich offene und fällige Einträge.', items)}

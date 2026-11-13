"""Local project routing and task-aware, evidence-labelled context packets.

Canonical knowledge remains in the journalled wiki. Machine-specific folder
bindings live in .state/project-paths.json; producing a brief never writes them.
The word budget applies to the complete rendered ``text``, including evidence
labels and source references. ``selected`` supplies unabridged machine-readable
provenance, not an additional prompt to append to that text.
"""
import json
from pathlib import Path
import re

if __package__:
    from . import hive
else:
    import hive


REGISTRY = Path('.state/project-paths.json')
MIN_BUDGET = 120
MAX_BUDGET = 2000


def _records(root):
    with hive.locked(root, shared=(root / '.hive.lock').exists()):
        hive.require_clean(root)
        records = hive.load_notes(root)
        hive.audit(root, records)
    return records


def _visible(note):
    return hive.active(note) and note['sensitivity'] != 'personal'


def _valid_project(cluster, notes):
    hive.validate_scope(cluster)
    if not cluster.startswith('projects/'):
        raise ValueError('Choose a projects/<id> cluster')
    if not any(_visible(n) and hive.cluster_for(n) == cluster for n in notes.values()):
        raise ValueError('Unknown or inactive project: ' + cluster)


def _registry(root):
    path = root / REGISTRY
    if not path.exists():
        return {'version': 1, 'mappings': []}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('mappings'), list):
        raise ValueError('Invalid project registry')
    seen = set()
    for row in data['mappings']:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not isinstance(row.get('cluster'), str):
            raise ValueError('Invalid project mapping')
        path = Path(row['path'])
        if not path.is_absolute() or str(path.resolve()) != row['path']:
            raise ValueError('Project paths must be absolute and resolved')
        home = Path.home().resolve()
        if path == home or path in home.parents:
            raise ValueError('Project mappings cannot claim the home or root directory')
        hive.validate_scope(row['cluster'])
        if not row['cluster'].startswith('projects/') or row['path'] in seen:
            raise ValueError('Duplicate or invalid project mapping')
        seen.add(row['path'])
    return data


def _legacy_enabled(root):
    path = root / 'config/projects.json'
    if not path.exists():
        return True
    data = json.loads(path.read_text())
    if data.get('version') != 1 or not isinstance(data.get('legacy_fallback', True), bool):
        raise ValueError('Invalid project configuration')
    return data.get('legacy_fallback', True)


def _resolve(root, target, notes):
    target = str(target)
    if target.startswith('projects/'):
        _valid_project(target, notes)
        return {'cluster': target, 'folder': None, 'method': 'cluster'}
    start = Path(target).expanduser().resolve()
    mappings = {Path(row['path']): row['cluster'] for row in _registry(root)['mappings']}
    # Explicit nested repositories are intentional: the most specific binding
    # wins. An invalid explicit binding must not fall through to legacy guesses.
    folder = next((p for p in (start, *start.parents) if p in mappings), None)
    if folder is not None:
        _valid_project(mappings[folder], notes)
        return {'cluster': mappings[folder], 'folder': str(folder), 'method': 'registry'}
    if not _legacy_enabled(root):
        return None
    points = hive.entry_points({i: n for i, n in notes.items() if _visible(n)})
    # An explicit mapping for a different project must not accidentally expose
    # its parent as legacy project context outside the registered subfolder.
    points = {p: c for p, c in points.items()
              if not any(p in child.parents and other != c for child, other in mappings.items())}
    folder = next((p for p in (start, *start.parents) if p in points), None)
    if folder is None:
        return None
    return {'cluster': points[folder], 'folder': str(folder), 'method': 'legacy'}


def resolve_project(root, target='.'):
    """Resolve a stable project cluster, preferring explicit local bindings."""
    root = Path(root).resolve()
    notes = {i: record[0] for i, record in _records(root).items()}
    return _resolve(root, target, notes)


def register_project(root, target, cluster):
    """Bind a local folder without writing a machine path into canonical notes."""
    root = Path(root).resolve()
    folder = Path(target).expanduser().resolve()
    home = Path.home().resolve()
    if not folder.is_dir() or folder == home or folder in home.parents:
        raise ValueError('Choose an existing project folder, not the home or root directory')
    with hive.locked(root):
        hive.require_clean(root)
        records = hive.load_notes(root)
        hive.audit(root, records)
        _valid_project(cluster, {i: row[0] for i, row in records.items()})
        data = _registry(root)
        existing = next((r for r in data['mappings'] if r['path'] == str(folder)), None)
        if existing and existing['cluster'] != cluster:
            raise ValueError('Folder already belongs to ' + existing['cluster'] + '; unregister it first')
        if existing is None:
            data['mappings'].append({'path': str(folder), 'cluster': cluster})
            data['mappings'].sort(key=lambda row: row['path'])
            hive.atomic(root / REGISTRY, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return {'cluster': cluster, 'folder': str(folder), 'registered': True, 'changed': existing is None}


def unregister_project(root, target):
    root = Path(root).resolve()
    folder = str(Path(target).expanduser().resolve())
    with hive.locked(root):
        data = _registry(root)
        before = len(data['mappings'])
        data['mappings'] = [r for r in data['mappings'] if r['path'] != folder]
        changed = before != len(data['mappings'])
        if changed:
            hive.atomic(root / REGISTRY, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return {'folder': folder, 'unregistered': changed}


def list_projects(root):
    root = Path(root).resolve()
    notes = {i: row[0] for i, row in _records(root).items()}
    result = []
    for row in _registry(root)['mappings']:
        current = any(_visible(n) and hive.cluster_for(n) == row['cluster'] for n in notes.values())
        result.append({**row, 'folder': row['path'], 'exists': Path(row['path']).is_dir(), 'active': current})
    return result


def _short(text, words):
    parts = str(text).split()
    return ' '.join(parts[:words]) + (' …' if len(parts) > words else '')


def _match(text, terms):
    return len(hive.matched_terms(terms, text))


def _paragraphs(body):
    """Keep section names beside excerpts, but don't imply claim-level citations."""
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', hive.FOOTNOTE.sub('', body))
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    heading = ''
    result = []
    for raw in text.split('\n\n'):
        lines = raw.strip().splitlines()
        while lines and lines[0].startswith('#'):
            heading = lines.pop(0).lstrip('#').strip()
        paragraph = '\n'.join(lines).strip()
        if paragraph:
            result.append((heading, paragraph))
    return result


def _handoff(note):
    """A handoff has its own evidential scope, independent of older note claims."""
    value = note.get('handoff')
    if not isinstance(value, dict):
        return None
    if value.get('evidence') not in hive.EVIDENCE or not isinstance(value.get('observed_at'), str):
        raise ValueError('Invalid handoff evidence: ' + note['id'])
    hive.timestamp(value['observed_at'])
    if not any(s['id'] == value.get('source_id') for s in note['sources']):
        raise ValueError('Missing handoff source: ' + note['id'])
    for field in ('decisions', 'open_questions', 'next_steps'):
        if not isinstance(value.get(field), list):
            raise ValueError('Invalid handoff ' + field + ': ' + note['id'])
    if not isinstance(value.get('project_state'), str):
        raise ValueError('Invalid handoff project state: ' + note['id'])
    if 'work_items' in note:
        if __package__:
            from . import work_items
        else:
            import work_items
        value = {**value, 'next_steps': [item for item in work_items.read(note) if item['state'] in {'open', 'waiting'}]}
    return value


def _handoff_excerpt(handoff, words):
    """Cover the important categories before spending budget on long prose."""
    blocks = []
    def evidence(field, item=None):
        origin = item if item and 'source_id' in item else handoff.get('field_sources', {}).get(field, handoff)
        if all(origin.get(key) == handoff.get(key) for key in ('source_id', 'evidence', 'observed_at')):
            return ''
        return (' [Quelle ' + origin['source_id'] + '; ' + hive.GERMAN_EVIDENCE[origin['evidence']] +
                '; ' + origin['observed_at'] + ']')
    decisions = handoff['decisions']
    if decisions:
        item = decisions[0]
        label = 'Vereinbart' if item['status'] == 'agreed' else 'Vorgeschlagen, nicht beschlossen'
        # Decision and reason share a budget so a long decision cannot hide why.
        blocks.append('Entscheidung (' + label + ')' + evidence('decisions') + ': ' + _short(item['decision'], 28)
                      + ' Begründung: ' + _short(item['reason'], 22))
    if handoff['next_steps']:
        item = handoff['next_steps'][0]
        label = 'vereinbart' if item['status'] == 'agreed' else 'vorgeschlagen'
        if item.get('state') == 'waiting':
            label += ', wartet'
        blocks.append('Nächster Schritt (' + label + ')' + evidence('next_steps', item) + ': ' + _short(item['text'], 30))
    if handoff['open_questions']:
        blocks.append('Offene Frage' + evidence('open_questions') + ': ' + _short(handoff['open_questions'][0], 25))
    if handoff['project_state'].strip():
        blocks.append('Projektstand' + evidence('project_state') + ': ' + _short(handoff['project_state'], 45))
    for item in decisions[1:]:
        label = 'vereinbart' if item['status'] == 'agreed' else 'vorgeschlagen'
        blocks.append('Weitere Entscheidung (' + label + ')' + evidence('decisions') + ': ' + _short(item['decision'], 20)
                      + ' Begründung: ' + _short(item['reason'], 15))
    text = '\n\n'.join(blocks)
    return text if len(text.split()) <= words else _short(text, max(1, words - 1))


def _excerpt(note, terms, words):
    if __package__:
        from . import knowledge
    else:
        import knowledge
    handoff = _handoff(note)
    chosen, remaining = [], words
    body = knowledge.render(note) + '\n\n' + note['body']
    if handoff:
        current = _handoff_excerpt(handoff, min(words, 230))
        if current:
            chosen.append(current)
            remaining -= len(current.split())
        body = re.sub(r'<!-- HIVEMIND:HANDOFF:START -->.*?<!-- HIVEMIND:HANDOFF:END -->', '', body, flags=re.S)
    paragraphs = _paragraphs(body)
    ranked = sorted(enumerate(paragraphs), key=lambda row: (-_match(' '.join(row[1]), terms), row[0])) if terms else list(enumerate(paragraphs))
    for _, (heading, paragraph) in ranked:
        if remaining < 8:
            break
        block = (heading + ': ' if heading else '') + paragraph
        chosen.append(block if len(block.split()) <= remaining else _short(block, max(1, remaining - 1)))
        remaining -= len(chosen[-1].split())
        if len(chosen) == 3:
            break
    return '\n\n'.join(chosen)


def _provenance(note, text, reason):
    result = {'id': note['id'], 'revision': hive.digest(text), 'title': note['title'],
            'cluster': hive.cluster_for(note), 'type': note['type'], 'reason': reason,
            'status': note['status'], 'evidence': note['evidence'],
            'freshness': hive.freshness(note), 'stale_after': note['stale_after'],
            'generated': note['generated'], 'verified': note.get('verified', []),
            'sources': note['sources']}
    handoff = _handoff(note)
    if handoff:
        result['handoff_evidence'] = {key: handoff[key] for key in ('evidence', 'source_id', 'observed_at')}
    return result


def _metadata(item):
    fresh = 'Prüftermin überschritten' if item['freshness'] == 'stale' else 'im Prüfintervall'
    handoff = item.get('handoff_evidence')
    source = next(s for s in item['sources'] if s['id'] == handoff['source_id']) if handoff else item['sources'][0]
    lines = ['## ' + _short(item['title'], 8),
             f"`{item['id']}@{item['revision']}`",
             f"Notizevidenz: {hive.GERMAN_EVIDENCE[item['evidence']]} · Status: {item['status']} · {fresh}; fällig {item['stale_after']}.",
             'Auswahl: ' + _short(item['reason'], 12),
             ('Quelle der Übergabe: ' if handoff else 'Quelle auf Notizebene: ') + _short(source['resource'], 12) + ' [' + source['id'] + ']']
    if handoff:
        lines.insert(3, 'Aktuelle Übergabe: ' + hive.GERMAN_EVIDENCE[handoff['evidence']]
                     + ' · beobachtet ' + handoff['observed_at'] + '; fortgeführte Felder behalten ihre eigenen Belege.')
    if len(item['sources']) > 1:
        lines[-1] += f"; {len(item['sources']) - 1} weitere in `hive note {item['id']}`."
    if item['verified']:
        last = max(item['verified'], key=lambda v: hive.timestamp(v['at']))
        lines.append(('Ältere Notizprüfung: ' if handoff else 'Prüfung laut Notiz: ') + last['at'] + '. Umfang: ' + _short(last.get('scope', 'nicht näher angegeben'), 18))
    else:
        lines.append('Keine gesonderte Verifikation dokumentiert.')
    return '\n'.join(lines)


def task_brief(root, target='.', task='', budget=1200, include_blocks=False):
    """Select context for a task. Unknown folders return None; no model is called."""
    if isinstance(budget, bool) or not isinstance(budget, int) or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise ValueError(f'Context budget must be {MIN_BUDGET}..{MAX_BUDGET} words')
    if not isinstance(task, str) or len(task) > 2000:
        raise ValueError('Task must be text of at most 2000 characters')
    root = Path(root).resolve()
    records = _records(root)
    notes = {i: row[0] for i, row in records.items()}
    route = _resolve(root, target, notes)
    if route is None:
        return None
    scope = route['cluster']
    members = sorted((n for n in notes.values() if _visible(n) and hive.cluster_for(n) == scope),
                     key=lambda n: (n['type'] != 'project', n['id'] != scope.split('/')[-1], n['id']))
    lead = members[0]
    member_ids = {n['id'] for n in members}
    linked = {}
    for origin, n in notes.items():
        if not _visible(n):
            continue
        for edge in n['links']:
            other = edge['target'] if origin in member_ids else origin if edge['target'] in member_ids else None
            if other is not None and other not in member_ids and other in notes and _visible(notes[other]):
                linked.setdefault(other, []).append(edge)
    terms = hive.query_terms(task)
    candidates = [(lead, 'Projektüberblick als gemeinsamer Ausgangspunkt', 0)]
    if task.strip():
        # Scoped search protects project details from being crowded out by global
        # hits. Cross-project matches are limited to reusable decisions/lessons
        # or an already recorded link, never an invented relationship.
        hits = {}
        for row in hive.search(root, task, limit=20, cluster=scope) + hive.search(root, task, limit=20):
            hits.setdefault(row['id'], row)
        ranked = []
        for ident, hit in hits.items():
            n = notes.get(ident)
            if n is None or not _visible(n) or ident == lead['id']:
                continue
            local = ident in member_ids
            if not local and ident not in linked and n['type'] not in {'lesson', 'decision'}:
                continue
            if __package__:
                from . import knowledge
            else:
                import knowledge
            score = _match(n['title'] + ' ' + n['summary'] + ' ' + n['body'] + ' ' + knowledge.render(n), terms)
            if not score:
                continue
            reason = 'Aufgabenbezug im Projekt' if local else 'Aufgabenbezug über belegte Querverbindung' if ident in linked else 'Passende Erfahrung/Entscheidung aus anderem Kontext; keine Projektabhängigkeit behauptet'
            ranked.append((n, reason, score, local))
        ranked.sort(key=lambda r: (-r[2], not r[3], r[0]['type'] not in {'decision', 'lesson'}, r[0]['id']))
        candidates += [(n, reason, score) for n, reason, score, _ in ranked[:7]]
    else:
        remaining = [n for n in members if n['id'] != lead['id']]
        remaining.sort(key=lambda n: (n['type'] not in {'decision', 'lesson'}, n['evidence'] != 'disputed', n['id']))
        candidates += [(n, 'Entscheidung oder Kontext im selben Projekt', 0) for n in remaining[:5]]
        external = sorted((notes[i] for i in linked), key=lambda n: (not any(e['relation'] in hive.STRONG for e in linked[n['id']]), n['type'] not in {'decision', 'lesson'}, n['id']))
        candidates += [(n, 'Belegte Querverbindung zum Projekt', 0) for n in external[:2]]
    header = '# Projektkontext · ' + scope
    if task.strip():
        header += '\nAufgabe: ' + _short(task, 16)
    footer = 'Gespeicherter Kontext, kein Live-Stand. Bestätigung gilt nur im dokumentierten Prüfumfang. Quellen gelten auf Notizebene; weiterführende Details mit `hive note ID`.'
    blocks, selected, reading_blocks = [], [], []
    remaining = budget - len(header.split()) - len(footer.split())
    for index, (n, reason, score) in enumerate(candidates):
        item = _provenance(n, records[n['id']][1], reason)
        if __package__:
            from . import knowledge
        else:
            import knowledge
        item['matched_terms'] = [term for term in terms if _match(n['title'] + ' ' + n['summary'] + ' ' + n['body'] + ' ' + knowledge.render(n), [term])]
        meta = _metadata(item)
        needed = len(meta.split())
        if remaining - needed < 10:
            if not selected:
                raise ValueError('Context budget too small for project provenance; choose a larger budget')
            continue
        later = len(candidates) - index - 1
        allowance = min(350 if n.get('handoff') else 220, remaining - needed,
                        max(180 if n.get('handoff') else 35, (remaining - needed) // (min(later, 3) + 1)))
        body = _excerpt(n, terms, allowance)
        if not body:
            body = _short(n['summary'], max(1, allowance - 1))
        block = meta + '\n\n' + body
        blocks.append(block)
        selected.append(item)
        reading_blocks.append({'ref': item['id'] + '@' + item['revision'], 'metadata': meta,
                               'paragraphs': body.split('\n\n')})
        remaining -= len(block.split())
    no_match = bool(task.strip()) and not any(i['matched_terms'] for i in selected)
    if no_match:
        notice = 'Keine passenden Aufgabenbelege gefunden; nur Projektüberblick.'
        if len(notice.split()) <= remaining:
            blocks.append(notice)
    text = '\n\n'.join([header, *blocks, footer]) + '\n'
    return {**route, 'task': task, 'budget': budget, 'word_count': len(text.split()),
            'text': text, 'selected': selected, 'task_match': not no_match if task.strip() else None,
            **({'_reading_blocks': reading_blocks} if include_blocks else {})}

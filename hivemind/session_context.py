"""A bounded working packet shared by session entry and task refresh."""
import datetime as dt
from pathlib import Path

if __package__:
    from . import hive
else:
    import hive
if __package__:
    from . import insights
else:
    import insights
if __package__:
    from . import project_context
else:
    import project_context
if __package__:
    from . import work_items
else:
    import work_items


def build(root, target, task, budget, previous_at=None):
    root = Path(root).resolve()
    if isinstance(budget, bool) or not isinstance(budget, int) or not 120 <= budget <= 2000:
        raise ValueError('Context budget must be 120..2000 words')
    route = project_context.resolve_project(root, target)
    if route is None:
        raise ValueError('No unique HiveMind project for this folder')
    records = project_context._records(root)
    projects = sorted((record for record in records.values() if record[0]['type'] == 'project'
                       and hive.cluster_for(record[0]) == route['cluster'] and project_context._visible(record[0])),
                      key=lambda r: (r[0]['id'] != route['cluster'].split('/')[-1], r[0]['id']))
    if not projects:
        raise ValueError('No active internal project note in this project')
    note, raw, _ = projects[0]
    items = work_items.read(note)
    handoff = project_context._handoff(note) or {}
    handoff_origin = {key: handoff.get(key) for key in ('source_id', 'observed_at', 'evidence')}
    field_sources = {field: handoff.get('field_sources', {}).get(field, handoff_origin)
                     for field in ('project_state', 'decisions', 'open_questions') if handoff.get(field)}
    # Canonical events have second precision. Round the prior offer down and
    # include that second; its changes are candidates, never proof of reading.
    boundary = hive.timestamp(previous_at) - dt.timedelta(seconds=1) if previous_at else dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    changes = insights.changes(root, route['cluster'], since=boundary.isoformat(), limit=5)
    changes['baseline'] = 'previous_context_offered' if previous_at else 'first_use_recent_window'
    changes['precision'] = 'Includes the boundary second; inspect event and revision before assuming a change is new.'
    lessons = insights.lessons(root, route['cluster'], task=task, limit=3)
    gaps = []
    if not handoff.get('project_state'):
        gaps.append('Kein strukturierter Projektstand; Projekttext und Quellen prüfen.')
    if not handoff.get('decisions'):
        gaps.append('Keine Entscheidungen in der letzten Übergabe; weitere Belege im Projektkontext prüfen.')
    if not items:
        gaps.append('Keine erfassten Arbeitsschritte; daraus folgt nicht, dass nichts offen ist.')
    if hive.freshness(note) == 'stale':
        gaps.append('Prüftermin überschritten; veränderliche Aussagen vor Verwendung prüfen.')
    workflow = {'version': 1, 'project': {'id': note['id'], 'revision': hive.digest(raw),
                'summary': note['summary'], 'project_state': handoff.get('project_state', ''),
                'decisions': handoff.get('decisions', []), 'open_questions': handoff.get('open_questions', []),
                'field_sources': field_sources, 'handoff_provenance': handoff_origin if handoff else None,
                'sources': note['sources'], 'note_evidence': note['evidence'], 'freshness': hive.freshness(note)},
                'work_items': items, 'open_work': [i for i in items if i['state'] in {'open', 'waiting'}],
                'changes': changes, 'lessons': lessons, 'gaps': gaps,
                'next_commands': ['session-context SESSION_ID --task TASK --budget 1200',
                                  'session-draft SESSION_ID --out UNIQUE_FILE',
                                  'session-close SESSION_ID --file UNIQUE_FILE',
                                  'session-close SESSION_ID --no-change']}
    reserve = min(480, max(0, budget - 180), budget // 2)
    lines = ['## Arbeit für die Sitzung']
    for item in workflow['open_work'][:8]:
        text = project_context._short(item['text'], 24)
        lines.append(f"- {item['id']} · {work_items.LABELS[item['state']]} · {item['status']}: {text} "
                     f"[Quelle {item['source_id']}; {item['evidence']}; {item['observed_at']}]")
    if len(workflow['open_work']) > 8:
        lines.append('Weitere offene Schritte im strukturierten workflow.open_work.')
    if not workflow['open_work']:
        lines.append('Keine erfassten offenen Schritte; dies ist kein Vollständigkeitsnachweis.')
    lines.append('## Änderungen / ' + ('seit dem letzten Kontextangebot' if previous_at else 'erster Einstieg, letzte sieben Tage'))
    for item in changes['items'][:3]:
        lines.append('- ' + item['id'] + ': ' + ' '.join(item['reasons']) + ' @' + item['revision'][:12])
    if not changes['items']:
        lines.append('Keine passenden Änderungen erfasst.')
    for item in lessons['items'][:2]:
        lines.append('Erfahrung: ' + item['id'] + ' — ' + project_context._short(item['summary'], 18) +
                     '; Übertragbarkeit prüfen, Belege in workflow.lessons.')
    if gaps:
        lines.append('Wissenslücke: ' + gaps[0])
    extra, used = [], 0
    for line in lines:
        size = len(line.split())
        if used + size + 9 > reserve:
            extra.append('Weitere Sitzungsdetails im strukturierten workflow; gezielt nachladen.')
            break
        extra.append(line)
        used += size
    if not reserve:
        extra = []
    prefix = '\n'.join(extra)
    context = project_context.task_brief(root, target, task=task, budget=budget - len(prefix.split()), include_blocks=True)
    if context['selected'][0]['revision'] != hive.digest(raw):
        raise ValueError('Project changed while preparing context; retry session-open/session-context')
    text = (prefix + '\n\n' if prefix else '') + context['text']
    context.update(text=text, budget=budget, word_count=len(text.split()))
    if context.get('task_match') is False:
        gaps.append('Keine passenden Aufgabenbelege gefunden; keine Antwort aus Worttreffern ableiten.')
    return context, workflow

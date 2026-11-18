"""Bounded, local session receipts and journaled project handoffs.

Receipts record that context was offered, never that an agent understood it.
Only IDs, revisions and timestamps live in operational session state. Curated
handoff content goes through hive.apply into the existing project note.
"""
import contextlib
import copy
import datetime as dt
import fcntl
import json
from pathlib import Path
import re
import uuid

if __package__:
    from . import hive
else:
    import hive
if __package__:
    from . import work_items
else:
    import work_items
if __package__:
    from .agent_contract import AgentError
else:
    from agent_contract import AgentError

SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
RUNTIME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RUNTIME_SOURCES = {'startup', 'resume', 'clear', 'compact', 'fork'}
END_REASONS = {'other', 'clear', 'resume', 'logout', 'prompt_input_exit', 'exit', 'unknown'}
START = "<!-- HIVEMIND:HANDOFF:START -->"
END = "<!-- HIVEMIND:HANDOFF:END -->"
FIELDS = {"session_id", "note_id", "expected_revision", "source", "evidence",
          "project_state", "decisions", "open_questions", "next_steps"}


def _now():
    # A start/resume pair can occur within a second. Keep its ordering distinct
    # even though canonical journal timestamps historically use whole seconds.
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _session_path(root, session_id):
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid session ID")
    return Path(root) / ".state" / "sessions" / (session_id + ".json")


@contextlib.contextmanager
def _locked(root):
    folder = Path(root) / ".state"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "sessions.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _read(root, session_id):
    path = _session_path(root, session_id)
    if not path.exists():
        raise AgentError('SESSION_NOT_FOUND', "Unknown session ID")
    if __package__:
        from .agent_contract import validate
    else:
        from agent_contract import validate
    try:
        value = json.loads(path.read_text())
        validate('receipt', value)
        if value['session_id'] != session_id:
            raise ValueError('Mismatched session ID')
    except (ValueError, TypeError, KeyError) as error:
        raise AgentError('INVALID_STATE', 'Invalid stored session receipt; inspect hive doctor before continuing') from error
    return value


def _save(root, value):
    hive.atomic(_session_path(root, value["session_id"]),
                json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _records(root):
    with hive.locked(root, shared=(Path(root) / ".hive.lock").exists()):
        hive.require_clean(root)
        records = hive.load_notes(root)
        hive.audit(root, records)
    return records


def context_baseline(root, cluster, before_session_id=None):
    """Last offered context for a project; optionally strictly before a session.

    No baseline is invented on first use. Call before open_session to compare
    with the previous visit, or pass the current session's ID afterwards.
    """
    bound = _read(root, before_session_id)["opened_at"] if before_session_id else None
    rows = []
    for path in sorted((Path(root) / ".state" / "sessions").glob("*.json")):
        record = json.loads(path.read_text())
        if record.get("cluster") != cluster or record.get("schema_version") != 1:
            continue
        at = record.get("opened_at")
        if not at or (bound and (hive.timestamp(at) >= hive.timestamp(bound))):
            continue
        rows.append({"at": at, "session_id": record["session_id"]})
    return max(rows, key=lambda row: (hive.timestamp(row["at"]), row["session_id"])) if rows else None


def _offered(context, workflow):
    rows = context.get('selected', []) + workflow['changes']['items'] + workflow['lessons']['items']
    revisions = dict.fromkeys((item['id'], item['revision']) for item in rows)
    return [{'id': ident, 'revision': revision} for ident, revision in revisions]


def _merge_offered(previous, current):
    pairs = dict.fromkeys((item['id'], item['revision']) for item in previous + current)
    if len(pairs) > 512:
        raise AgentError('LIMIT_EXCEEDED', 'Session has 512 offered revisions; close it before starting a new work episode')
    return [{'id': ident, 'revision': revision} for ident, revision in pairs]


def _view(view, max_bytes):
    if __package__:
        from .context_packet import byte_limit
    else:
        from context_packet import byte_limit
    if view not in {'compact', 'full'} or (view == 'full' and max_bytes is not None):
        raise AgentError('INVALID_ARGUMENT', 'Use compact with max-bytes, or full without max-bytes')
    if view == 'compact':
        byte_limit(max_bytes)


def _offer(root, session, context, workflow, view, max_bytes, task, stage):
    rendered = set()
    if view == 'compact':
        if __package__:
            from .context_packet import compact
        else:
            from context_packet import compact
        result = compact(session, context, workflow, max_bytes, rendered_refs=rendered)
    else:
        rendered.update(block['ref'] for block in context['_reading_blocks'])
        result = {**session, 'offered': _merge_offered(session['offered'], _offered(context, workflow)),
                  'context': {k: v for k, v in context.items() if not k.startswith('_')}, 'workflow': workflow}
    session['offered'] = _merge_offered(session['offered'], result['offered'])
    _save(root, session)
    if __package__:
        from . import correction_checks
    else:
        import correction_checks
    correction_checks.observe(root, session, task, stage, context['_reading_blocks'], rendered)
    return result


def _refresh(root, session, task, budget, source=None, view='full', max_bytes=None, corrections=None):
    if __package__:
        from .session_context import build
    else:
        from session_context import build
    context, workflow = build(root, session['cluster'], task, budget, session.get('previous_context_at'),
                              corrections=corrections)
    session['refreshed_at'] = _now()
    if source is not None:
        session['runtime']['last_source'] = source
        session['runtime'].pop('ended_at', None)
        session['runtime'].pop('end_reason', None)
    return _offer(root, session, context, workflow, view, max_bytes, task, 'refresh')


def _runtime_id(value):
    if not isinstance(value, str) or not RUNTIME_ID.fullmatch(value):
        raise AgentError('INVALID_ARGUMENT', 'Invalid runtime session ID (1..128 identifier characters)')
    return value


def _runtime_records(root, agent, runtime_id):
    rows = []
    for path in sorted((root / '.state/sessions').glob('*.json')):
        value = _read(root, path.stem)
        if value.get('agent') == agent and value.get('runtime', {}).get('id') == runtime_id:
            rows.append(value)
    return sorted(rows, key=lambda row: (hive.timestamp(row['opened_at']), row['session_id']))


def open_session(root, target=".", agent="codex", task="", budget=1200, session_id=None,
                 runtime_id=None, runtime_source=None, view='full', max_bytes=None, corrections=None):
    """Offer bounded project context and return an attributable session ID."""
    if __package__:
        from .project_context import resolve_project
    else:
        from project_context import resolve_project
    if __package__:
        from .session_context import build
    else:
        from session_context import build
    root = Path(root).resolve()
    _view(view, max_bytes)
    if not isinstance(agent, str) or not AGENT.fullmatch(agent):
        raise ValueError("Invalid agent name")
    if runtime_id is not None:
        _runtime_id(runtime_id)
        if session_id is not None:
            raise AgentError('INVALID_ARGUMENT', 'Use either a HiveMind session ID or a runtime binding')
        runtime_source = runtime_source or 'startup'
    if runtime_source is not None and (runtime_id is None or runtime_source not in RUNTIME_SOURCES):
        raise AgentError('INVALID_ARGUMENT', 'Runtime source requires a runtime ID and a supported lifecycle source')
    ident = session_id or uuid.uuid4().hex
    _session_path(root, ident)
    route = resolve_project(root, target)
    if route is None:
        raise AgentError('UNKNOWN_PROJECT', "No unique HiveMind project for this folder")
    with _locked(root):
        related = []
        if runtime_id is not None:
            related = [row for row in _runtime_records(root, agent, runtime_id) if row['cluster'] == route['cluster']]
            active = [row for row in related if row['status'] == 'context_offered']
            if len(active) > 1:
                raise AgentError('INVALID_STATE', 'Multiple open receipts for this runtime and project; inspect session-status')
            if active:
                return _refresh(root, active[0], task, budget, runtime_source, view, max_bytes, corrections)
        if _session_path(root, ident).exists():
            raise AgentError('SESSION_EXISTS', "Session already exists; inspect session-status or open a new session")
        previous = context_baseline(root, route["cluster"])
        context, workflow = build(root, target, task, budget, previous['at'] if previous else None,
                                  corrections=corrections)
        value = {"schema_version": 1, "session_id": ident, "agent": agent,
                 "cluster": context["cluster"], "status": "context_offered",
                 "opened_at": _now(), "previous_context_at": previous["at"] if previous else None,
                 "offered": []}
        if runtime_id is not None:
            value['runtime'] = {'id': runtime_id, 'source': runtime_source, 'last_source': runtime_source}
            if related:
                value['previous_session_id'] = related[-1]['session_id']
        return _offer(root, value, context, workflow, view, max_bytes, task, 'startup')


def session_context(root, session_id, task='', budget=1200, view='full', max_bytes=None, corrections=None):
    """Refresh an active session for its actual task without a second receipt."""
    root = Path(root).resolve()
    _view(view, max_bytes)
    with _locked(root):
        session = _read(root, session_id)
        if session['status'] != 'context_offered':
            raise AgentError('SESSION_CLOSED', 'Session is already closed; open a new session')
        return _refresh(root, session, task, budget, view=view, max_bytes=max_bytes, corrections=corrections)


def _mark_ended(root, session, reason):
    if session['status'] == 'context_offered':
        if 'runtime' not in session:
            raise AgentError('INVALID_ARGUMENT', 'Session has no runtime binding')
        if 'ended_at' not in session['runtime']:
            session['runtime'].update(ended_at=_now(), end_reason=reason)
            _save(root, session)
    return session


def end_session(root, session_id, reason='other'):
    if reason not in END_REASONS:
        raise AgentError('INVALID_ARGUMENT', 'Unsupported runtime end reason')
    with _locked(root):
        return _mark_ended(root, _read(root, session_id), reason)


def end_runtime(root, agent, runtime_id, reason='other'):
    """Mark unfinished episodes only. Never read transcripts or write knowledge."""
    root = Path(root).resolve()
    _runtime_id(runtime_id)
    if reason not in END_REASONS:
        reason = 'unknown'
    with _locked(root):
        rows = _runtime_records(root, agent, runtime_id)
        for row in rows:
            _mark_ended(root, row, reason)
    return {'matched': len(rows)}


def feedback(root, session_id, outcome, used=None, cited=None, missing='none', actor='agent'):
    """Explicit metadata reports, separate from evidence that context was offered."""
    if __package__:
        from .agent_contract import validate
    else:
        from agent_contract import validate
    data = {'outcome': outcome, 'used': used or [], 'cited': cited or [], 'missing': missing, 'actor': actor}
    try:
        validate('feedback', data)
    except ValueError as error:
        raise AgentError('INVALID_ARGUMENT', str(error)) from error
    used_pairs = {(r['id'], r['revision']) for r in data['used']}
    cited_pairs = {(r['id'], r['revision']) for r in data['cited']}
    if not cited_pairs <= used_pairs or (outcome == 'not_used' and (used_pairs or cited_pairs)):
        raise AgentError('INVALID_ARGUMENT', 'Citations require explicit use; not_used cannot report use or citations')
    data['used'] = [{'id': i, 'revision': r} for i, r in sorted(used_pairs)]
    data['cited'] = [{'id': i, 'revision': r} for i, r in sorted(cited_pairs)]
    ident = hive.digest(json.dumps(data, sort_keys=True, separators=(',', ':')))
    with _locked(root):
        session = _read(root, session_id)
        offered = {(r['id'], r['revision']) for r in session['offered']}
        if not used_pairs <= offered:
            raise AgentError('INVALID_ARGUMENT', 'Use must reference exact revisions offered in this session')
        reports = session.setdefault('feedback', [])
        for report in reports:
            if report['id'] == ident:
                return {'session_id': session_id, 'feedback': report}
        if len(reports) >= 32:
            raise AgentError('LIMIT_EXCEEDED', 'At most 32 distinct feedback reports per session')
        report = {'id': ident, 'at': _now(), **data}
        reports.append(report)
        _save(root, session)
    return {'session_id': session_id, 'feedback': report}


def metrics(root, cluster=None):
    """Read-only counts; latest explicit report per actor and session, no quality inference."""
    if cluster is not None and (not isinstance(cluster, str) or not re.fullmatch(r'projects/[a-z0-9][a-z0-9-]*', cluster)):
        raise AgentError('INVALID_ARGUMENT', 'Metrics scope must be projects/ID')
    rows = [_read(root, p.stem) for p in sorted((Path(root) / '.state/sessions').glob('*.json'))]
    rows = [row for row in rows if cluster is None or row['cluster'] == cluster]
    counts = {actor: {outcome: 0 for outcome in ('helpful', 'unhelpful', 'not_used')} for actor in ('agent', 'user')}
    used, cited, offered = set(), set(), set()
    missing = {}
    for row in rows:
        offered.update((r['id'], r['revision']) for r in row['offered'])
        latest = {report['actor']: report for report in row.get('feedback', [])}
        for actor, report in latest.items():
            counts[actor][report['outcome']] += 1
            used.update((r['id'], r['revision']) for r in report['used'])
            cited.update((r['id'], r['revision']) for r in report['cited'])
            if report['missing'] != 'none':
                missing[report['missing']] = missing.get(report['missing'], 0) + 1
    return {'sessions': len(rows), 'cluster': cluster, 'saved': sum(r['status'] == 'saved' for r in rows),
            'no_change': sum(r['status'] == 'no_change' for r in rows),
            'open': sum(r['status'] == 'context_offered' for r in rows),
            'ended_without_close': sum(r['status'] == 'context_offered' and bool(r.get('runtime', {}).get('ended_at')) for r in rows),
            'feedback': counts, 'missing': missing, 'offered_revisions': len(offered),
            'used_revisions': len(used), 'cited_revisions': len(cited),
            'limitations': ['Explicit self-reports; actor is a declared label, not authenticated identity.',
                            'Offered context does not establish reading or usefulness; counts are not a quality benchmark.']}


def status(root, session_id):
    """Read the receipt without interpreting offered context as consumed."""
    with _locked(root):
        return _read(root, session_id)


session_status = status


def draft_handoff(root, session_id, note_id=None):
    """Create an editable delta for an existing project; no canonical write."""
    session = status(root, session_id)
    if session["status"] != "context_offered":
        raise AgentError("SESSION_CLOSED", "Session is already closed")
    records = _records(Path(root).resolve())
    candidates = [(ident, n, text) for ident, (n, text, _) in records.items()
                  if hive.cluster_for(n) == session["cluster"] and n["type"] == "project"
                  and hive.active(n) and n["sensitivity"] != "personal"]
    if note_id:
        candidates = [row for row in candidates if row[0] == note_id]
    candidates.sort(key=lambda row: (row[0] != session["cluster"].split("/")[-1], row[0]))
    if not candidates:
        raise ValueError("No active internal project note in this session's project")
    ident, note, text = candidates[0]
    return {"session_id": session_id, "note_id": ident, "expected_revision": hive.digest(text),
            "source": {"id": "handoff-" + session_id, "resource": "", "title": "", "observed_at": ""},
            "evidence": "reported", "project_state": "", "decisions": [],
            "open_questions": [], "next_steps": [], "step_updates": []}


def _text(value, field, limit=1200, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("Invalid or oversized handoff " + field)
    if START in value or END in value:
        raise ValueError("Handoff content cannot contain managed-section markers")
    return value.strip()


def _validate_payload(payload, session):
    if not isinstance(payload, dict) or not FIELDS <= set(payload) or set(payload) - FIELDS - {'step_updates'}:
        raise ValueError("Handoff must contain the documented structured fields and optional step_updates")
    if len(json.dumps(payload, ensure_ascii=False)) > 24000:
        raise ValueError("Handoff is too large; curate a concise project update")
    if payload["session_id"] != session["session_id"]:
        raise ValueError("Handoff belongs to a different session")
    if not isinstance(payload["note_id"], str) or not hive.ID.fullmatch(payload["note_id"]):
        raise ValueError("Invalid handoff note ID")
    if not isinstance(payload["expected_revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["expected_revision"]):
        raise ValueError("Handoff requires the draft's expected revision")
    if payload["evidence"] not in hive.EVIDENCE:
        raise ValueError("Handoff requires an explicit evidence status")
    source = payload["source"]
    if not isinstance(source, dict) or set(source) - {"id", "resource", "title", "observed_at", "sha256"}:
        raise ValueError("Invalid handoff source")
    for key, limit in (("id", 120), ("resource", 1000), ("title", 280), ("observed_at", 50)):
        _text(source.get(key), "source." + key, limit)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", source["id"]):
        raise ValueError("Use a simple source ID for an attributable footnote")
    hive.timestamp(source["observed_at"])
    if "sha256" in source and not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
        raise ValueError("Invalid source fingerprint")
    _text(payload["project_state"], "project_state", 1600, empty=True)
    for key in ("decisions", "open_questions", "next_steps"):
        rows = payload[key]
        if not isinstance(rows, list) or len(rows) > 8:
            raise ValueError("Handoff lists contain at most eight curated items")
        for item in rows:
            if key == "open_questions":
                _text(item, key, 500)
                continue
            fields = {"decision", "reason", "status"} if key == "decisions" else {"text", "status"}
            if not isinstance(item, dict) or set(item) != fields:
                raise ValueError("Invalid structured " + key + " item")
            if item["status"] not in {"agreed", "proposed"}:
                raise ValueError("Distinguish agreed from proposed decisions and next steps")
            for field in fields - {"status"}:
                _text(item[field], key + "." + field, 800)
    work_items.validate_updates(payload.get('step_updates', []))
    if not any(payload[key] for key in ("project_state", "decisions", "open_questions", "next_steps")) and not payload.get('step_updates'):
        raise ValueError("Nothing durable supplied; close with --no-change instead")


def _section(payload, items, sources):
    source = payload["source"]
    citation = "[^" + source["id"] + "]"
    lines = [START, "## Current handoff", "",
             "Observed: " + source["observed_at"] + " · Evidence: " +
             hive.EVIDENCE_LABELS[payload["evidence"]] + "." + citation]
    def cite(field):
        evidence = payload['field_sources'][field]
        return ('[^' + evidence['source_id'] + '] (' + evidence['observed_at'] + ' · ' +
                hive.EVIDENCE_LABELS[evidence['evidence']] + ')')
    if payload["project_state"].strip():
        lines += ["", "### Project status", "", payload["project_state"].strip() + cite('project_state')]
    if payload["decisions"]:
        lines += ["", "### Decisions", ""]
        for item in payload["decisions"]:
            label = "Agreed" if item["status"] == "agreed" else "Proposed, not agreed"
            lines.append("- " + label + ": " + item["decision"].strip() + " Reason: " + item["reason"].strip() + cite('decisions'))
    if payload["open_questions"]:
        lines += ["", "### Open questions", ""] + ["- " + item.strip() + cite('open_questions') for item in payload["open_questions"]]
    if items:
        lines += ["", "### Work items", ""]
        for item in items:
            lines.append('- ' + work_items.line(item) + (' — ' + item['reason'] if item['reason'] else '') +
                         ' [^' + item['source_id'] + '] (' + item['observed_at'] + ' · ' + hive.EVIDENCE_LABELS[item['evidence']] + ')')
    referenced = {source['id'], *(item['source_id'] for item in items),
                  *(entry['source_id'] for entry in payload['field_sources'].values())}
    lines.append('')
    for entry in sources:
        if entry['id'] in referenced:
            lines.append('[^' + entry['id'] + ']: ' + entry['title'] + ' — ' + entry['resource'] + '.')
    lines.append(END)
    return "\n".join(lines)


def _updated_note(note, payload):
    updated = copy.deepcopy(note)
    items = work_items.merge(note, payload)
    effective = copy.deepcopy(payload)
    old = note.get('handoff', {})
    attribution = {'source_id': payload['source']['id'], 'observed_at': payload['source']['observed_at'], 'evidence': payload['evidence']}
    changed_fields = any(payload[field] and (payload[field] != old.get(field) or
                         old.get('field_sources', {}).get(field, {key: old.get(key) for key in attribution}) != attribution)
                         for field in ('project_state', 'decisions', 'open_questions'))
    if not changed_fields and items == work_items.read(note):
        return updated
    effective['field_sources'] = {}
    for field in ('project_state', 'decisions', 'open_questions'):
        if not payload[field] and old.get(field):
            effective[field] = copy.deepcopy(old[field])
            effective['field_sources'][field] = copy.deepcopy(old.get('field_sources', {}).get(field, {
                'source_id': old['source_id'], 'observed_at': old['observed_at'], 'evidence': old['evidence']}))
        else:
            effective['field_sources'][field] = {'source_id': payload['source']['id'],
                'observed_at': payload['source']['observed_at'], 'evidence': payload['evidence']}
    body = updated["body"]
    if START in body or END in body:
        if body.count(START) != 1 or body.count(END) != 1 or body.index(START) > body.index(END):
            raise ValueError("Ambiguous handoff section; inspect the project note")
        first, rest = body.split(START, 1)
        _, last = rest.split(END, 1)
        body = (first.rstrip() + "\n\n" + last.lstrip()).strip()
    # Keep the latest handoff before historical prose, while preserving all
    # previous canonical text outside the explicitly managed current section.
    heading, sep, remaining = body.partition("\n")
    section = _section(effective, items, [*note['sources'], *([] if any(s['id'] == payload['source']['id'] for s in note['sources']) else [payload['source']])])
    updated["body"] = (heading + "\n\n" + section + "\n\n" + remaining.lstrip()).strip() if heading.startswith("# ") else section + "\n\n" + body
    existing = next((s for s in updated["sources"] if s["id"] == payload["source"]["id"]), None)
    if existing and existing != payload["source"]:
        raise ValueError("Source ID already identifies different evidence; use a new source ID")
    if not existing:
        updated["sources"].append(copy.deepcopy(payload["source"]))
    updated["handoff"] = {key: copy.deepcopy(effective[key]) for key in
                          ("project_state", "decisions", "open_questions", "next_steps", "evidence", "field_sources")}
    updated["handoff"].update(source_id=payload["source"]["id"], observed_at=payload["source"]["observed_at"])
    updated['work_items'] = items
    # A scoped observation does not verify all older claims in this note.
    return updated


def _committed(root, session, fingerprint):
    context = "hivemind-session:" + session["session_id"] + ":" + fingerprint
    with hive.locked(root, shared=(Path(root) / ".hive.lock").exists()):
        hive.require_clean(root)
        records = hive.load_notes(root)
        hive.audit(root, records)
        matches = [e for e in hive.events(root) if e.get("context") == context]
    if len(matches) > 1:
        raise ValueError("Duplicate session journal event; inspect history")
    return matches[0] if matches else None


def _receipt(session, event, fingerprint):
    note = hive.decode(event['changes'][0]['after'])
    return {**session, "status": "saved", "closed_at": _now(), "committed_at": event["at"],
            "payload_sha256": fingerprint, "event": event["id"],
            "work_items": [{key: item[key] for key in ('id', 'status', 'state', 'source_id')} for item in work_items.read(note)],
            "notes": [{"id": c["id"], "revision": hive.digest(c["after"])} for c in event["changes"]]}


def record_handoff(root, session_id, *, expected, source, source_title, observed_at,
                   evidence='reported', project_state='', decision=None, because=None,
                   agreement=None, questions=None):
    """Build the existing handoff contract from a small explicit CLI delta.

    No implicit latest revision, automatic evidence promotion, or new write path.
    Observation time is explicit so a retry reconstructs the identical payload.
    Omitted categories retain their contents and original field provenance.
    """
    if __package__:
        from . import evidence as evidence_api
    else:
        import evidence as evidence_api
    match = evidence_api.REFERENCE.fullmatch(expected or '')
    if not match or not all((source, source_title, observed_at)):
        raise AgentError('INVALID_ARGUMENT', '--record requires --expected ID@REVISION, --source, --source-title and --observed-at')
    if decision is not None and (not because or agreement not in {'agreed', 'proposed'}):
        raise AgentError('INVALID_HANDOFF', '--decision requires --because and --agreement agreed|proposed')
    if decision is None and (because is not None or agreement is not None):
        raise AgentError('INVALID_HANDOFF', '--because and --agreement require --decision')
    ident, revision = match.groups()
    payload = {'session_id': session_id, 'note_id': ident, 'expected_revision': revision,
               'source': evidence_api.attributed_source(root, 'handoff-' + session_id,
                                                       source, source_title, observed_at),
               'evidence': evidence, 'project_state': project_state, 'decisions': [],
               'open_questions': questions or [], 'next_steps': [], 'step_updates': []}
    if decision is not None:
        payload['decisions'] = [{'decision': decision, 'reason': because, 'status': agreement}]
    return close_session(root, session_id, payload=payload)


def close_session(root, session_id, payload=None, no_change=False, reason=""):
    """Commit a bounded handoff or explicitly record no durable change.

    A stable journal context correlates retries across process death after apply.
    A pending interrupted canonical transaction still requires hive recover.
    """
    root = Path(root).resolve()
    if (payload is None) == (not no_change):
        raise ValueError("Provide a handoff payload or --no-change, exclusively")
    reason = _text(reason, "reason", 280, empty=True)
    with _locked(root):
        session = _read(root, session_id)
        if no_change:
            if session["status"] == "no_change":
                return session
            if session["status"] != "context_offered" or session.get("intent_sha256"):
                raise ValueError("Session has a handoff write; retry it before declaring no change")
            session.update(status="no_change", closed_at=_now(), reason=reason or "No durable change", notes=[])
            _save(root, session)
            return session
        try:
            _validate_payload(payload, session)
        except (ValueError, TypeError, KeyError) as error:
            raise AgentError('INVALID_HANDOFF', str(error)) from error
        fingerprint = hive.digest(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        if session["status"] == "saved":
            if session.get("payload_sha256") != fingerprint:
                raise ValueError("Session was already closed with a different handoff")
            return session
        if session["status"] == "no_change" and session.get("payload_sha256") == fingerprint:
            return session
        if session["status"] != "context_offered":
            raise AgentError("SESSION_CLOSED", "Session is already closed")
        if session.get("intent_sha256") not in (None, fingerprint):
            # A failed revision check must not poison a session forever, but an
            # already committed different payload may never be overwritten.
            previous = _committed(root, session, session["intent_sha256"])
            if previous:
                raise ValueError("A different handoff was committed; retry that original payload")
        event = _committed(root, session, fingerprint)
        if event:
            receipt = _receipt(session, event, fingerprint)
            _save(root, receipt)
            return receipt
        records = _records(root)
        ident = payload["note_id"]
        if ident not in records:
            raise ValueError("Handoffs update an existing project note")
        note, text, _ = records[ident]
        if (note["type"] != "project" or hive.cluster_for(note) != session["cluster"] or
                note["sensitivity"] == "personal" or not hive.active(note)):
            raise ValueError("Handoff target must be an active internal note in the session's project")
        if hive.digest(text) != payload["expected_revision"]:
            raise AgentError("REVISION_CONFLICT", "Revision conflict: reread the note and draft a fresh handoff")
        updated = _updated_note(note, payload)
        # Validate before persisting intent, so malformed payloads cannot leave
        # an apparent write in flight. hive.apply repeats validation under lock.
        hive.validate(updated, {i: n[0]["type"] for i, n in records.items()}, require_cluster=True)
        if updated == note:
            session.update(status="no_change", closed_at=_now(), reason="Identical curated handoff",
                           payload_sha256=fingerprint, notes=[])
            _save(root, session)
            return session
        session["intent_sha256"] = fingerprint
        _save(root, session)
        hive.apply(root, {"changes": [{"expected_revision": payload["expected_revision"], "note": updated}]},
                   actor=session["agent"] + "/session-" + session_id,
                   reason="Structured project handoff", context="hivemind-session:" + session_id + ":" + fingerprint, respect_pause=True)
        event = _committed(root, session, fingerprint)
        if event is None:
            raise ValueError("No journal proof found for the handoff; inspect the session")
        receipt = _receipt(session, event, fingerprint)
        _save(root, receipt)
        return receipt

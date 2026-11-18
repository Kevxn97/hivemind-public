"""Read, correct, archive and compensate local memories through the canonical journal."""
import argparse
import copy
import json
from pathlib import Path
import sys

if __package__:
    from . import automation, hive, work_items
    from .agent_contract import AgentError
else:
    import automation
    import hive
    import work_items
    from agent_contract import AgentError


def configure(sub):
    parser = sub.add_parser('memory', help='Inspect and manage local memories and incoming processing')
    commands = parser.add_subparsers(dest='memory_command')
    listing = commands.add_parser('list', help='Recent memories with provenance')
    listing.add_argument('--history', action='store_true', help='Include archived memories')
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--cluster')
    show = commands.add_parser('show', help='Canonical content, sources and journal history')
    show.add_argument('id')
    edit = commands.add_parser('edit', help='Replace false content with an attributable correction')
    edit.add_argument('id')
    edit.add_argument('--title')
    edit.add_argument('--summary')
    body = edit.add_mutually_exclusive_group()
    body.add_argument('--body')
    body.add_argument('--body-file', type=Path)
    edit.add_argument('--source', required=True, help='Correction source, e.g. user:reviewed-decision')
    archive = commands.add_parser('archive', help='Remove a memory from active context')
    archive.add_argument('id')
    undo = commands.add_parser('undo', help='Compensate a journal event without deleting history')
    undo.add_argument('event')
    pause = commands.add_parser('pause', help='Pause incoming agent/source knowledge writes')
    resume = commands.add_parser('resume', help='Resume incoming knowledge writes')
    commands.add_parser('status', help='Inspect incoming knowledge processing state')
    checks = commands.add_parser('check', help='Follow a corrected decision into a new agent session')
    check_actions = checks.add_subparsers(dest='check_command', required=True)
    add = check_actions.add_parser('add', help='Save the expected revision, question and target agent')
    add.add_argument('reference', metavar='NOTE_ID@REVISION')
    add.add_argument('--question', required=True, help='Typical follow-up question')
    add.add_argument('--agent', choices=('codex', 'claude'), required=True)
    check_actions.add_parser('list', help='List saved correction checks')
    inspect = check_actions.add_parser('show', help='Show offered context and recorded user reviews')
    inspect.add_argument('check_id')
    review = check_actions.add_parser('review', help='Record your review of one answer')
    review.add_argument('check_id')
    review.add_argument('--session', required=True, metavar='SESSION_ID')
    review.add_argument('--used', choices=('yes', 'no'), required=True)
    review.add_argument('--corrected-again', choices=('yes', 'no'), required=True)
    stop = check_actions.add_parser('stop', help='Stop collecting observations and keep past results')
    stop.add_argument('check_id')
    for mutation in (edit, archive, undo, pause, resume):
        mutation.add_argument('--actor', default='user/memory')
        mutation.add_argument('--reason', required=True)
        mutation.add_argument('--apply', action='store_true')
        mutation.add_argument('--preview', help='Exact token returned by a prior preview')
    return parser


def _snapshot(root):
    with hive.locked(root, shared=True):
        hive.require_clean(root)
        records = hive.load_notes(root)
        hive.audit(root, records)
        return records, hive.events(root)


def _record(records, ident):
    if ident not in records:
        raise ValueError('Unknown memory ID ' + ident)
    return records[ident]


def _history(events, ident):
    result = []
    for event in events:
        for change in event['changes']:
            if change['id'] == ident:
                result.append({key: event[key] for key in ('id', 'at', 'actor', 'reason', 'context')} | {
                    'before_revision': hive.digest(change['before']) if change['before'] is not None else 'new',
                    'after_revision': hive.digest(change['after'])})
    return result


def _safe(value):
    # Treat note content as text even when a terminal interprets escape sequences.
    return ''.join(c if c == '\n' or (c.isprintable() and c != '\x1b') else ' ' for c in str(value))


def _show(records, events, ident):
    note, text, _ = _record(records, ident)
    history = _history(events, ident)
    lines = [note['title'] + ' · ' + ident, 'Revision ' + hive.digest(text), note['summary'], '', note['body'], '', 'Sources']
    for source in note['sources']:
        lines.append('  ' + source['id'] + ' · ' + source['title'] + ' · ' + source['resource'])
    lines += ['', 'History']
    for event in reversed(history):
        lines.append('  ' + event['id'] + ' · ' + event['actor'] + ' · ' + event['reason'] + ' · ' + event['context'])
    return {'note': note, 'revision': hive.digest(text), 'history': history, 'text': _safe('\n'.join(lines))}


def _list(records, events, args):
    if not 1 <= args.limit <= 100:
        raise ValueError('Memory limit must be between 1 and 100')
    hive.validate_scope(args.cluster)
    rows = []
    for ident, (note, text, _) in records.items():
        if (note.get('archived', False) and not args.history) or not hive.in_cluster(note, args.cluster):
            continue
        latest = _history(events, ident)[-1]
        rows.append({'id': ident, 'title': note['title'], 'summary': note['summary'], 'revision': hive.digest(text),
                     'archived': note.get('archived', False), 'status': note['status'], 'sources': note['sources'],
                     'changed_at': latest['at'], 'actor': latest['actor'], 'event': latest['id']})
    rows.sort(key=lambda row: row['event'], reverse=True)
    total = len(rows)
    rows = rows[:args.limit]
    lines = ['Memories · most recently changed first']
    for row in rows:
        lines += [row['id'] + ' · ' + row['title'] + (' · archived' if row['archived'] else '') + (' · deprecated' if row['status'] == 'deprecated' else ''),
                  '  ' + row['summary'], '  ' + row['changed_at'] + ' · ' + row['actor'],
                  '  Sources ' + ', '.join(s['resource'] for s in row['sources'])]
    lines += ['', f'{len(rows)} of {total} memories.', 'Details: hive memory show ID.']
    return {'memories': rows, 'total': total, 'text': _safe('\n'.join(lines))}


def _edit(root, records, args):
    note, text, _ = _record(records, args.id)
    updated = copy.deepcopy(note)
    values = {'title': args.title, 'summary': args.summary,
              'body': args.body_file.read_text(encoding='utf-8') if args.body_file is not None else args.body}
    changed = {key: value for key, value in values.items() if value is not None and value != note[key]}
    if not changed:
        raise ValueError('Provide changed --title, --summary or --body/--body-file content')
    cleared = []
    if 'body' in changed or 'summary' in changed:
        # A free-text correction cannot establish which old assertions remain
        # true. Preserve independent work rows while removing stale assertions.
        if note.get('handoff') and 'work_items' not in updated:
            updated['work_items'] = work_items.read(note)
        for key in ('claims', 'experience', 'handoff', 'verified'):
            if key in updated:
                cleared.append(key)
                updated.pop(key)
        # Summary-only editing would leave the old decision in current prose.
        if values['body'] is None:
            raise ValueError('A summary correction also requires the complete corrected --body or --body-file')
    updated.update(changed)
    source_id = 'correction-' + hive.digest(json.dumps([args.source, args.reason, changed], sort_keys=True))[:16]
    if __package__:
        from . import evidence
    else:
        import evidence
    source = evidence.attributed_source(root, source_id, args.source,
                                        'User correction · ' + args.reason, hive.now())
    existing = next((s for s in updated['sources'] if s['id'] == source_id), None)
    if existing is None:
        updated['sources'].insert(0, source)
    else:
        if existing.get('sha256') != source.get('sha256'):
            raise ValueError('Correction source changed; use a distinct attributable source')
        updated['sources'] = [existing] + [s for s in updated['sources'] if s['id'] != source_id]
    updated['evidence'] = 'reported'
    return [{'expected_revision': hive.digest(text), 'note': updated}], cleared


def _undo(records, events, event_id):
    target = next((e for e in events if e['id'] == event_id), None)
    if target is None:
        raise ValueError('Unknown journal event')
    changes = []
    for change in target['changes']:
        note, text, _ = _record(records, change['id'])
        if text != change['after']:
            raise AgentError('REVISION_CONFLICT', 'A later edit exists for ' + change['id'] + '. Undo the latest event or make a fresh correction.')
        restored = hive.decode(change['before']) if change['before'] is not None else {**copy.deepcopy(note), 'archived': True}
        changes.append({'expected_revision': hive.digest(text), 'note': restored})
    return changes


def _mutation(root, records, events, args):
    if not args.actor.strip() or not args.reason.strip():
        raise ValueError('Actor and reason are required')
    command = args.memory_command
    cleared = []
    state = None
    if command in {'pause', 'resume'}:
        state = automation.status(root)
        changes = []
        binding = {'state_revision': state['revision']}
    else:
        if command == 'edit':
            changes, cleared = _edit(root, records, args)
        elif command == 'archive':
            note, text, _ = _record(records, args.id)
            if note.get('archived', False):
                raise ValueError('Memory is already archived')
            changes = [{'expected_revision': hive.digest(text), 'note': {**copy.deepcopy(note), 'archived': True}}]
        else:
            changes = _undo(records, events, args.event)
        # Validate the proposed content during preview as well as during apply.
        types = {ident: row[0]['type'] for ident, row in records.items()}
        for change in changes:
            hive.validate(change['note'], types, require_cluster=True)
        binding = {'revisions': {c['note']['id']: c['expected_revision'] for c in changes}}
    request = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()
               if key not in {'apply', 'preview', 'json', 'root', 'plain', 'body_file'}}
    if command == 'edit' and args.body_file is not None:
        request['body'] = changes[0]['note']['body']
    if command == 'edit':
        request['source_sha256'] = changes[0]['note']['sources'][0].get('sha256')
    token = hive.digest(json.dumps({'request': request, **binding}, sort_keys=True, ensure_ascii=False))
    result = {'status': 'preview', 'action': command, 'preview': token, 'cleared': cleared,
              'changes': [{'id': c['note']['id'], 'expected_revision': c['expected_revision'],
                           'note': c['note']} for c in changes]}
    lines = ['Preview · ' + command]
    if state is not None:
        lines.append('Incoming processing will be ' + ('paused.' if command == 'pause' else 'resumed.'))
    for change in changes:
        lines += [change['note']['id'] + ' · ' + change['note']['title'], change['note']['summary'], change['note']['body']]
    if cleared:
        lines.append('Removed structured fields and checks ' + ', '.join(cleared) + '. The previous state remains in history.')
    if not args.apply:
        lines += ['', 'Apply with the same arguments and --apply --preview ' + token]
        return {**result, 'text': _safe('\n'.join(lines))}
    if args.preview != token:
        raise AgentError('REVISION_CONFLICT', 'A matching prior --preview token is required. Content or state may have changed; preview again.')
    if state is not None:
        result.update(automation.set_paused(root, command == 'pause', actor=args.actor, reason=args.reason,
                                            expected_revision=state['revision']))
    else:
        context = args.source if command == 'edit' else 'hivemind-memory:' + command + ':' + getattr(args, 'event', args.id if hasattr(args, 'id') else '')
        result.update(hive.apply(root, {'changes': changes}, args.actor, args.reason, context))
    result.update(status='applied', text=_safe('\n'.join(lines) + '\nSaved.'))
    return result


def _checks_module():
    if __package__:
        from . import correction_checks
    else:
        import correction_checks
    return correction_checks


def _check_action(root, args):
    checks = _checks_module()
    if args.check_command == 'add':
        return checks.add(root, args.reference, args.question, args.agent)
    if args.check_command == 'list':
        return checks.list_checks(root)
    if args.check_command == 'show':
        return checks.show(root, args.check_id)
    if args.check_command == 'review':
        return checks.review(root, args.check_id, args.session, args.used == 'yes', args.corrected_again == 'yes')
    return checks.stop(root, args.check_id)


def execute(args, root):
    root = Path(root)
    command = args.memory_command
    if command is None and sys.stdin.isatty() and sys.stdout.isatty() and not getattr(args, 'json', False):
        return interactive(root)
    if command == 'check':
        return _check_action(root, args)
    if command == 'status':
        state = automation.status(root)
        return {**state, 'text': 'Incoming processing ' + ('paused.' if state['paused'] else 'active.')}
    records, events = _snapshot(root)
    if command in {None, 'list'}:
        if command is None:
            args = argparse.Namespace(limit=20, history=False, cluster=None)
        return _list(records, events, args)
    if command == 'show':
        return _show(records, events, args.id)
    return _mutation(root, records, events, args)


def _confirm(prompt):
    return input(prompt + ' [y/N] > ').strip().lower() in {'yes', 'y', 'ja'}


def _check_add_form(root, checks):
    ident = input('Corrected memory ID > ').strip()
    if not ident:
        return
    records, _ = _snapshot(root)
    note, raw, _ = _record(records, ident)
    if not note['sources'] or not note['sources'][0]['id'].startswith('correction-'):
        raise ValueError('Choose a memory corrected with memory edit.')
    source = note['sources'][0]
    reference = ident + '@' + hive.digest(raw)
    print(_safe(reference + '\n' + note['summary'] + '\nSource ' + source['id'] + ' · ' + source['resource']))
    question = input('Typical follow-up question > ').strip()
    if not question:
        return
    agent = input('Target agent [codex/claude] > ').strip().lower()
    if not agent:
        return
    if agent not in {'codex', 'claude'}:
        raise ValueError('Choose codex or claude.')
    print(_safe('Question for ' + agent + ' · ' + question))
    if _confirm('Save this check?'):
        print(_safe(checks.add(root, reference, question, agent)['text']))


def _check_review_form(root, checks, check_id):
    session = input('Session ID from the check > ').strip()
    if not session:
        return
    answers = []
    for prompt in ('Did the answer use the correction?', 'Did you have to correct it again?'):
        value = input(prompt + ' [yes/no, Enter to cancel] > ').strip().lower()
        if not value:
            return
        if value not in {'yes', 'y', 'no', 'n'}:
            raise ValueError('Choose yes or no.')
        answers.append(value in {'yes', 'y'})
    if _confirm('Save your review?'):
        print(_safe(checks.review(root, check_id, session, *answers)['text']))


def _checks_menu(root):
    checks = _checks_module()
    while True:
        print('\nCorrection checks · l List  a Add  s Show  r Review answer  x Stop  q Back')
        choice = input('Choice > ').strip().lower()
        if choice in {'q', ''}:
            return
        try:
            if choice == 'l':
                print(_safe(checks.list_checks(root)['text']))
            elif choice == 'a':
                _check_add_form(root, checks)
            elif choice in {'s', 'r', 'x'}:
                check_id = input('Check ID > ').strip()
                if not check_id:
                    continue
                print(_safe(checks.show(root, check_id)['text']))
                if choice == 'r':
                    _check_review_form(root, checks, check_id)
                elif choice == 'x' and _confirm('Stop checking new sessions?'):
                    print(_safe(checks.stop(root, check_id)['text']))
        except (ValueError, OSError, RuntimeError) as exc:
            print(_safe(str(exc)))


def interactive(root):
    """A small terminal menu using the same revision-bound command operations."""
    parser = argparse.ArgumentParser(prog='hive')
    configure(parser.add_subparsers(dest='command', required=True))
    print('HiveMind · Manage memories')
    while True:
        print('\n1 Memories  2 Show  3 Correct  4 Archive  5 Undo\n6 Pause  7 Resume  8 Status  9 Correction checks  q Back')
        choice = input('Choice > ').strip().lower()
        if choice in {'q', ''}:
            return {'status': 'closed', 'text': 'Memory management closed.'}
        commands = {'1': 'list', '2': 'show', '3': 'edit', '4': 'archive', '5': 'undo', '6': 'pause', '7': 'resume', '8': 'status'}
        if choice == '9':
            _checks_menu(root)
            continue
        if choice not in commands:
            continue
        command = commands[choice]
        arguments = ['memory', command]
        if command in {'show', 'edit', 'archive'}:
            arguments.append(input('Memory ID > ').strip())
        if command == 'undo':
            arguments.append(input('History event ID > ').strip())
        if command == 'edit':
            summary = input('Corrected summary > ')
            print('Full corrected text; finish with a single . on its own line.')
            body = []
            while True:
                line = input()
                if line == '.':
                    break
                body.append(line)
            arguments += ['--summary', summary, '--body', '\n'.join(body), '--source', input('Source > ').strip()]
        if command in {'edit', 'archive', 'undo', 'pause', 'resume'}:
            arguments += ['--reason', input('Reason > ').strip()]
        try:
            args = parser.parse_args(arguments)
            result = execute(args, root)
            print(result['text'])
            if result.get('status') == 'preview' and _confirm('Apply?'):
                args.apply = True
                args.preview = result['preview']
                print(execute(args, root)['text'])
        except (ValueError, OSError, RuntimeError) as exc:
            print(_safe(str(exc)))

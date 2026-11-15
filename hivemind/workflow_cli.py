"""Shared CLI adapters for local context, handoffs and evidence-backed work views."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
if __package__:
    from . import agent_contract
else:
    import agent_contract


class ArgumentParser(argparse.ArgumentParser):
    """Machine-readable argument failures also before dispatch reaches a command."""
    def error(self, message):
        self.exit(2, json.dumps(agent_contract.failure(agent_contract.AgentError('INVALID_ARGUMENT', message)), ensure_ascii=False) + '\n')

COMMANDS = {'project-register', 'project-unregister', 'projects', 'session-open', 'session-context', 'session-draft',
            'session-close', 'session-status', 'why', 'impact', 'lessons',
            'changes', 'feedback', 'session-end', 'session-feedback', 'session-metrics', 'doctor', 'contract', 'evidence',
            'review-plan', 'review-record', 'verify-plan', 'verify-draft', 'learn-draft'}


def configure(sub):
    p = sub.add_parser('review-plan', help='Prepare up to five evidence-backed review candidates')
    p.add_argument('target', nargs='?', default='.')
    p.add_argument('--limit', type=int, default=5); p.add_argument('--offset', type=int, default=0)
    p.add_argument('--scan-offset', type=int, default=0)
    p.add_argument('--max-bytes', type=int)
    p = sub.add_parser('review-record', help='Record a review with evidence and a follow-up date')
    p.add_argument('reference'); p.add_argument('--fingerprint', required=True)
    p.add_argument('--source', required=True); p.add_argument('--reason', required=True)
    p.add_argument('--retry-after', required=True)
    p.add_argument('--scan-scope'); p.add_argument('--scan-offset', type=int, default=0)
    p = sub.add_parser('verify-plan', help='Read claims, scope, and verification paths')
    p.add_argument('reference'); p.add_argument('--claim'); p.add_argument('--offset', type=int, default=0)
    p.add_argument('--max-bytes', type=int)
    for name in ('verify-draft', 'learn-draft'):
        p = sub.add_parser(name, help='Prepare an evidence-backed verification or lesson draft bound to a revision')
        p.add_argument('reference'); p.add_argument('--file', type=Path, required=True)
        p.add_argument('--out', type=Path, required=True)
        if name == 'verify-draft': p.add_argument('--claim', required=True)
    p = sub.add_parser('project-register', help='Explicitly map a project folder')
    p.add_argument('target'); p.add_argument('cluster')
    p = sub.add_parser('project-unregister', help='Remove a local folder mapping')
    p.add_argument('target')
    sub.add_parser('projects', help='Show local project mappings')
    p = sub.add_parser('session-open', help='Offer project context and start a handoff')
    p.add_argument('target', nargs='?', default='.')
    p.add_argument('--agent', default='codex'); p.add_argument('--task', default='')
    p.add_argument('--budget', type=int, default=1200); p.add_argument('--session-id')
    p.add_argument('--runtime-id'); p.add_argument('--runtime-source')
    p.add_argument('--view', choices=('compact', 'full'), default='compact')
    p.add_argument('--max-bytes', type=int)
    p = sub.add_parser('session-context', help='Load task context for an existing session')
    p.add_argument('session_id'); p.add_argument('--task', default='')
    p.add_argument('--budget', type=int, default=1200)
    p.add_argument('--view', choices=('compact', 'full'), default='compact')
    p.add_argument('--max-bytes', type=int)
    p = sub.add_parser('evidence', help='Read an exact note revision and specific evidence')
    p.add_argument('reference')
    selector = p.add_mutually_exclusive_group()
    selector.add_argument('--section'); selector.add_argument('--source')
    p.add_argument('--offset', type=int, default=0); p.add_argument('--limit', type=int, default=4000)
    p.add_argument('--max-bytes', type=int)
    p = sub.add_parser('session-draft', help='Prepare a structured handoff')
    p.add_argument('session_id'); p.add_argument('--note'); p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('session-close', help='Save a handoff or close without changing knowledge')
    p.add_argument('session_id')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--file', type=Path); mode.add_argument('--no-change', action='store_true')
    p = sub.add_parser('session-status', help='Inspect a session receipt')
    p.add_argument('session_id')
    p = sub.add_parser('session-end', help='Record runtime end without inventing a handoff')
    p.add_argument('session_id'); p.add_argument('--reason', default='other')
    p = sub.add_parser('session-feedback', help='Report explicit use and usefulness as metadata')
    p.add_argument('session_id'); p.add_argument('--outcome', required=True, choices=('helpful', 'unhelpful', 'not_used'))
    p.add_argument('--used', action='append', default=[], metavar='ID@REVISION')
    p.add_argument('--cited', action='append', default=[], metavar='ID@REVISION')
    p.add_argument('--missing', default='none', choices=('none', 'context', 'stale', 'evidence', 'decision', 'next_step', 'other'))
    p.add_argument('--actor', choices=('agent', 'user'), default='agent')
    p = sub.add_parser('session-metrics', help='Count offered context and explicit feedback separately')
    p.add_argument('--cluster')
    p = sub.add_parser('doctor', help='Inspect local integration and state read-only')
    p.add_argument('--home', type=Path)
    p = sub.add_parser('contract', help='Show the session contract and JSON schemas')
    p.add_argument('--schema')
    for name in ('why', 'impact'):
        p = sub.add_parser(name, help='Decision evidence' if name == 'why' else 'Dependent notes and projects')
        p.add_argument('id'); p.add_argument('--limit', type=int, default=5)
        if name == 'impact': p.add_argument('--depth', type=int, default=4)
    for name in ('lessons', 'changes'):
        p = sub.add_parser(name, help='Relevant lessons' if name == 'lessons' else 'Changes and open reviews')
        p.add_argument('target', nargs='?', default='.')
        p.add_argument('--limit', type=int, default=5)
        if name == 'lessons': p.add_argument('--task', default='')
        else:
            scope = p.add_mutually_exclusive_group()
            scope.add_argument('--since'); scope.add_argument('--session-id')
    p = sub.add_parser('feedback', help='Rate search results as helpful or unhelpful')
    p.add_argument('query')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--helpful', action='store_true'); mode.add_argument('--unhelpful', action='store_true')
    p.add_argument('--result', action='append', default=[]); p.add_argument('--cluster')
    p.add_argument('--actor', default='user')


def execute(args, root):
    return agent_contract.success(args.command, _execute(args, root))


def _references(values):
    result = []
    for value in values:
        if '@' not in value:
            raise agent_contract.AgentError('INVALID_ARGUMENT', 'Reference must be ID@REVISION')
        ident, revision = value.rsplit('@', 1)
        result.append({'id': ident, 'revision': revision})
    return result


def _execute(args, root):
    command = args.command
    if command in {'review-plan', 'review-record'}:
        if __package__:
            from . import curation
        else:
            import curation
        if command == 'review-plan': return curation.plan(root, args.target, args.limit, args.offset, args.max_bytes, args.scan_offset)
        return curation.record(root, args.reference, args.fingerprint, args.source, args.reason, args.retry_after, args.scan_scope, args.scan_offset)
    if command in {'verify-plan', 'verify-draft', 'learn-draft'}:
        if __package__:
            from . import knowledge
        else:
            import knowledge
        if command == 'verify-plan': return knowledge.verify_plan(root, args.reference, args.claim, args.offset, args.max_bytes)
        return knowledge.write_draft(root, args.reference, args.file, args.out, getattr(args, 'claim', None))
    if command == 'evidence':
        if __package__:
            from . import evidence
        else:
            import evidence
        return evidence.read(root, args.reference, args.section, args.source, args.offset, args.limit, args.max_bytes)
    if command == 'doctor':
        if __package__:
            from . import doctor
        else:
            import doctor
        return doctor.inspect(root, home=args.home)
    if command == 'contract':
        return {'schema': agent_contract.describe(args.schema)} if args.schema else agent_contract.describe()
    if command in {'project-register', 'project-unregister', 'projects'}:
        if __package__:
            from . import project_context
        else:
            import project_context
        if command == 'project-unregister':
            return project_context.unregister_project(root, args.target)
        return (project_context.register_project(root, args.target, args.cluster)
                if command == 'project-register' else project_context.list_projects(root))
    if command.startswith('session-'):
        if __package__:
            from . import sessions
        else:
            import sessions
        if command == 'session-open':
            return sessions.open_session(root, args.target, agent=args.agent, task=args.task,
                                         budget=args.budget, session_id=args.session_id,
                                         runtime_id=args.runtime_id, runtime_source=args.runtime_source,
                                         view=args.view, max_bytes=args.max_bytes)
        if command == 'session-end':
            return sessions.end_session(root, args.session_id, args.reason)
        if command == 'session-feedback':
            return sessions.feedback(root, args.session_id, args.outcome, _references(args.used),
                                     _references(args.cited), args.missing, args.actor)
        if command == 'session-metrics':
            return sessions.metrics(root, args.cluster)
        if command == 'session-context':
            return sessions.session_context(root, args.session_id, task=args.task, budget=args.budget,
                                            view=args.view, max_bytes=args.max_bytes)
        if command == 'session-draft':
            value = sessions.draft_handoff(root, args.session_id, note_id=args.note)
            # A prepared draft never silently replaces another agent's work.
            with args.out.open('x', encoding='utf-8') as out:
                json.dump(value, out, ensure_ascii=False, indent=2)
                out.write('\n')
            return {'session_id': args.session_id, 'draft': str(args.out)}
        if command == 'session-close':
            payload = json.loads(args.file.read_text()) if args.file else None
            return sessions.close_session(root, args.session_id, payload=payload, no_change=args.no_change)
        return sessions.session_status(root, args.session_id)
    if command == 'feedback':
        if __package__:
            from . import hive
        else:
            import hive
        return hive.feedback(root, args.query, args.helpful, result_ids=args.result,
                             cluster=args.cluster, actor=args.actor)
    if __package__:
        from . import insights
    else:
        import insights
    if command == 'why': return insights.explain(root, args.id, limit=args.limit)
    if command == 'impact': return insights.impact(root, args.id, limit=args.limit, depth=args.depth)
    if command == 'lessons': return insights.lessons(root, args.target, task=args.task, limit=args.limit)
    if command == 'changes': return insights.changes(root, args.target, since=args.since, limit=args.limit, session_id=args.session_id)
    raise ValueError('Unknown workflow command: ' + command)


def display(result, json_mode=False):
    if isinstance(result, dict) and result.get('view') == 'evidence':
        # An excerpt without its source state/revision is misleading, including
        # an empty excerpt for changed or inaccessible source bytes.
        json_mode = True
    if not json_mode and isinstance(result, dict):
        if result.get('scope') == 'read_only_disk_snapshot':
            print('HiveMind Doctor · ' + result['status'])
            for check in result['checks']:
                print('[' + check['status'] + '] ' + check['id'] + ': ' + check['detail'])
            return
        if 'context' in result and isinstance(result['context'], dict):
            print(result['context'].get('text', ''))
            if result.get('view') == 'compact':
                for ref in result['refs']:
                    print('Evidence: ' + ref['ref'] + ' · ' + ref['title'] + ' · ' + ref['evidence'] + ' · ' + ref['freshness'])
                print('Load more: evidence ID@REVISION; full view: session-context ' + result['session_id'] + ' --view full')
                print('Ausgelassen: ' + json.dumps(result['omitted'], ensure_ascii=False))
            print('Session: ' + result['session_id'] + ' · context offered')
            return
        if 'text' in result:
            print(result['text'], end='' if result['text'].endswith('\n') else '\n')
            return
        if result.get('status') in {'saved', 'no_change'}:
            print('Handoff saved.' if result['status'] == 'saved' else 'Session closed without knowledge changes.')
            print('Session: ' + result['session_id'])
            for note in result.get('notes', []):
                print('  ' + note['id'] + ' · Revision ' + note['revision'])
            return
    if __package__:
        from .context_packet import wire
    else:
        from context_packet import wire
    print(wire(result), end='')


def dispatch(argv, default_root):
    """Handle new commands for the human launcher; None leaves legacy parsing intact."""
    common = ArgumentParser(add_help=False)
    common.add_argument('--root', type=Path, default=default_root)
    common.add_argument('--json', action='store_true'); common.add_argument('--plain', action='store_true')
    global_args, remaining = common.parse_known_args(argv)
    if not remaining or remaining[0] not in COMMANDS:
        return None
    parser = ArgumentParser(prog='hive', description=__doc__)
    configure(parser.add_subparsers(dest='command', required=True))
    args = parser.parse_args(remaining)
    try:
        result = execute(args, global_args.root.expanduser().resolve())
        display(result, global_args.json)
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
        if global_args.json: print(json.dumps(agent_contract.failure(error), ensure_ascii=False), file=sys.stderr)
        else: print('HiveMind: ' + str(error), file=sys.stderr)
        return 1

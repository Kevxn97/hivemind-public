"""Read-only diagnostics of stored knowledge and local agent integration.

This is an on-disk snapshot, not proof that a host trusted or executed a hook.
No locks, indexes, configuration, receipts, or repairs are written here.
"""
import argparse
import json
from pathlib import Path
if __package__:
    from .runtime_paths import default_vault
else:
    from runtime_paths import default_vault
import re
import shlex
import sys

# Standalone diagnostics must not create Python caches next to the inspected code.
sys.dont_write_bytecode = True
if __package__:
    from . import hive
else:
    import hive
if __package__:
    from . import project_context
else:
    import project_context
if __package__:
    from .agent_contract import validate as validate_contract
else:
    from agent_contract import validate as validate_contract

START, END = '<!-- HIVEMIND:START -->', '<!-- HIVEMIND:END -->'
SESSION_ID = re.compile(r'^[0-9a-f]{32}$')


def _check(ident, status, detail, **metadata):
    return {'id': ident, 'status': status, 'detail': detail, **metadata}


def _canonical(root):
    if not root.is_dir() or not (root / 'wiki').is_dir():
        return _check('canonical', 'error', 'Vault is missing; check the selected path.', state='missing_store'), None
    if (root / '.pending.json').exists():
        return _check('canonical', 'error', 'Pending transaction: check the documented recover workflow before continuing.', state='pending_transaction'), None
    try:
        records = hive.load_notes(root)
        types = {ident: row[0]['type'] for ident, row in records.items()}
        for note, _, _ in records.values():
            hive.validate(note, types)
        hive.audit(root, records)
        if (root / '.pending.json').exists():
            return _check('canonical', 'attention', 'A transaction started during the check; retry after it finishes.', state='changed_during_check'), None
        return _check('canonical', 'ok', 'The note structure and journal match in the observed state; this does not establish factual correctness.',
                      state='valid', notes=len(records)), records
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return _check('canonical', 'error', 'Note structure or journal cannot be read consistently; investigate with hive check.', state='invalid'), None


def _rules(root, home, agent):
    ident = agent + '.rules'
    path = home / ('.' + agent) / ('AGENTS.md' if agent == 'codex' else 'CLAUDE.md')
    if not path.exists():
        return _check(ident, 'attention', 'No global instructions file is present; an integration may be configured elsewhere.', state='not_installed')
    try:
        text = path.read_text()
        starts, ends = text.count(START), text.count(END)
        if starts != ends or starts > 1 or (starts == 1 and text.index(START) >= text.index(END)):
            return _check(ident, 'error', 'The HiveMind instruction block is incomplete or duplicated.', state='invalid')
        if not starts:
            delegated = 'START.md' in text and ('HiveMind' in text or str(root) in text)
            return _check(ident, 'attention',
                          'The instructions point to START.md; no managed block can be compared directly. This does not prove a runtime failure.' if delegated else
                          'No managed HiveMind instruction block found; project instructions may still apply.',
                          state='delegated' if delegated else 'not_installed')
        template = root / 'integrations' / (agent + '-global.md')
        if not template.is_file():
            template = Path(__file__).resolve().parent / 'integrations' / (agent + '-global.md')
        expected = template.read_text().strip()
        block = text[text.index(START):text.index(END) + len(END)].strip()
        if block == expected:
            return _check(ident, 'ok', 'The installed HiveMind instruction block matches the repository template.', state='current')
        return _check(ident, 'attention', 'The installed HiveMind instruction block differs from the repository template; review differences before an authorized update.', state='drift')
    except (OSError, ValueError):
        return _check(ident, 'error', 'The instructions file or comparison template is unreadable.', state='unreadable')


def _command(command, root):
    """Classify a known adapter without executing or returning command contents."""
    try:
        args = shlex.split(command)
    except ValueError:
        return 'invalid' if 'hivemind' in command.lower() or str(root) in command else None
    scripts = [arg for arg in args if Path(arg).name in {'hive.py', 'codex_session_start.py', 'claude_session_start.py'}]
    if not scripts:
        return 'unsupported' if 'hivemind' in command.lower() or str(root) in command else None
    for index, arg in enumerate(args):
        if arg == '--root':
            if index + 1 == len(args):
                return 'invalid'
            target = args[index + 1]
        elif arg.startswith('--root='):
            target = arg.split('=', 1)[1]
        else:
            continue
        path = Path(target).expanduser()
        if not path.is_absolute() or path.resolve() != root:
            return 'other_store'
    for script in scripts:
        path = Path(script).expanduser()
        if not path.is_absolute():
            return 'unsupported'
        if path.resolve() not in {root / 'hive.py', root / 'integrations/codex_session_start.py', root / 'integrations/claude_session_start.py'}:
            return 'other_store'
        if not path.is_file():
            return 'missing_adapter'
        if path.name.endswith('_session_start.py') or ('--hook' in args and 'brief' in args):
            return 'configured'
    return 'unsupported'


def _hooks(root, home, agent):
    path = home / ('.' + agent) / ('hooks.json' if agent == 'codex' else 'settings.json')
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
            raise ValueError('Invalid hooks')
        hooks = data.get('hooks', {})
        rows = []
        for event, suffix in [('SessionStart', 'session_start'), ('SessionEnd', 'session_end')]:
            groups = hooks.get(event, [])
            if not isinstance(groups, list):
                raise ValueError('Invalid groups')
            found = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get('hooks', []), list):
                    raise ValueError('Invalid group')
                for hook in group.get('hooks', []):
                    if not isinstance(hook, dict):
                        raise ValueError('Invalid hook')
                    command = hook.get('command')
                    if command is None:
                        continue
                    if not isinstance(command, str):
                        raise ValueError('Invalid command')
                    state = _command(command, root)
                    if state == 'configured':
                        matcher = group.get('matcher', '')
                        if not isinstance(matcher, str):
                            state = 'invalid'
                        elif matcher not in ('', '*'):
                            try:
                                values = ('startup', 'resume', 'clear', 'compact', 'fork') if event == 'SessionStart' else (
                                    ('other',) if agent == 'codex' else ('clear', 'resume', 'logout', 'prompt_input_exit', 'other'))
                                if not all(re.search(matcher, value) for value in values):
                                    state = 'limited_events'
                            except re.error:
                                state = 'invalid'
                    if state:
                        found.append(state)
            if not found:
                rows.append(_check(agent + '.' + suffix, 'attention', event + ': no HiveMind command found in this configuration.', state='not_installed', configured=0))
            elif found == ['configured']:
                rows.append(_check(agent + '.' + suffix, 'ok', event + ': a known local adapter is configured; its actual execution has not been checked here.', state='configured', configured=1))
            else:
                errors = any(s in {'invalid', 'missing_adapter'} for s in found)
                rows.append(_check(agent + '.' + suffix, 'error' if errors else 'attention',
                                   event + ': check the adapter path, command, or duplicate configuration.',
                                   state='needs_review', configured=len(found), adapter_states=found))
        return rows
    except (OSError, ValueError):
        return [_check(agent + '.hooks', 'error', 'Hook configuration cannot be read as valid configuration; private content is not displayed.', state='invalid')]


def _projects(root, records):
    path = root / project_context.REGISTRY
    if not path.exists():
        return _check('projects', 'attention', 'No explicit project mappings; existing legacy mappings or projects/<id> may still work.', state='not_configured', registered=0)
    try:
        rows = project_context._registry(root)['mappings']
        missing = sum(not Path(row['path']).is_dir() for row in rows)
        inactive = sum(not any(hive.active(note) and note['sensitivity'] != 'personal' and hive.cluster_for(note) == row['cluster']
                               for note, _, _ in records.values()) for row in rows) if records is not None else None
        attention = bool(missing or inactive or records is None)
        return _check('projects', 'attention' if attention else 'ok',
                      'Check the project folder or target cluster; an invalid vault makes the cluster check incomplete.' if attention else
                      'Explicit mappings point to existing folders and active internal project areas.',
                      state='needs_review' if attention else 'valid', registered=len(rows), missing_folders=missing, inactive_projects=inactive)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return _check('projects', 'error', 'Project mappings are not valid or readable; check hive projects and the local registry.', state='invalid')


def _sessions(root):
    folder = root / '.state/sessions'
    if folder.exists() and not folder.is_dir():
        return _check('sessions', 'error', 'The session folder is unreadable.', state='invalid', malformed=1)
    counts = {'total': 0, 'malformed': 0, 'unfinished': 0, 'open': 0, 'ended_without_close': 0, 'write_intents': 0}
    try:
        paths = sorted(folder.glob('*.json'))
        for path in paths:
            counts['total'] += 1
            try:
                record = json.loads(path.read_text())
                validate_contract('receipt', record)
                if not SESSION_ID.fullmatch(path.stem) or record['session_id'] != path.stem:
                    raise ValueError('Mismatched receipt ID')
                runtime = record.get('runtime', {})
                if record['status'] == 'context_offered':
                    counts['unfinished'] += 1
                    counts['ended_without_close' if runtime.get('ended_at') else 'open'] += 1
                    counts['write_intents'] += bool(record.get('intent_sha256'))
                else:
                    hive.timestamp(record['closed_at'])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                counts['malformed'] += 1
        status = 'error' if counts['malformed'] else 'attention' if counts['unfinished'] else 'ok'
        return _check('sessions', status,
                      'Check invalid receipts; no session was modified.' if counts['malformed'] else
                      'Unfinished sessions are present. Open does not prove active; a runtime end does not confirm task success.' if counts['unfinished'] else
                      'No open or invalid session receipts in the observed state.',
                      state='needs_review' if status != 'ok' else 'valid', **counts)
    except OSError:
        return _check('sessions', 'error', 'Session receipts are unreadable.', state='unreadable')


def inspect(root, home=None):
    """Return safe diagnostic metadata without creating or repairing anything."""
    root = Path(root).expanduser().resolve()
    home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    canonical, records = _canonical(root)
    checks = [canonical, _projects(root, records), _sessions(root)]
    for agent in ('codex', 'claude'):
        checks.append(_rules(root, home, agent))
        checks.extend(_hooks(root, home, agent))
    summary = {status: sum(check['status'] == status for check in checks) for status in ('ok', 'attention', 'error')}
    status = 'error' if summary['error'] else 'attention' if summary['attention'] else 'ok'
    return {'status': status, 'scope': 'read_only_disk_snapshot', 'summary': summary, 'checks': checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=default_vault())
    parser.add_argument('--home', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.root, args.home), ensure_ascii=False, indent=2))
    return 0  # Findings live in status; producing the diagnostic report succeeded.


if __name__ == '__main__':
    raise SystemExit(main())

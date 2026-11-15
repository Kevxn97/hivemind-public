"""Guide a new user from a project folder to the first retrievable memory."""
import argparse
from pathlib import Path
import shlex
import sqlite3
import sys

from . import connections, hive, onboarding, project_context
from .runtime_paths import cli_command


def configure(sub):
    parser = sub.add_parser('setup', help='Set up a project, connect an agent and save a first memory')
    parser.add_argument('--project', type=Path, help='Project folder to remember')
    parser.add_argument('--name', help='Project name, defaults to the folder name')
    parser.add_argument('--agent', choices=('codex', 'claude', 'none'))
    parser.add_argument('--remember', help='First short memory for the project')
    parser.add_argument('--remember-type', choices=('decision', 'lesson'), default='lesson',
                        help='Type of the first memory')
    parser.add_argument('--connect', action='store_true', help='Install the selected agent connection')
    parser.add_argument('--yes', action='store_true', help='Confirm the supplied setup and first memory')
    return parser


def _ask(label, default=''):
    print(label + (f' [{default}]' if default else '') + ' > ', end='', flush=True)
    return input().strip() or default


def _confirm(label):
    while True:
        answer = _ask(label + ' [Y/n]').lower()
        if answer in {'', 'y', 'yes'}:
            return True
        if answer in {'n', 'no'}:
            return False
        print('Enter y or n.')


def _project_default(root):
    cwd = Path.cwd().resolve()
    code = Path(__file__).resolve().parent.parent
    if cwd != code and code not in cwd.parents and cwd != Path.home().resolve():
        return str(cwd)
    if (root / onboarding.MARKER).is_file():
        projects = [p for p in project_context.list_projects(root) if p['active'] and p['exists']]
        if len(projects) == 1:
            return projects[0]['folder']
    return ''


def _folder(value, root):
    if not value:
        raise ValueError('Choose your project folder with --project PATH, or run setup in a terminal.')
    folder = Path(value).expanduser().resolve()
    home = Path.home().resolve()
    if not folder.is_dir() or folder == home or folder in home.parents:
        raise ValueError('Choose an existing project folder, outside your home or root directory.')
    if folder == root or root in folder.parents:
        raise ValueError('Choose a project folder outside the knowledge vault.')
    return folder


def _bind_project(root, folder, name):
    resolved = project_context.resolve_project(root, str(folder))
    if resolved is not None:
        if name is not None:
            ident = resolved['cluster'].split('/')[-1]
            record = project_context._records(root).get(ident)
            if record is None or record[0]['title'] != name:
                raise ValueError('This folder already belongs to a project. Use its current name or choose another folder.')
        return resolved
    title = onboarding._title(name or folder.name)
    note = onboarding._project_note(title)
    records = project_context._records(root)
    if note['id'] in records:
        mapped = [p for p in project_context.list_projects(root) if p['cluster'] == note['cluster']]
        original = records[note['id']][0]
        recoverable = (not mapped and original['type'] == 'project' and hive.active(original)
                       and original['title'] == title and any(s['resource'] == 'user:onboarding' for s in original['sources']))
        if not recoverable:
            raise ValueError('That project name is already in use. Choose another --name for this folder.')
    else:
        hive.apply(root, {'changes': [{'expected_revision': 'new', 'note': note}]},
                   actor='hivemind-setup', reason='Register the selected project', context='user:onboarding')
    return project_context.register_project(root, folder, note['cluster'])


def _first_memory(root, project, text, note_type):
    from . import source_connections
    selection = source_connections.remember_selection(root, project['cluster'], text, 'user:setup',
                                                      note_type, setup=True)
    return source_connections.capture_selection(root, selection, yes=True, note_type=note_type)


def _connection(root, agent, interactive, install):
    if agent == 'none':
        return None
    args = argparse.Namespace(command='connect', agent=agent, agent_home=None, apply=False)
    result = connections.execute(args, root)
    if interactive and result['changed']:
        print('\nConnect ' + agent)
        print('HiveMind will add its project-memory instructions and session hooks to these local settings.')
        for path in result['changes']:
            if path != str(root) and not str(path).startswith(str(root) + '/'):
                print('  ' + path)
        try:
            install = install or _confirm('Connect this agent now?')
        except EOFError:
            install = False
    if install:
        args.apply = True
        result = connections.execute(args, root)
    return result


def _text(result):
    project = result['project']
    lines = ['Ready — HiveMind project memory', '',
             'Project  ' + project['folder'], 'Vault    ' + result['vault']]
    memory = result['memory']
    if memory is not None:
        lines.append('Remembered  ' + memory['note']['summary'])
    connection = result['connection']
    if connection is not None:
        if connection['status'] == 'preview':
            lines.extend(['', 'Review and install the agent connection when ready.', result['commands']['connect']])
        else:
            lines.extend(['', 'Open ' + result['agent'] + ' in your project folder.'])
            if result['agent'] == 'codex':
                lines.append('Review HiveMind hooks with /hooks in Codex, then open a fresh session.')
            else:
                lines.append('Review the session hooks in Claude Code, then open a fresh session.')
            lines.append('Try asking “What do you remember about this project?”')
    lines.extend(['', 'Remember a decision', result['commands']['remember'],
                  '', 'Connect Gmail, Calendar or another source', result['commands']['sources']])
    lines.extend(['','Optional background sync and weekly review',result['commands']['automation'],
                  'Start with everything off; choose sources and activate each feature when ready.'])
    return '\n'.join(lines)


def execute(args, root):
    root = Path(root).expanduser().resolve()
    interactive = sys.stdin.isatty() and not args.yes and not getattr(args, 'json', False)
    if not interactive and not args.yes:
        raise ValueError('Use --yes with --project PATH for noninteractive setup, or run setup in a terminal.')
    try:
        # Detect missing SQLite FTS5 before creating a vault.
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE VIRTUAL TABLE probe USING fts5(content)')
    except sqlite3.Error as error:
        raise ValueError('This Python build needs SQLite FTS5 support. Use a Python 3.11+ installation with FTS5.') from error
    if (root / onboarding.MARKER).exists():
        onboarding._validate_existing(root)
    value = str(args.project) if args.project is not None else _project_default(root)
    agent = args.agent or 'none'
    memory = args.remember
    if interactive:
        print('Welcome to HiveMind\nChoose a project, an agent and one thing to remember.\n')
        try:
            value = _ask('Project folder', value)
            agent = _ask('Agent — codex, claude or none', args.agent or 'codex').lower()
            memory = _ask('First memory — Enter to skip', memory or '') or None
        except EOFError:
            return {'status': 'cancelled', 'text': 'Setup cancelled. Run setup again when ready.'}
    folder = _folder(value, root)
    if agent not in {'codex', 'claude', 'none'}:
        raise ValueError('Choose codex, claude or none with --agent.')
    if args.connect and agent == 'none':
        raise ValueError('--connect requires --agent codex or --agent claude.')
    if memory is not None:
        memory = memory.strip()
        if not memory or len(memory) > 280 or any(ord(char) < 32 for char in memory):
            raise ValueError('Enter a first memory of 1–280 printable characters.')
    if args.name is not None:
        onboarding._title(args.name)
    if interactive:
        print('\nProject  ' + str(folder) + '\nVault    ' + str(root) + '\nAgent    ' + agent)
        if memory:
            print('First memory  ' + memory)
        try:
            if not _confirm('Set up HiveMind?'):
                return {'status': 'cancelled', 'text': 'Setup cancelled.'}
        except EOFError:
            return {'status': 'cancelled', 'text': 'Setup cancelled. Run setup again when ready.'}
    initialized = onboarding.initialize(root, project_path=folder, name=args.name)
    project = initialized['project'] or _bind_project(root, folder, args.name)
    memory_result = _first_memory(root, project, memory, args.remember_type) if memory else None
    connection = _connection(root, agent, interactive, args.connect)
    commands = {'sources': shlex.join(cli_command(root, 'sources')),
                'automation': shlex.join(cli_command(root, 'automation')),
                'remember': shlex.join(cli_command(root, 'remember', 'Your next decision', '--type', 'decision'))}
    if agent != 'none':
        commands['connect'] = shlex.join(cli_command(root, 'connect', agent, '--apply'))
    result = {'status': 'ready', 'vault': str(root), 'initialized': initialized['initialized'],
              'project': project, 'agent': agent, 'connection': connection,
              'memory': memory_result, 'commands': commands}
    result['text'] = _text(result)
    return result

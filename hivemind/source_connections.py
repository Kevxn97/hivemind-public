"""Host-owned source connections and explicit, revision-bound selected capture."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from . import connections, hive, project_context
from .runtime_paths import require_vault

CODEX_DOCS = 'https://learn.chatgpt.com/docs/extend/mcp'
PLUGIN_DOCS = 'https://developers.openai.com/training/walkthroughs/plugins-and-skills'
CLAUDE_DOCS = 'https://code.claude.com/docs/en/mcp'
GOOGLE_DOCS = 'https://support.claude.com/en/articles/10166901-use-google-workspace-connectors'
NAME = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
MAX_CAPTURE_BYTES = 32768
GUIDE = '''Connect a source through your agent, then capture a selected summary.

hive sources setup gmail --agent codex
hive sources setup calendar --agent claude
hive sources setup mcp --agent codex --project PATH --name NAME --url HTTPS_URL
hive sources capture --file selected.json

Review the capture and repeat with --apply --preview TOKEN to save it.
Use hive sources COMMAND --help for options.'''


def configure(sub):
    parser = sub.add_parser('sources', help='Connect host-owned sources and capture selected knowledge')
    actions = parser.add_subparsers(dest='source_action')
    setup = actions.add_parser('setup', help='Show connector guidance or preview project MCP configuration')
    setup.add_argument('kind', choices=('gmail', 'calendar', 'mcp'))
    for item in (setup, actions.add_parser('remove', help='Preview removal of an owned project MCP entry')):
        item.add_argument('--agent', choices=('codex', 'claude'), required=True)
        item.add_argument('--project', type=Path, help='Registered project folder for an MCP connection')
        item.add_argument('--name', help='MCP server name')
        item.add_argument('--apply', action='store_true')
    setup.add_argument('--url', help='Streamable HTTP endpoint, HTTPS without credentials or query')
    capture = actions.add_parser('capture', help='Preview a selected summary and its source before journalled capture')
    capture.add_argument('--file', type=Path, required=True)
    capture.add_argument('--apply', action='store_true')
    capture.add_argument('--preview', help='Token returned by the exact capture preview')
    return parser


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def _text(value, field, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Expected printable ' + field + ' of 1–' + str(maximum) + ' characters')
    return value.strip()


def _guidance(kind, agent):
    label = 'Gmail' if kind == 'gmail' else 'Google Calendar'
    if agent == 'codex':
        steps = ['Open Plugins in your ChatGPT/Codex host and select ' + label + '.',
                 'Connect your account through the host sign-in flow and review the requested access.',
                 'Ask the host for one selected, relevant message or event and review its source link.',
                 'If your host or workspace does not offer this plugin, follow its approved connector setup.']
        docs = [PLUGIN_DOCS, CODEX_DOCS]
    else:
        steps = ['Open https://claude.ai/customize/connectors and connect ' + label + ' there.',
                 'Sign into Claude Code with the same claude.ai account and check /mcp for the connector.',
                 'Ask for one selected, relevant message or event and review its source link.']
        docs = [GOOGLE_DOCS, CLAUDE_DOCS]
    template = {'project': 'projects/your-project', 'title': 'Selected project update',
                'summary': 'Write the durable conclusion you reviewed.',
                'source': {'kind': kind, 'resource': 'https://example.com/selected-source',
                           'title': 'Selected ' + label + ' source', 'observed_at': hive.now()}}
    text = '\n'.join(steps) + '\n\nSave your reviewed selection as selected.json.\n' + _json(template).decode()
    text += '\nhive sources capture --file selected.json\nReview the preview, then repeat with --apply --preview TOKEN.\n'
    text += '\nThe host owns authentication. Connection availability and source facts require host verification.\n'
    text += '\n'.join(docs)
    return {'action': 'setup', 'kind': kind, 'agent': agent, 'applied': False,
            'host_connection': 'unverified', 'authentication_owner': 'host',
            'steps': steps, 'documentation': docs, 'capture_template': template, 'text': text}


def _local_path(value, label):
    requested = Path(value).expanduser().absolute()
    system_aliases = {Path('/tmp'): Path('/private/tmp'), Path('/var'): Path('/private/var')}
    for path in (requested, *requested.parents):
        if path.is_symlink() and (path not in system_aliases or path.resolve() != system_aliases[path]):
            raise ValueError(label + ' must use real paths without symlinks')
    return requested.resolve()


def _project_folder(root, target):
    if target is None:
        raise ValueError('MCP setup requires --project PATH for a registered project')
    folder = _local_path(target, 'Project folder')
    code = Path(__file__).resolve().parent
    if (not folder.is_dir() or folder == root or root in folder.parents or folder in root.parents
            or folder == code or code in folder.parents or folder in code.parents):
        raise ValueError('Choose an existing project folder separate from the vault and application')
    resolved = project_context.resolve_project(root, str(folder))
    if resolved is None or resolved['folder'] != str(folder):
        raise ValueError('Register the exact project folder with hive project-register before MCP setup')
    return folder


def _endpoint(value):
    value = _text(value, 'MCP URL', 2000)
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and bool(parsed.hostname) and parsed.port != 0
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment and '?' not in value and '#' not in value
                 and not any(c.isspace() for c in value) and '\\' not in value)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Use an HTTPS MCP endpoint without user info, query, fragment or embedded credentials')
    return value


def _locations(root, args):
    name = args.name
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('Choose --name with 1–64 lowercase letters, digits, underscores or hyphens, starting with a letter')
    folder = _project_folder(root, args.project)
    path = folder / ('.codex/config.toml' if args.agent == 'codex' else '.mcp.json')
    state_dir = root / '.state/source-connections'
    if any(p.is_symlink() for p in (root / '.state', state_dir, path.parent)):
        raise ValueError('Source configuration directories must be real directories')
    key = _hash((args.agent + '\0' + str(path) + '\0' + name).encode())[:24]
    return folder, path, state_dir / (key + '.json')


def _decode_config(raw, agent):
    try:
        value = {} if raw is None else (tomllib.loads(raw.decode()) if agent == 'codex' else json.loads(raw))
    except (ValueError, UnicodeError):
        raise ValueError('Invalid ' + agent + ' MCP configuration. Review its syntax before retrying.') from None
    field = 'mcp_servers' if agent == 'codex' else 'mcpServers'
    if not isinstance(value, dict) or not isinstance(value.get(field, {}), dict):
        raise ValueError('Expected an MCP server mapping in the host configuration')
    return value, field


def _config_plan(root, args):
    folder, path, state_path = _locations(root, args)
    raw, state_raw = connections._read(path), connections._read(state_path)
    value, field = _decode_config(raw, args.agent)
    state = connections._decode_json(state_raw, state_path) if state_raw is not None else None
    existing = value.get(field, {}).get(args.name)
    if state is not None:
        if (state.get('version') != 1 or state.get('root') != str(root) or state.get('path') != str(path)
                or state.get('agent') != args.agent or state.get('name') != args.name
                or state.get('transaction') != 'complete' or not isinstance(state.get('entry'), dict)
                or not isinstance(state.get('config_existed'), bool)
                or not isinstance(state.get('mapping_existed'), bool)):
            raise ValueError('Source ownership state is invalid or interrupted. Review ' + str(state_path))
        if existing != state['entry']:
            raise ValueError('Managed MCP entry has drifted. Review the host configuration and recorded ownership before retrying.')
        if args.agent == 'codex' and (not isinstance(state.get('suffix'), str) or not state['suffix']
                                     or raw.decode().count(state['suffix']) != 1):
            raise ValueError('Managed MCP configuration block has drifted. Review it before retrying.')
    elif existing is not None:
        raise ValueError('This MCP server name already exists without ownership for this vault. Choose another name.')
    if args.source_action == 'remove':
        if state is None:
            return folder, path, state_path, raw, state_raw, {}, None
        if args.agent == 'codex':
            updated = raw.decode().replace(state['suffix'], '', 1).encode()
        else:
            updated_value = copy.deepcopy(value)
            del updated_value[field][args.name]
            if not updated_value[field] and not state['mapping_existed']:
                del updated_value[field]
            updated = _json(updated_value)
        if not state['config_existed'] and (not updated.strip() or args.agent == 'claude' and json.loads(updated) == {}):
            updated = None
        _decode_config(updated, args.agent)
        return folder, path, state_path, raw, state_raw, {path: updated, state_path: None}, state['entry']
    entry = {'url': _endpoint(args.url)}
    if args.agent == 'claude':
        entry = {'type': 'http', **entry}
    if state is not None:
        if state['entry'] != entry:
            raise ValueError('Owned MCP settings differ. Remove the existing owned entry before configuring a new endpoint.')
        return folder, path, state_path, raw, state_raw, {}, entry
    suffix = ''
    if args.agent == 'codex':
        suffix = ('\n\n' if raw else '') + '# hivemind source ' + args.name + ' begin\n'
        suffix += '[mcp_servers.' + args.name + ']\nurl = ' + json.dumps(entry['url']) + '\n'
        suffix += '# hivemind source ' + args.name + ' end\n'
        updated = (raw or b'') + suffix.encode()
    else:
        updated_value = copy.deepcopy(value)
        updated_value.setdefault(field, {})[args.name] = entry
        updated = _json(updated_value)
    _decode_config(updated, args.agent)
    state = {'version': 1, 'transaction': 'complete', 'root': str(root), 'agent': args.agent,
             'name': args.name, 'path': str(path), 'entry': entry, 'suffix': suffix,
             'config_existed': raw is not None, 'mapping_existed': field in value}
    return folder, path, state_path, raw, state_raw, {path: updated, state_path: _json(state)}, entry


def _write_config(plan):
    _, path, state_path, raw, state_raw, changes, _ = plan
    for target, original in ((path, raw), (state_path, state_raw)):
        if connections._read(target) != original:
            raise ValueError('Source configuration changed during planning. Preview again.')
    path.parent.mkdir(exist_ok=True, mode=0o700)
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    pending = {'version': 1, 'transaction': 'pending', 'path': str(path),
               'before_sha256': _hash(raw) if raw is not None else None,
               'after_sha256': _hash(changes[path]) if changes[path] is not None else None}
    connections._atomic(state_path, _json(pending))
    try:
        connections._atomic(path, changes[path], mode)
        connections._atomic(state_path, changes[state_path])
    except BaseException:
        # The directory locks serialize cooperating HiveMind writers. Preserve
        # outside changes and leave pending hashes for review if rollback fails.
        current = connections._read(path)
        if current == changes[path]:
            connections._atomic(path, raw, mode)
            connections._atomic(state_path, state_raw)
        elif current == raw:
            connections._atomic(state_path, state_raw)
        raise


def _setup_mcp(root, args):
    plan = _config_plan(root, args)
    applied = False
    if args.apply:
        from . import automation
        with connections._directory_lock(root), connections._directory_lock(plan[0]):
            plan = _config_plan(root, args)
            with hive.locked(root):
                automation.require_running(root)
                if plan[5]:
                    _write_config(plan)
                    applied = True
    _, path, state_path, _, _, changes, entry = plan
    removing = args.source_action == 'remove'
    steps = [] if removing else [
        'Start the host in this project and review its MCP settings and project trust.',
        'Authenticate in the host if the server requires OAuth, then check /mcp.',
        'Use a selected source summary with hive sources capture --file selected.json.']
    status = 'removed' if removing and args.apply else 'configured' if args.apply else 'preview'
    text = ('Remove' if removing else 'Configure') + ' ' + args.agent + ' MCP server ' + args.name + '\n'
    text += 'Project configuration ' + str(path) + '\n'
    if entry:
        text += _json(entry).decode()
    text += ('Applied locally.\n' if applied else 'No local changes applied.\n')
    if changes and not args.apply:
        text += 'Repeat the same command with --apply to apply these changes.\n'
    text += '\n'.join(steps)
    return {'action': 'remove' if removing else 'setup', 'kind': 'mcp', 'agent': args.agent,
            'name': args.name, 'config_path': str(path), 'ownership_path': str(state_path),
            'entry': entry, 'status': status, 'changed': bool(changes), 'applied': applied,
            'host_connection': 'unverified', 'authentication_owner': 'host',
            'next_steps': steps, 'documentation': [CODEX_DOCS if args.agent == 'codex' else CLAUDE_DOCS], 'text': text}


def _selection(path):
    path = _local_path(path, 'Selected capture file')
    if not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError('Choose a selected JSON object of at most 32768 bytes')
    raw = path.read_bytes()
    if len(raw) > MAX_CAPTURE_BYTES:
        raise ValueError('Selected capture file exceeded 32768 bytes')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('Invalid selected capture JSON') from None
    if not isinstance(value, dict) or set(value) != {'project', 'title', 'summary', 'source'}:
        raise ValueError('Capture requires exactly project, title, summary and source for one reviewed selection')
    source = value['source']
    if not isinstance(source, dict) or set(source) != {'kind', 'resource', 'title', 'observed_at'}:
        raise ValueError('Source requires exactly kind, resource, title and observed_at')
    if source['kind'] not in {'gmail', 'calendar', 'mcp'}:
        raise ValueError('Source kind must be gmail, calendar or mcp')
    value['project'] = _text(value['project'], 'project', 100)
    value['title'] = _text(value['title'], 'title', 200)
    value['summary'] = _text(value['summary'], 'summary', 280)
    source['resource'] = _text(source['resource'], 'source resource', 2000)
    source['title'] = _text(source['title'], 'source title', 200)
    source['observed_at'] = _text(source['observed_at'], 'source observation timestamp', 50)
    hive.timestamp(source['observed_at'])
    if not value['project'].startswith('projects/'):
        raise ValueError('Choose a projects/<id> project for selected capture')
    return raw, value


def _capture(root, args):
    raw, selected = _selection(args.file)
    records = project_context._records(root)
    project_id = selected['project'].split('/')[-1]
    project = records.get(project_id)
    if (project is None or project[0]['type'] != 'project' or not hive.active(project[0])
            or project[0]['sensitivity'] != 'internal' or hive.cluster_for(project[0]) != selected['project']):
        raise ValueError('Choose an active internal project in this vault')
    source = selected['source']
    identity = _hash(json.dumps(selected, sort_keys=True, ensure_ascii=False).encode())
    ident = 'source-' + identity[:24]
    at = hive.now()
    note = {'id': ident, 'type': 'lesson', 'cluster': selected['project'],
            'title': selected['title'], 'summary': selected['summary'],
            'body': '# Selected source summary\n\n' + selected['summary'] + '\n\n'
                    'This user-reviewed selection is reported source content. Its source facts require independent verification.',
            'status': 'draft', 'evidence': 'reported', 'sensitivity': 'internal',
            'aliases': [], 'tags': ['selected-source', source['kind']],
            'sources': [{'id': 'selected-' + identity[:16], 'resource': source['resource'],
                         'title': source['title'], 'observed_at': source['observed_at']}],
            'links': [{'relation': 'about', 'target': project_id}],
            'stale_after': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'created': at, 'generated': {'at': at, 'by': 'sources:capture'}}
    existing = records.get(ident)
    if existing and any(existing[0].get(key) != note[key] for key in ('title', 'summary', 'cluster', 'sources', 'evidence', 'body')):
        raise ValueError('Captured note has changed. Review its journalled revision before importing this selection again.')
    if existing:
        note = existing[0]
    hive.validate(note, {key: row[0]['type'] for key, row in records.items()} | {ident: 'lesson'}, require_cluster=True)
    project_revision = hive.digest(project[1])
    expected = hive.digest(existing[1]) if existing else 'new'
    preview = _hash(_json({'version': 1, 'root': str(root), 'file_sha256': _hash(raw),
                          'project': project_id, 'project_revision': project_revision, 'note_revision': expected}))
    applied, event = False, None
    if args.apply:
        if args.preview != preview:
            raise ValueError('Capture preview is missing or changed. Run sources capture --file FILE and review its new token.')
        from . import automation
        automation.require_running(root)
        if not existing:
            payload = {'expected_revisions': {project_id: project_revision},
                       'changes': [{'expected_revision': 'new', 'note': note}]}
            result = hive.apply(root, payload, 'sources:capture', 'Capture an explicitly reviewed source selection',
                                source['resource'], respect_pause=True)
            applied, event = True, result['event']
    status = 'captured' if applied else 'already_captured' if existing else 'preview'
    text = selected['title'] + '\n' + selected['summary'] + '\n'
    text += 'Project ' + selected['project'] + '\nEvidence reported\nSource ' + source['resource'] + '\n'
    text += 'Source title ' + source['title'] + '\nKind ' + source['kind'] + '\n'
    text += 'Observed ' + source['observed_at'] + '\n'
    text += 'Preview token ' + preview + '\n'
    text += 'Saved to the journal.\n' if applied else 'Already captured.\n' if existing else 'Review, then repeat with --apply --preview ' + preview + '\n'
    return {'action': 'capture', 'status': status, 'applied': applied, 'event': event,
            'preview': preview, 'file_sha256': _hash(raw), 'project_revision': project_revision,
            'expected_revision': expected, 'note': note, 'note_path': str(hive.path_for(root, note).relative_to(root)),
            'source_state': 'reported', 'text': text}


def execute(args, root):
    root = require_vault(Path(root).resolve())
    action = getattr(args, 'source_action', None)
    if action is None:
        return {'action': 'guide', 'applied': False, 'text': GUIDE}
    if action == 'capture':
        return _capture(root, args)
    if action == 'setup' and args.kind in {'gmail', 'calendar'}:
        if args.apply or args.project is not None or args.name is not None or args.url is not None:
            raise ValueError('Gmail and Calendar use host connector setup. Run setup with kind and --agent for guidance.')
        return _guidance(args.kind, args.agent)
    return _setup_mcp(root, args)

"""Explicit, local Codex and Claude connection management.

Only the managed instruction block and exact hook groups belong to HiveMind.
Configuration text stays in the agent home. Vault metadata contains ownership,
paths and hashes, never copies of the user's settings or instruction text.
"""
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import tomllib

BEGIN = '<!-- hivemind:connection:v1 BEGIN -->'
END = '<!-- hivemind:connection:v1 END -->'
AGENTS = {'codex': ('AGENTS.md', 'hooks.json', 'CODEX_HOME'),
          'claude': ('CLAUDE.md', 'settings.json', 'CLAUDE_CONFIG_DIR')}
EVENTS = {'SessionStart': 10, 'SessionEnd': 3}


def configure(sub):
    for action in ('connect', 'disconnect'):
        parser = sub.add_parser(action, help=action.capitalize() + ' a local agent connection')
        parser.add_argument('agent', choices=sorted(AGENTS))
        parser.add_argument('--agent-home', type=Path, help='Agent configuration directory')
        parser.add_argument('--apply', action='store_true', help='Apply the previewed changes')


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _read(path):
    if path.is_symlink():
        raise ValueError('Refusing symlink target ' + str(path))
    if path.exists() and not path.is_file():
        raise ValueError('Expected a regular file at ' + str(path))
    return path.read_bytes() if path.exists() else None


def _decode_json(raw, path):
    try:
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ValueError('Invalid JSON configuration at ' + str(path)) from None
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object at ' + str(path))
    return value


def _locations(root, agent, agent_home):
    if agent not in AGENTS:
        raise ValueError('Supported agents are codex and claude')
    root = Path(root).expanduser().resolve()
    marker = root / '.hivemind.json'
    raw = _read(marker)
    value = _decode_json(raw, marker) if raw is not None else {}
    if value.get('version') != 1 or value.get('kind') != 'hivemind-vault':
        raise ValueError('Select an initialized HiveMind vault with --root')
    rules_name, config_name, env_name = AGENTS[agent]
    home = Path(agent_home or os.environ.get(env_name) or Path.home() / ('.' + agent)).expanduser().resolve()
    if home.exists() and not home.is_dir():
        raise ValueError('Agent home must be a directory')
    state = root / '.state' / 'connections'
    if (root / '.state').is_symlink() or state.is_symlink():
        raise ValueError('Connection state directories must be local directories')
    key = hashlib.sha256((agent + '\0' + str(home)).encode()).hexdigest()[:24]
    return root, home, home / rules_name, home / config_name, state / (agent + '-' + key + '.json')


def _artifacts(root, agent):
    core = Path(__file__).resolve().with_name('hive.py')
    if not core.is_file():
        raise ValueError('The installed HiveMind core script is missing')
    prefix = [sys.executable, str(core), '--root', str(root)]
    command = shlex.join([*prefix, 'brief', '--hook', '--agent', agent, '--budget', '500'])
    brief = shlex.join([*prefix, 'brief', '/absolute/project/path', '--budget', '500'])
    context = shlex.join([*prefix, 'session-context', 'SESSION_ID', '--task', 'TASK', '--budget', '500'])
    draft = shlex.join([*prefix, 'session-draft', 'SESSION_ID', '--out', 'handoff.json'])
    close = shlex.join([*prefix, 'session-close', 'SESSION_ID', '--file', 'handoff.json'])
    no_change = shlex.join([*prefix, 'session-close', 'SESSION_ID', '--no-change'])
    block = '\n'.join([
        BEGIN, '## HiveMind', '',
        'Local knowledge vault ' + json.dumps(str(root), ensure_ascii=False) + '.',
        'Use relevant HiveMind context with its evidence and freshness information.',
        'Read the vault START.md for the local knowledge workflow.',
        'Retrieve project context with a 500-word budget using `' + brief + '`.',
        'Reuse the supplied HiveMind session ID for context and handoff calls.',
        'When the task is known, run `' + context + '`.',
        "Write durable knowledge within the user's authorized task using the journaled handoff workflow.",
        'Prepare a handoff with `' + draft + '`, review its evidence, and save with `' + close + '`.',
        'When there is no durable update, finish with `' + no_change + '`.', END, '\n'])
    groups = {event: {'hooks': [{'type': 'command', 'command': command, 'timeout': timeout}]}
              for event, timeout in EVENTS.items()}
    return block, groups


def _config(raw, path):
    value = {} if raw is None else _decode_json(raw, path)
    hooks = value.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Expected a hooks object at ' + str(path))
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise ValueError('Expected hook group lists at ' + str(path))
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError('Invalid hook group at ' + str(path))
            if any(not isinstance(entry, dict) for entry in group['hooks']):
                raise ValueError('Invalid hook entry at ' + str(path))
    return value


def _hive_group(group):
    for entry in group.get('hooks', []):
        command = entry.get('command', '')
        if not isinstance(command, str):
            continue
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if any(Path(arg).name == 'hive.py' for arg in argv) and all(arg in argv for arg in ('brief', '--hook', '--root', '--agent')):
            return True
    return False


def _valid_owned_group(group, timeout):
    if not isinstance(group, dict) or set(group) != {'hooks'}:
        return False
    hooks = group['hooks']
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        return False
    hook = hooks[0]
    return (set(hook) == {'type', 'command', 'timeout'} and hook['type'] == 'command'
            and isinstance(hook['command'], str) and bool(hook['command'])
            and type(hook['timeout']) is int and hook['timeout'] == timeout)


def _load(root, agent, agent_home):
    root, home, rules_path, config_path, state_path = _locations(root, agent, agent_home)
    originals = {path: _read(path) for path in (rules_path, config_path, state_path)}
    raw_rules = originals[rules_path]
    try:
        rules = '' if raw_rules is None else raw_rules.decode('utf-8')
    except UnicodeError:
        raise ValueError('Instructions must be UTF-8 at ' + str(rules_path)) from None
    config = _config(originals[config_path], config_path)
    metadata = _decode_json(originals[state_path], state_path) if originals[state_path] is not None else None
    if metadata is not None:
        if (metadata.get('version') != 1 or metadata.get('agent') != agent
                or metadata.get('root') != str(root) or metadata.get('agent_home') != str(home)):
            raise ValueError('Connection ownership metadata is invalid at ' + str(state_path))
        if metadata.get('transaction') == 'pending':
            raise ValueError('An interrupted connection transaction needs review at ' + str(state_path)
                             + '. Compare the recorded hashes and managed artifacts before restoring metadata or files.')
        valid = (metadata.get('transaction') == 'complete'
                 and isinstance(metadata.get('block'), str) and bool(metadata['block'])
                 and isinstance(metadata.get('rules_suffix'), str)
                 and metadata['rules_suffix'].count(metadata['block']) == 1
                 and isinstance(metadata.get('groups'), dict)
                 and set(metadata['groups']) == set(EVENTS)
                 and all(_valid_owned_group(metadata['groups'][event], timeout) for event, timeout in EVENTS.items())
                 and all(isinstance(metadata.get(key), bool) for key in ('rules_existed', 'config_existed', 'hooks_existed'))
                 and isinstance(metadata.get('events_existed'), dict)
                 and all(isinstance(metadata['events_existed'].get(event), bool) for event in EVENTS))
        if not valid:
            raise ValueError('Connection ownership metadata is invalid at ' + str(state_path))
    return root, home, rules_path, config_path, state_path, originals, rules, config, metadata


def _verify_owned(rules, config, metadata):
    if rules.count(BEGIN) != 1 or rules.count(END) != 1 or rules.count(metadata['rules_suffix']) != 1:
        raise ValueError('Managed HiveMind instruction block has drifted. Restore the recorded managed block before retrying.')
    for event in EVENTS:
        expected = metadata['groups'].get(event)
        groups = config.get('hooks', {}).get(event, [])
        if expected is None or groups.count(expected) != 1:
            raise ValueError('Managed HiveMind ' + event + ' hook has drifted. Restore the recorded exact hook before retrying.')
        if any(_hive_group(group) and group != expected for group in groups):
            raise ValueError('Another HiveMind hook is present. Review its ownership before retrying.')
    for event, groups in config.get('hooks', {}).items():
        if event not in EVENTS and any(_hive_group(group) for group in groups):
            raise ValueError('Another HiveMind hook is present. Review its ownership before retrying.')


def _plan(action, root, agent, agent_home):
    data = _load(root, agent, agent_home)
    root, home, rules_path, config_path, state_path, originals, rules, config, metadata = data
    block, desired_groups = _artifacts(root, agent)
    if metadata is None:
        if 'hivemind:connection:' in rules or any(_hive_group(group) for groups in config.get('hooks', {}).values() for group in groups):
            raise ValueError('Existing HiveMind artifacts have no ownership record for this vault. Review the current installation before connecting.')
        if action == 'disconnect':
            return data, {}
    else:
        _verify_owned(rules, config, metadata)
        if action == 'connect' and metadata['block'] == block and metadata['groups'] == desired_groups:
            return data, {}
    updated = copy.deepcopy(config)
    if action == 'connect':
        if metadata is None:
            suffix = ('\n\n' if rules else '') + block
            metadata = {'version': 1, 'transaction': 'complete', 'root': str(root), 'agent': agent,
                        'agent_home': str(home), 'rules_existed': originals[rules_path] is not None,
                        'config_existed': originals[config_path] is not None, 'hooks_existed': 'hooks' in config,
                        'events_existed': {event: event in config.get('hooks', {}) for event in EVENTS},
                        'block': block, 'rules_suffix': suffix, 'groups': desired_groups}
            new_rules = rules + suffix
            hooks = updated.setdefault('hooks', {})
            for event, group in desired_groups.items():
                hooks.setdefault(event, []).append(group)
        else:
            suffix = metadata['rules_suffix'].replace(metadata['block'], block)
            new_rules = rules.replace(metadata['rules_suffix'], suffix, 1)
            for event, group in desired_groups.items():
                groups = updated['hooks'][event]
                groups[groups.index(metadata['groups'][event])] = group
            metadata = {**metadata, 'block': block, 'rules_suffix': suffix, 'groups': desired_groups}
        targets = {rules_path: new_rules.encode('utf-8'), config_path: _json(updated), state_path: _json(metadata)}
    else:
        new_rules = rules.replace(metadata['rules_suffix'], '', 1)
        hooks = updated['hooks']
        for event in EVENTS:
            hooks[event].remove(metadata['groups'][event])
            if not hooks[event] and not metadata['events_existed'][event]:
                del hooks[event]
        if not hooks and not metadata['hooks_existed']:
            del updated['hooks']
        targets = {rules_path: new_rules.encode('utf-8') if new_rules or metadata['rules_existed'] else None,
                   config_path: _json(updated) if updated or metadata['config_existed'] else None, state_path: None}
    changes = {path: content for path, content in targets.items() if content != originals[path]}
    return data, changes


def _hash(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _atomic(path, content, mode=0o600):
    if content is None:
        path.unlink(missing_ok=True)
        return
    descriptor, name = tempfile.mkstemp(prefix='.hivemind-', dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def _directory_lock(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _locks(home, state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.mkdir(parents=True, exist_ok=True)
    with _directory_lock(home):
        yield


def _apply(data, changes):
    root, home, rules_path, config_path, state_path, originals, rules, config, metadata = data
    for path, expected in originals.items():
        if _read(path) != expected:
            raise ValueError('Configuration changed during planning. Retry after the other editor finishes.')
    modes = {path: path.stat().st_mode & 0o777 if path.exists() else 0o600 for path in originals}
    pending = {'version': 1, 'transaction': 'pending', 'root': str(root), 'agent': metadata['agent'] if metadata else state_path.name.split('-')[0],
               'agent_home': str(home), 'targets': {str(path): {'before_sha256': _hash(originals[path]), 'after_sha256': _hash(raw)} for path, raw in changes.items()},
               'managed_artifacts': {'rules_block': _artifacts(root, metadata['agent'] if metadata else state_path.name.split('-')[0])[0]}}
    pending['previous_ownership'] = metadata
    pending['planned_ownership'] = (_decode_json(changes[state_path], state_path)
                                    if changes.get(state_path) is not None else None)
    pending['managed_artifacts']['groups'] = _artifacts(root, pending['agent'])[1]
    completed = []
    _atomic(state_path, _json(pending))
    try:
        for path, content in changes.items():
            if path == state_path:
                continue
            if _read(path) != originals[path]:
                raise ValueError('Configuration changed during apply. Retry after the other editor finishes.')
            _atomic(path, content, modes[path])
            completed.append(path)
        _atomic(state_path, changes.get(state_path, originals[state_path]))
    except BaseException as error:
        rollback_errors = []
        for path in reversed(completed):
            try:
                if _read(path) != changes[path]:
                    raise ValueError('concurrent change')
                _atomic(path, originals[path], modes[path])
            except (OSError, ValueError):
                rollback_errors.append(str(path))
        if not rollback_errors:
            try:
                _atomic(state_path, originals[state_path])
            except OSError:
                rollback_errors.append(str(state_path))
        if rollback_errors:
            raise ValueError('Connection apply was interrupted and needs review at ' + str(state_path)
                             + '. Review affected paths ' + ', '.join(rollback_errors)) from error
        raise


def _steps(agent):
    if agent == 'codex':
        return ['Open Codex /hooks and review the exact installed hook definitions.',
                'Start a fresh session and verify context delivery. Host execution and trust remain unverified.',
                'If hooks are disabled in Codex configuration, enable them using Codex settings.']
    return ['Review the installed Claude hook definitions in the agent settings.',
            'Start a fresh Claude session and verify context delivery. Host execution remains unverified.']


def execute(args, root):
    agent = args.agent
    root = _locations(root, agent, getattr(args, 'agent_home', None))[0]
    with _directory_lock(root):
        action = getattr(args, 'cmd', None) or getattr(args, 'command', None)
        if action not in ('connect', 'disconnect'):
            raise ValueError('Expected connect or disconnect')
        agent = args.agent
        agent_home = getattr(args, 'agent_home', None)
        data, changes = _plan(action, root, agent, agent_home)
        changed_paths = [str(path) for path in changes]
        applied = False
        if getattr(args, 'apply', False) and changes:
            with _locks(data[1], data[4]):
                data, changes = _plan(action, root, agent, agent_home)
                changed_paths = [str(path) for path in changes]
                if changes:
                    _apply(data, changes)
                    applied = True
        status = inspect(root, agent, agent_home) if getattr(args, 'apply', False) else None
        if action == 'connect':
            proposed_block, proposed_hooks = _artifacts(data[0], agent)
        else:
            proposed_block = data[-1]['block'] if data[-1] else ''
            proposed_hooks = data[-1]['groups'] if data[-1] else {}
        return {'action': action, 'agent': agent, 'agent_home': str(data[1]), 'root': str(data[0]),
                'applied': applied, 'changed': bool(changed_paths), 'changes': changed_paths,
                'proposed': {'instructions': proposed_block, 'hooks': proposed_hooks},
                'status': status['status'] if status else ('preview' if changes else ('connected' if data[-1] else 'not_connected')),
                'checks': status['checks'] if status else [],
                'next_steps': _steps(agent) if action == 'connect' else []}

def _local_codex_checks(home):
    checks = []
    config_path = home / 'config.toml'
    try:
        raw = _read(config_path)
        config = {} if raw is None else tomllib.loads(raw.decode('utf-8'))
        features = config.get('features', {})
        if not isinstance(features, dict) or ('hooks' in features and not isinstance(features['hooks'], bool)):
            raise ValueError('Expected a boolean features.hooks setting')
        enabled = features.get('hooks', True)
        state = 'enabled_explicit' if 'hooks' in features else 'enabled_default'
        checks.append({'id': 'codex.hook_setting', 'status': 'ok' if enabled else 'attention',
                       'state': state if enabled else 'disabled', 'path': str(config_path),
                       'detail': ('Local hook feature is enabled. Administrator controls and host execution require host verification.'
                                  if enabled else 'Local features.hooks is false. Enable hooks in Codex settings to use this connection.')})
    except (ValueError, UnicodeError, OSError):
        checks.append({'id': 'codex.hook_setting', 'status': 'error', 'state': 'invalid',
                       'path': str(config_path), 'detail': 'Cannot interpret local Codex hook feature settings. Review config.toml.'})
    override = home / 'AGENTS.override.md'
    shadowed = override.exists() or override.is_symlink()
    checks.append({'id': 'codex.rules_override', 'status': 'attention' if shadowed else 'ok',
                   'state': 'shadowed' if shadowed else 'absent', 'path': str(override),
                   'detail': ('AGENTS.override.md takes precedence over global AGENTS.md. Review the intended global instructions.'
                              if shadowed else 'No global AGENTS.override.md shadows the managed instruction block.')})
    return checks


def inspect(root, agent, agent_home=None):
    if agent not in AGENTS:
        raise ValueError('Supported agents are codex and claude')
    try:
        root, home, rules_path, config_path, state_path = _locations(root, agent, agent_home)
    except (ValueError, OSError) as error:
        home = Path(agent_home or os.environ.get(AGENTS[agent][2]) or Path.home() / ('.' + agent)).expanduser().resolve()
        return {'agent': agent, 'agent_home': str(home), 'root': str(Path(root).expanduser().resolve()),
                'status': 'attention', 'checks': [{'id': agent + '.vault', 'status': 'error', 'state': 'invalid', 'detail': str(error)}],
                'next_steps': ['Initialize or select a valid HiveMind vault before connecting an agent.']}
    checks = []
    try:
        data = _load(root, agent, home)
        metadata = data[-1]
        if metadata is not None:
            _verify_owned(data[6], data[7], metadata)
            _, desired_groups = _artifacts(root, agent)
            stale = metadata['groups'] != desired_groups
            for group in metadata['groups'].values():
                argv = shlex.split(group['hooks'][0]['command'])
                stale = stale or len(argv) < 2 or not os.access(argv[0], os.X_OK) or not Path(argv[1]).is_file()
            checks.append({'id': agent + '.runtime_paths', 'status': 'attention' if stale else 'ok',
                           'state': 'stale' if stale else 'current',
                           'detail': ('Managed hook paths differ from the current installation or point to missing executables. Preview connect and apply it to refresh the managed artifacts.'
                                      if stale else 'Managed hooks reference the current Python executable and installed HiveMind core.')})
            state = 'attention' if stale else 'connected'
        elif 'hivemind:connection:' in data[6] or any(_hive_group(group) for groups in data[7].get('hooks', {}).values() for group in groups):
            state = 'attention'
            checks.append({'id': agent + '.ownership', 'status': 'attention', 'state': 'unmanaged',
                           'detail': 'HiveMind artifacts exist without an ownership record for this vault.'})
        else:
            state = 'not_connected'
        if state != 'attention':
            checks.extend([{'id': agent + '.rules', 'status': 'ok' if metadata else 'attention', 'state': state,
                            'path': str(rules_path), 'detail': 'Managed instructions match ownership metadata.' if metadata else 'Managed instructions are absent.'},
                           {'id': agent + '.hooks', 'status': 'ok' if metadata else 'attention', 'state': state,
                            'path': str(config_path), 'detail': 'Both managed hooks match ownership metadata.' if metadata else 'Managed hooks are absent.'}])
    except (ValueError, OSError) as error:
        state = 'attention'
        checks.append({'id': agent + '.configuration', 'status': 'error', 'state': 'invalid_or_drifted', 'detail': str(error)})
    if agent == 'codex':
        local_checks = _local_codex_checks(home)
        if any(check['status'] != 'ok' for check in local_checks) and state == 'connected':
            state = 'attention'
        checks.extend(local_checks)
    checks.append({'id': agent + '.host', 'status': 'attention', 'state': 'unverified',
                   'detail': 'Host execution, administrator controls and hook trust require verification in a fresh agent session.'})
    return {'agent': agent, 'agent_home': str(home), 'root': str(root), 'status': state,
            'checks': checks, 'next_steps': _steps(agent)}

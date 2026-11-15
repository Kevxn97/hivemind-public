"""Local, explicitly approved hints from repeated corrections in one project."""
import datetime as dt
import json
from pathlib import Path
import re

if __package__:
    from . import evidence, hive
else:
    import evidence
    import hive


MAX_RULES = 8
MAX_BYTES = 65536
MAX_HINT_BYTES = 2300
ATTRIBUTE = re.compile(r'[a-z][a-z0-9_]{0,31}')


def configure(sub):
    parser = sub.add_parser('dreaming', help='Review local correction rules for a project')
    actions = parser.add_subparsers(dest='dream_action', required=True)
    enable = actions.add_parser('enable', help='Approve a rule backed by two distinct earlier corrections')
    enable.add_argument('--file', type=Path, required=True)
    enable.add_argument('--actor', required=True)
    enable.add_argument('--reason', required=True)
    actions.add_parser('status').add_argument('project')
    disable = actions.add_parser('disable', help='Remove an approved rule from project context')
    disable.add_argument('project')
    disable.add_argument('rule_id')


def context_flags(parser):
    parser.add_argument('--task-kind', help='Explicit task type for approved Dreaming rules')
    parser.add_argument('--task-attr', action='append', default=[], metavar='KEY=VALUE')
    parser.add_argument('--override-rule', action='append', default=[], metavar='RULE_ID')


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or not value.isprintable():
        raise ValueError(f'{label} must be a single line of 1..{maximum} characters')


def _conditions(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 8:
        raise ValueError('Conditions must contain 1..8 exact task attributes')
    for key, expected in value.items():
        if not ATTRIBUTE.fullmatch(key) or key in {'project', 'task'}:
            raise ValueError('Invalid task attribute name')
        _text(expected, 'Task attribute value', 80)


def request(args):
    if args.task_kind is None:
        if args.task_attr or args.override_rule:
            raise ValueError('Task attributes and overrides require --task-kind')
        return None
    attributes = {'task_kind': args.task_kind}
    for argument in args.task_attr:
        key, separator, value = argument.partition('=')
        if not separator or key in attributes:
            raise ValueError('Use unique task attributes in KEY=VALUE form')
        attributes[key] = value
    _conditions(attributes)
    if any(not hive.ID.fullmatch(rule_id) for rule_id in args.override_rule):
        raise ValueError('Invalid overridden rule ID')
    return attributes, frozenset(args.override_rule)


def _project(value):
    if not isinstance(value, str) or not value.startswith('projects/') or not hive.CLUSTER.fullmatch(value):
        raise ValueError('Choose a projects/ID cluster')
    return value.split('/')[1]


def _validate(rule, project=None):
    required = {'version', 'id', 'project', 'statement', 'uncertainty', 'when', 'unless', 'sources'}
    if not isinstance(rule, dict) or set(rule) != required or type(rule['version']) is not int or rule['version'] != 1:
        raise ValueError('Invalid Dreaming rule shape or version')
    if not isinstance(rule['id'], str) or not hive.ID.fullmatch(rule['id']):
        raise ValueError('Invalid rule ID')
    _project(rule['project'])
    if project is not None and rule['project'] != project:
        raise ValueError('Rule belongs to a different project')
    _text(rule['statement'], 'Rule statement', 600)
    _text(rule['uncertainty'], 'Uncertainty', 240)
    _conditions(rule['when'])
    if 'task_kind' not in rule['when']:
        raise ValueError('A rule needs an explicit task_kind condition')
    if not isinstance(rule['unless'], list) or len(rule['unless']) > 4:
        raise ValueError('Provide an array of up to four exception clauses')
    for exception in rule['unless']:
        _conditions(exception)
    if not isinstance(rule['sources'], list) or not 2 <= len(rule['sources']) <= 4:
        raise ValueError('Provide two to four attributable correction sources')
    for binding in rule['sources']:
        if not isinstance(binding, dict) or set(binding) != {'ref', 'source_id'}:
            raise ValueError('Each correction needs an exact ref and source_id')
        if not isinstance(binding['ref'], str) or not evidence.REFERENCE.fullmatch(binding['ref']):
            raise ValueError('Use an exact NOTE_ID@REVISION for each correction')
        _text(binding['source_id'], 'Source ID', 160)


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Choose a regular JSON file')
    with path.open('rb') as stream:
        size_limited = stream.read(MAX_BYTES + 1)
    if len(size_limited) > MAX_BYTES:
        raise ValueError('Dreaming JSON exceeds the local size limit')
    try:
        return json.loads(size_limited)
    except RecursionError as exc:
        raise ValueError('Dreaming JSON is too deeply nested') from exc


def _registry_path(root, project):
    path = root / '.state' / 'dreaming' / (_project(project) + '.json')
    hive.reject_symlinks(root, path)
    return path


def _registry(path, project):
    if not path.exists():
        return {}
    value = _read(path)
    if (not isinstance(value, dict) or set(value) != {'version', 'rules'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['rules'], dict) or len(value['rules']) > MAX_RULES):
        raise ValueError('Invalid local Dreaming registry')
    for rule_id, entry in value['rules'].items():
        if not isinstance(entry, dict) or set(entry) != {'rule', 'actor', 'reason', 'approved_at'}:
            raise ValueError('Invalid local Dreaming entry')
        _validate(entry['rule'], project)
        if rule_id != entry['rule']['id']:
            raise ValueError('Dreaming rule ID does not match registry key')
    return value['rules']


def _proof(root, records, rule):
    seen = set()
    for binding in rule['sources']:
        note_id, revision = binding['ref'].split('@')
        note, raw, _ = records.get(note_id, ({}, '', None))
        if (hive.digest(raw) != revision or note.get('type') not in {'lesson', 'decision'}
                or note.get('sensitivity') != 'internal' or not hive.active(note)
                or note['evidence'] == 'disputed' or hive.cluster_for(note) != rule['project']):
            raise ValueError('Correction must reference the active, exact local project lesson or decision')
        exp = note.get('experience', {})
        observations = [row for row in exp.get('observations', [])
                        if row['source_id'] == binding['source_id']
                        and row['kind'] == 'correction' and row['evidence'] == 'confirmed']
        if exp.get('status') != 'supported' or len(observations) != 1:
            raise ValueError('Each source needs one confirmed correction observation in a supported experience')
        source = next(row for row in note['sources'] if row['id'] == binding['source_id'])
        state, _, sha = evidence._source(root, source)
        if state != 'hash_matched' or source.get('derived_from'):
            raise ValueError('Correction requires a direct local source with a matching hash')
        observation = observations[0]
        occurrence = (hive.timestamp(observation['observed_at']), ' '.join(observation['result'].casefold().split()))
        identities = {('bytes', sha), ('observation', occurrence)}
        if source.get('origin_event_id') is not None:
            _text(source['origin_event_id'], 'Origin event ID', 160)
            identities.add(('event', source['origin_event_id']))
        if seen & identities:
            raise ValueError('Copied or repeated references count as one correction')
        seen.update(identities)


def _render(rule):
    conditions = json.dumps(rule['when'], ensure_ascii=False, sort_keys=True)
    exceptions = json.dumps(rule['unless'], ensure_ascii=False, sort_keys=True)
    refs = ', '.join(f"{item['ref']} (source {item['source_id']})" for item in rule['sources'])
    text = '\n'.join((
        '## Dreaming correction rule: ' + rule['id'],
        'Locally approved tentative hint inferred from earlier corrections.',
        'Statement: ' + rule['statement'],
        'When: ' + conditions,
        'Unless: ' + exceptions,
        'Uncertainty: ' + rule['uncertainty'],
        'Evidence: ' + refs,
        'Current instructions take priority.',
    ))
    if len(text.encode('utf-8')) > MAX_HINT_BYTES:
        raise ValueError('Keep the complete rendered rule within 2300 UTF-8 bytes')
    return text


def _matches(rule, attributes, overrides):
    if rule['id'] in overrides or not all(attributes.get(key) == value for key, value in rule['when'].items()):
        return False
    # Every exception needs at least one known mismatch to be ruled out.
    return all(any(key in attributes and attributes[key] != value for key, value in clause.items())
               for clause in rule['unless'])


def select(root, project, records, requested):
    if requested is None:
        return None
    try:
        entries = _registry(_registry_path(root, project), project)
    except (ValueError, OSError):
        return None
    for entry in entries.values():
        rule = entry['rule']
        if _matches(rule, *requested):
            try:
                _proof(root, records, rule)
                return {'id': rule['id'], 'text': _render(rule)}
            except (ValueError, TypeError, OSError, KeyError, StopIteration, RuntimeError):
                continue
    return None


def execute(args, root):
    action = args.dream_action
    if action == 'enable':
        rule = _read(args.file)
        _validate(rule)
        _render(rule)
        _text(args.actor, 'Approval actor', 120)
        _text(args.reason, 'Approval reason', 300)
        project, rule_id = rule['project'], rule['id']
        if __package__:
            from . import project_context
        else:
            import project_context
        records = project_context._records(root)
        project_context._valid_project(project, {ident: row[0] for ident, row in records.items()})
        _proof(root, records, rule)
    else:
        project = args.project
    path = _registry_path(root, project)
    if action == 'status':
        entries = _registry(path, project)
        return {'project': project, 'enabled': bool(entries), 'rules': sorted(entries)}
    hive.reject_symlinks(root, path.parent / '.hive.lock')
    with hive.locked(path.parent):
        entries = _registry(_registry_path(root, project), project)
        if action == 'enable':
            if rule_id not in entries and len(entries) >= MAX_RULES:
                raise ValueError('Disable a rule before approving another for this project')
            entries[rule_id] = {'rule': rule, 'actor': args.actor, 'reason': args.reason,
                                'approved_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        else:
            rule_id = args.rule_id
            if rule_id not in entries:
                raise ValueError('Unknown Dreaming rule ID')
            del entries[rule_id]
        hive.atomic(path, json.dumps({'version': 1, 'rules': entries}, ensure_ascii=False, sort_keys=True) + '\n')
    result = {'project': project, 'rule_id': rule_id, 'enabled': action == 'enable'}
    if action == 'enable':
        result['corrections'] = len(rule['sources'])
    return result

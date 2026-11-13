"""Local, explicit onboarding for a new, separate HiveMind vault.

No provider calls, global configuration, source discovery or background activation.
Existing vaults are validated and returned without modifying their contents.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata

try:
    from . import hive, project_context
except ImportError:
    if __package__:
        raise
    import hive
    import project_context

MARKER = '.hivemind.json'
VAULT_MARKER = {'version': 1, 'kind': 'hivemind-vault'}
BACKGROUND = {
    'version': 1, 'enabled': False, 'scan_seconds': 60,
    'debounce_seconds': 120, 'min_sync_seconds': 1800,
    'failure_backoff_seconds': 3600, 'daily_hour': 9,
    'calendar_seconds': 1800, 'integrity_seconds': 3600,
}
START = '''# HiveMind\n\nThis vault contains your local, journaled project knowledge.\n\nStart with `hive brief /path/to/project` after registering a project.\nUse `hive browse`, `hive search "topic"`, and `hive show ID` to retrieve context.\nUse the journaled draft/apply workflow when changing canonical notes.\n\nSources are empty and background operation is disabled. No provider or model\nis configured by initialization. Treat stored context according to its source\nand evidence status; it does not establish the current state of a project.\n\nKeep this vault separate from the application checkout. Back it up privately.\n'''


def configure(sub):
    parser = sub.add_parser('init', help='Create a separate local vault and optionally its first project')
    parser.add_argument('vault', nargs='?', help='New vault directory (default: selected vault)')
    parser.add_argument('--project', metavar='PATH', help='Existing folder to associate with the first project')
    parser.add_argument('--name', metavar='NAME', help='First project title (default: folder name)')
    parser.add_argument('--yes', action='store_true', help='Use supplied values without interactive questions')
    parser.add_argument('--demo', action='store_true', help='Include an explicitly synthetic example project')
    parser.add_argument('--json', action='store_true', help='Return structured output')
    return parser


def _title(value):
    value = str(value).strip()
    if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError('Project name must contain 1–200 printable characters')
    return value


def _slug(name):
    ascii_name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', ascii_name).strip('-')
    if not slug:
        slug = 'project-' + hashlib.sha256(name.encode()).hexdigest()[:12]
    if not slug[0].isalpha():
        slug = 'project-' + slug
    if len(slug) < 2:
        slug += '-project'
    return slug[:80].rstrip('-')


def _destination(vault):
    requested = Path(vault).expanduser().absolute()
    # Check before resolve(): resolving a link would silently select its target.
    if requested.is_symlink():
        raise ValueError('Vault directory must not be a symlink')
    target = requested.resolve()
    code = Path(__file__).resolve().parent
    if target == code or target in code.parents or code in target.parents:
        raise ValueError('Choose a vault outside the application and resource directories')
    if target.exists() and not target.is_dir():
        raise ValueError('Vault must be a directory')
    return target


def _validate_existing(vault):
    # Validation is intentionally read-only: init never repairs or migrates a store.
    if any(p.is_symlink() for p in vault.rglob('*')):
        raise ValueError('Existing vault contains a symlink; initialization will not modify it')
    try:
        marker = json.loads((vault / MARKER).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('Invalid vault marker; existing directory is preserved') from exc
    if marker != VAULT_MARKER:
        raise ValueError('Unsupported vault marker; existing directory is preserved')
    required = ['START.md', '.hive.lock', 'wiki/index.md', 'config/projects.json',
                'config/sources.json', 'config/background.json']
    if not all((vault / name).is_file() for name in required):
        raise ValueError('Incomplete vault; initialization will not repair or overwrite it')
    with hive.locked(vault, shared=True):
        hive.require_clean(vault)
        notes = hive.load_notes(vault)
        hive.audit(vault, notes)
        types = {key: row[0]['type'] for key, row in notes.items()}
        for note, _, _ in notes.values():
            hive.validate(note, types)
    projects = json.loads((vault / 'config/projects.json').read_text())
    sources = json.loads((vault / 'config/sources.json').read_text())
    background = json.loads((vault / 'config/background.json').read_text())
    if not isinstance(projects, dict) or projects.get('version') != 1 or type(projects.get('legacy_fallback')) is not bool:
        raise ValueError('Invalid project configuration')
    if (not isinstance(sources, dict) or sources.get('version') != 1
            or not isinstance(sources.get('files'), list) or not isinstance(sources.get('remote'), list)):
        raise ValueError('Invalid source configuration')
    if not isinstance(background, dict) or background.get('version') != 1 or type(background.get('enabled')) is not bool:
        raise ValueError('Invalid background configuration')
    project_context.list_projects(vault)


def _project_note(name, demo=False):
    source = 'synthetic:onboarding-demo' if demo else 'user:onboarding'
    summary = ('Synthetic example project for trying local context retrieval.' if demo else
               'Project declared during local vault setup; goals and current state remain to be recorded.')
    body = ('# Synthetic example\n\nThis synthetic project demonstrates source-labelled context. '
            'It does not describe a real person, organization, or project.\n\n'
            '## Next step\n\nReplace this example with your own journaled project knowledge.' if demo else
            '# Project context\n\nThis project was declared during vault setup. Its folder association '
            'is stored separately in local operational state.\n\n'
            '## Open questions\n\nRecord the goal, current state, decisions, and next steps with their sources.')
    return {
        'id': 'demo-project' if demo else _slug(name), 'type': 'project',
        'cluster': 'projects/demo-project' if demo else 'projects/' + _slug(name),
        'title': name, 'summary': summary, 'body': body,
        'status': 'draft', 'evidence': 'inferred' if demo else 'reported',
        'sensitivity': 'internal', 'aliases': [], 'tags': ['synthetic-demo'] if demo else [],
        'sources': [{'id': 'onboarding-demo' if demo else 'onboarding-declaration',
                     'resource': source, 'title': 'Synthetic onboarding example' if demo else 'User project declaration',
                     'observed_at': hive.now()}],
        'stale_after': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'links': [],
    }


def _private_directories(vault):
    vault.chmod(0o700)
    for path in vault.rglob('*'):
        if path.is_dir():
            path.chmod(0o700)


def initialize(vault, project_path=None, name=None, demo=False):
    """Create a new vault atomically, or validate and return an existing one.

    A repeat call never applies the supplied project/demo flags to existing data.
    Project folders stay machine-local; canonical notes contain no machine paths.
    """
    target = _destination(vault)
    if target.exists() and (target / MARKER).exists():
        _validate_existing(target)
        return {'vault': str(target), 'initialized': False, 'existing': True,
                'project': None, 'demo': False}
    if target.exists() and any(target.iterdir()):
        raise ValueError('Refusing nonempty directory without a valid vault marker; choose a new vault')
    project = None
    if project_path is not None:
        project = Path(project_path).expanduser().resolve()
        home = Path.home().resolve()
        if not project.is_dir() or project == home or project in home.parents:
            raise ValueError('Choose an existing project folder, not the home or root directory')
        if project == target or target in project.parents:
            raise ValueError('Project folder must be outside the new vault')
        name = _title(name if name is not None else project.name)
    elif name is not None:
        raise ValueError('--name requires --project')
    if project is not None and demo and _slug(name) == 'demo-project':
        raise ValueError('Choose a project name other than Demo Project when including the demo')
    # Missing parents may be created, but no existing content is modified.
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix='.' + target.name + '-init-', dir=target.parent))
    association = None
    try:
        for directory in ['config', 'wiki', '.history', '.state']:
            (stage / directory).mkdir(mode=0o700)
        hive.atomic(stage / 'START.md', START)
        for filename, data in [('projects.json', {'version': 1, 'legacy_fallback': False}),
                               ('sources.json', {'version': 1, 'files': [], 'remote': []}),
                               ('background.json', BACKGROUND)]:
            hive.atomic(stage / 'config' / filename, json.dumps(data, indent=2) + '\n')
        notes = ([_project_note(name)] if project is not None else [])
        if demo:
            notes.append(_project_note('Demo Project', demo=True))
        if notes:
            hive.apply(stage, {'changes': [{'expected_revision': 'new', 'note': note} for note in notes]},
                       actor='hivemind-init', reason='Initialize explicitly requested project context',
                       context='user:onboarding; synthetic:onboarding-demo' if demo else 'user:onboarding')
        else:
            with hive.locked(stage):
                hive.rebuild(stage)
        if project is not None:
            association = project_context.register_project(stage, project, 'projects/' + _slug(name))
        hive.atomic(stage / MARKER, json.dumps(VAULT_MARKER, indent=2) + '\n')
        _private_directories(stage)
        _validate_existing(stage)
        if target.is_symlink() or (target.exists() and any(target.iterdir())):
            raise ValueError('Vault destination changed during initialization; existing content is preserved')
        # rename refuses to replace a nonempty directory, including a concurrent store.
        os.rename(stage, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'vault': str(target), 'initialized': True, 'existing': False,
            'project': association, 'demo': bool(demo)}


def _ask(label, default=''):
    print(label + (f' [{default}]' if default else '') + ': ', end='', file=sys.stderr, flush=True)
    try:
        answer = input().strip()
    except EOFError as exc:
        raise ValueError('Initialization cancelled: input ended before confirmation') from exc
    return answer or default


def _yes(label, default=False):
    answer = _ask(label + ' (y/n)', 'y' if default else 'n').lower()
    if answer not in {'y', 'yes', 'n', 'no'}:
        raise ValueError('Answer y or n; initialization cancelled')
    return answer in {'y', 'yes'}


def execute(args, root):
    vault = getattr(args, 'vault', None) or str(root)
    project = getattr(args, 'project', None)
    name = getattr(args, 'name', None)
    demo = bool(getattr(args, 'demo', False))
    if sys.stdin.isatty() and not getattr(args, 'yes', False):
        vault = _ask('Separate vault directory', vault)
        project = _ask('First project folder (blank to skip)', project or '') or None
        name = _ask('Project name (blank for folder name)', name or '') or None
        demo = _yes('Include a synthetic demo project', demo)
        if not _yes('Create this local vault', False):
            raise ValueError('Initialization cancelled; no vault was created')
    return initialize(vault, project_path=project, name=name, demo=demo)

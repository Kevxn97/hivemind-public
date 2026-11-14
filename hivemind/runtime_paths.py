"""Keep immutable application resources separate from explicitly selected data."""
import json
import os
from pathlib import Path
import shlex
import sys

CODE = Path(__file__).resolve().parent
MARKER = {'version': 1, 'kind': 'hivemind-vault'}


def default_vault():
    selected = os.environ.get('HIVEMIND_HOME')
    return Path(selected).expanduser().resolve() if selected else Path.home() / '.local/share/hivemind'


def resolve_vault(value=None):
    return Path(value).expanduser().resolve() if value is not None else default_vault()


def cli_command(root, *args):
    """Use this installation and selected vault in executable follow-up commands."""
    source_entry = CODE.parent / 'run.py'
    if source_entry.is_file() and (CODE.parent / 'pyproject.toml').is_file():
        command = [sys.executable, str(source_entry)]
    else:
        command = [sys.executable, '-m', 'hivemind']
    return [*command, '--root', str(Path(root).resolve()), *map(str, args)]


def require_vault(root):
    root = Path(root)
    if root == CODE or CODE in root.parents or root in CODE.parents:
        raise ValueError('The vault must be outside the installed application. Use hive init /path/to/vault.')
    try:
        marker = json.loads((root / '.hivemind.json').read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('No initialized HiveMind vault at ' + str(root) + '. Run ' + shlex.join(cli_command(root, 'setup')) + '.') from exc
    if marker != MARKER or not (root / 'wiki').is_dir() or not (root / '.hive.lock').is_file():
        raise ValueError('Invalid or incomplete vault. Preserve its data and inspect it before running hive init with a new directory.')
    return root

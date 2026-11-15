"""Explicit provider registrations outside agent-editable vault data."""
import hashlib
import json
import os
from pathlib import Path
import stat

from . import hive


def path_for(root):
    root = Path(root).resolve()
    folder = Path.home().resolve() / '.config/hivemind/providers'
    if folder.is_relative_to(root) or root.is_relative_to(folder):
        raise ValueError('Provider registrations must be outside the vault.')
    for path in (folder, *folder.parents):
        if path.is_symlink():
            raise ValueError('Provider registration directories must not be symlinks.')
    return folder / (hashlib.sha256(str(root).encode()).hexdigest() + '.json')


def read(root):
    path = path_for(root)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise ValueError('Invalid provider registration file.')
            value = json.loads(handle.read(8193))
    except (OSError, ValueError) as exc:
        raise ValueError('Provider registration is invalid; explicitly register the provider again.') from exc
    if (not isinstance(value, dict) or set(value) != {'version', 'vault', 'codex', 'agent_home', 'sha256'}
            or value.get('version') != 1 or value.get('vault') != str(Path(root).resolve())):
        raise ValueError('Provider registration does not match this vault.')
    return value


def snapshot(root):
    """Bind an explicit replacement preview even when old JSON is invalid."""
    path = path_for(root)
    if path.is_symlink():
        return {'invalid': 'symlink'}
    if not path.exists():
        return None
    try:
        with path.open('rb') as handle:
            raw = handle.read(8193)
        return {'sha256': hashlib.sha256(raw).hexdigest(), 'oversized': len(raw) > 8192}
    except OSError:
        return {'invalid': 'unreadable'}


def candidate(root, prefs):
    root = Path(root).resolve()
    try:
        binary = Path(prefs['codex']).expanduser().resolve(strict=True)
        profile = Path(prefs['agent_home']).expanduser().resolve(strict=True)
    except (TypeError, KeyError, OSError) as exc:
        raise ValueError('Choose an existing Codex executable and profile with hive automation provider.') from exc
    if binary.is_relative_to(root) or profile.is_relative_to(root) or root.is_relative_to(profile):
        raise ValueError('The provider executable and profile must be outside the vault.')
    if not binary.is_file() or not os.access(binary, os.X_OK) or not profile.is_dir():
        raise ValueError('Choose an existing executable Codex CLI and profile directory.')
    path_for(root)
    with binary.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {'version': 1, 'vault': str(root), 'codex': str(binary),
            'agent_home': str(profile), 'sha256': digest}


def register(root, registration):
    if candidate(root, registration) != registration:
        raise ValueError('Provider changed since preview; preview registration again.')
    path = path_for(root)
    if path.is_symlink():
        raise ValueError('Provider registration must not be a symlink.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    hive.atomic(path, json.dumps(registration, indent=2) + '\n')
    path.chmod(0o600)


def require(root, prefs):
    registered = read(root)
    if registered is None:
        raise ValueError('Provider approval is missing. Disable sync, then explicitly register the provider with hive automation provider. Existing sources and knowledge are preserved.')
    if candidate(root, prefs) != registered:
        raise ValueError('Provider executable or profile differs from its approval. Disable sync and preview provider registration again.')
    return {**prefs, 'codex': registered['codex'], 'agent_home': registered['agent_home']}

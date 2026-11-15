"""Application version and read-only local source checkout identity.

The package version is deliberately independent from the selected knowledge
vault.  Git metadata is optional and is only read from this source checkout,
so a vault nested in another repository cannot accidentally label the app.
"""

import os
from pathlib import Path
import re
import subprocess


# Keep the public release's manually managed version separate from Git's
# short-lived source identity.  Packaging metadata mirrors this value.
VERSION = '0.1.0a2'


def version_label(source_dir=None):
    """Return the application version plus a bounded local Git identity.

    ``source_dir`` is the checkout that owns the running application, not the
    selected knowledge vault.  A repository is accepted only when its own
    top-level directory is exactly ``source_dir``; this prevents borrowing a
    parent repository's HEAD or dirty state.  Git failures are intentionally
    reduced to a safe label without exposing paths or command diagnostics.
    """
    source_dir = Path(source_dir or Path(__file__).resolve().parents[1]).resolve()
    prefix = f'v{VERSION}'
    unavailable = prefix + ' · Git unavailable'
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'

    def git(*args):
        return subprocess.run(
            ['git', '-C', str(source_dir), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )

    try:
        top = git('rev-parse', '--show-toplevel')
        if top.returncode or Path(top.stdout.strip()).resolve() != source_dir:
            return unavailable

        head = git('rev-parse', '--verify', '--short=7', 'HEAD')
        revision = head.stdout.strip()
        if head.returncode:
            # An unborn branch is useful information, distinct from an
            # unreadable/damaged HEAD or detached repository state.
            branch = git('symbolic-ref', '-q', 'HEAD')
            if branch.returncode:
                return unavailable
            ref = git('show-ref', '--verify', '--quiet', branch.stdout.strip())
            if ref.returncode != 1:
                return unavailable
            revision = 'no commit'
        elif not re.fullmatch(r'[0-9a-f]{7,64}', revision):
            return unavailable

        status = git('status', '--porcelain=v1', '--untracked-files=normal', '--ignore-submodules=none')
        suffix = ' · status unknown' if status.returncode else ' · local changes' if status.stdout else ''
        return prefix + ' · ' + revision + suffix
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return unavailable

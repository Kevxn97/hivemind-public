#!/usr/bin/env python3
"""Prepare a reviewed source archive without vault data or Git history."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
import zipfile

FORBIDDEN = {'.git', '.history', '.state', '.cache', 'wiki', 'sources', 'INBOX',
             'archive', 'maintenance', 'config', 'output', '.agents', '.codex',
             '.hive.lock', '.hivemind.json', '.pending.json', '__pycache__'}


def prepare(source, out):
    source, out = source.resolve(), out.resolve()
    if out == source or source in out.parents:
        raise ValueError('Release output must be outside the application source directory')
    manifest = source / 'RELEASE_FILES.json'
    if manifest.is_symlink():
        raise ValueError('Release manifest must be a regular local file')
    files = json.loads(manifest.read_text())
    if not isinstance(files, list) or not files or any(not isinstance(f, str) for f in files):
        raise ValueError('Release manifest must be a nonempty list of reviewed files')
    if len(set(files)) != len(files):
        raise ValueError('Duplicate release paths')
    for private in FORBIDDEN - {'.git', '__pycache__', 'output'}:
        if (source / private).exists() or (source / private).is_symlink():
            raise ValueError('Private or runtime content present in source: ' + private)
    selected = set(files) | {'RELEASE_FILES.json'}
    data = {}
    for relative in sorted(selected):
        parts = PurePosixPath(relative).parts
        if (not parts or PurePosixPath(relative).is_absolute() or '..' in parts
                or str(PurePosixPath(relative)) != relative or any(p in FORBIDDEN for p in parts)):
            raise ValueError('Unpublishable path in release manifest: ' + relative)
        path = source / relative
        if any(p.is_symlink() for p in (path, *path.parents) if p != source and source in p.parents):
            raise ValueError('Symlinks cannot be published: ' + relative)
        if not path.is_file() or not path.resolve().is_relative_to(source):
            raise ValueError('Missing or external release file: ' + relative)
        data[relative] = path.read_bytes()
    # New code and resources need explicit review before entering a release.
    for top in ('hivemind', 'tests', 'scripts', 'docs', '.github'):
        for path in (source / top).rglob('*'):
            if '__pycache__' in path.parts:
                continue
            if path.is_symlink() or (path.is_file() and str(path.relative_to(source)) not in selected):
                raise ValueError('Unreviewed source/resource file: ' + str(path.relative_to(source)))
    out.mkdir(parents=True, exist_ok=True)
    archive = out / 'hivemind-public-0.1.0a2.zip'
    with tempfile.NamedTemporaryFile(dir=out, suffix='.zip', delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
            for relative, content in data.items():
                entry = zipfile.ZipInfo('hivemind-public/' + relative, date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                zipped.writestr(entry, content)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    report = {'status': 'prepared', 'published': False, 'git_history_included': False,
              'included_files': len(data), 'archive': archive.name,
              'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
              'source_sha256': {f: hashlib.sha256(content).hexdigest() for f, content in data.items()},
              'prepared_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    (out / 'release-manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.source, args.out), indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print('Release refused: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Release archives contain only explicitly reviewed files, never vault/history."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class PublicArchiveE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hivemind-release-test-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.source = self.folder / 'source'
        self.source.mkdir()
        self.out = self.folder / 'release'
        # Starting data is synthetic: an allowlist, a tiny app, and a fake Git history.
        (self.source / 'hivemind').mkdir()
        (self.source / 'hivemind/__init__.py').write_text("__version__ = '0.1.0a2'\n")
        (self.source / 'README.md').write_text('# Synthetic project\n')
        (self.source / '.git').mkdir()
        (self.source / '.git/private-history').write_text('SYNTHETIC_GIT_HISTORY_SENTINEL')
        self.manifest = ['README.md', 'hivemind/__init__.py']
        self.save()

    def save(self):
        (self.source / 'RELEASE_FILES.json').write_text(json.dumps(self.manifest))

    def run_release(self, ok=True):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/package_release.py'),
                                 '--source', str(self.source), '--out', str(self.out)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def test_archive_uses_allowlist_and_has_no_previous_git_history(self):
        self.run_release()
        archives = list(self.out.glob('*.zip'))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            names = archive.namelist()
            self.assertEqual(sorted(names), sorted(['hivemind-public/' + f for f in self.manifest] + ['hivemind-public/RELEASE_FILES.json']))
            self.assertNotIn(b'SYNTHETIC_GIT_HISTORY_SENTINEL', b''.join(archive.read(f) for f in names))
        report = json.loads((self.out / 'release-manifest.json').read_text())
        self.assertEqual(report['status'], 'prepared')
        self.assertEqual(report['included_files'], len(self.manifest) + 1)

    def test_release_refuses_vault_content_and_unknown_code(self):
        for relative in ('.history/private.json', 'wiki/project/private.md', 'hivemind/unreviewed.py', '.github/workflows/unreviewed.yml'):
            with self.subTest(relative=relative):
                path = self.source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('SYNTHETIC_PRIVATE_CONTENT')
                self.run_release(ok=False)
                path.unlink()
                # Remove only the synthetic fixture folder created in this case.
                if relative.startswith(('.history/', 'wiki/')):
                    shutil.rmtree(self.source / relative.split('/')[0])
                self.assertFalse(list(self.out.glob('*.zip')) if self.out.exists() else [])

    def test_allowlist_refuses_symlink_and_parent_escape(self):
        outside = self.folder / 'private.txt'
        outside.write_text('SYNTHETIC_PRIVATE_CONTENT')
        alias = self.source / 'linked.md'
        alias.symlink_to(outside)
        self.manifest.append('linked.md')
        self.save()
        self.run_release(ok=False)
        self.manifest[-1] = '../private.txt'
        self.save()
        self.run_release(ok=False)


if __name__ == '__main__':
    unittest.main()

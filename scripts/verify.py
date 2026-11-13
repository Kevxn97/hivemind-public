#!/usr/bin/env python3
"""Run local checks with reproducible evidence, including failure or interruption."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pattern', default='test*.py')
    p.add_argument('--evidence-dir', type=Path, default=ROOT.parent / 'hivemind-public-evidence')
    args = p.parse_args()
    out = args.evidence_dir / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out.mkdir(parents=True)
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', args.pattern, '-v']
    files = [path for path in ROOT.rglob('*') if path.is_file() and not
             any(part in {'.git', '__pycache__', '.venv', 'output', 'dist', 'build'} or part.endswith('.egg-info') for part in path.relative_to(ROOT).parts)]
    report = {'command': command, 'cwd': str(ROOT), 'outcome': 'running',
              'environment': {'python': sys.version, 'platform': platform.platform(),
                              'installed_python': os.environ.get('HIVEMIND_TEST_PYTHON')},
              'fixture': 'Temporary synthetic vaults, projects and agent homes; no provider calls or personal data.',
              'source_sha256': {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    evidence = out / 'evidence.json'
    def save():
        evidence.write_text(json.dumps(report, indent=2) + '\n')
    save()
    code = 1
    try:
        with (out / 'test.log').open('w') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HIVEMIND_EVIDENCE_DIR=str(out),
                                                HIVEMIND_E2E_EVIDENCE_DIR=str(out / 'connections')))
            try:
                code = process.wait(timeout=240)
            except BaseException:
                process.terminate()
                process.wait(timeout=10)
                raise
        report.update(outcome='passed' if code == 0 else 'failed', returncode=code)
    except BaseException as exc:
        report.update(outcome='aborted', error=type(exc).__name__)
        raise
    finally:
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
        print((out / 'test.log').read_text()[-6000:])
        print('Evidence:', evidence)
    return code


if __name__ == '__main__':
    raise SystemExit(main())

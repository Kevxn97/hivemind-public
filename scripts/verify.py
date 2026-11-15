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
STANDALONE_SUITES = (
    'scripts.verify_background.BackgroundE2E',
    'scripts.verify_ingest.IngestE2E',
    'scripts.verify_jobs.JobsE2E',
    'scripts.verify_launcher.CliE2E',
    'scripts.verify_terminal.TerminalE2E',
)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pattern', help='Run only matching files under tests/.')
    p.add_argument('--evidence-dir', type=Path, default=ROOT.parent / 'hivemind-public-evidence')
    args = p.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    out = args.evidence_dir.expanduser().resolve() / started.strftime('%Y%m%dT%H%M%S%fZ')
    out.mkdir(parents=True)
    unittest = [sys.executable, '-B', '-m', 'unittest']
    checks = [{'command': [*unittest, 'discover', '-s', 'tests', '-p', args.pattern or 'test*.py', '-v'],
               'log': 'test.log', 'outcome': 'pending'}]
    if args.pattern is None:
        checks.append({'command': [*unittest, '-v', *STANDALONE_SUITES],
                       'log': 'standalone.log', 'outcome': 'pending'})
    files = [path for path in ROOT.rglob('*') if path.is_file() and not
             any(part in {'.git', '__pycache__', '.venv', 'output', 'dist', 'build'} or part.endswith('.egg-info') for part in path.relative_to(ROOT).parts)]
    report = {'checks': checks, 'cwd': str(ROOT), 'outcome': 'running', 'started_at': started.isoformat(),
              'environment': {'python': sys.version, 'platform': platform.platform(),
                              'installed_python': os.environ.get('HIVEMIND_TEST_PYTHON')},
              'fixture': 'Temporary synthetic vaults, projects and agent homes. Standalone suites use fake Codex, Calendar and launchctl boundaries and xterm-256color POSIX PTYs. No provider calls or personal data.',
              'source_sha256': {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    evidence = out / 'evidence.json'

    def save():
        evidence.write_text(json.dumps(report, indent=2) + '\n')

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HIVEMIND_EVIDENCE_DIR=str(out),
               HIVEMIND_E2E_EVIDENCE_DIR=str(out / 'connections'))
    save()
    code = 0
    try:
        for check in checks:
            check['outcome'] = 'running'
            save()
            with (out / check['log']).open('w') as log:
                with subprocess.Popen(check['command'], cwd=ROOT, stdout=log,
                                      stderr=subprocess.STDOUT, env=env) as process:
                    try:
                        status = process.wait(timeout=240)
                    except BaseException:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise
            check.update(outcome='passed' if status == 0 else 'failed', returncode=status)
            code = code or status
            save()
        report.update(outcome='passed' if code == 0 else 'failed', returncode=code)
    except BaseException as exc:
        check.update(outcome='aborted', error=type(exc).__name__)
        report.update(outcome='aborted', error=type(exc).__name__)
        raise
    finally:
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
        for check in checks:
            log = out / check['log']
            if log.exists():
                print(log.name, log.read_text(encoding='utf-8', errors='replace')[-6000:], sep='\n')
        print('Evidence:', evidence)
    return code


if __name__ == '__main__':
    raise SystemExit(main())

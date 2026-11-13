#!/usr/bin/env python3
"""Exercise the shared session contract through both public CLI adapters.

Uses a temporary synthetic store only; every run retains commands, outcomes,
source hashes and environment under output/agent-contract-e2e, including failures.
"""
import os
import argparse
import datetime as dt
import hashlib
import importlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=Path(__file__).resolve().parents[1] / 'hivemind')
    args = parser.parse_args()
    base = args.base.resolve()
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(base.parent.parent / 'hivemind-public-evidence'))) / 'agent-contract-e2e' / stamp
    out.mkdir(parents=True)
    files = [*base.glob('*.py'), *base.glob('schemas/*.json'), Path(__file__).resolve()]
    report = {'command': [sys.executable, *sys.argv], 'code_root': str(base), 'outcome': 'running',
              'environment': {'python': sys.version, 'platform': platform.platform()},
              'fixture': 'Temporary synthetic demo project, seeded via hive.py apply. No providers or private content.',
              'source_sha256': {str(path.relative_to(base)) if path.is_relative_to(base) else path.name:
                                hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
              'cases': [], 'commands': []}
    evidence = out / 'evidence.json'
    def save(): evidence.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    save()
    try:
        sys.path.insert(0, str(base))
        sys.path.insert(0, str(base.parent / 'tests'))
        contract = importlib.import_module('agent_contract')
        from test_sessions import project
        with tempfile.TemporaryDirectory(prefix='hive-contract-e2e-') as temp:
            root = Path(temp).resolve()
            def cli(adapter, *arguments, fail=False):
                argv = [sys.executable, str(base / adapter), '--root', str(root), *arguments]
                if adapter == 'launcher.py': argv.append('--json')
                result = subprocess.run(argv, text=True, capture_output=True, timeout=25)
                report['commands'].append({'argv': argv, 'returncode': result.returncode,
                                           'stdout': result.stdout, 'stderr': result.stderr})
                save()
                if bool(result.returncode) != fail:
                    raise AssertionError(f'{adapter} {arguments[0]} returned {result.returncode}')
                value = json.loads(result.stderr if fail else result.stdout)
                if arguments[0].startswith('session-'):
                    if value.get('contract_version') != 1 or value.get('ok') is not (not fail):
                        raise AssertionError('Missing or incorrect versioned result envelope')
                if fail: contract.validate('error', value)
                return value
            seed = root / 'seed.json'
            seed.write_text(json.dumps({'changes': [{'expected_revision': 'new', 'note': project()}]}))
            cli('hive.py', 'apply', str(seed), '--actor', 'test', '--reason', 'Contract fixture', '--context', 'test:contract')
            for index, adapter in enumerate(('hive.py', 'launcher.py')):
                case = {'adapter': adapter, 'outcome': 'running'}
                report['cases'].append(case); save()
                opened = cli(adapter, 'session-open', 'projects/demo', '--agent', 'contract-test', '--task', 'API retries')
                contract.validate('compact-session', opened)
                ident = opened['session_id']
                other = 'launcher.py' if adapter == 'hive.py' else 'hive.py'
                current = cli(other, 'session-status', ident)
                contract.validate('receipt', current)
                if current['offered'] != opened['offered']: raise AssertionError('Adapters disagree on offered revisions')
                refreshed = cli(adapter, 'session-context', ident, '--task', 'API retries')
                contract.validate('compact-session', refreshed)
                full = cli(adapter, 'session-context', ident, '--view', 'full')
                contract.validate('session', full)
                expanded = cli(adapter, 'evidence', opened['refs'][0]['ref'])
                contract.validate('evidence', expanded)
                draft_path = root / f'handoff-{index}.json'
                created = cli(adapter, 'session-draft', ident, '--out', str(draft_path))
                contract.validate('draft-result', created)
                draft = json.loads(draft_path.read_text())
                contract.validate('draft', draft)
                draft['source'].update(resource=f'test:contract/{index}', title='Synthetic conformance check',
                                       observed_at='2026-10-31T12:00:00Z')
                draft['project_state'] = f'Contract adapter {index} left a verified synthetic handoff.'
                contract.validate('handoff', draft)
                draft_path.write_text(json.dumps(draft))
                closed = cli(adapter, 'session-close', ident, '--file', str(draft_path))
                contract.validate('receipt', closed)
                repeated = cli(other, 'session-close', ident, '--file', str(draft_path))
                if repeated != closed: raise AssertionError('Cross-adapter retry changed the receipt')
                empty = cli(adapter, 'session-open', 'projects/demo', '--agent', 'contract-test')
                no_change = cli(adapter, 'session-close', empty['session_id'], '--no-change')
                contract.validate('receipt', no_change)
                failure = cli(adapter, 'session-status', '0' * 32, fail=True)
                if failure['error_code'] != 'SESSION_NOT_FOUND': raise AssertionError('Wrong stable unknown-session code')
                case.update(outcome='passed', session_id=ident); save()
            cli('hive.py', 'check')
        report['outcome'] = 'passed'
        return 0
    except BaseException as error:
        report.update(outcome='aborted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error={'type': type(error).__name__, 'detail': str(error)})
        for case in report['cases']:
            if case['outcome'] == 'running': case['outcome'] = report['outcome']
        print(f'Conformance {report["outcome"]}: {type(error).__name__}: {error}', file=sys.stderr)
        return 1
    finally:
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
        print('Evidence:', evidence)


if __name__ == '__main__': raise SystemExit(main())

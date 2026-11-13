#!/usr/bin/env python3
"""Paired, local task comparison. Measure CLI traffic; let a reviewer assess answers."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ARMS = ('full', 'compact')
KEY = re.compile(r'[a-z0-9][a-z0-9-]{0,79}\Z')
REF = re.compile(r'([a-z0-9][a-z0-9-]*)@([0-9a-f]{64})\Z')
WORD_BUDGET, BYTE_BUDGET, REDUCTION_TARGET = 1200, 12000, 0.50


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def save(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())
    Path(tmp.name).replace(path)


def parse_spec(spec):
    if spec['version'] != 1 or not spec['cluster'].startswith('projects/'):
        raise ValueError('Invalid case specification')
    tasks = {t['id']: t for t in spec['tasks']}
    if not tasks or len(tasks) != len(spec['tasks']):
        raise ValueError('Expected unique tasks')
    for key, task in tasks.items():
        if not KEY.fullmatch(key) or not task['question'].strip() or not task['criteria']:
            raise ValueError('Expected task ID, question and checklist')
        if not all(KEY.fullmatch(k) and isinstance(v, str) and v.strip()
                   for k, v in task['criteria'].items()):
            raise ValueError('Invalid checklist')
    return tasks


def select(root, patterns):
    return sorted({p.relative_to(root) for pattern in patterns for p in root.glob(pattern)
                   if p.name not in {'log.md', 'index.md'}})


CORPUS = ('wiki/*/*.md', '.history/*.json')
CODE = ('*.py', 'schemas/*.json')


def local_bytes(root, rel):
    p = root / rel
    if p.resolve() != p or not p.is_file():
        raise ValueError(f'Expected a regular local file: {rel}')
    return p.read_bytes()


def freeze(root, dest, paths):
    hashes = {}
    for rel in paths:
        data = local_bytes(root, rel)
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(0o400)
        hashes[str(rel)] = digest(data)
    return hashes


def capture_corpus(source, snapshot, paths):
    curated = [Path(p) for p in paths]
    if len(set(curated)) != len(curated):
        raise ValueError('Duplicate source allowlist entries')
    for rel in curated:
        if (len(rel.parts) != 2 or rel.parts[0] != 'sources' or rel.suffix not in {'.md', '.txt', '.json'}
                or (source / rel).stat().st_size > 128 * 1024):
            raise ValueError('Snapshot sources must be explicitly listed, small, curated files under sources/')
    lock = source / '.hive.lock'
    if lock.is_symlink():
        raise ValueError('Unexpected symlink at HiveMind lock')
    with lock.open('rb') if lock.exists() else nullcontext(None) as lock_handle:
        if lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_SH)
        if (source / '.pending.json').exists():
            raise ValueError('Snapshot blocked by pending HiveMind transaction')
        canonical = select(source, CORPUS)
        if not canonical:
            raise ValueError('No canonical store to snapshot')
        return freeze(source, snapshot, [*canonical, *curated])


def run_command(argv, folder, stdout_path, stderr_path, cwd):
    folder.mkdir(parents=True, exist_ok=True)
    ledger_path = folder / (stdout_path.stem + '.json')
    event = {'argv': argv, 'cwd': str(cwd), 'started_at': now(), 'status': 'running',
             'stdout': stdout_path.name, 'stderr': stderr_path.name}
    save(ledger_path, event)
    start = time.perf_counter()
    out = err = b''
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=60,
                              env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': ''})
        out, err = proc.stdout, proc.stderr
        event.update(status='ok' if proc.returncode == 0 else 'failed', returncode=proc.returncode)
    except subprocess.TimeoutExpired as error:
        out, err = error.stdout or b'', error.stderr or b''
        event.update(status='failed', returncode=124, error='TimeoutExpired')
    except BaseException as error:
        event.update(status='aborted', returncode=130 if isinstance(error, KeyboardInterrupt) else 1,
                     error=type(error).__name__)
        raise
    finally:
        event.update(finished_at=now(), wall_ms=round((time.perf_counter() - start) * 1000, 3),
                     stdout_bytes=len(out), stderr_bytes=len(err),
                     stdout_sha256=digest(out), stderr_sha256=digest(err))
        stdout_path.write_bytes(out)
        stderr_path.write_bytes(err)
        save(ledger_path, event)
    return event


def command(run, *arguments):
    return [sys.executable, str(run / 'code/hive.py'), '--root', str(run / 'snapshot'), *arguments]


def record(run, folder, stem, argv):
    return run_command(argv, folder, folder / (stem + '.stdout'), folder / (stem + '.stderr'), run / 'code')


def verify(run, manifest):
    for dirname, key, patterns in (('snapshot', 'corpus_sha256', CORPUS),
                                   ('code', 'code_sha256', CODE)):
        root = run / dirname
        expected = manifest[key]
        actual = {str(rel) for rel in select(root, patterns)}
        if actual != {name for name in expected if not name.startswith('sources/')}:
            raise ValueError(f'{dirname} file inventory changed after preparation')
        for name, hash_value in expected.items():
            if digest(local_bytes(root, Path(name))) != hash_value:
                raise ValueError(f'Frozen {dirname} changed: {name}')
    if digest(local_bytes(run, Path('spec.json'))) != manifest['spec_sha256']:
        raise ValueError('Frozen task specification changed')
    if digest(local_bytes(run, Path('compare_agent_context.py'))) != manifest['runner_sha256']:
        raise ValueError('Frozen comparison runner changed')


def prepare(args):
    run, source, code = args.out.resolve(), args.root.resolve(), args.code_root.resolve()
    run.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = {'version': 1, 'status': 'running', 'started_at': now(), 'argv': sys.argv,
               'cwd': str(Path.cwd()), 'source_root': str(source), 'python': sys.version,
               'platform': platform.platform(), 'word_budget': WORD_BUDGET, 'compact_max_bytes': BYTE_BUDGET,
               'fixture': 'Canonical wiki and journal, selected curated sources, no operational receipts.'}
    receipt_path = run / 'manifest.json'
    save(receipt_path, receipt)
    try:
        spec_bytes = args.spec.read_bytes()
        receipt['spec_sha256'] = digest(spec_bytes)
        frozen_spec = run / 'spec.json'
        frozen_spec.write_bytes(spec_bytes)
        frozen_spec.chmod(0o400)
        spec = json.loads(spec_bytes)
        tasks = parse_spec(spec)
        receipt['code_sha256'] = freeze(code, run / 'code', select(code, CODE))
        # Keep the runner outside the tool code inventory: it is an experiment driver.
        runner = run / 'compare_agent_context.py'
        runner.write_bytes(Path(__file__).read_bytes())
        runner.chmod(0o400)
        receipt['runner_sha256'] = digest(runner.read_bytes())
        receipt['corpus_sha256'] = capture_corpus(source, run / 'snapshot', spec['sources'])
        save(receipt_path, receipt)
        audit = record(run, run, 'audit', command(run, 'check'))
        if audit['status'] != 'ok' or load(run / 'audit.stdout').get('valid') is not True:
            raise ValueError('Snapshot did not pass HiveMind check; see audit outputs')
        failed = []
        session_state = run / 'snapshot/.state/sessions'
        for index, (task_id, task) in enumerate(tasks.items()):
            task_dir = run / 'tasks' / task_id
            task_dir.mkdir(parents=True)
            (task_dir / 'question.txt').write_text(task['question'] + '\n', encoding='utf-8')
            for arm in (ARMS if index % 2 == 0 else ARMS[::-1]):
                if session_state.exists():
                    raise ValueError('Unexpected existing session state in the frozen store')
                folder = task_dir / arm
                argv = command(run, 'session-open', spec['cluster'], '--task', task['question'],
                               '--view', arm, '--budget', str(WORD_BUDGET))
                if arm == 'compact':
                    argv += ['--max-bytes', str(BYTE_BUDGET)]
                try:
                    event = record(run, folder, 'initial', argv)
                finally:
                    if session_state.exists():
                        shutil.move(str(session_state), folder / 'session-receipts')
                if event['status'] != 'ok':
                    failed.append(f'{task_id}/{arm}')
                else:
                    packet = load(folder / 'initial.stdout')
                    if packet.get('previous_context_at') is not None:
                        raise ValueError('A comparison arm inherited another session baseline')
        verify(run, receipt)
        if failed:
            raise ValueError('Initial packet failures: ' + ', '.join(failed))
        receipt['status'] = 'ready'
    except BaseException as error:
        receipt.update(status='aborted' if isinstance(error, KeyboardInterrupt) else 'failed', error=str(error))
        raise
    finally:
        receipt['finished_at'] = now()
        save(receipt_path, receipt)
        if receipt['status'] == 'ready':
            seal = run / 'manifest.sha256'
            seal.write_text(digest(receipt_path.read_bytes()) + '\n', encoding='ascii')
            seal.chmod(0o400)
            receipt_path.chmod(0o400)
        print(f'Manifest: {receipt_path}')
    return 0


def opened(args, ready=True):
    run = args.run.resolve()
    raw = local_bytes(run, Path('manifest.json'))
    receipt = json.loads(raw)
    seal = run / 'manifest.sha256'
    if receipt['status'] == 'ready' or seal.exists():
        if (not seal.is_file() or seal.is_symlink() or digest(raw) != seal.read_text(encoding='ascii').strip()
                or receipt['status'] != 'ready'):
            raise ValueError('Frozen manifest changed')
        verify(run, receipt)
        tasks = parse_spec(load(run / 'spec.json'))
    else:
        if ready:
            raise ValueError('Run is not ready; inspect its manifest')
        try:
            tasks = parse_spec(load(run / 'spec.json'))
        except (ValueError, OSError, KeyError, AttributeError, TypeError):
            # A run can fail before its specification is available or valid.
            tasks = {}
    if hasattr(args, 'task') and args.task not in tasks:
        raise ValueError('Unknown task')
    return run, receipt, tasks


def folder_for(run, task, arm):
    return run / 'tasks' / task / arm


def legacy_ref(run, match):
    ident, revision = match.groups()
    files = list((run / 'snapshot/wiki').glob(f'*/{ident}.md'))
    if len(files) != 1:
        return False
    raw = files[0].read_text(encoding='utf-8')
    header = raw.split('\n---\n', 1)[0]
    return '\nsensitivity: "internal"\n' in header and digest(raw.encode()) == revision


def read(args):
    run, _, _ = opened(args)
    folder = folder_for(run, args.task, args.arm)
    if (folder / 'answer.json').exists():
        raise ValueError('This arm already submitted its answer')
    match = REF.fullmatch(args.ref)
    if not match:
        raise ValueError('Use a full ID@REVISION reference')
    if args.legacy_show and (args.arm != 'full' or args.section or args.source or
                             args.offset is not None or args.limit is not None or args.max_bytes is not None):
        raise ValueError('--legacy-show requires the full arm and no evidence selectors')
    call = Path(tempfile.mkdtemp(prefix='read-', dir=folder))
    if args.legacy_show and not legacy_ref(run, match):
        msg = b'Legacy show requires a current internal note at the exact frozen revision.\n'
        (call / 'output.stdout').write_bytes(b'')
        (call / 'output.stderr').write_bytes(msg)
        save(call / 'output.json', {'status': 'failed', 'returncode': 1, 'wall_ms': 0,
             'stdout_bytes': 0, 'stderr_bytes': len(msg), 'ref': args.ref,
             'stdout_sha256': digest(b''), 'stderr_sha256': digest(msg), 'started_at': now(),
             'argv': sys.argv, 'cwd': str(Path.cwd())})
        sys.stderr.buffer.write(msg)
        return 1
    argv = command(run, 'show', match[1]) if args.legacy_show else command(run, 'evidence', args.ref)
    for name in ('section', 'source', 'offset', 'limit', 'max_bytes'):
        value = getattr(args, name)
        if value is not None:
            argv += ['--' + name.replace('_', '-'), str(value)]
    event = record(run, call, 'output', argv)
    sys.stdout.buffer.write((call / 'output.stdout').read_bytes())
    sys.stderr.buffer.write((call / 'output.stderr').read_bytes())
    return event['returncode']


def submit(args):
    run, _, _ = opened(args)
    result = load(args.file)
    if (set(result) != {'answer', 'refs'} or not isinstance(result['answer'], str)
            or not 0 < len(result['answer'].strip()) <= 6000 or not isinstance(result['refs'], list)
            or not all(isinstance(ref, str) and 0 < len(ref) <= 300 for ref in result['refs'])):
        raise ValueError('Expected {answer: text, refs: [text]}')
    path = folder_for(run, args.task, args.arm) / 'answer.json'
    with path.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    return 0


def grade(args):
    run, _, tasks = opened(args)
    value = load(args.file)
    if (not isinstance(value['reviewer'], str) or not value['reviewer'].strip()
            or value['method'] not in {'agent', 'human'} or not value['entries']):
        raise ValueError('An attributed agent or human review is required')
    seen = set()
    for entry in value['entries']:
        key = (entry['task'], entry['arm'])
        checklist = tasks[key[0]]['criteria']
        if (key in seen or key[1] not in ARMS or entry['criteria'].keys() != checklist.keys()
                or any(type(v) is not bool for v in entry['criteria'].values())
                or not entry['comment'].strip() or not (folder_for(run, *key) / 'answer.json').exists()):
            raise ValueError('Review each submitted answer once using every fixed criterion and a comment')
        seen.add(key)
    with (run / 'grades.json').open('x', encoding='utf-8') as handle:
        json.dump({**value, 'reviewed_at': now()}, handle, ensure_ascii=False, indent=2)
    return 0


def measurements(folder):
    initial = load(folder / 'initial.json') if (folder / 'initial.json').exists() else {}
    calls = [load(p) for p in sorted(folder.glob('read-*/output.json'))]
    size = lambda row: row.get('stdout_bytes', 0) + row.get('stderr_bytes', 0)
    first, extra = size(initial), sum(map(size, calls))
    return {'initial_status': initial.get('status', 'missing'), 'initial_bytes': first, 'followup_bytes': extra,
            'total_bytes': first + extra, 'followup_calls': len(calls),
            'followup_failures': sum(c['status'] != 'ok' for c in calls),
            'cli_wall_ms': round(sum(c.get('wall_ms', 0) for c in [initial, *calls]), 3),
            'answer_submitted': (folder / 'answer.json').exists()}


def report(args):
    run, receipt, tasks = opened(args, ready=False)
    rows = {task: {arm: measurements(folder_for(run, task, arm)) for arm in ARMS} for task in tasks}
    totals = {arm: {key: sum(r[arm][key] for r in rows.values()) for key in (
        'initial_bytes', 'followup_bytes', 'total_bytes', 'followup_calls', 'followup_failures',
        'cli_wall_ms', 'answer_submitted')} for arm in ARMS}
    paired = bool(rows) and all(r[arm]['initial_status'] == 'ok' for r in rows.values() for arm in ARMS)
    complete = bool(rows) and all(r[arm]['answer_submitted'] for r in rows.values() for arm in ARMS)
    reduction = 1 - totals['compact']['total_bytes'] / totals['full']['total_bytes'] if paired else None
    assessment = {'status': 'unscored', 'scope': 'No correctness assessment from payload, metrics or keyword matching.'}
    if receipt['status'] == 'ready' and (run / 'grades.json').exists():
        grades = load(run / 'grades.json')
        entries = grades['entries']
        scores = {arm: {'criteria_met': sum(sum(e['criteria'].values()) for e in entries if e['arm'] == arm),
                        'criteria_reviewed': sum(len(e['criteria']) for e in entries if e['arm'] == arm),
                        'answers_reviewed': sum(e['arm'] == arm for e in entries)} for arm in ARMS}
        assessment = {'status': 'reviewed' if len(entries) == 2 * len(tasks) else 'partially_reviewed',
                      'reviewer': grades['reviewer'], 'method': grades['method'], 'totals': scores,
                      'scope': f"Attributed {grades['method']} assessment on fixed tasks; no general quality claim.",
                      'details': 'grades.json'}
    result = {'run_status': receipt['status'], 'prepare_error': receipt.get('error'),
              'task_count': len(tasks), 'technical': {
        'paired_initials_complete': paired, 'paired_answers_complete': complete, 'totals': totals,
        'compact_reduction_fraction': reduction, 'target_reduction_fraction': REDUCTION_TARGET,
        'target_met': reduction >= REDUCTION_TARGET if reduction is not None and complete else None,
        'scope': 'Raw stdout + stderr UTF-8 bytes and elapsed CLI process time; includes failed reads. '
                 'No token, cost, agent latency or host effectiveness inference.'},
        'assessment': assessment, 'tasks': rows}
    save(run / 'report.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--root', type=Path, default=HERE.parent)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--spec', type=Path, default=HERE / 'agent_context_cases.json')
    p.add_argument('--code-root', type=Path, default=HERE.parent, help=argparse.SUPPRESS)
    p.set_defaults(func=prepare)
    for name, fn in [('read', read), ('submit', submit), ('grade', grade), ('report', report)]:
        p = sub.add_parser(name)
        p.add_argument('run', type=Path)
        if name in {'read', 'submit'}:
            p.add_argument('task')
            p.add_argument('arm', choices=ARMS)
        if name in {'submit', 'grade'}:
            p.add_argument('file', type=Path)
        if name == 'read':
            p.add_argument('ref')
            group = p.add_mutually_exclusive_group()
            group.add_argument('--section')
            group.add_argument('--source')
            group.add_argument('--legacy-show', action='store_true')
            for option in ('offset', 'limit', 'max-bytes'):
                p.add_argument('--' + option, type=int)
        p.set_defaults(func=fn)
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ValueError, OSError, KeyError, AttributeError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Single active, bounded Codex sync. Receipts, not process exit, prove completion.

Uses the existing Codex login, model and approval configuration. No new API key,
provider, scheduler or permission bypass. Raw agent/provider output is discarded.
Run metadata, bounded coverage receipts and journaled knowledge remain local.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
import re
from pathlib import Path
if __package__:
    from .runtime_paths import default_vault
else:
    from runtime_paths import default_vault
import shutil
import signal
import subprocess
import sys
import time
import uuid

if __package__:
    from .integrations import ingest
else:
    from integrations import ingest

BASE = Path(__file__).resolve().parent
MAX_SECONDS = 1800


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


@contextlib.contextmanager
def locked(root):
    folder = Path(root)/'.state'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder/'jobs.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def read(root):
    path = Path(root)/'.state/job.json'
    if not path.exists():
        return {'status': 'idle', 'message': 'No sync has been started yet.'}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('status') not in {
            'idle', 'running', 'complete', 'partial', 'failed'}:
        raise ValueError('Invalid job status; existing data is preserved.')
    return value


def dispatch_path(root, dispatch_id):
    if not isinstance(dispatch_id,str) or not re.fullmatch(r'[0-9a-f]{32}',dispatch_id):
        raise ValueError('Invalid dispatch ID')
    return Path(root)/'.state/dispatches'/(dispatch_id+'.json')


def remember_dispatch(root,value):
    if value.get('dispatch_id'):
        ingest.atomic(dispatch_path(root,value['dispatch_id']),value)


def write(root, value):
    ingest.atomic(Path(root)/'.state/job.json', value)
    remember_dispatch(root,value)
    return value


def writer_active(root):
    """A guard retains this lease even if its orchestration worker is killed."""
    with (Path(root)/'.state/job-worker.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def alive(root, value):
    # Test the actual lease first: the worker PID can be dead while Codex writes.
    if writer_active(root):
        return True
    if value.get('status') != 'running':
        return False
    age = (datetime.now(timezone.utc)-datetime.fromisoformat(
        value['started_at'].replace('Z', '+00:00'))).total_seconds()
    if age < 10 and value.get('phase') == 'dispatching':
        return True  # reservation -> Popen -> worker lease startup window
    try:
        os.kill(int(value['pid']), 0)
    except (KeyError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    age = (datetime.now(timezone.utc)-datetime.fromisoformat(
        value['started_at'].replace('Z', '+00:00'))).total_seconds()
    if age < 10:
        return True  # detached child has a short startup window
    return False


def owned_run(root, value):
    """Find the reservation by owner, including begin_run -> job.json crash gap."""
    active = ingest.source_status(root).get('active_run')
    if not active:
        return None
    if not isinstance(active, str) or Path(active).name != active:
        raise ValueError('Invalid active source reservation.')
    path = Path(root)/'.state/runs'/active/'manifest.json'
    manifest = json.loads(path.read_text())
    if manifest.get('owner') == value.get('job_id') and value.get('job_id'):
        return active
    return None


def recover(root, value):
    """Only abort the source reservation this interrupted job owned."""
    run_id = owned_run(root, value)
    if run_id:
        ingest.abort_run(root, run_id, 'The local sync process was interrupted.')
    return write(root, {**value, 'status': 'failed', 'finished_at': now(),
                        'start_failed': value.get('phase') in {'dispatching','preparing'} and not value.get('agent_started_at'),
                        'message': 'Sync interrupted. Saved knowledge is preserved; start again.'})


def public(value):
    return {key: val for key, val in value.items() if key != 'pid'}


def status(root):
    root = Path(root).resolve()
    with locked(root):
        value = read(root)
        if value['status'] == 'running' and not alive(root, value):
            value = recover(root, value)
        return public(value)


def dispatch_status(root, dispatch_id):
    """Recover an accepted dispatch even if a newer manual job replaced job.json."""
    root=Path(root).resolve();path=dispatch_path(root,dispatch_id)
    with locked(root):
        value=read(root)
        if value.get('dispatch_id')==dispatch_id:
            if value['status']=='running' and not alive(root,value):value=recover(root,value)
            remember_dispatch(root,value)
            return public(value)
        return public(json.loads(path.read_text())) if path.exists() else None


def view(root):
    """UI state separates a disabled dispatcher from an idle/running job."""
    value=status(root)
    if (Path(root)/'.hivemind.json').is_file() and value['status']!='running':
        if __package__:
            from . import feature_controls,automation
        else:
            sys.path.insert(0,str(BASE.parent))
            from hivemind import feature_controls,automation
        state='disabled' if not feature_controls.settings(root)['sync_enabled'] else 'paused' if automation.status(root)['paused'] else None
        if state:return {**value,'last_status':value['status'],'status':state}
    return value


def start(root, *, plan=None, dispatch_id=None):
    root = Path(root).resolve()
    if (root / '.hivemind.json').exists():
        if __package__:
            from . import feature_controls
        else:
            sys.path.insert(0,str(BASE.parent))
            from hivemind import feature_controls
        preferences=feature_controls.require_sync(root)
    else:
        preferences=None
    if dispatch_id is not None:dispatch_path(root,dispatch_id)
    if plan is not None:
        plan=ingest.validate_plan(root,plan)
        if plan.get('dispatch_id')!=dispatch_id:
            raise ValueError('Plan and dispatch IDs do not match')
    with locked(root):
        value = read(root)
        # Complete the tiny job.json -> dispatch receipt gap before replacing it.
        remember_dispatch(root,value)
        if writer_active(root):return public(value)
        if value['status'] == 'running':
            if alive(root, value):return public(value)
            value=recover(root, value)
        if dispatch_id is not None:
            receipt=dispatch_path(root,dispatch_id)
            accepted=json.loads(receipt.read_text()) if receipt.exists() else None
            if accepted and not accepted.get('start_failed',False):return public(accepted)
        plan=plan if plan is not None else ingest.prepare_plan(root,dispatch_id=dispatch_id)
        binary = preferences['codex'] if preferences else shutil.which('codex')
        if not binary:
            fallback = Path.home()/'.local/bin/codex'
            binary = str(fallback) if fallback.is_file() else None
        value = {'status': 'running', 'job_id': uuid.uuid4().hex,
                 'dispatch_id':dispatch_id,'plan':plan,
                 'started_at': now(), 'message': 'Preparing sources.',
                 'phase': 'dispatching'}
        if not binary:
            return public(write(root, {**value,'status': 'failed', 'finished_at': now(),
                                      'start_failed':True,'message': 'Codex CLI is unavailable.'}))
        write(root, value)
        try:
            child = subprocess.Popen(
                [sys.executable, str(BASE/'jobs.py'), '--root', str(root),
                 '--worker', value['job_id'], '--codex', binary],
                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
                close_fds=True)
        except OSError:
            return public(write(root, {**value, 'status': 'failed', 'finished_at': now(),
                                      'start_failed':True,'message': 'The local sync process could not start.'}))
        value['pid'] = child.pid
        return public(write(root, value))


def update(root, job_id, **fields):
    with locked(root):
        value = read(root)
        if value.get('job_id') != job_id:
            raise ValueError('Job was replaced by another run.')
        return write(root, {**value, **fields})


def prompt_for(root, run_id):
    if (Path(root)/'.hivemind.json').is_file():
        if __package__:
            from .sync_prompt import prompt
        else:
            sys.path.insert(0,str(BASE.parent))
            from hivemind.sync_prompt import prompt
        return prompt(root,run_id)
    return (
        '$hivemind-sync\n'
        f'Process the prepared HiveMind run {run_id}. '
        f'Working root: {root}. '
        f'Read .state/runs/{run_id}/manifest.json and '
        '.agents/skills/hivemind-sync/SKILL.md. '
        'The manifest plan limits the task: read only its local IDs and remote time windows. '
        'If windows are empty, do not call Gmail/Wispr tools; local sync needs no remote source. '
        f'Write only the final source receipts to .state/runs/{run_id}/receipts.json. '
        'The calling process validates and finishes the run. '
        'Do not call begin/finish/abort or start another job yourself. '
        'Read Calendar only from its existing operational snapshot when needed; check its freshness. '
        'Curate relevant knowledge with hive.py draft/apply. '
        'Do not change project implementation, instructions, skills, configuration, or permissions. '
        'Do not send messages, write externally, publish, or set up new services. '
        'Report missing access truthfully as blocked; finish independent sources. '
        'Do not use subagents for this bounded sync. '
        'Finish within 20 minutes with a complete receipt envelope.'
    )


def stop_child(child):
    """Only signal the exact live subprocess/session this process created."""
    if child and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        except ProcessLookupError:
            child.wait()


def agent_guard(root, run_id, binary, lease_fd, max_seconds):
    """Independent lease holder: bound Codex even after its worker is SIGKILLed.

    The descriptor is inherited from the worker's flock open description. Codex
    does not inherit it. Its raw stdout/stderr never enter files or job metadata.
    """
    child = None

    def interrupted(signum, frame):
        raise InterruptedError('Agent invocation was interrupted.')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with os.fdopen(lease_fd, 'a'):
        try:
            environment=None
            if (Path(root)/'.hivemind.json').is_file():
                if __package__:
                    from . import feature_controls
                else:
                    sys.path.insert(0,str(BASE.parent))
                    from hivemind import feature_controls
                preferences=feature_controls.require_sync(root)
                # Select only the explicitly configured profile. No credentials,
                # model, approval policy, hooks or global configuration are copied.
                environment=dict(os.environ,CODEX_HOME=preferences['agent_home'])
            child = subprocess.Popen(
                [binary, '--no-daemon', 'exec', '--ephemeral', '--skip-git-repo-check',
                 '--cd', str(root), '--json', prompt_for(root, run_id)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,env=environment)
            return child.wait(timeout=max_seconds)
        except subprocess.TimeoutExpired:
            return 124
        except (OSError, InterruptedError):
            return 125
        finally:
            stop_child(child)


def worker(root, job_id, binary, max_seconds=MAX_SECONDS):
    root = Path(root).resolve()
    runner = (root/'.state/job-worker.lock').open('a')
    try:
        fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        runner.close()
        return 1
    child = None
    run_id = None
    finished = False
    calendar_ok = True
    failure = None

    def interrupted(signum, frame):
        raise InterruptedError('Sync was interrupted.')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if (root/'.hivemind.json').is_file():
            if __package__:
                from . import feature_controls
            else:
                sys.path.insert(0,str(BASE.parent))
                from hivemind import feature_controls
            feature_controls.require_sync(root)
        with locked(root):
            current = read(root)
            if current.get('job_id') != job_id or current['status'] != 'running':
                return 1
        update(root,job_id,phase='preparing')
        manifest = ingest.begin_run(root, owner=job_id, plan=current.get('plan'))
        run_id = manifest['id']
        update(root, job_id, run_id=run_id)
        if manifest['plan']['reason'] != 'local-change' and ingest.config(root).get('calendar', {}).get('enabled', not (root/'.hivemind.json').exists()):
            snapshot = ingest.refresh_calendar(root)
            calendar_ok = snapshot.get('status') == 'ok'
        update(root, job_id, phase='curating',
               message='Codex is checking only the sources in the prepared sync plan.')
        # The guard inherits the existing lease atomically with process creation.
        # It survives worker SIGKILL and is solely responsible for bounding Codex.
        child = subprocess.Popen(
            [sys.executable, str(BASE/'jobs.py'), '--root', str(root),
             '--agent-run', run_id, '--codex', binary, '--lease-fd', str(runner.fileno()),
             '--agent-timeout', str(max_seconds)],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            pass_fds=(runner.fileno(),))
        update(root, job_id, phase='curating', agent_started_at=now())
        # Keep an otherwise idle Mac awake while Codex works; a closed lid on battery still sleeps.
        try:
            subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(child.pid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError:
            pass
        code = child.wait()
        if code == 124:
            raise ValueError('Time limit reached. Already curated knowledge is preserved.')
        if code:
            raise ValueError(f'Codex exited with error code {code}; no complete source receipt.')
        update(root, job_id, phase='validating', message='Validating source receipts and knowledge integrity.')
        path = root/'.state/runs'/run_id/'receipts.json'
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError('Codex did not produce a valid final source receipt.')
        receipts = json.loads(path.read_text())
        result = ingest.finish_run(root, run_id, receipts)
        finished = True
        state = result['status']
        if state == 'complete' and not calendar_ok:
            state = 'partial'
        messages = {
            'complete': 'Sync complete. All prepared sources have receipts and knowledge has been checked.',
            'partial': 'Partially complete. Some sources or the calendar remain pending; see source details.',
            'failed': 'Sync could not be fully verified. Source status remains unchanged accordingly.'}
        update(root, job_id, status=state, phase='finished', finished_at=now(),
               calendar_status='ok' if calendar_ok else 'unavailable',
               message=messages[state])
        return 0 if state in {'complete', 'partial'} else 1
    except Exception as error:
        # Only our own bounded validation messages, never child output.
        detail = str(error)[:400] if isinstance(error, (ValueError, InterruptedError)) else type(error).__name__
        failure = detail
        return 1
    finally:
        guard_pending = False
        if child and child.poll() is None:
            # Signal only our actual guard. Its handler stops its actual Codex child.
            try:
                child.terminate()
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Do not kill the independent watchdog and strand its writer.
                # It retains the lease and deadline; a later status/start recovers
                # the reservation only after that lease has actually disappeared.
                guard_pending = True
            except ProcessLookupError:
                pass
        if not finished and not guard_pending:
            # Owner lookup also covers an interruption before run_id assignment.
            owned = owned_run(root, {'job_id': job_id})
            try:
                if owned:
                    ingest.abort_run(root, owned, 'Sync ended without fully validated source receipts.')
            except ValueError:
                # If it remains ours, leave the job running for explicit recovery.
                if owned_run(root, {'job_id': job_id}):
                    raise
        # Never expose terminal failure before source reservation cleanup completes.
        if failure is not None and not guard_pending:
            update(root, job_id, status='failed', phase='finished',
                   finished_at=now(), message='Sync failed: '+failure)
        runner.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=default_vault())
    parser.add_argument('--worker')
    parser.add_argument('--codex')
    parser.add_argument('--agent-run', help=argparse.SUPPRESS)
    parser.add_argument('--lease-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--agent-timeout', type=float, default=MAX_SECONDS,
                        help=argparse.SUPPRESS)
    parser.add_argument('command', nargs='?', choices=['start', 'status'])
    args = parser.parse_args()
    if not 0 < args.agent_timeout <= MAX_SECONDS:
        parser.error('agent timeout must be positive and at most 1800 seconds')
    if args.agent_run:
        if not args.codex or args.lease_fd is None:
            parser.error('--agent-run requires --codex and --lease-fd')
        return agent_guard(args.root.resolve(), args.agent_run, args.codex,
                           args.lease_fd, args.agent_timeout)
    if args.worker:
        if not args.codex:
            parser.error('--worker requires --codex')
        return worker(args.root, args.worker, args.codex, args.agent_timeout)
    try:
        print(json.dumps(start(args.root) if args.command == 'start' else status(args.root),
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

"""Owned per-vault user services. Never address the legacy personal service."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from . import hive
from .runtime_paths import CODE, cli_command, require_vault


def spec(root, *, directory=None, platform=None):
    root = Path(root).resolve()
    platform = platform or sys.platform
    ident = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    command = cli_command(root, 'automation', 'serve')
    if any('\n' in arg or '\r' in arg for arg in command):
        raise ValueError('Service paths must not contain line breaks')
    if platform == 'darwin':
        label = 'local.hivemind.public.' + ident
        folder = Path(directory) if directory else Path.home()/'Library/LaunchAgents'
        path = folder/(label+'.plist')
        data = plistlib.dumps({'Label': label, 'ProgramArguments': command,
            'WorkingDirectory': str(CODE.parent), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 30, 'ProcessType': 'Background',
            'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
                                     'PYTHONDONTWRITEBYTECODE': '1'},
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}, sort_keys=True)
    elif platform.startswith('linux'):
        label = 'hivemind-' + ident
        folder = Path(directory) if directory else Path.home()/'.config/systemd/user'
        path = folder/(label+'.service')
        def quote(value):
            return json.dumps(str(value)).replace('%','%%').replace('$','$$')
        data = ('[Unit]\nDescription=HiveMind local vault scheduler\n\n[Service]\nType=simple\n'
            'ExecStart='+' '.join(map(quote,command))+'\nWorkingDirectory='+quote(CODE.parent)+'\n'
            'Restart=on-failure\nRestartSec=30\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
            'StandardOutput=null\nStandardError=null\n\n[Install]\nWantedBy=default.target\n').encode()
    else:
        raise ValueError('Login startup needs macOS launchd or Linux systemd --user. Use hive automation serve in a terminal.')
    return {'label':label,'path':str(path),'platform':platform,'command':command,'data':data}


def _record(root):
    path = Path(root)/'.state/service.json'
    if path.is_symlink():
        raise ValueError('Service ownership must not be a symlink')
    return json.loads(path.read_text()) if path.exists() else None


def _owned(root, selected):
    path = Path(selected['path'])
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('Service path must not use symlinks')
    record = _record(root)
    if path.exists():
        if (not record or record.get('root')!=str(Path(root).resolve())
                or record.get('path')!=str(path) or record.get('label')!=selected['label']
                or record.get('sha256')!=hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError('Service file changed or is not owned by this vault; review it before changing startup.')
    return record


def status(root):
    try:
        selected = spec(root)
        record = _owned(root, selected)
        installed=bool(record and Path(selected['path']).is_file())
        stale=installed and record['sha256']!=hashlib.sha256(selected['data']).hexdigest()
        return {'installed':installed,'needs_update':stale,
                'label':selected['label'],'path':selected['path'],'supported':True,
                'repair':'Preview and apply hive automation enable background.' if stale else None}
    except ValueError as error:
        return {'installed':False,'supported':False,'error':str(error)}


def _run(runner, command, *, allow_failure=False):
    result = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, timeout=20, check=False)
    if result.returncode and not allow_failure:
        raise RuntimeError('User service command failed: '+' '.join(command[:3])+
            '. Check the user service manager, then retry; vault data is preserved.')
    return result


def install(root, *, directory=None, platform=None, runner=None):
    require_vault(root)
    selected = spec(root,directory=directory,platform=platform)
    _owned(root,selected)
    runner = runner or subprocess.run
    path = Path(selected['path'])
    changed = not path.exists() or path.read_bytes()!=selected['data']
    if selected['platform']=='darwin':
        target='gui/'+str(os.getuid())+'/'+selected['label']
        loaded = _run(runner,['/bin/launchctl','print',target],allow_failure=True).returncode==0
        if loaded and changed:
            _run(runner,['/bin/launchctl','bootout',target]);loaded=False
    path.parent.mkdir(parents=True,exist_ok=True)
    if changed:
        hive.atomic(path,selected['data'].decode())
        path.chmod(0o600)
    # Retain ownership on a service-manager failure so disable/retry can recover.
    record={'version':1,'root':str(Path(root).resolve()),'path':str(path),
            'label':selected['label'],'sha256':hashlib.sha256(selected['data']).hexdigest()}
    hive.atomic(Path(root)/'.state/service.json',json.dumps(record,indent=2)+'\n')
    if selected['platform']=='darwin':
        _run(runner,['/bin/launchctl','enable',target])
        if not loaded:
            _run(runner,['/bin/launchctl','bootstrap','gui/'+str(os.getuid()),str(path)])
    else:
        _run(runner,['systemctl','--user','daemon-reload'])
        _run(runner,['systemctl','--user','enable','--now',path.name])
        if changed:
            _run(runner,['systemctl','--user','restart',path.name])
    return {'installed':True,'label':selected['label'],'path':str(path)}


def uninstall(root, *, directory=None, platform=None, runner=None):
    require_vault(root)
    selected=spec(root,directory=directory,platform=platform)
    record=_owned(root,selected)
    path=Path(selected['path']);runner=runner or subprocess.run
    if not record:
        return {'installed':False,'label':selected['label']}
    if selected['platform']=='darwin':
        target='gui/'+str(os.getuid())+'/'+selected['label']
        stopped=_run(runner,['/bin/launchctl','bootout',target],allow_failure=True)
        if stopped.returncode and _run(runner,['/bin/launchctl','print',target],allow_failure=True).returncode==0:
            raise RuntimeError('Could not stop this vault service; its owned file is preserved.')
    else:
        _run(runner,['systemctl','--user','disable','--now',path.name])
    path.unlink(missing_ok=True)
    (Path(root)/'.state/service.json').unlink(missing_ok=True)
    if selected['platform']!='darwin':
        _run(runner,['systemctl','--user','daemon-reload'])
    return {'installed':False,'label':selected['label'],
            'message':'Startup removed for this vault. Already running sync jobs finish independently.'}

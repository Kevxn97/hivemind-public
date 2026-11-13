"""One entrypoint for setup, agent workflows and the local terminal."""
import argparse
import json
from pathlib import Path
import shlex
import sys

from . import __version__, agent_contract, hive, launcher, onboarding, workflow_cli
from .runtime_paths import default_vault, require_vault, resolve_vault

CORE_COMMANDS = {'browse', 'show', 'search', 'draft', 'apply', 'reindex', 'recover',
                 'check', 'eval', 'feedback', 'history'}


def _parser():
    parser = workflow_cli.ArgumentParser(prog='hive', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = onboarding.configure(sub)
    init.add_argument('--agent', choices=('codex', 'claude', 'none'), help='Prepare the selected agent connection')
    from . import connections
    connections.configure(sub)
    doctor = sub.add_parser('doctor', help='Inspect the selected vault and local integration without writing')
    doctor.add_argument('--home', type=Path)
    doctor.add_argument('--codex-home', type=Path)
    doctor.add_argument('--claude-home', type=Path)
    test = sub.add_parser('self-test', help='Prove a first memory round trip in an isolated synthetic vault')
    test.add_argument('--evidence-dir', type=Path)
    return parser


def diagnose(root, home=None, codex_home=None, claude_home=None):
    from . import connections, doctor
    canonical, records = doctor._canonical(root)
    checks = [canonical, doctor._projects(root, records), doctor._sessions(root)]
    for agent, explicit in (('codex', codex_home), ('claude', claude_home)):
        folder = explicit or (Path(home) / ('.' + agent) if home else None)
        result = connections.inspect(root, agent, agent_home=folder)
        checks.extend(result['checks'])
    summary = {status: sum(c['status'] == status for c in checks) for status in ('ok', 'attention', 'error')}
    return {'status': 'error' if summary['error'] else 'attention' if summary['attention'] else 'ok',
            'scope': 'read_only_disk_snapshot', 'host_execution': 'unverified',
            'summary': summary, 'checks': checks}


def _initialize(args, root):
    result = onboarding.execute(args, root)
    if result.get('cancelled'):
        return result
    root = Path(result['vault'])
    agent = args.agent
    if agent is None and sys.stdin.isatty() and not args.yes:
        print('Agent [codex/claude/none] (none): ', end='', file=sys.stderr, flush=True)
        agent = sys.stdin.readline().strip() or 'none'
        if agent not in {'codex', 'claude', 'none'}:
            raise ValueError('Choose codex, claude or none. The vault has been initialized; use hive connect to continue.')
    result['agent'] = agent or 'none'
    result['next_steps'] = [shlex.join(['hive', '--root', str(root), 'self-test']),
                            shlex.join(['hive', '--root', str(root), 'doctor'])]
    if agent and agent != 'none':
        from . import connections
        connection_args = argparse.Namespace(command='connect', agent=agent, agent_home=None, apply=False)
        result['connection'] = connections.execute(connection_args, root)
        if sys.stdin.isatty() and not args.yes:
            print(json.dumps(result['connection'], ensure_ascii=False, indent=2), file=sys.stderr)
            print('Install these agent settings now? [y/N]: ', end='', file=sys.stderr, flush=True)
            if sys.stdin.readline().strip().lower() in {'y', 'yes'}:
                connection_args.apply = True
                result['connection'] = connections.execute(connection_args, root)
        result['next_steps'].insert(0, shlex.join(['hive', '--root', str(root), 'connect', agent, '--apply']))
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ['--version']:
        print('HiveMind ' + __version__)
        return 0
    if argv in (['--help'], ['-h']):
        print('HiveMind — local project memory for Codex and Claude\n\n'
              'Setup:    hive init [VAULT] [--project PATH] [--agent codex|claude|none]\n'
              'Connect:  hive connect codex|claude [--apply] [--agent-home PATH]\n'
              'Verify:   hive self-test; hive doctor\n'
              'Context:  hive brief PATH; hive session-open PATH --agent codex\n'
              'Handoff:  hive session-draft ID --out FILE; hive session-close ID --file FILE\n'
              'Inspect:  hive browse; hive search TEXT; hive show ID; hive check\n'
              'Terminal: hive\n\n'
              'Vault: --root PATH > HIVEMIND_HOME > ~/.local/share/hivemind\n'
              'Add --json for machine-readable output. Use COMMAND --help for details.')
        return 0
    common = workflow_cli.ArgumentParser(add_help=False)
    common.add_argument('--root', type=Path)
    common.add_argument('--json', action='store_true')
    common.add_argument('--plain', action='store_true')
    global_args, rest = common.parse_known_args(argv)
    root = resolve_vault(global_args.root)
    command = rest[0] if rest else None
    try:
        if command in {'init', 'connect', 'disconnect', 'doctor', 'self-test'}:
            args = _parser().parse_args(rest)
            if command == 'init':
                args.json = global_args.json
                value = _initialize(args, root)
            elif command == 'doctor':
                value = diagnose(root, args.home, args.codex_home, args.claude_home)
            elif command == 'self-test':
                from . import selftest
                value = selftest.run(args.evidence_dir)
            else:
                from . import connections
                require_vault(root)
                value = connections.execute(args, root)
            workflow_cli.display(value, global_args.json)
            return 1 if command == 'self-test' and value['status'] != 'passed' else 0
        if '--help' not in rest and '-h' not in rest and command != 'contract':
            require_vault(root)
        if command in {'sync', 'start'} or '--sync' in rest or ('--background' in rest and 'start' in rest):
            raise ValueError('Automatic source sync and background installation are unavailable in the public preview; use session-draft and session-close.')
        options = ['--root', str(root)]
        if command in CORE_COMMANDS or (command == 'brief' and '--hook' in rest):
            return hive.main([*options, *rest])
        if global_args.json: options.append('--json')
        if global_args.plain: options.append('--plain')
        return launcher.main([*options, *rest])
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        if global_args.json:
            print(json.dumps(agent_contract.failure(exc), ensure_ascii=False), file=sys.stderr)
        else:
            print('HiveMind: ' + str(exc), file=sys.stderr)
        return 1

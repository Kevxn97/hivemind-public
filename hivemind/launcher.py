#!/usr/bin/env python3
"""HiveMind: Hintergrundwissen und Terminal. Kein HTTP-Server, kein Browser."""
import argparse
import datetime as dt
import json
from pathlib import Path
if __package__:
    from .runtime_paths import default_vault
else:
    from runtime_paths import default_vault
import shutil
import sys
import textwrap
import time

BASE = Path(__file__).resolve().parent


def knowledge(root):
    if __package__:
        from . import hive
    else:
        import hive
    result = hive.check(root)
    with hive.locked(root, shared=True):
        hive.require_clean(root)
        records = hive.load_notes(root)
    return result, {key: value[0] for key, value in records.items()}


def service_status(root):
    try:
        if __package__:
            from . import background
        else:
            import background
        return background.status(root)
    except ImportError:
        return {'installed': False, 'daemon_running': False, 'message': 'Hintergrunddienst noch nicht installiert.'}


def snapshot(root):
    if __package__:
        from .integrations import ingest
    else:
        from integrations import ingest
    if __package__:
        from . import jobs
    else:
        import jobs
    check, notes = knowledge(root)
    return {'knowledge': check, 'background': service_status(root), 'job': jobs.view(root),
            'sources': ingest.source_status(root),
            'active_notes': sum(not n.get('archived', False) and n['status'] != 'deprecated' for n in notes.values()),
            'pending_thoughts': sum(not item['processed'] for item in ingest.inbox(root))}


def plain_status(data):
    bg = data['background']
    running = bg.get('daemon_running', False)
    job = data['job']
    operational = ''
    if bg.get('status') == 'paused': operational = ' · Automatik pausiert'
    elif bg.get('status') == 'degraded': operational = ' · gestört'
    elif bg.get('backoff_until'):
        try:
            if dt.datetime.fromisoformat(bg['backoff_until'].replace('Z','+00:00')) > dt.datetime.now(dt.timezone.utc): operational = ' · Quellenpause'
        except (ValueError, TypeError): operational = ' · Pausenzeit prüfen'
    width = max(40, min(shutil.get_terminal_size((80, 24)).columns, 110))
    line = '─' * width
    return '\n'.join([line, '  HiveMind  /  DEIN WISSEN IM HINTERGRUND', line,
        f"  Wissen       {data['active_notes']} aktiv · {data['knowledge']['notes']} erhalten",
        f"  Eingang      {data['pending_thoughts']} offen",
        f"  Dienst       {'läuft' if running else 'gestoppt'}{operational}",
        '  Automatik    '+str(bg.get('message') or 'Stand wird geprüft.'),
        '  Abgleich     '+{'disabled':'ausgeschaltet','paused':'pausiert'}.get(job.get('status'),job.get('status','idle')), '',
        '  hive          Terminal öffnen', '  hive search   Wissen suchen',
        '  hive capture  Gedanken festhalten', '  hive sync     Quellen abgleichen',
        '  hive status   Hintergrund prüfen', line])


def display(value, json_mode=False):
    if json_mode:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)
    elif isinstance(value, dict) and 'knowledge' in value and 'background' in value:
        print(plain_status(value))
    elif isinstance(value, dict) and 'note' in value:
        n = value['note']
        print('\n'+n['title']+'\n'+'═'*min(80,len(n['title']))+'\n')
        print(n['body'])
        print('\nGRUNDLAGE / QUELLEN')
        for i, source in enumerate(n.get('sources', []), 1):
            print(f"  {i}. {source.get('title', source['id'])}\n     {source['resource']}")
    elif isinstance(value, list):
        if not value:
            print('Keine Einträge.')
        for item in value:
            print(f"  {item.get('id', item.get('target', ''))}  {item.get('title', item.get('relation', ''))}")
            if item.get('summary'):
                print(textwrap.indent(textwrap.fill(item['summary'], 78), '    '))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def neighbors(root, ident):
    _, notes = knowledge(root)
    if ident not in notes:
        raise ValueError('Unbekannte Notiz: '+ident)
    return [{'origin': origin, 'relation': edge['relation'], 'target': edge['target'],
             'id': edge['target'] if origin == ident else origin,
             'title': notes[edge['target'] if origin == ident else origin]['title']}
            for origin, note in notes.items() for edge in note['links']
            if origin == ident or edge['target'] == ident]


def open_terminal(root, initial_view='overview', plain=False):
    if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
        display(snapshot(root)); return 0
    if __package__:
        from . import terminal_ui
    else:
        import terminal_ui
    return terminal_ui.run(root, initial_view=initial_view) or 0


def main(argv=None):
    if 'doctor' in (sys.argv[1:] if argv is None else argv):
        sys.dont_write_bytecode = True
    if __package__:
        from . import workflow_cli
    else:
        import workflow_cli
    forwarded = workflow_cli.dispatch(sys.argv[1:] if argv is None else argv, default_vault())
    if forwarded is not None:
        return forwarded
    parser = argparse.ArgumentParser(description='HiveMind läuft im Hintergrund. Ohne Befehl öffnet sich das Terminal-Interface.',
        epilog='Arbeitsbefehle: project-register, project-unregister, projects, session-open, session-draft, session-close, session-status, why, impact, lessons, changes, feedback. Details: hive BEFEHL --help.')
    parser.add_argument('--root', type=Path, default=default_vault())
    parser.add_argument('--json', action='store_true', help='Maschinenlesbare Ausgabe')
    parser.add_argument('--plain', action='store_true', help='Lesbare Textausgabe statt Vollbild-Terminal')
    parser.add_argument('--no-open', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    for flag, help_text in [('status','Hintergrund und Quellen prüfen'),('sync','Quellen abgleichen'),('sources','Ausgewählte Quellen anzeigen'),('source-status','Quellenstände anzeigen'),('calendar','Mac-Kalender laden'),('check','Kanon prüfen'),('graph','Verbindungen im Terminal erkunden'),('stop','Hintergrunddienst stoppen'),('serve',argparse.SUPPRESS)]:
        mode.add_argument('--'+flag, action='store_true', help=help_text)
    mode.add_argument('--search', metavar='TEXT')
    mode.add_argument('--note', metavar='ID')
    mode.add_argument('--neighbors', metavar='ID')
    mode.add_argument('--brief', nargs='?', const='.', metavar='ORDNER', help='Projektkontext für einen Ordner oder projects/<id>')
    mode.add_argument('--read-source', metavar='ID')
    mode.add_argument('--capture', nargs='?', const='', metavar='TEXT')
    mode.add_argument('--background', choices=['start','stop','status'])
    parser.add_argument('--title', default='', help='Titel für einen Gedanken')
    parser.add_argument('--history', action='store_true', help='Archiv in Suche einbeziehen')
    parser.add_argument('--task', help='Aufgabe für die Auswahl des Projektkontexts')
    parser.add_argument('--budget', type=int, help='Gesamtbudget des Projektkontexts in Wörtern')
    parser.add_argument('--detach', action='store_true', help='Abgleich starten und sofort zurückkehren')
    parser.add_argument('command', nargs='?', choices=['status','sync','sources','capture','search','note','neighbors','brief','graph','check','start','stop','calendar'])
    parser.add_argument('words', nargs='*')
    args = parser.parse_args(argv); root = args.root.expanduser().resolve()
    if args.command:
        key = args.command.replace('-', '_')
        if args.command in {'search','note','neighbors','capture','brief'}:
            setattr(args, key, ' '.join(args.words))
        elif args.command == 'start': args.background = 'start'
        else: setattr(args, key, True)
    if args.title and args.capture is None: parser.error('--title erfordert capture')
    try:
        if __package__:
            from . import hive
        else:
            import hive
        if __package__:
            from .integrations import ingest
        else:
            from integrations import ingest
        if args.serve:
            print('HiveMind wird ausschließlich im Terminal und im Hintergrund betrieben. Kein Webserver. Nutze hive oder hive start.', file=sys.stderr); return 2
        if args.stop: args.background = 'stop'
        if args.background:
            if __package__:
                from . import background
            else:
                import background
            value = background.install(root) if args.background == 'start' else background.uninstall(root) if args.background == 'stop' else background.status(root)
            display(value,args.json); return 0
        if args.status or args.no_open:
            display(snapshot(root),args.json); return 0
        if args.check:
            display({'knowledge':hive.check(root),'sources':ingest.source_status(root)},args.json); return 0
        if args.search is not None:
            found = hive.search(root,args.search,limit=20,include_deprecated=args.history,include_archived=args.history)
            if not found: hive.record_miss(root,args.search)
            display(found,args.json); return 0
        if args.note:
            _, notes = knowledge(root)
            if args.note not in notes: raise ValueError('Unbekannte Notiz: '+args.note)
            display({'note':notes[args.note],'freshness':hive.freshness(notes[args.note])},args.json); return 0
        if args.neighbors:
            display(neighbors(root,args.neighbors),args.json); return 0
        if args.brief is not None:
            target = args.brief or '.'
            result = hive.brief(root,target,task=args.task,budget=args.budget)
            if result is None: raise ValueError('Kein eindeutiges HiveMind-Projekt für '+str(Path(target).expanduser().resolve()))
            if args.json: display(result,True)
            else: print(result['text'],end='')
            return 0
        if args.graph:
            if sys.stdin.isatty() and not args.plain and not args.json: return open_terminal(root,'network')
            _, notes = knowledge(root)
            ident = 'hivemind' if 'hivemind' in notes else next(iter(notes),None)
            display(neighbors(root,ident) if ident else [],args.json); return 0
        if args.sources:
            display(ingest.scan(root),args.json); return 0
        if args.source_status:
            display(ingest.source_status(root),args.json); return 0
        if args.read_source:
            display(ingest.read_source(root,args.read_source),args.json); return 0
        if args.capture is not None:
            text = args.capture
            if text == '' and sys.stdin.isatty(): return open_terminal(root,'inbox',args.plain)
            if text == '': text = sys.stdin.read(24001)
            if not text.strip(): raise ValueError('Ein Gedanke braucht Text.')
            display(ingest.capture(root,text,title=args.title),args.json); return 0
        if args.calendar:
            result=ingest.refresh_calendar(root);display(result,args.json)
            return 0 if result.get('status') == 'ok' else 1
        if args.sync:
            if __package__:
                from . import jobs
            else:
                import jobs
            value=jobs.start(root)
            if not args.detach:
                while value.get('status') == 'running':
                    time.sleep(.5);value=jobs.status(root)
            display(value,args.json);return 0 if value.get('status') in {'complete','running'} else 1
        if args.json: display(snapshot(root),True);return 0
        return open_terminal(root,plain=args.plain)
    except KeyboardInterrupt:
        print('\nTerminal geschlossen. Ein laufender Abgleich bleibt im Hintergrund prüfbar.',file=sys.stderr); return 130
    except (OSError,ValueError,KeyError,ImportError,RuntimeError) as error:
        print('HiveMind: '+str(error),file=sys.stderr); return 1


if __name__ == '__main__':
    raise SystemExit(main())

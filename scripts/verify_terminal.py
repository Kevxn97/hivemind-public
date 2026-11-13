#!/usr/bin/env python3
"""Real stdlib-curses PTY acceptance with synthetic journaled knowledge only.

No provider, native Calendar, HTTP server or Codex process is invoked. A separate
subprocess injects explicit doubles only for the two user-triggered actions.
Every invocation records the command, fixture, terminal dimensions, transcripts,
results and source hashes under output/terminal-e2e, including failed runs.
"""
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

BASE = Path(__file__).resolve().parents[1] / 'hivemind'
sys.path.insert(0, str(BASE))
import hive

STAMP = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
OUT = Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(BASE.parent.parent/'hivemind-public-evidence')))/'terminal-e2e'/STAMP
OUT.mkdir(parents=True, exist_ok=True)


def note(ident, **fields):
    value = {'id': ident, 'type': 'lesson', 'cluster': 'knowledge',
        'title': ident.replace('-', ' ').title(), 'summary': 'Synthetic terminal acceptance fixture.',
        'body': 'Synthetic body. No private content.\n\n'+('\n'.join('Paragraph %02d: synthetic content for scrolling.' % n for n in range(50))),
        'status': 'stable', 'evidence': 'confirmed', 'sensitivity': 'internal',
        'aliases': [], 'tags': [], 'sources': [{'id':'synthetic-source',
        'resource':'synthetic://source', 'title':'Synthetic original source'}],
        'verified': [], 'stale_after':'2100-01-01T00:00:00Z', 'links':[]}
    value.update(fields)
    return value


class Terminal:
    def __init__(self, root, name, code=None):
        self.master, slave = pty.openpty()
        self.sizes = []
        self.resize(80, 24, notify=False)
        env = dict(os.environ, TERM='xterm-256color', LANG='en_US.UTF-8', LC_CTYPE='UTF-8')
        args = [sys.executable, '-c', code, str(root)] if code else [sys.executable, str(BASE/'terminal_ui.py'), '--root', str(root)]
        self.proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave,
            cwd=BASE, env=env, start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(slave, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.data = bytearray()
        self.name = name

    def resize(self, cols, rows, notify=True):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        self.sizes.append({'columns':cols, 'rows':rows})
        if notify:
            os.kill(self.proc.pid, signal.SIGWINCH)

    def drain(self, duration=.2):
        end = time.monotonic()+duration
        while time.monotonic() < end:
            if select.select([self.master], [], [], max(0, end-time.monotonic()))[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.data.extend(data)
        return self.data.decode('utf-8', 'replace')

    def expect(self, text, timeout=5, since=0):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            raw=self.drain(.08)
            if text in raw[since:] or text in self.screen_text():
                return
            if self.proc.poll() is not None:
                break
        raise AssertionError('Terminal did not render '+repr(text)+'; tail='+repr(self.data.decode('utf-8','replace')[-1800:]))

    def send(self, text):
        self.drain(.05)
        mark = len(self.data.decode('utf-8','replace'))
        os.write(self.master, text.encode('utf-8'))
        return mark

    def screen_text(self):
        """Small VT screen reader for curses cursor updates, not raw substring QA."""
        cols=160;rows=60;grid=[[' ']*cols for _ in range(rows)];x=y=0
        raw=self.data.decode('utf-8','replace');i=0
        while i<len(raw):
            char=raw[i]
            if char=='\x1b':
                if i+1<len(raw) and raw[i+1]=='[':
                    end=i+2
                    while end<len(raw) and not ('@'<=raw[end]<='~'):end+=1
                    if end>=len(raw):break
                    cmd=raw[end];params=raw[i+2:end];private=params.startswith('?')
                    try:args=[int(v) if v else 0 for v in params.lstrip('?').split(';')]
                    except ValueError:args=[]
                    arg=(args[0] or 1) if args else 1
                    if not private:
                        if cmd in ('H','f'):y=max(0,(args[0] or 1)-1) if args else 0;x=max(0,(args[1] or 1)-1) if len(args)>1 else 0
                        elif cmd=='G':x=arg-1
                        elif cmd=='d':y=arg-1
                        elif cmd=='A':y-=arg
                        elif cmd=='B':y+=arg
                        elif cmd=='C':x+=arg
                        elif cmd=='D':x-=arg
                        elif cmd=='J' and args and args[0] in (2,3):grid=[[' ']*cols for _ in range(rows)]
                        elif cmd=='K':
                            first,last=(0,cols) if args and args[0]==2 else ((0,x+1) if args and args[0]==1 else (x,cols))
                            grid[max(0,min(rows-1,y))][max(0,first):max(0,last)]=[' ']*max(0,last-first)
                        elif cmd=='X':
                            yy=max(0,min(rows-1,y));first=max(0,min(cols,x));last=min(cols,first+arg);grid[yy][first:last]=[' ']*(last-first)
                    i=end+1;continue
                if i+1<len(raw) and raw[i+1] in '()':i+=3;continue
                i+=2;continue
            if char=='\r':x=0
            elif char=='\n':y+=1
            elif char=='\b':x=max(0,x-1)
            elif ord(char)>=32:
                if 0<=y<rows and 0<=x<cols:grid[y][x]=char
                x+=1
            x=max(0,min(cols-1,x));y=max(0,min(rows-1,y));i+=1
        return '\n'.join(''.join(row).rstrip() for row in grid)

    def wait(self,timeout=3):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.drain(.05)
            if self.proc.poll() is not None:return self.proc.returncode
        raise subprocess.TimeoutExpired(self.proc.args,timeout)

    def close(self):
        if self.proc.poll() is None:
            try:self.send('\x1bq')
            except OSError:pass
            try:self.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=3)
        self.drain(.05)
        text = self.data.decode('utf-8','replace')
        (OUT/(self.name+'.ansi.txt')).write_text(text)
        clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][AB012]|\x1b[=>78]|\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
        (OUT/(self.name+'.text.txt')).write_text(clean)
        (OUT/(self.name+'.screen.txt')).write_text(self.screen_text())
        (OUT/(self.name+'.json')).write_text(json.dumps({'dimensions':self.sizes,'exit_code':self.proc.returncode},indent=2))
        os.close(self.master)
        return self.proc.returncode


class TerminalE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hivemind-terminal-fixture-')
        self.root = Path(self.tmp.name)
        self.sessions = []
        values = [note('alpha-note', title='Einzigartige Erkenntnis', links=[{'target':'beta-note','relation':'depends_on','reason':'Synthetic dependency'}]),
            note('beta-note', title='Verbundenes Wissen', evidence='reported'),
            note('archived-note', archived=True), note('deprecated-note', status='deprecated')]
        hive.apply(self.root, {'changes':[{'expected_revision':'new','note':n} for n in values]}, 'terminal-e2e','Synthetic terminal fixture','synthetic://test')
        config = self.root/'config';config.mkdir()
        (self.root/'selected.txt').write_text('Selected synthetic file only.\n')
        (config/'sources.json').write_text(json.dumps({'version':1,'files':[{'id':'selected','path':'selected.txt','title':'Selected source'}],'remote':[]}))
        self.before = {str(p.relative_to(self.root)):p.read_bytes() for parent in ('wiki','.history') for p in (self.root/parent).rglob('*') if p.is_file()}

    def tearDown(self):
        for session in self.sessions:
            session.close()
        self.tmp.cleanup()

    def terminal(self, suffix, code=None):
        session = Terminal(self.root, self.id().split('.')[-1]+'-'+suffix, code)
        self.sessions.append(session)
        return session

    def test_background_paused_degraded_and_backoff_are_not_reported_as_ready(self):
        for state,expected in [({'status':'paused','message':'Automatik ausgeschaltet'},'pausiert'),({'status':'degraded','message':'Konfiguration beschädigt'},'gestört'),({'status':'running','message':'Quellenpause nach Fehler','backoff_until':'2100-01-01T00:00:00Z'},'Quellenpause')]:
            code="import sys;sys.path.insert(0,sys.argv[1]);import terminal_ui,background;background.status=lambda root:"+repr({'daemon_running':True,**state})+";print(terminal_ui.render_plain(__import__('pathlib').Path(sys.argv[2]),width=120))"
            result=subprocess.run([sys.executable,'-c',code,str(BASE),str(self.root)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn(expected,result.stdout)

    def test_non_tty_is_a_plain_local_snapshot_and_never_launches_a_server(self):
        result = subprocess.run([sys.executable,str(BASE/'terminal_ui.py'),'--root',str(self.root)],capture_output=True,text=True,timeout=5)
        (OUT/'plain-snapshot.txt').write_text(result.stdout+result.stderr)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('HIVE',result.stdout);self.assertIn('2 Notizen',result.stdout)
        self.assertIn('Kein interaktives Terminal',result.stdout)
        self.assertNotIn('\x1b',result.stdout)
        self.assertFalse((self.root/'.state/launcher.json').exists())
        self.assertFalse((self.root/'.state/job.json').exists())

    def test_real_pty_search_detail_sources_network_resize_and_quit(self):
        session=self.terminal('navigation')
        session.expect('ÜBERSICHT')
        session.send('2');session.expect('WISSEN')
        session.send('/');session.expect('SUCHE')
        session.send('Einzigartig\r');session.expect('Einzigartige Erkenntnis')
        session.send('\r');session.expect('Synthetic body')
        session.send('o');session.expect('synthetic://source')
        session.send('\x1b');session.drain(.2)
        session.send('\x1b');session.drain(.2)
        session.send('3');session.expect('NETZ')
        session.send('\r');session.expect('Synthetic dependency')
        mark=session.send('z');session.expect('ZEITRAFFER',since=mark)
        session.send('z');session.send('+');session.send(' ');session.send(' ');session.drain(.2)
        session.send('\r');session.send('\r');session.send('\x7f');session.drain(.3)
        mark=session.send('f');session.expect('Übersicht',since=mark)
        session.send('f');session.drain(.2)
        session.resize(120,36);session.drain(.3)
        session.resize(80,24);session.drain(.3)
        session.resize(58,17);session.drain(.3)
        session.resize(80,24);session.drain(.3)
        session.send('q');session.wait(timeout=3)
        self.assertEqual(session.proc.returncode,0)
        self.assertNotIn('Traceback',session.data.decode('utf-8','replace'))
        self.assertEqual(self.before,{str(p.relative_to(self.root)):p.read_bytes() for parent in ('wiki','.history') for p in (self.root/parent).rglob('*') if p.is_file()})

    def test_capture_multiline_unicode_is_pending_and_canonical_is_unchanged(self):
        session=self.terminal('capture')
        session.expect('ÜBERSICHT')
        session.send('n');session.expect('GEDANKE FESTHALTEN')
        session.send('Terminal-Gedanke\rGröße und Ruhe.\nZweite Zeile.\x07')
        session.expect('Gedanke gespeichert')
        session.send('4');session.expect('Terminal-Gedanke')
        session.send('\r');session.expect('Zweite Zeile.')
        session.send('q');session.wait(timeout=3)
        files=list((self.root/'INBOX').glob('*.md'))
        self.assertEqual(len(files),1)
        self.assertEqual(files[0].read_text(),'# Terminal-Gedanke\n\nGröße und Ruhe.\nZweite Zeile.\n')
        self.assertEqual(files[0].stat().st_mode & 0o777,0o600)
        self.assertEqual(self.before,{str(p.relative_to(self.root)):p.read_bytes() for parent in ('wiki','.history') for p in (self.root/parent).rglob('*') if p.is_file()})
        self.assertTrue(hive.check(self.root)['valid'])

    def test_decision_views_changes_and_explicit_search_feedback(self):
        project=note('demo-project',type='project',cluster='projects/demo-project',title='Projektansicht',
                     body='# Projekt\n\n## Offene Fragen\n\nWelche Quelle erklärt die Abhängigkeit?',
                     links=[{'target':'alpha-note','relation':'related','reason':'Recorded reusable lesson'}])
        hive.apply(self.root,{'changes':[{'expected_revision':'new','note':project}]},
                   'terminal-e2e','Synthetic decision workspace','synthetic://test')
        before={str(p.relative_to(self.root)):p.read_bytes() for parent in ('wiki','.history') for p in (self.root/parent).rglob('*') if p.is_file()}
        session=self.terminal('decision-workspace')
        session.expect('ÜBERSICHT')
        session.send('2/Einzigartig\r');session.expect('Einzigartige Erkenntnis')
        session.send('w');session.expect('WARUM /')
        session.send('\x1b');session.drain(.2)
        session.send('u');session.expect('Suchfeedback gespeichert')
        feedback=[json.loads(line) for line in (self.root/'.state/search-feedback.jsonl').read_text().splitlines()]
        self.assertEqual(len(feedback),1)
        self.assertEqual(feedback[0]['query'],'Einzigartig')
        self.assertFalse(feedback[0]['helpful'])
        session.send('\x1b');session.drain(.2)
        session.send('/Verbundenes\r');session.expect('Suche: Verbundenes')
        session.send('a');session.expect('AUSWIRKUNG /')
        session.expect('Einzigartige Erkenntnis')
        session.send('\x1b');session.drain(.2)
        session.send('\x1b');session.drain(.2)
        session.send('/Projektansicht\r');session.expect('Suche: Projektansicht')
        session.send('e');session.expect('ERFAHRUNGEN /')
        session.expect('Einzigartige Erkenntnis')
        session.send('\x1b');session.drain(.2)
        session.send('D');session.expect('ÄNDERUNGEN /')
        session.send('\x1b');session.send('q');session.wait(timeout=3)
        self.assertEqual(session.proc.returncode,0)
        self.assertNotIn('Traceback',session.data.decode('utf-8','replace'))
        self.assertEqual(before,{str(p.relative_to(self.root)):p.read_bytes() for parent in ('wiki','.history') for p in (self.root/parent).rglob('*') if p.is_file()})

    def test_cancelled_capture_and_empty_search_do_not_change_knowledge(self):
        session=self.terminal('cancel')
        session.expect('ÜBERSICHT');session.send('n');session.expect('GEDANKE FESTHALTEN')
        session.send('Verwerfen\rNichts speichern.\x1b');session.drain(.4)
        session.send('2/kein-passender-begriff\r');session.expect('Keine Treffer')
        session.send('q');session.wait(timeout=3)
        self.assertFalse(list((self.root/'INBOX').glob('*.md')))

    def test_project_work_states_are_visible_in_overview_and_details(self):
        import sessions
        value=note('work-project',type='project',cluster='projects/work-project',title='Projektarbeit',body='# Projektarbeit\n\nSynthetischer Arbeitsablauf.')
        hive.apply(self.root,{'changes':[{'expected_revision':'new','note':value}]},'test','Synthetic work','test:work')
        opened=sessions.open_session(self.root,'projects/work-project',agent='test-agent')
        payload=sessions.draft_handoff(self.root,opened['session_id'])
        payload['source'].update(resource='synthetic://work',title='Arbeitsquelle',observed_at='2026-10-30T10:00:00Z')
        payload['step_updates']=[{'text':'Zebrapruefung','status':'agreed','state':'open'}]
        sessions.close_session(self.root,opened['session_id'],payload)
        session=self.terminal('work-states')
        session.expect('Offene Arbeit')
        session.expect('Zebrapruefung')
        for cols,rows in ((120,36),(58,17),(80,24)):
            session.resize(cols,rows);session.drain(.3)
            self.assertIn('Offene Arbeit',session.screen_text())
        session.send('2/Projektarbeit\r');session.send('\r')
        session.expect('Zebrapruefung');session.expect('vereinbart')
        session.send('\x1bq');session.wait(timeout=3)
        self.assertEqual(session.proc.returncode,0)

    def test_sync_and_calendar_are_only_started_by_explicit_keys_with_doubles(self):
        code = """import pathlib,sys;import terminal_ui
root=pathlib.Path(sys.argv[1])
def sync(root):
 (root/'sync-called.txt').write_text('explicit')
 return {'status':'running','message':'Synthetic sync started'}
def calendar(root):
 (root/'calendar-called.txt').write_text('explicit')
 return {'status':'ok','events':[],'exported_at':'2100-01-01T00:00:00Z'}
terminal_ui.jobs.start=sync
terminal_ui.ingest.refresh_calendar=calendar
raise SystemExit(terminal_ui.run(root))
"""
        session=self.terminal('explicit-actions',code)
        session.expect('ÜBERSICHT')
        self.assertFalse((self.root/'sync-called.txt').exists());self.assertFalse((self.root/'calendar-called.txt').exists())
        session.send('s');session.expect('läuft im Hintergrund weiter.')
        self.assertTrue((self.root/'sync-called.txt').exists())
        session.send('r');session.expect('Kalender aktualisiert',timeout=8)
        self.assertTrue((self.root/'calendar-called.txt').exists())
        session.send('q');session.wait(timeout=3)

    def test_corrupt_canonical_state_is_reported_as_error_not_empty_success(self):
        record=hive.load_notes(self.root)['alpha-note']
        record[2].write_text(record[1]+'\nUnjournaled corruption.\n')
        result=subprocess.run([sys.executable,str(BASE/'terminal_ui.py'),'--root',str(self.root)],capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Wissensprüfung fehlgeschlagen',result.stdout)
        self.assertNotIn('2 Notizen',result.stdout)


if __name__=='__main__':
    stream=io.StringIO()
    suite=(unittest.TestSuite([TerminalE2E(sys.argv[1])]) if len(sys.argv)>1 else unittest.defaultTestLoader.loadTestsFromTestCase(TerminalE2E))
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    log=stream.getvalue();(OUT/'test.log').write_text(log)
    hashes={str(p.relative_to(BASE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [BASE/'terminal_ui.py',Path(__file__),BASE/'hive.py',BASE/'jobs.py',BASE/'integrations/ingest.py'] if p.exists()}
    evidence={'started_at':STAMP,'command':'python3 '+' '.join(sys.argv),'fixture':'synthetic journaled notes and project work, one selected local text source; no private data','environment':{'python':sys.version,'platform':sys.platform,'terminal':'xterm-256color','dimensions':['80x24','120x36','58x17']},'scope':'Real curses through POSIX PTYs. Sync and Calendar explicit-action adapters are doubles; no HTTP server, browser, model or provider call.','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'success':result.wasSuccessful(),'source_sha256':hashes,'artifacts':[p.name for p in OUT.iterdir()]}
    (OUT/'evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(log,end='');print('Evidence:',OUT/'evidence.json')
    raise SystemExit(0 if result.wasSuccessful() else 1)

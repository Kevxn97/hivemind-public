"""Automation controls rendered inside HiveMind's existing curses design."""
import curses
import shutil

from . import automation, feature_controls as controls, maintenance, project_context
from .workflow_cli import ArgumentParser


class Cancelled(Exception):
    pass


class MenuParser(ArgumentParser):
    """Keep invalid form values inside the TUI instead of exiting the process."""
    def error(self, message):
        raise ValueError('Ungültige Eingabe: '+message)


class Panel:
    def __init__(self, interface):
        self.app=interface
        self.root=interface.root
        self.screen=interface.screen
        self.app.clear()

    def frame(self, title='Automatik', footer=None):
        height,width=self.screen.getmaxyx()
        self.screen.erase()
        self.app.write(1,3,[('HIVE','accent'),('MIND','ivory'),('  /  '+title,'soft')])
        path=str(self.root)
        if len(path)>width-8:path='…'+path[-(width-9):]
        self.app.put(2,3,path,self.app.tone['mute'])
        self.app.rule(3)
        self.app.rule(height-4)
        self.app.write(height-2,3,footer or [('Esc','accent'),(' Zurück','mute')])
        return height,width

    def key(self):
        try:return self.screen.get_wch()
        except curses.error:return None

    def text(self, lines, start=5, offset=0):
        from .terminal_ui import wrap
        height,width=self.screen.getmaxyx()
        rows=[part for line in lines for part in wrap(str(line),width-8)]
        for y,line in enumerate(rows[offset:offset+max(1,height-start-5)],start):
            self.app.put(y,3,line,self.app.tone['soft'])
        return rows

    def message(self,title,lines):
        offset=0
        while True:
            height,width=self.frame(title,[('Enter','accent'),(' Zurück  ','mute'),('↑↓','accent'),(' Lesen','mute')])
            rows=self.text(lines,offset=offset);self.screen.refresh()
            key=self.key()
            if key in ('\n','\r',curses.KEY_ENTER,'\x1b','\x03','\x04'):return
            if key in (curses.KEY_DOWN,'j'):offset=min(max(0,len(rows)-1),offset+1)
            elif key in (curses.KEY_UP,'k'):offset=max(0,offset-1)

    def field(self,label,default='',hint=''):
        value=str(default);replace=True
        while True:
            height,width=self.frame('Automatik · Eingabe',[
                ('Enter','accent'),(' Weiter  ','mute'),('Esc','accent'),(' Abbrechen  ','mute'),('Ctrl+U','accent'),(' Leeren','mute')])
            self.app.put(5,3,label,self.app.tone['shade'])
            self.text([hint] if hint else [],start=7)
            display=value if len(value)<width-9 else '…'+value[-(width-11):]
            self.app.write(min(height-7,11),3,[('› ','accent'),(display,'ivory'),('▏','accent')])
            self.screen.refresh();key=self.key()
            if key in ('\x1b','\x03','\x04'):raise Cancelled()
            if key in ('\n','\r',curses.KEY_ENTER):return value.strip()
            if key=='\x15':value='';replace=False
            elif key in (curses.KEY_BACKSPACE,'\x7f','\b'):value=value[:-1];replace=False
            elif isinstance(key,str) and key.isprintable() and len(value)<1000:
                value=key if replace else value+key;replace=False

    def confirm(self,lines):
        offset=0
        while True:
            self.frame('Automatik · Bestätigen',[
                ('y','accent'),(' Übernehmen  ','mute'),('n / Esc','accent'),(' Abbrechen  ','mute'),('↑↓','accent'),(' Lesen','mute')])
            rows=self.text(lines,offset=offset);self.screen.refresh();key=self.key()
            if key in ('y','Y'):return True
            if key in ('n','N','\x1b','\x03','\x04'):return False
            if key in (curses.KEY_DOWN,'j'):offset=min(max(0,len(rows)-1),offset+1)
            elif key in (curses.KEY_UP,'k'):offset=max(0,offset-1)

    def perform(self,words):
        parser=MenuParser()
        sub=parser.add_subparsers(dest='command',required=True);controls.configure(sub)
        args=parser.parse_args(['automation',*words]);args.json=True
        plan=controls.execute(args,self.root)
        if not self.confirm(plan['text'].splitlines()):return
        args.apply=True;args.preview=plan['preview']
        self.frame('Automatik · Wird angewendet');self.text(['Änderung wird angewendet …']);self.screen.refresh()
        result=controls.execute(args,self.root)
        self.message('Automatik · Gespeichert',result.get('text',result.get('message',result['status'])).splitlines())

    def project(self):
        projects=project_context.list_projects(self.root)
        available=[p for p in projects if p['active']]
        if len(available)==1:return available[0]['cluster']
        return self.field('Projektname oder projects/ID',hint='Verfügbare Projekte: '+', '.join(p['cluster'] for p in available))

    def sources(self):
        while True:
            rows=controls.status(self.root)['sources']
            height,width=self.frame('Automatik · Quellen',[
                ('a','accent'),(' Datei  ','mute'),('r','accent'),(' Remote  ','mute'),('e','accent'),(' Ein  ','mute'),
                ('d','accent'),(' Aus  ','mute'),('x','accent'),(' Entfernen  ','mute'),('Esc','accent'),(' Zurück','mute')])
            self.app.put(5,3,'Bewusst ausgewählte Quellen',self.app.tone['shade'])
            for y,row in enumerate(rows[:max(1,height-12)],7):
                self.app.write(y,3,[('● ' if row['enabled'] else '○ ','accent' if row['enabled'] else 'mute'),
                    (row['id'],'ivory'),('  '+row.get('path',row.get('scope','')),'mute')])
            if not rows:self.text(['Noch keine Quelle ausgewählt.','a wählt eine Textdatei, r eine Quelle aus deinem Agent-Profil.'],start=7)
            self.screen.refresh();key=self.key()
            if key in ('\x1b','0','q','\x04','\x03'):return
            if key in ('a','r'):
                project=self.project()
                if key=='a':
                    path=self.field('Textdatei auswählen',hint='Nur diese einzelne Datei wird gelesen. Keine Ordnerimporte.')
                    ident=self.field('Name für diese Quelle','notizen')
                    words=['source','add',ident,'--project',project,'--path',path]
                    heading=self.field('Optionaler Markdown-Abschnitt',hint='Leerlassen für die ganze Datei. Sonst die Überschrift inklusive # eingeben.')
                    if heading:words+=['--heading',heading]
                else:
                    kind=self.field('Quelle — gmail, calendar, wispr oder mcp','gmail',
                        'Die Verbindung muss im ausgewählten Codex-Profil angemeldet und verfügbar sein.').lower()
                    ident=self.field('Name für diese Quelle',kind)
                    scope=self.field('Welche Inhalte darf HiveMind lesen?',hint='Zum Beispiel nur Nachrichten zum ausgewählten Pilotprojekt.')
                    days=self.field('Erster Zeitraum in Tagen (1–31)','7')
                    words=['source','add',ident,'--project',project,'--remote',kind,'--scope',scope,'--lookback-days',days]
                self.perform(words)
            elif key in ('e','d','x'):
                ident=self.field('Name der Quelle',rows[0]['id'] if len(rows)==1 else '',', '.join(r['id'] for r in rows))
                self.perform(['source',{'e':'enable','d':'disable','x':'remove'}[key],ident])

    def run(self):
        while True:
            try:
                state=controls.status(self.root);prefs=controls.settings(self.root)
                height,width=self.frame('Automatik',[
                    ('1–9','accent'),(' Auswählen  ','mute'),('p','accent'),(' Pause / Weiter  ','mute'),('Esc','accent'),(' Zurück','mute')])
                if height<24 or width<64:
                    self.text(['Für die Einstellungen bitte mindestens 64 × 24 Zeichen.','Vergrößere das Fenster oder gehe mit Esc zurück.'])
                    self.screen.refresh()
                    if self.key() in ('\x1b','q','0','\x04','\x03'):return
                    continue
                self.app.write(5,3,[('Eingehende Updates ','mute'),('pausiert' if state['paused'] else 'aktiv','shade' if state['paused'] else 'ok'),
                    ('  ·  '+str(sum(r['enabled'] for r in state['sources']))+' Quellen','mute')])
                rows=[('1','Quellen verwalten',None),('2','Codex-Profil wählen',None),
                    ('3','Quellenabgleich','sync'),('4','Hintergrunddienst','background'),('5','Wöchentliche Wissenspflege','maintenance'),
                    ('6','Zeitplan ändern',None),('7','Quellen jetzt abgleichen',None),
                    ('8','Wissen jetzt prüfen',None),('9','Letzte Prüfliste öffnen',None)]
                for y,(key,label,feature) in enumerate(rows,7):
                    self.app.write(y,3,[(key,'accent'),('  '+label,'ivory')])
                    if feature:
                        enabled=state[feature]['enabled']
                        self.app.write(y,min(width-13,45),[('● EIN' if enabled else '○ AUS','accent' if enabled else 'mute')])
                self.app.put(18,3,'Jede Änderung wird vor dem Speichern bestätigt.',self.app.tone['mute'])
                if width>=110:
                    self.app.section(7,64,'Dein Zeitplan')
                    self.app.put(9,64,f"Quellen täglich {state['schedule']['daily_hour']:02d}:00",self.app.tone['soft'])
                    days=('Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag')
                    self.app.put(10,64,days[prefs['maintenance_day']]+f" {prefs['maintenance_hour']:02d}:00 · Pflege",self.app.tone['soft'])
                    self.app.put(12,64,'Lokale Änderungen nach 2 ruhigen Minuten.',self.app.tone['mute'])
                    self.app.put(13,64,'Mindestens 30 Minuten zwischen Abgleichen.',self.app.tone['mute'])
                    self.app.put(15,64,'Eigener Dienst für diesen Wissensspeicher.',self.app.tone['mute'])
                self.screen.refresh();key=self.key()
                if key in ('\x1b','q','0','\x03','\x04','b'):return
                if key=='1':self.sources()
                elif key=='2':
                    binary=self.field('Codex-Programm',prefs['codex'] or shutil.which('codex') or '')
                    profile=self.field('Codex-Profilordner',prefs['agent_home'] or '',
                        'Modell, Anmeldung und Berechtigungen bleiben in diesem Profil. Für Tests ein getrenntes Profil wählen.')
                    self.perform(['provider','--codex',binary,'--agent-home',profile])
                elif key in ('3','4','5'):
                    feature={'3':'sync','4':'background','5':'maintenance'}[key]
                    self.perform(['disable' if state[feature]['enabled'] else 'enable',feature])
                elif key=='6':
                    hour=self.field('Täglicher Quellenabgleich · Stunde 0–23',str(state['schedule']['daily_hour']))
                    day=self.field('Wissenspflege · Wochentag 0–6',str(prefs['maintenance_day']),'0 Montag · 1 Dienstag · 2 Mittwoch · 3 Donnerstag · 4 Freitag · 5 Samstag · 6 Sonntag')
                    review_hour=self.field('Wissenspflege · Stunde 0–23',str(prefs['maintenance_hour']))
                    self.perform(['schedule','--daily-hour',hour,'--maintenance-day',day,'--maintenance-hour',review_hour])
                elif key in ('7','8'):self.perform(['run','sync' if key=='7' else 'maintenance'])
                elif key=='9':
                    report=maintenance.read(self.root)
                    lines=['Noch keine Prüfliste vorhanden.'] if report is None else [
                        str(len(report['review']['items']))+' Einträge · '+report['finished_at'],'']+[
                            row['title']+' — '+', '.join(r['kind'] for r in row['reasons'])+'\n'+row['ref'] for row in report['review']['items']]
                    self.message('Automatik · Prüfliste',lines)
                elif key=='p':
                    current=automation.status(self.root)
                    reason=self.field('Grund für die Änderung','Wieder aktivieren' if current['paused'] else 'Eingehende Updates prüfen')
                    if self.confirm(['Eingehende Updates '+('fortsetzen' if current['paused'] else 'pausieren')+'?',reason,
                                     'Bereits laufende Abgleiche können ihren Lauf beenden.']):
                        automation.set_paused(self.root,not current['paused'],actor='hivemind-user',reason=reason,expected_revision=current['revision'])
            except Cancelled:
                pass
            except (OSError,ValueError,RuntimeError) as error:
                self.message('Automatik · Hinweis',[str(error)])

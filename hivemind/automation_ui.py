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
        raise ValueError('Invalid input: '+message)


class Panel:
    def __init__(self, interface):
        self.app=interface
        self.root=interface.root
        self.screen=interface.screen
        self.app.clear()

    def frame(self, title='Automation', footer=None):
        height,width=self.screen.getmaxyx()
        self.screen.erase()
        self.app.write(1,3,[('HIVE','accent'),('MIND','ivory'),('  /  '+title,'soft')])
        path=str(self.root)
        if len(path)>width-8:path='…'+path[-(width-9):]
        self.app.put(2,3,path,self.app.tone['mute'])
        self.app.rule(3)
        self.app.rule(height-4)
        self.app.write(height-2,3,footer or [('Esc','accent'),(' Back','mute')])
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
            height,width=self.frame(title,[('Enter','accent'),(' Back  ','mute'),('↑↓','accent'),(' Read','mute')])
            rows=self.text(lines,offset=offset);self.screen.refresh()
            key=self.key()
            if key in ('\n','\r',curses.KEY_ENTER,'\x1b','\x03','\x04'):return
            if key in (curses.KEY_DOWN,'j'):offset=min(max(0,len(rows)-1),offset+1)
            elif key in (curses.KEY_UP,'k'):offset=max(0,offset-1)

    def field(self,label,default='',hint=''):
        value=str(default);replace=True
        while True:
            height,width=self.frame('Automation · Input',[
                ('Enter','accent'),(' Continue  ','mute'),('Esc','accent'),(' Cancel  ','mute'),('Ctrl+U','accent'),(' Clear','mute')])
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
            self.frame('Automation · Confirm',[
                ('y','accent'),(' Apply  ','mute'),('n / Esc','accent'),(' Cancel  ','mute'),('↑↓','accent'),(' Read','mute')])
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
        self.frame('Automation · Applying');self.text(['Applying change …']);self.screen.refresh()
        result=controls.execute(args,self.root)
        self.message('Automation · Saved',result.get('text',result.get('message',result['status'])).splitlines())

    def project(self):
        projects=project_context.list_projects(self.root)
        available=[p for p in projects if p['active']]
        if len(available)==1:return available[0]['cluster']
        return self.field('Project name or projects/ID',hint='Available projects: '+', '.join(p['cluster'] for p in available))

    def sources(self):
        while True:
            rows=controls.status(self.root)['sources']
            height,width=self.frame('Automation · Sources',[
                ('a','accent'),(' File  ','mute'),('r','accent'),(' Remote  ','mute'),('e','accent'),(' Enable  ','mute'),
                ('d','accent'),(' Disable  ','mute'),('x','accent'),(' Remove  ','mute'),('Esc','accent'),(' Back','mute')])
            self.app.put(5,3,'Selected sources',self.app.tone['shade'])
            for y,row in enumerate(rows[:max(1,height-12)],7):
                self.app.write(y,3,[('● ' if row['enabled'] else '○ ','accent' if row['enabled'] else 'mute'),
                    (row['id'],'ivory'),('  '+row.get('path',row.get('scope','')),'mute')])
            if not rows:self.text(['No sources selected yet.','a selects a text file; r selects a source from your agent profile.'],start=7)
            self.screen.refresh();key=self.key()
            if key in ('\x1b','0','q','\x04','\x03'):return
            if key in ('a','r'):
                project=self.project()
                if key=='a':
                    path=self.field('Select a text file',hint='Only this file will be read. No folder imports.')
                    ident=self.field('Name this source','notes')
                    words=['source','add',ident,'--project',project,'--path',path]
                    heading=self.field('Optional Markdown section',hint='Leave blank for the whole file, or enter the heading including #.')
                    if heading:words+=['--heading',heading]
                else:
                    kind=self.field('Source — gmail, calendar, wispr or mcp','gmail',
                        'The connection must be signed in and available in the selected Codex profile.').lower()
                    ident=self.field('Name this source',kind)
                    scope=self.field('What can HiveMind read?',hint='For example, only messages about the selected pilot project.')
                    days=self.field('Initial lookback in days (1–31)','7')
                    words=['source','add',ident,'--project',project,'--remote',kind,'--scope',scope,'--lookback-days',days]
                self.perform(words)
            elif key in ('e','d','x'):
                ident=self.field('Source name',rows[0]['id'] if len(rows)==1 else '',', '.join(r['id'] for r in rows))
                self.perform(['source',{'e':'enable','d':'disable','x':'remove'}[key],ident])

    def run(self):
        while True:
            try:
                state=controls.status(self.root);prefs=controls.settings(self.root)
                height,width=self.frame('Automation',[
                    ('1–9','accent'),(' Select  ','mute'),('p','accent'),(' Pause / Resume  ','mute'),('Esc','accent'),(' Back','mute')])
                if height<24 or width<64:
                    self.text(['Settings need at least 64 × 24 characters.','Enlarge the window or press Esc to go back.'])
                    self.screen.refresh()
                    if self.key() in ('\x1b','q','0','\x04','\x03'):return
                    continue
                self.app.write(5,3,[('Incoming updates ','mute'),('paused' if state['paused'] else 'active','shade' if state['paused'] else 'ok'),
                    ('  ·  '+str(sum(r['enabled'] for r in state['sources']))+' sources','mute')])
                rows=[('1','Manage sources',None),('2','Select Codex profile',None),
                    ('3','Source sync','sync'),('4','Background service','background'),('5','Weekly knowledge review','maintenance'),
                    ('6','Change schedule',None),('7','Sync sources now',None),
                    ('8','Review knowledge now',None),('9','Open latest review list',None)]
                for y,(key,label,feature) in enumerate(rows,7):
                    self.app.write(y,3,[(key,'accent'),('  '+label,'ivory')])
                    if feature:
                        enabled=state[feature]['enabled']
                        self.app.write(y,min(width-13,45),[('● ON' if enabled else '○ OFF','accent' if enabled else 'mute')])
                self.app.put(18,3,'Every change is reviewed before saving.',self.app.tone['mute'])
                if width>=110:
                    self.app.section(7,64,'Your schedule')
                    self.app.put(9,64,f"Sources daily {state['schedule']['daily_hour']:02d}:00",self.app.tone['soft'])
                    days=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
                    self.app.put(10,64,days[prefs['maintenance_day']]+f" {prefs['maintenance_hour']:02d}:00 · review",self.app.tone['soft'])
                    self.app.put(12,64,'Local changes after 2 quiet minutes.',self.app.tone['mute'])
                    self.app.put(13,64,'At least 30 minutes between syncs.',self.app.tone['mute'])
                    self.app.put(15,64,'A separate service for this vault.',self.app.tone['mute'])
                self.screen.refresh();key=self.key()
                if key in ('\x1b','q','0','\x03','\x04','b'):return
                if key=='1':self.sources()
                elif key=='2':
                    binary=self.field('Codex executable',prefs['codex'] or shutil.which('codex') or '')
                    profile=self.field('Codex profile directory',prefs['agent_home'] or '',
                        'This profile keeps its model, login, and permissions. Use a separate profile for testing.')
                    self.perform(['provider','--codex',binary,'--agent-home',profile])
                elif key in ('3','4','5'):
                    feature={'3':'sync','4':'background','5':'maintenance'}[key]
                    self.perform(['disable' if state[feature]['enabled'] else 'enable',feature])
                elif key=='6':
                    hour=self.field('Daily source sync · hour 0–23',str(state['schedule']['daily_hour']))
                    day=self.field('Knowledge review · Weekday 0–6',str(prefs['maintenance_day']),'0 Monday · 1 Tuesday · 2 Wednesday · 3 Thursday · 4 Friday · 5 Saturday · 6 Sunday')
                    review_hour=self.field('Knowledge review · hour 0–23',str(prefs['maintenance_hour']))
                    self.perform(['schedule','--daily-hour',hour,'--maintenance-day',day,'--maintenance-hour',review_hour])
                elif key in ('7','8'):self.perform(['run','sync' if key=='7' else 'maintenance'])
                elif key=='9':
                    report=maintenance.read(self.root)
                    lines=['No review list yet.'] if report is None else [
                        str(len(report['review']['items']))+' entries · '+report['finished_at'],'']+[
                            row['title']+' — '+', '.join(r['kind'] for r in row['reasons'])+'\n'+row['ref'] for row in report['review']['items']]
                    self.message('Automation · Review list',lines)
                elif key=='p':
                    current=automation.status(self.root)
                    reason=self.field('Reason for the change','Resume incoming updates' if current['paused'] else 'Review incoming updates')
                    if self.confirm(['Incoming updates '+('resume' if current['paused'] else 'pause')+'?',reason,
                                     'Syncs already running can finish.']):
                        automation.set_paused(self.root,not current['paused'],actor='hivemind-user',reason=reason,expected_revision=current['revision'])
            except Cancelled:
                pass
            except (OSError,ValueError,RuntimeError) as error:
                self.message('Automation · Notice',[str(error)])

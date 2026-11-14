#!/usr/bin/env python3
"""HiveMind's local terminal workspace. Standard library; no HTTP or browser.

Opening or repainting this interface never starts a model/provider call. Sources
and journaled knowledge are read locally; `s` explicitly starts the existing
bounded sync job and `r` explicitly refreshes the approved Calendar snapshot.
Quitting restores the terminal and leaves existing background work untouched.
"""
import argparse
import curses
import datetime as dt
import json
import locale
import math
import os
from pathlib import Path
if __package__:
    from .runtime_paths import default_vault
else:
    from runtime_paths import default_vault
import queue
import re
import select
import sys
import termios
import textwrap
import threading
import time
import tty
import unicodedata
from zoneinfo import ZoneInfo

if __package__:
    from . import constellation, terminal_brand
else:
    import constellation
    import terminal_brand
if __package__:
    from . import hive
else:
    import hive
if __package__:
    from . import jobs
else:
    import jobs
if __package__:
    from .integrations import ingest
else:
    from integrations import ingest

BASE = Path(__file__).resolve().parent
TABS = ('Übersicht', 'Wissen', 'Netz', 'Eingang', 'Quellen')
# xterm-256 grays for the constellation's brightness levels, dimmest first.
GRAYS = (237, 238, 239, 240, 242, 243, 244, 245, 246, 247, 248, 250, 251, 253, 255, 231)
FRAME = 1/24
# Netz in 24-bit colour sends only changed cells, so it can afford smoother motion.
GLOW_FRAME = 1/60
IDLE = 300
# Honey gold and warm ivory, matching the horizontal H / honeycomb identity.
# Existing network palette names carry an amber-to-ivory ramp.
# Each entry is an xterm-256 colour and its basic terminal fallback.
PALETTE = {'accent':(221,curses.COLOR_YELLOW),'shade':(179,curses.COLOR_YELLOW),
           'ivory':(230,curses.COLOR_WHITE),'soft':(250,curses.COLOR_WHITE),
           'abyss':(52,curses.COLOR_RED),'deep':(94,curses.COLOR_YELLOW),
           'tide':(136,curses.COLOR_YELLOW),'azure':(221,curses.COLOR_YELLOW),
           'ice':(222,curses.COLOR_YELLOW),'spark':(230,curses.COLOR_WHITE),
           'mute':(244,curses.COLOR_WHITE),'line':(238,curses.COLOR_WHITE),'ok':(108,curses.COLOR_GREEN),
           'due':(180,curses.COLOR_YELLOW),'error':(167,curses.COLOR_RED),'projects':(179,curses.COLOR_YELLOW),
           'work':(144,curses.COLOR_YELLOW),'personal':(181,curses.COLOR_MAGENTA),'people':(182,curses.COLOR_MAGENTA),
           'knowledge':(151,curses.COLOR_GREEN),'system':(246,curses.COLOR_WHITE)}
AREAS = (('personal','Persönlich'),('people','Personen'),('work','Arbeit'),('projects','Projekte'),('knowledge','Wissen'),('system','Betrieb'))
MARKS = {'confirmed':('●','ok'),'reported':('○','mute'),'inferred':('◌','mute'),'disputed':('◇','error')}
TONES = {'complete':'ok','ok':'ok','active':'ok','idle':'soft','running':'accent','partial':'due','pending':'due',
         'stale':'due','incomplete':'due','stopped':'due','not_run':'mute','unavailable':'mute','failed':'error','blocked':'error'}
STATUS = {'idle':'bereit', 'disabled':'ausgeschaltet', 'paused':'pausiert', 'running':'läuft', 'complete':'vollständig',
          'partial':'teilweise', 'failed':'fehlgeschlagen', 'pending':'offen',
          'not_run':'noch nicht gelesen', 'unavailable':'nicht verfügbar',
          'blocked':'blockiert', 'incomplete':'unvollständig', 'ok':'aktuell',
          'stale':'veraltet', 'stopped':'gestoppt', 'active':'aktiv'}
# Warm network light, from deep amber to pale gold.
LIGHT = ((0,(0,0,0)),(.25,(24,12,2)),(.5,(82,43,7)),(.75,(170,108,24)),(1,(255,206,72)))


def clean(value):
    """Knowledge is display content, never terminal escape/control instructions."""
    return ''.join(c if c == '\n' or (ord(c) >= 32 and ord(c) != 127 and unicodedata.category(c) != 'Cc') else ' ' for c in str(value)).replace('\t','    ')


def cells(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in value)


def clip(value, width):
    result=[];used=0
    for char in clean(value).replace('\n',' ').replace('\r',' '):
        size=0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W','F') else 1
        if used+size>max(0,width):break
        result.append(char);used+=size
    return ''.join(result)


def wrap(value, width):
    """Wrap without cutting wide glyphs; preserve authored blank lines."""
    result=[]
    for paragraph in clean(value).splitlines() or ['']:
        if not paragraph:result.append('');continue
        current=''
        for token in re.findall(r'\S+\s*',paragraph):
            if current and cells(current+token.rstrip())>width:
                result.append(current.rstrip());current=''
            while cells(token.rstrip())>width:
                piece=clip(token,width)
                if not piece:break
                result.append(piece);token=token[len(piece):]
            current+=token
        if current:result.append(current.rstrip())
    return result


def _json(path, fallback):
    if not path.exists():return fallback
    return json.loads(path.read_text())


def _agenda(root):
    value=_json(root/'.state/agenda.json',{'status':'unavailable','events':[]})
    if not isinstance(value,dict) or not isinstance(value.get('events',[]),list):raise ValueError('Ungültiger Kalenderstand.')
    if value.get('status')=='ok':
        try:
            age=(dt.datetime.now(dt.timezone.utc)-hive.timestamp(value['exported_at'])).total_seconds()
            if age>1800 or hive.timestamp(value['range_end_exclusive'])<=dt.datetime.now(dt.timezone.utc):
                value={**value,'status':'stale'}
        except (KeyError,ValueError,TypeError):value={**value,'status':'stale'}
    return value


def _background(root):
    try:
        if __package__:
            from . import background
        else:
            import background
    except ImportError:
        return _json(root/'.state/background.json',{})
    return background.status(root)


def snapshot(root):
    """One bounded, validated local refresh; never called from a paint method."""
    root=Path(root or default_vault()).resolve()
    result={'root':str(root),'loaded_at':hive.now(),'notes':{},'inbox':[],
            'sources':[],'agenda':{'status':'unavailable','events':[]},
            'job':{'status':'idle'},'background':{},'errors':[],'valid':False}
    try:
        with hive.locked(root,shared=(root/'.hive.lock').exists()):
            hive.require_clean(root)
            records=hive.load_notes(root)
            types={ident:record[0]['type'] for ident,record in records.items()}
            for note,_,_ in records.values():hive.validate(note,types)
            hive.audit(root,records)
            result['notes']={ident:{**record[0],'revision':hive.digest(record[1]),'path':str(record[2])} for ident,record in records.items()}
            if __package__:
                from . import work_items
            else:
                import work_items
            result['work_items']=[{'project':ident, **item} for ident,(note,_,_) in records.items()
                                  if note['type']=='project' and note['sensitivity']!='personal' and hive.active(note)
                                  for item in work_items.read(note)]
        result['valid']=True
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
        result['errors'].append('Wissensprüfung fehlgeschlagen: '+str(exc))
    for key,operation in (
        ('inbox',lambda:ingest.inbox(root)),
        ('sources',lambda:ingest.source_status(root)['sources']),
        ('job',lambda:jobs.view(root)),
        ('agenda',lambda:_agenda(root)),
        ('background',lambda:_background(root)),
    ):
        try:result[key]=operation()
        except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:result['errors'].append(key+': '+str(exc))
    return result


def visible_notes(data, history=False):
    return {ident:note for ident,note in data['notes'].items() if history or (not note.get('archived') and note['status']!='deprecated')}


def truecolor():
    return os.environ.get('COLORTERM','').lower() in ('truecolor','24bit') and not os.environ.get('NO_COLOR')


def backdrop():
    """The terminal's background as (r, g, b), asked once before curses starts (OSC 11) so Netz
    light can be blended onto it; None when the terminal does not say or uses a light theme. A
    device-attributes request behind it ends the wait at once on terminals that stay silent."""
    if not truecolor():return None
    try:fd=sys.stdin.fileno();saved=termios.tcgetattr(fd)
    except (termios.error,OSError,ValueError):return None
    reply=b''
    try:
        tty.setcbreak(fd);os.write(sys.stdout.fileno(),b'\x1b]11;?\x1b\\\x1b[c');end=time.monotonic()+.5
        while not re.search(rb'\x1b\[\?[\d;]*c',reply) and select.select([fd],[],[],max(0.0,end-time.monotonic()))[0]:
            chunk=os.read(fd,256)
            if not chunk:break
            reply+=chunk
    except OSError:return None
    finally:termios.tcsetattr(fd,termios.TCSAFLUSH,saved)
    found=re.search(rb'\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})',reply)
    if not found:return None
    colour=tuple(round(int(part,16)*255/(16**len(part)-1)) for part in found.groups())
    return colour if sum(colour)<330 else None


def _send(data):
    """Bytes straight to the terminal, past curses; a tty may take a large frame in parts."""
    view=memoryview(data)
    while view:
        try:view=view[os.write(sys.stdout.fileno(),view):]
        except InterruptedError:continue


def _rgb(index):
    """An xterm-256 colour (16–255) as (r, g, b)."""
    if index>=232:return (8+10*(index-232),)*3
    steps=(0,95,135,175,215,255);index-=16
    return (steps[index//36],steps[index//6%6],steps[index%6])


def _add(colour, light, amount):
    return tuple(min(255,c+l*amount) for c,l in zip(colour,light))


def _fg(colour):
    return '\x1b[38;2;%d;%d;%dm'%tuple(round(c) for c in colour)


def _bg(colour):
    return '\x1b[48;2;%d;%d;%dm'%tuple(round(c) for c in colour)


class Glow:
    """Netz in 24-bit colour, sent straight to the terminal after curses: smooth grays, the amber
    impulse ramp and, when the background is known, the light behind the dots. Serves as the colour
    tables of constellation.Canvas.paint; only changed cells are sent, in one synchronized update."""

    def __init__(self, backdrop):
        self.backdrop=backdrop;self.base=backdrop or (16,16,16);self.front=None;self.at=None;self.pending=None;self.last=(None,None);self.cache={}
        self.grays=[(v,v,v) for v in (round(58+197*(i/15)**1.2) for i in range(constellation.LEVELS))]
        self.colours={name:_rgb(rich) for name,(rich,_) in PALETTE.items()}
        self.dot=[[_fg(_add(gray,self.shine(16*glow+8),.8)) for glow in range(16)] for gray in self.grays]
        self.wash=['\x1b[49m']+[_bg(_add(self.base,self.shine(value),.6)) for value in range(1,256)]
        self.haze=[_fg(_add(self.base,self.shine(value),.6)) for value in range(256)]
        self.empty=(None,'\x1b[49m',' ')

    @staticmethod
    def shine(value):
        """Light as colour; the soft knee keeps overlapping glows from clipping."""
        t=1-math.exp(-value/110)
        for (a,low),(b,high) in zip(LIGHT,LIGHT[1:]):
            if t<=b:return tuple(l+(h-l)*(t-a)/(b-a) for l,h in zip(low,high))
        return LIGHT[-1][1]

    def tinted(self, name, level, glow):
        """A tinted dot: its palette colour, dimmer at low levels so trails fade, plus light."""
        key=(name,level,glow)
        if key not in self.cache:self.cache[key]=_fg(_add(tuple(c*(.55+.45*level/15) for c in self.colours.get(name,self.grays[-1])),self.shine(16*glow+8),.5))
        return self.cache[key]

    def text(self, style, light):
        """(fg, bg) of text: a palette name or a gray level, reversed onto the gray if marked."""
        key=(style,light)
        if key not in self.cache:
            if isinstance(style,str):self.cache[key]=(_fg(self.colours[style]) if style in self.colours else '\x1b[39m',self.wash[light])
            elif style&constellation.REVERSE:self.cache[key]=(_fg(self.base),_bg(self.grays[style%constellation.LEVELS]))
            else:self.cache[key]=(_fg(self.grays[style%constellation.LEVELS]),self.wash[light])
        return self.cache[key]

    def update(self, top, left, grid):
        """Escape sequences that turn the cells on screen into `grid`: changed stretches only, short
        unchanged gaps included, everything after a reset or a move."""
        at=(top,left,len(grid),len(grid[0]) if grid else 0);front=self.front if self.at==at else None;out=[];fg=bg=None
        for r,line in enumerate(grid):
            old=front[r] if front else None
            if line==old:continue
            c=0;n=len(line)
            while c<n:
                if old is not None and line[c]==old[c]:c+=1;continue
                start=last=c
                while c<n and c-last<=3:
                    if old is None or line[c]!=old[c]:last=c+1
                    c+=1
                # Never start inside a wide glyph, old or new.
                if start>0 and (not line[start][2] or old is not None and not old[start][2]):start-=1
                out.append('\x1b[%d;%dH'%(top+r+1,left+start+1))
                for cell_fg,cell_bg,char in line[start:last]:
                    if not char:continue
                    if cell_bg!=bg:out.append(cell_bg);bg=cell_bg
                    if cell_fg is not None and cell_fg!=fg:out.append(cell_fg);fg=cell_fg
                    out.append(char)
        self.front=grid;self.at=at
        return ''.join(out)


def _when(value):
    if not value:return '—'
    try:return hive.timestamp(value).astimezone().strftime('%d.%m. %H:%M')
    except (ValueError,TypeError):return clean(value)


def _background_label(data):
    value=data.get('background') or {}
    if not isinstance(value,dict) or not value:return 'kein lokaler Herzschlag'
    # A stored timestamp is evidence of a previous tick, not proof a process lives.
    at=next((value.get(k) for k in ('heartbeat_at','heartbeat','last_tick_at','updated_at','last_run_at') if value.get(k)),None)
    state=('aktiv' if value['daemon_running'] else 'gestoppt') if 'daemon_running' in value else STATUS.get(value.get('status'),value.get('status') or 'gespeichert')
    operational=''
    if value.get('status')=='paused':operational=' · Automatik pausiert'
    elif value.get('status')=='degraded':operational=' · gestört'
    elif value.get('backoff_until'):
        try:
            if hive.timestamp(value['backoff_until'])>dt.datetime.now(dt.timezone.utc):operational=' · Quellenpause'
        except (ValueError,TypeError):operational=' · Pausenzeit prüfen'
    return state+operational+(' · Stand '+_when(at) if at else ' · gespeicherter Stand')


def _area(note):
    return hive.cluster_for(note).split('/')[0]


def _background_state(data):
    """Short state and tone of the background service."""
    value=data.get('background') or {}
    if not isinstance(value,dict) or not value:return 'kein Herzschlag','mute'
    if value.get('status')=='degraded':return 'gestört','error'
    if value.get('status')=='paused':return 'pausiert','due'
    if value.get('daemon_running') is False:return 'gestoppt','due'
    try:
        if value.get('backoff_until') and hive.timestamp(value['backoff_until'])>dt.datetime.now(dt.timezone.utc):return 'Quellenpause','due'
    except (ValueError,TypeError):return 'Pausenzeit prüfen','due'
    return 'aktiv','ok'


def _tone(message):
    text=message.casefold()
    if any(word in text for word in ('fehlgeschlagen','fehler','gestört','konnte nicht','ungültig')):return 'error'
    if any(word in text for word in ('gespeichert','aktualisiert','gestartet')):return 'ok'
    return 'soft'


def agenda_lines(data, days=14):
    agenda=data.get('agenda') or {};events=agenda.get('events',[])
    try:zone=ZoneInfo(agenda.get('system_time_zone') or 'Europe/Berlin')
    except (ValueError,KeyError):zone=dt.datetime.now().astimezone().tzinfo
    today=dt.datetime.now(zone).date();last=today+dt.timedelta(days=days)
    result=[]
    for event in events:
        if event.get('cancelled'):continue
        try:
            start=hive.timestamp(event['start']).astimezone(zone)
            end=hive.timestamp(event['end']).astimezone(zone)
            if event.get('all_day'):
                first=dt.date.fromisoformat(event['start_date']);exclusive=dt.date.fromisoformat(event['end_date_exclusive'])
                if exclusive<=today or first>=last:continue
                day=max(today,first);label=day.strftime('%d.%m.')+'  ganztags'
            else:
                if end<=dt.datetime.combine(today,dt.time(),zone) or start.date()>=last:continue
                day=max(today,start.date());label=day.strftime('%d.%m.')+'  '+start.strftime('%H:%M')
            result.append((day,start,label+'  '+clean(event.get('title') or 'Ohne Titel')))
        except (ValueError,TypeError,KeyError):continue
    return [row[2] for row in sorted(result,key=lambda row:(row[0],row[1]))]


def render_plain(root, width=80, data=None):
    """Printable status for pipes/automation; no ANSI, server, provider or browser."""
    data=snapshot(root) if data is None else data;width=max(30,min(int(width),160))
    notes=visible_notes(data);review=[n for n in notes.values() if hive.freshness(n)=='stale' or n['evidence']!='confirmed']
    pending=sum(not item.get('processed') for item in data['inbox'])
    lines=['HIVEMIND / persönliches Wissen', '─'*width]
    if data['valid']:lines.append(f'{len(notes)} Notizen · {len(review)} zur Prüfung · {pending} Gedanken offen')
    lines.extend(['Hintergrund: '+_background_label(data),'Abgleich: '+STATUS.get(data['job'].get('status'),data['job'].get('status','unbekannt')),
                  'Kalender: '+STATUS.get(data['agenda'].get('status'),'unbekannt')+' · Stand '+_when(data['agenda'].get('exported_at'))])
    if data.get('background',{}).get('message'):lines.extend(['Automatik: '+clean(data['background']['message'])])
    if data['sources']:
        lines.extend(['','QUELLEN']+[row.get('title',row['id'])+' · '+STATUS.get(row.get('status'),row.get('status','unbekannt')) for row in data['sources']])
    if __package__:
        from . import work_items
    else:
        import work_items
    pending_work=[item for item in data.get('work_items',[]) if item['state'] in {'open','waiting'}]
    if pending_work:
        lines.extend(['','OFFENE ARBEIT']+[item['project']+' / '+work_items.line(item) for item in pending_work[:5]])
    if data['errors']:lines.extend(['','HINWEISE']+data['errors'])
    lines.extend(['','Kein interaktives Terminal: lokaler Status ohne Website.',
                  'Im Terminal: hive   /   n Gedanke   s Abgleich   q Schließen'])
    return '\n'.join(line for item in lines for line in wrap(item,width))+'\n'


class Interface:
    def __init__(self, screen, root, initial_view='overview', backdrop=None):
        self.screen=screen;self.root=Path(root or default_vault()).resolve();self.data={};self.tab=0
        self.selection=0;self.scroll=0;self.query='';self.history=False
        self.detail=None;self.sources=False;self.network_focus=None
        self.insight=None;self.insight_return=None
        self.modal=None;self.input='';self.capture_title='';self.capture_body=''
        self.capture_field='title';self.notice='';self.items=[];self.top=0
        self.work=queue.Queue();self.busy=False;self.last_refresh=0;self.note_cache={};self.detail_cache={}
        self.fresh=queue.Queue();self.loading=False;self.last_key=time.monotonic()
        self.sky=constellation.Sky();self.trail=[];self.network_return=None;self.immersive=True
        self.tab={'overview':0,'knowledge':1,'network':2,'inbox':3,'sources':4}.get(initial_view,0)
        self.automation_entry=initial_view=='automation'
        self.reload()
        self.accent=curses.A_BOLD
        self.levels=[curses.A_DIM]*5+[curses.A_NORMAL]*6+[curses.A_BOLD]*5
        self.tone={name:0 for name in PALETTE}
        self.tone.update(accent=curses.A_BOLD,soft=curses.A_DIM,mute=curses.A_DIM,line=curses.A_DIM,error=curses.A_BOLD,strong=curses.A_BOLD,select=curses.A_REVERSE,mark=curses.A_REVERSE)
        if curses.has_colors():
            try:
                # The terminal's own colours stay the base; NO_COLOR only withholds the palette.
                curses.start_color();curses.use_default_colors();wide=curses.COLORS>=256
                if not os.environ.get('NO_COLOR'):
                    for index,(name,(rich,basic)) in enumerate(PALETTE.items(),40):
                        curses.init_pair(index,rich if wide else basic,-1);self.tone[name]=curses.color_pair(index)
                    self.tone['accent']|=curses.A_BOLD;self.accent=self.tone['accent']
                    if wide:
                        curses.init_pair(38,230,236);curses.init_pair(39,221,236)
                        self.tone['select']=curses.color_pair(38);self.tone['mark']=curses.color_pair(39)|curses.A_BOLD
                    if wide and curses.COLOR_PAIRS>16+len(GRAYS):
                        for index,gray in enumerate(GRAYS):curses.init_pair(16+index,gray,-1)
                        self.levels=[curses.color_pair(16+index) for index in range(len(GRAYS))]
            except curses.error:pass
        # 24-bit terminals get Netz in smooth colour and, when their background is known, with light.
        self.glow=Glow(backdrop) if truecolor() and curses.has_colors() else None
        self.sky.light=bool(self.glow and backdrop)
        self.dim=self.tone['soft']
        try:curses.curs_set(0)
        except curses.error:pass
        try:curses.set_escdelay(35)
        except AttributeError:pass
        self.screen.keypad(True);self.screen.timeout(150)
        # Ctrl+S must reach the capture editor rather than freeze output (IXON).
        curses.raw()

    def reload(self):
        self.data=snapshot(self.root);self.last_refresh=time.monotonic()
        self.note_cache={};self.detail_cache={}
        if self.data['errors']:self.notice=self.data['errors'][0]

    def reload_later(self):
        """An animated view keeps its cadence while the same validated snapshot loads."""
        if self.loading:return
        self.loading=True;self.last_refresh=time.monotonic()
        def worker():
            try:self.fresh.put(snapshot(self.root))
            except Exception as exc:self.fresh.put(exc)
        threading.Thread(target=worker,daemon=True).start()

    def animated(self):
        height,width=self.screen.getmaxyx();quiet=time.monotonic()-self.last_key
        if not (self.tab==2 and not self.detail and not self.modal and height>=17 and width>=54 and self.data.get('valid')):return False
        return self.sky.animating(quiet<IDLE) or (self.immersive and quiet<6)

    def look(self,style):
        """Attribute for a renderer style: a palette name, or a gray level that may be reversed."""
        if isinstance(style,str):return self.tone.get(style,0)
        return self.levels[style%constellation.LEVELS]|(curses.A_REVERSE if style&constellation.REVERSE else 0)

    def write(self,y,x,parts,limit=None):
        """Coloured segments from left to right; returns the column after the last one."""
        height,width=self.screen.getmaxyx();end=width-2 if limit is None else min(width-2,x+limit)
        if not 0<=y<height:return x
        for text,style in parts:
            if x>=end:break
            text=clip(text,end-x)
            try:self.screen.addstr(y,x,text,self.look(style) if isinstance(style,str) else style)
            except curses.error:pass
            x+=cells(text)
        return x

    def section(self,y,x,title,count=None):
        self.write(y,x,[(title,'shade')]+([('  '+str(count),'mute')] if count is not None else []))

    def put(self,y,x,text,attr=0,width=None):
        height,columns=self.screen.getmaxyx()
        if y<0 or y>=height or x<0 or x>=columns:return
        width=min(columns-x-1,columns if width is None else width)
        if width<=0:return
        try:self.screen.addstr(y,x,clip(text,width),attr)
        except curses.error:pass

    def line(self,y,text='',attr=0):self.put(y,3,text,attr)

    def rule(self,y):
        _,width=self.screen.getmaxyx();self.put(y,2,'─'*max(0,width-4),self.tone['line'])

    def clear(self):
        """A full curses repaint; Netz cells in 24-bit colour are then sent again in full."""
        self.screen.clear()
        if self.glow:self.glow.front=None

    def present(self):
        """curses first, then any Netz cells in 24-bit colour, together as one synchronized update."""
        pending=self.glow.pending if self.glow else None
        if not pending:self.screen.refresh();return
        self.glow.pending=None;top,left,canvas=pending
        # A resting picture comes back as the same canvas and keeps its cells.
        if self.glow.last[0] is not canvas:self.glow.last=(canvas,canvas.paint(self.glow))
        cells=self.glow.update(top,left,self.glow.last[1])
        # Cursor and attributes are saved and restored around the cells, so curses keeps its bearings.
        _send(b'\x1b[?2026h');self.screen.refresh();_send(('\x1b7'+cells+'\x1b8\x1b[?2026l').encode())

    def launch(self,kind):
        if self.busy:self.notice='Eine lokale Aktion läuft bereits.';return
        self.busy=True;self.notice='Abgleich wird gestartet …' if kind=='sync' else 'Kalender wird gelesen …'
        def worker():
            try:
                result=jobs.start(self.root) if kind=='sync' else ingest.refresh_calendar(self.root)
                if kind=='sync':
                    message='Abgleich gestartet · läuft im Hintergrund weiter.' if result.get('status')=='running' else result.get('message','Abgleich: '+str(result.get('status')))
                else:message='Kalender aktualisiert.' if result.get('status')=='ok' else 'Kalender: '+STATUS.get(result.get('status'),'nicht verfügbar')
            except Exception as exc:message='Aktion fehlgeschlagen: '+str(exc)
            self.work.put(message)
        threading.Thread(target=worker,daemon=True).start()

    def notes(self):
        cache_key=(self.query,self.history)
        if cache_key in self.note_cache:return self.note_cache[cache_key]
        values=visible_notes(self.data,self.history);rank={}
        if self.query:
            # The canonical literal FTS query is used, with its normal history scope.
            try:
                found=hive.search(self.root,self.query,limit=100,include_archived=self.history,include_deprecated=self.history)
                matches={row['id'] for row in found};rank={row['id']:index for index,row in enumerate(found)}
                # Literal title fragments also work while preserving canonical FTS.
                phrase=self.query.casefold()
                matches.update(ident for ident,note in values.items() if phrase in (' '.join([note['title'],note['summary']]+note.get('aliases',[]))).casefold())
                values={ident:note for ident,note in values.items() if ident in matches}
            except (OSError,ValueError) as exc:self.notice='Suche: '+str(exc);return []
        result=sorted(values.values(),key=lambda n:(rank.get(n['id'],len(rank)),n['title'].casefold(),n['id']))
        self.note_cache[cache_key]=result
        return result

    def neighbors(self,ident):
        notes=visible_notes(self.data,self.history);result=[]
        for origin,note in notes.items():
            for edge in note.get('links',[]):
                if origin==ident and edge['target'] in notes:
                    result.append({'id':edge['target'],'title':notes[edge['target']]['title'],'relation':edge['relation'],'direction':'→','reason':edge.get('reason','')})
                elif edge['target']==ident:
                    result.append({'id':origin,'title':note['title'],'relation':edge['relation'],'direction':'←','reason':edge.get('reason','')})
        return sorted(result,key=lambda row:(row['title'].casefold(),row['relation']))

    def show_text(self, lines, start, bottom):
        available=max(1,bottom-start);self.scroll=max(0,min(self.scroll,max(0,len(lines)-available)))
        for y,line in enumerate(lines[self.scroll:self.scroll+available],start):
            text,style=line if isinstance(line,tuple) else (line,'')
            self.line(y,text,self.look(style))
        if len(lines)>available:self.put(bottom-1,max(3,self.screen.getmaxyx()[1]-18),f'{self.scroll+1}–{min(len(lines),self.scroll+available)}/{len(lines)}',self.dim)

    def show_list(self,items,start,bottom,empty='Noch nichts vorhanden.',width=None):
        """Rows carry coloured `parts` (or a plain `label`) and optional right-aligned parts."""
        self.items=items;self.selection=max(0,min(self.selection,max(0,len(items)-1)))
        height=max(1,bottom-start)
        if not items:self.line(start,empty,self.dim);return
        if self.selection<self.top:self.top=self.selection
        if self.selection>=self.top+height:self.top=self.selection-height+1
        self.top=max(0,min(self.top,max(0,len(items)-height)))
        width=self.screen.getmaxyx()[1]-8 if width is None else width
        for index,row in enumerate(items[self.top:self.top+height],self.top):
            y=start+index-self.top;parts=row.get('parts') or [(row['label'],'')];right=row.get('right') or []
            span=sum(cells(clean(text)) for text,_ in right);room=width-(span+3 if right else 0)
            if index==self.selection:
                self.put(y,2,' '*(width+3),self.tone['select']);self.put(y,2,'▍',self.tone['mark'])
                parts=[(text,self.tone['select']|curses.A_BOLD) for text,_ in parts];right=[(text,self.tone['select']) for text,_ in right]
            self.write(y,4,parts,room)
            if right:self.write(y,4+width-span,right)

    def note_row(self,note,right=None):
        """A note as a list row: evidence mark, title and its area."""
        area=_area(note);mark,tone=MARKS.get(note['evidence'],('○','mute'))
        return {'id':note['id'],'kind':'note','parts':[(mark,tone),('  '+note['title'],'')],
                'right':right or [(hive.cluster_for(note),area if area in PALETTE else 'mute')]}

    def wordmark(self, y, x, compact=False):
        """Horizontal H / honeycomb mark, sized to the available vertical space."""
        emblem = terminal_brand.COMPACT_EMBLEM if compact else terminal_brand.EMBLEM
        for offset, line in enumerate(emblem):
            self.put(y + offset, x, line, self.tone['accent'])
        left = x + len(emblem[0]) + 3
        if compact:
            self.write(y + 1, left, [('HIVE', 'accent'), ('MIND', 'ivory')])
            self.put(y + 2, left, 'ÜBERSICHT', self.tone['mute'])
        else:
            for offset, (hive_part, mind_part) in enumerate(zip(terminal_brand.HIVE, terminal_brand.MIND)):
                self.write(y + 1 + offset, left, [(hive_part, 'accent'), (' ', ''), (mind_part, 'ivory')])
        return y + len(emblem)

    def agenda(self,y,x,width,bottom,limit=6):
        """Upcoming events, today and tomorrow by name; returns the next free row."""
        events=agenda_lines(self.data);today=dt.date.today()
        names={today.strftime('%d.%m.'):'Heute',(today+dt.timedelta(days=1)).strftime('%d.%m.'):'Morgen'}
        shown=events[:max(1,min(limit,bottom-y-1))]
        for event in shown or ['Keine geladenen Termine. Mit r bewusst neu lesen.']:
            if y>=bottom:break
            if event in events:
                day,clock,title=(event.split('  ',2)+['',''])[:3]
                self.write(y,x,[(names.get(day,day).ljust(8),'accent' if day in names else 'mute'),(clock.ljust(10),'soft'),(title,'')],width)
            else:self.write(y,x,[(event,'mute')],width)
            y+=1
        if len(events)>len(shown) and y<bottom:self.write(y,x,[(f'+ {len(events)-len(shown)} weitere Termine in den nächsten 14 Tagen','mute')],width);y+=1
        return y

    def view_overview(self,start,bottom):
        """The entry: what needs attention, what comes next and whether the hive is working."""
        height,width=self.screen.getmaxyx()
        notes=visible_notes(self.data);self.sky.sync(notes);now=dt.datetime.now(dt.timezone.utc)
        review=sorted((note for note in notes.values() if hive.freshness(note)=='stale' or note['evidence']!='confirmed'),
                      key=lambda note:(note['evidence']!='disputed',hive.freshness(note)!='stale',note.get('stale_after',''),note['title'].casefold()))
        pending=sum(not item.get('processed') for item in self.data['inbox'])
        def due(note):
            if note['evidence']=='disputed':return [('umstritten','error')]
            if hive.freshness(note)!='stale':return [(constellation.EVIDENCE.get(note['evidence'],note['evidence']),'mute')]
            days=(now-hive.timestamp(note['stale_after'])).days
            return [('fällig seit '+('einem Tag' if days==1 else f'{days} Tagen') if days>0 else 'heute fällig','due')]
        rows=[self.note_row(note,due(note)) for note in review]
        pending_work=[item for item in self.data.get('work_items',[]) if item['state'] in {'open','waiting'}]
        def show_work(y,x,room):
            if not pending_work or y>=bottom-2:return y
            if __package__:
                from . import work_items
            else:
                import work_items
            self.section(y,x,'Offene Arbeit',len(pending_work));y+=1
            for item in pending_work[:min(3,max(0,bottom-y-2))]:
                self.write(y,x,[(work_items.LABELS[item['state']]+' · ','accent'),(clean(item['text']),'')],room);y+=1
            return y+1
        background=self.data.get('background') or {};job=self.data['job'];agenda=self.data['agenda'];state,tone=_background_state(self.data)
        beat=next((background.get(key) for key in ('heartbeat_at','heartbeat','last_tick_at','updated_at','last_run_at') if isinstance(background,dict) and background.get(key)),None)
        status=[('Hintergrund',tone,state+('  ·  '+_when(beat) if beat else ''),clean(background.get('message','')) if tone!='ok' else ''),
                ('Abgleich',TONES.get(job.get('status'),'mute'),STATUS.get(job.get('status'),'unbekannt'),clean(job.get('message','')) if job.get('status') in ('failed','partial','blocked') else ''),
                ('Kalender',TONES.get(agenda.get('status'),'mute'),STATUS.get(agenda.get('status'),'unbekannt')+'  ·  '+_when(agenda.get('exported_at')),'')]
        empty='Nichts fällig. Dein Wissen ist im Prüfzeitraum.'
        if width<118 or bottom-start<24:
            y=start
            if pending_work and bottom-start<10:
                self.line(y,'ÜBERSICHT / '+str(len(notes))+' NOTIZEN',self.accent)
                show_work(y+1,3,width-6)
                self.items=rows
                return
            if height>=30 and width>=68:
                y=self.wordmark(y,3)+1
            elif bottom-start>=12:
                y=self.wordmark(y,3,compact=True)
            else:
                self.line(y,'ÜBERSICHT',self.accent);y+=1
            self.write(y,3,[(str(len(notes)),'strong'),(' Notizen     ','soft'),(str(pending),'strong'),(' im Eingang     ','soft'),(str(len(review)),'strong'),(' zur Prüfung','soft')]);y+=2
            for label,tone,value,message in status:
                self.write(y,3,[('●',tone),('  '+label.ljust(13),'soft'),(value,'')]+([('  '+message,'mute')] if message else []));y+=1
            y+=1
            y=show_work(y,3,width-6)
            if y+7<bottom:self.section(y,3,'Termine');y=self.agenda(y+1,3,width-6,bottom-4,3)+1
            if y<bottom-1:self.section(y,3,'Zur Prüfung',len(review));self.show_list(rows,y+1,bottom,empty)
            return
        split=int(width*.56);right=split+4;room=width-right-3
        brand_bottom=self.wordmark(start,3)
        y=show_work(brand_bottom+2,3,split-6);self.section(y,3,'Heute und demnächst');y=self.agenda(y+1,3,split-6,bottom-6)+1
        self.section(y,3,'Zur Prüfung',len(review));self.show_list(rows,y+1,bottom,empty,split-8)
        # Right column: how the hive is doing.
        y=start;self.section(y,right,'Betrieb');y+=1
        for label,tone,value,message in status:
            self.write(y,right,[('●',tone),('  '+label.ljust(13),'soft'),(value,'')],room);y+=1
            for line in wrap(message,room-16)[:2] if message else []:self.write(y,right+16,[(line,'mute')],room-16);y+=1
        y+=1;self.section(y,right,'Quellen',len(self.data['sources']));y+=1
        for row in self.data['sources'][:max(0,min(9,bottom-y-8))]:
            when=_when(row.get('last_complete_at'))
            self.write(y,right,[('●',TONES.get(row.get('status'),'mute')),('  '+row.get('title',row['id']),'')],room-len(when)-2);self.write(y,right+room-len(when),[(when,'mute')]);y+=1
        y+=1;self.section(y,right,'Wissen');y+=1
        confirmed=sum(note['evidence']=='confirmed' for note in notes.values())
        for first,label,second,other in ((len(notes),'Notizen',len(self.sky.edges),'Verbindungen'),(confirmed,'bestätigt',pending,'im Eingang')):
            self.write(y,right,[(f'{first:>4}','strong'),('  '+label.ljust(14),'soft'),(f'{second:>4}','strong'),('  '+other,'soft')],room);y+=1
        y+=1
        if y+2<bottom and notes:
            # One bar shares the width by area; the legend below names each part.
            counts=sorted(((key,label,sum(_area(note)==key for note in notes.values())) for key,label in AREAS),key=lambda part:-part[2])
            x=right
            for key,label,count in counts:
                if count:span=max(1,round(count*room/len(notes)));self.put(y,x,'━'*max(0,min(span,right+room-x)),self.tone[key]);x+=span
            y+=1;x=right
            for key,label,count in counts:
                if not count:continue
                if x+len(label)+len(str(count))+1>right+room:y+=1;x=right
                x=self.write(y,x,[(label+' ','soft'),(str(count),key)])+3

    def view_notes(self,start,bottom):
        self.line(start,'WISSEN   /   '+('Suche: '+self.query if self.query else 'Alle aktiven Notizen'),self.accent)
        self.line(start+1,'/ suchen   h '+('aktive Notizen' if self.history else 'Archiv')+'   Enter lesen   y hilfreich / u unbrauchbar',self.dim)
        self.line(start+2,'w Warum   a Auswirkung   e Erfahrungen   D Änderungen',self.dim)
        values=self.notes()
        self.show_list([self.note_row(note) for note in values],start+3,bottom,'Keine Treffer.' if self.query else 'Noch keine Notizen.')

    def view_network(self,start,bottom):
        """Living tissue of all notes; Enter focuses a cell and travels along its links."""
        notes=visible_notes(self.data,self.history);self.sky.sync(notes)
        if self.network_focus not in notes:self.network_focus=None
        if not self.network_focus:self.trail=[]
        matches={note['id'] for note in self.notes()} if self.query else None
        if self.network_focus:self.items=[{**row,'kind':'focus'} for row in self.sky.arrange(constellation.merge(self.neighbors(self.network_focus)))]
        else:self.items=[{'id':ident,'kind':'focus'} for ident in self.sky.order(notes,matches)]
        if self.network_return:
            self.selection=next((index for index,item in enumerate(self.items) if item['id']==self.network_return),0);self.network_return=None
        self.selection=max(0,min(self.selection,len(self.items)-1))
        _,width=self.screen.getmaxyx();lapse=self.sky.timelapse;now=time.monotonic()
        heading='NETZ   /   '+('ZEITRAFFER' if lapse else 'FOKUS   ·   '+self.sky.names.get(self.network_focus,'') if self.network_focus else 'LEBENDES WISSEN')+('   ·   Suche: '+self.query if self.query and not self.network_focus and not lapse else '')
        stats=f'{len(notes)} NOTIZEN  ·  {len(self.sky.edges)} VERBINDUNGEN' if cells(heading)+40<width else ''
        if lapse:hints='z oder Esc beendet den Zeitraffer'
        elif self.network_focus:hints='↑↓ Nachbar   Enter hinüber   ⌫ zurück   d lesen   Esc Gesamtbild'
        else:hints='↑↓ Notiz   Enter Fokus   / suchen   z Zeitraffer'
        hints+='   w Warum   a Auswirkung   e Erfahrungen'
        hints+='   f '+('Rahmen' if self.immersive else 'Vollbild')+'   Leertaste '+('anhalten' if self.sky.motion else 'bewegen')
        if self.glow and self.glow.backdrop:hints+='   l Licht '+('aus' if self.sky.light else 'an')
        if self.immersive:
            # Full screen: the hints fade after a few quiet seconds and return with any key.
            quiet=now-self.last_key;fade=7 if quiet<4 else int(7*(1-(quiet-4)/1.5)) if quiet<5.5 else -1
            hints+='   m Erinnerungen   1–5 Ansicht   q Ende'+('   ·   '+self.notice if self.notice else '')
            header=((heading,12,stats),(hints,fade) if fade>=0 else ('',0));top,left,rows,cols=start,0,bottom-start,width-1
        else:
            self.line(start,heading,self.accent)
            if stats:self.put(start,width-cells(stats)-3,stats,self.tone['mute'])
            self.line(start+1,hints,self.dim)
            header=();top,left,rows,cols=start+3,1,bottom-start-3,width-2
        frame=self.sky.frame(cols,rows,notes,self.items,self.selection,self.network_focus,matches,self.trail,now,now-self.last_key<IDLE,(self.data.get('loaded_at'),self.history),not self.immersive,header,bool(self.glow))
        if self.glow:self.glow.pending=(top,left,frame);return
        for row,col,text,style in frame:
            try:self.screen.addstr(top+row,left+col,text,self.look(style))
            except curses.error:pass

    def toggle_timelapse(self):
        """Replay the journal: each note appears when it was first written."""
        if self.sky.timelapse:self.sky.timelapse=None;return
        try:
            with hive.locked(self.root,shared=(self.root/'.hive.lock').exists()):events=hive.events(self.root)
            rows=sorted(((hive.timestamp(event['at']),[change['id'] for change in event['changes'] if change['before'] is None],[change['id'] for change in event['changes']],clean(event.get('reason','')),clean(event.get('actor',''))) for event in events),key=lambda row:row[0])
        except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:self.notice='Zeitraffer: '+str(exc);return
        self.network_focus=None;self.trail=[];self.reset_view()
        if not self.sky.start_timelapse(rows,time.monotonic()):self.notice='Noch keine protokollierte Entwicklung.'

    def view_inbox(self,start,bottom):
        self.line(start,'EINGANG   /   EIN GEDANKE GENÜGT.',self.accent)
        self.line(start+1,'n festhalten   Enter lesen   ·   wird beim nächsten Abgleich kuratiert',self.dim)
        self.show_list([{'id':item['id'],'kind':'inbox','parts':[('✓' if item.get('processed') else '○','ok' if item.get('processed') else 'accent'),('  '+item['title'],'soft' if item.get('processed') else '')],
                         'right':[('kuratiert  ' if item.get('processed') else 'offen  ','ok' if item.get('processed') else 'accent'),(_when(item.get('created_at')),'mute')]} for item in self.data['inbox']],
                       start+3,bottom,'Dein Eingang ist leer. Mit n einen Gedanken festhalten.')

    def view_sources(self,start,bottom):
        self.line(start,'QUELLEN   /   HERKUNFT BLEIBT SICHTBAR.',self.accent)
        self.line(start+1,'Stand beschreibt den letzten geprüften Zeitraum. Enter für Details.',self.dim)
        self.show_list([{'id':row['id'],'kind':'source','parts':[('●',TONES.get(row.get('status'),'mute')),('  '+row.get('title',row['id']),'')],
                         'right':[(STATUS.get(row.get('status'),str(row.get('status')))+'  ',TONES.get(row.get('status'),'mute')),(_when(row.get('last_complete_at')),'mute')]} for row in self.data['sources']],
                       start+3,bottom,'Noch keine Quellen ausgewählt.')

    def detail_lines(self,width):
        key=(self.detail,self.sources,width)
        if key not in self.detail_cache:self.detail_cache[key]=self._detail_lines(width)
        return self.detail_cache[key]

    def _detail_lines(self,width):
        """Wrapped (text, style) lines: headings in the darker blue, footnotes and hints quiet."""
        kind,ident=self.detail;lines=[]
        def add(value,style=''):lines.extend((line,style) for line in wrap(value,width))
        def body(text):
            for raw in clean(text).splitlines() or ['']:add(raw,'shade' if raw.startswith('#') else 'mute' if raw.startswith('[^') else '')
        if kind=='insight':
            add(self.insight['title'],'accent');add('↑↓ / Bild↓ scrollen   Esc zurück','mute');add('')
            body(self.insight['text'].partition('\n')[2]);return lines
        if kind=='note':
            note=self.data['notes'].get(ident)
            if not note:return [('Diese Notiz wurde inzwischen verändert. Zurück und neu öffnen.','due')]
            if self.sources:
                add('QUELLEN / '+note['title'],'accent');add('');add('o Notiztext   Esc zurück','mute');add('')
                for source in note['sources']:
                    add(source['title']);add(source['resource'],'soft')
                    for key in ('observed_at','modified_at','sha256'):
                        if source.get(key):add(key+': '+source[key],'mute')
                    add('')
                return lines
            stale=hive.freshness(note)=='stale'
            add(note['title'],'accent');add('')
            add(hive.cluster_for(note)+' / '+note['status']+' / '+note['evidence'],MARKS.get(note['evidence'],('','soft'))[1] if note['evidence']!='reported' else 'soft')
            add('Prüfung: '+('fällig' if stale else 'im Zeitraum')+' · '+str(note.get('stale_after','')),'due' if stale else 'mute')
            handoff=note.get('handoff')
            if isinstance(handoff,dict):
                add('Aktuelle Übergabe: '+hive.GERMAN_EVIDENCE.get(handoff.get('evidence'),'nicht eingeordnet')+
                    ' · '+str(handoff.get('observed_at','nicht datiert'))+' · Quelle '+str(handoff.get('source_id','nicht zugeordnet')),'due')
                add('Der Notizstatus verifiziert diese Übergabe nicht.','mute')
            if note['type']=='project':
                if __package__:
                    from . import work_items
                else:
                    import work_items
                for item in work_items.read(note):
                    add(work_items.line(item),'soft')
                    add('Quelle '+item['source_id']+' · '+item['observed_at']+' · '+item['evidence'],'mute')
            add('');add(note['summary']);add('');add('o Quellen   g Nachbarschaft   ↑↓ / Bild↓ scrollen   Esc zurück','mute')
            add('w Warum   a Auswirkung   e Erfahrungen   D Änderungen','mute');add('');body(note['body'])
            return lines
        if kind=='inbox':
            try:item=ingest.read_source(self.root,ident)
            except (OSError,ValueError) as exc:return [('Gedanke konnte nicht gelesen werden: '+str(exc),'error')]
            add('EINGANG / '+ident,'accent');add('');add('Esc zurück','mute');add('');body(item['content']);return lines
        row=next((row for row in self.data['sources'] if row['id']==ident),None)
        if not row:return [('Quelle ist nicht mehr konfiguriert.','due')]
        add('QUELLE / '+row.get('title',ident),'accent');add('');add(STATUS.get(row.get('status'),'unbekannt'),TONES.get(row.get('status'),'mute'))
        add('Letzter vollständiger Stand: '+_when(row.get('last_complete_at')),'soft');add('Letzter Versuch: '+_when(row.get('attempted_at')),'soft')
        add('');add(row.get('detail') or '');add('');add('Esc zurück','mute')
        try:
            source=ingest.read_source(self.root,ident)
            add('');add(source['path'],'soft');add('SHA256 '+source['sha256'],'mute');add('');body(source['content'])
        except ValueError:pass # Remote metadata has no local raw document.
        except OSError as exc:add('');add('Quelle konnte nicht gelesen werden: '+str(exc),'error')
        return lines

    def draw_modal(self):
        height,width=self.screen.getmaxyx();self.screen.erase();self.rule(1)
        if self.modal=='search':
            self.line(3,'SUCHE / WISSEN UND VERBINDUNGEN',self.accent)
            self.line(5,'Enter suchen   Esc abbrechen',self.dim)
            self.line(8,'› '+self.input[-max(1,width-10):],self.accent)
            self.rule(height-3)
            self.line(height-2,'Wörtliche Suche im lokalen Wissensindex.',self.dim)
            return
        self.line(3,'GEDANKE FESTHALTEN',self.accent)
        self.line(5,'Enter: Text beginnen / neue Zeile   Tab: Titel ↔ Text',self.dim)
        self.line(6,'Ctrl+G oder Ctrl+S speichern   Esc verwerfen',self.dim)
        self.line(8,'TITEL'+('  ←' if self.capture_field=='title' else ''),self.dim)
        self.line(9,self.capture_title or 'Optionaler Titel',self.accent if self.capture_field=='title' else 0)
        self.line(11,'GEDANKE'+('  ←' if self.capture_field=='body' else ''),self.dim)
        lines=wrap(self.capture_body or 'Schreib oder diktiere hier deinen Gedanken.',max(12,width-7))
        available=max(1,height-16)
        for y,line in enumerate(lines[-available:],12):self.line(y,line)
        self.rule(height-3)
        self.line(height-4,self.notice,self.dim)
        self.line(height-2,f'{len(self.capture_body)} / 24000 Zeichen · bleibt zunächst im Eingang',self.dim)

    def paint(self):
        height,width=self.screen.getmaxyx()
        # curses never saw the 24-bit Netz cells, so leaving Netz repaints the whole screen.
        if self.glow and self.glow.front is not None and not (self.tab==2 and not self.detail and not self.modal and self.data.get('valid') and height>=17 and width>=54):self.clear()
        self.screen.erase();self.items=[]
        if height<17 or width<54:
            self.put(1,1,'HIVEMIND / Terminal bitte vergrößern.',self.accent)
            self.put(3,1,'Mindestens 54 × 17. q schließt nur diese Ansicht.')
            self.screen.refresh();return
        if self.modal:self.draw_modal();self.screen.refresh();return
        if self.tab==2 and self.immersive and not self.detail and self.data.get('valid'):
            self.view_network(1,height-1);self.present();return
        # Header: the mark, the background's state and the time.
        state,tone=_background_state(self.data);clock=dt.datetime.now().strftime('%d.%m.%Y  %H:%M' if width>=100 else '%H:%M')
        self.write(1,3,[('HIVE','accent'),('MIND','ivory')])
        status=[('● ',tone),('Hintergrund '+state,'soft'),('     '+clock,'mute')]
        status_left=max(14,width-3-sum(cells(text) for text,_ in status))
        if status_left>=40:self.put(1,14,'Dein persönliches Wissen',self.tone['mute'])
        self.write(1,status_left,status)
        x=3;pending=sum(not item.get('processed') for item in self.data.get('inbox',[]))
        underline=None
        tab_gap=4 if width>=70 else 1
        for index,label in enumerate(TABS):
            active=index==self.tab;begin=x
            x=self.write(3,x,[(str(index+1)+' ','accent' if active else 'mute'),(label,'accent' if active else 'soft')]+([(' '+str(pending),'accent')] if index==3 and pending and width>=70 else []))
            if active:underline=(begin,x-begin)
            x+=tab_gap
        self.rule(4)
        if underline:self.put(4,underline[0],'━'*underline[1],self.tone['accent'])
        bottom=height-5
        if self.detail:self.show_text(self.detail_lines(width-7),6,bottom)
        elif not self.data.get('valid'):
            self.show_text([(line,'error') for error in self.data['errors'] for line in wrap(error,width-7)],6,bottom)
        else:(self.view_overview,self.view_notes,self.view_network,self.view_inbox,self.view_sources)[self.tab](6,bottom)
        self.rule(height-5)
        compact=width<100
        hints='D Änderungen · w Warum · a Wirkung · e Erfahrungen' if compact else 'D Änderungen und offene Punkte · w Warum · a Auswirkung · e Erfahrungen'
        self.put(height-4,3,self.notice or hints,self.tone[_tone(self.notice)] if self.notice else self.tone['mute'])
        x=3
        shortcuts = [('1–5','Tabs' if compact else 'Ansicht'), ('m','Wissen' if compact else 'Erinnerungen')]
        if (self.root / '.hivemind.json').is_file():
            shortcuts.append(('s','Quellen'))
            shortcuts.append(('b','Auto' if compact else 'Automatik'))
        shortcuts.extend([('q','Ende'), ('/','Suche'), ('n','Neu' if compact else 'Gedanke')])
        if not (self.root / '.hivemind.json').is_file():
            shortcuts.extend([('s','Sync' if compact else 'Abgleich'), ('r','Kal.' if compact else 'Kalender')])
        for key,label in shortcuts:
            x=self.write(height-2,x,[(key,'accent'),(' '+label,'mute')])+(1 if compact else 3)
        self.present()

    def reset_view(self):
        self.selection=0;self.top=0;self.scroll=0;self.detail=None;self.sources=False
        self.insight=None;self.insight_return=None

    def selected_note(self):
        if self.detail and self.detail[0]=='note':return self.detail[1]
        if self.detail and self.detail[0]=='insight':return self.insight.get('subject')
        if self.items:
            selected=self.items[min(self.selection,len(self.items)-1)]
            if selected['kind'] in ('note','focus'):return selected['id']
        return self.network_focus

    def open_insight(self,kind):
        if __package__:
            from . import insights
        else:
            import insights
        ident=self.selected_note()
        try:
            if kind in ('explain','impact'):
                if not ident:raise ValueError('Zuerst eine Notiz auswählen.')
                result=getattr(insights,kind)(self.root,ident)
            else:
                note=self.data['notes'].get(ident,{})
                target=hive.cluster_for(note) if note else '.'
                if not target.startswith('projects/'):
                    if __package__:
                        from . import project_context
                    else:
                        import project_context
                    project=project_context.resolve_project(self.root,'.')
                    target=project['cluster'] if project else 'all'
                result=insights.lessons(self.root,target,task=self.query) if kind=='lessons' else insights.changes(self.root,target)
            if not self.detail or self.detail[0]!='insight':self.insight_return=self.detail
            self.insight=result;self.detail=('insight',kind);self.scroll=0;self.detail_cache={}
        except (OSError,ValueError,KeyError) as exc:self.notice=str(exc)

    def search_feedback(self,helpful):
        if not self.query or self.tab not in (1,2):
            self.notice='Zuerst eine Suche ausführen.';return
        try:
            hive.feedback(self.root,self.query,helpful,result_ids=[n['id'] for n in self.notes()[:20]],actor='terminal-user')
            self.notice='Suchfeedback gespeichert: '+('hilfreich.' if helpful else 'unbrauchbar.')
        except (OSError,ValueError) as exc:self.notice='Suchfeedback: '+str(exc)

    def switch(self,tab):
        """Change tabs; entering Netz wakes its tissue."""
        if tab==2 and self.tab!=2:self.sky.wake()
        self.tab=tab;self.reset_view()

    def handle_modal(self,key):
        if key in ('\x1b','\x03'):self.modal=None;return
        if self.modal=='search':
            if key in ('\n','\r',curses.KEY_ENTER):
                self.query=self.input.strip();self.modal=None;self.reset_view()
                if self.tab not in (1,2):self.tab=1
            elif key in (curses.KEY_BACKSPACE,'\x7f','\b'):self.input=self.input[:-1]
            elif isinstance(key,str) and key.isprintable() and len(self.input)<200:self.input+=key
            return
        if key in ('\x07','\x13'):
            try:
                result=ingest.capture(self.root,self.capture_body,self.capture_title)
                self.modal=None;self.reload();self.notice='Gedanke gespeichert · '+result['title']
            except (OSError,ValueError) as exc:self.notice=str(exc)
            return
        if key=='\t':self.capture_field='body' if self.capture_field=='title' else 'title';return
        if key in ('\n','\r',curses.KEY_ENTER):
            if self.capture_field=='title':self.capture_field='body'
            elif len(self.capture_body)<23800:self.capture_body+='\n'
            return
        field='capture_title' if self.capture_field=='title' else 'capture_body'
        value=getattr(self,field)
        if key in (curses.KEY_BACKSPACE,'\x7f','\b'):setattr(self,field,value[:-1])
        elif isinstance(key,str) and key.isprintable() and len(value)<(160 if field=='capture_title' else 23800):setattr(self,field,value+key)

    def open_menu(self, operation, pause=False):
        curses.def_prog_mode()
        curses.endwin()
        try:
            result = operation(self.root)
            if pause:
                print(result['text'])
                input('Press Enter to return to HiveMind > ')
        except (EOFError, KeyboardInterrupt):
            pass
        except (OSError, ValueError, RuntimeError) as error:
            self.notice = clean(str(error))
        finally:
            curses.reset_prog_mode()
            self.screen.refresh()
            self.reload()
            self.clear()

    def handle(self,key):
        if self.modal:self.handle_modal(key);return True
        if key in ('q','\x03'):return False
        if key=='b' and (self.root/'.hivemind.json').is_file():
            if __package__:
                from .automation_ui import Panel
            else:
                sys.path.insert(0,str(BASE.parent))
                from hivemind.automation_ui import Panel
            Panel(self).run()
            self.reload();self.clear()
            return True
        if key=='m':
            if __package__:
                from . import memory_control
            else:
                import memory_control
            self.open_menu(memory_control.interactive)
            return True
        if key=='s' and (self.root / '.hivemind.json').is_file():
            if __package__:
                from . import source_connections
            else:
                sys.path.insert(0, str(BASE.parent))
                from hivemind import source_connections
            args = argparse.Namespace(command='sources', source_action=None, json=False)
            self.open_menu(lambda root: source_connections.execute(args, root), pause=True)
            return True
        if key in ('1','2','3','4','5'):
            self.switch(int(key)-1);return True
        if key in ('\t',curses.KEY_RIGHT,curses.KEY_LEFT):
            self.switch((self.tab+(-1 if key==curses.KEY_LEFT else 1))%len(TABS));return True
        if key=='n':
            self.modal='capture';self.capture_title='';self.capture_body='';self.capture_field='title';self.notice='';return True
        if key=='/':self.modal='search';self.input=self.query;return True
        if key=='s':self.launch('sync');return True
        if key=='r':self.launch('calendar');return True
        if key in ('w','a','e','D'):
            self.open_insight({'w':'explain','a':'impact','e':'lessons','D':'changes'}[key]);return True
        if key in ('y','u') and not self.detail:
            self.search_feedback(key=='y');return True
        if key=='h' and not self.detail:self.history=not self.history;self.reset_view();return True
        if key=='\x1b':
            if self.detail and self.detail[0]=='insight':
                self.detail=self.insight_return;self.insight=None;self.insight_return=None;self.scroll=0;self.detail_cache={}
            elif self.detail:self.detail=None;self.sources=False;self.scroll=0
            elif self.tab==2 and self.sky.timelapse:self.sky.timelapse=None
            elif self.network_focus:self.network_return=self.network_focus;self.network_focus=None;self.reset_view()
            elif self.query:self.query='';self.reset_view()
            return True
        if self.detail:
            if key=='o' and self.detail[0]=='note':self.sources=not self.sources;self.scroll=0
            elif key=='g' and self.detail[0]=='note':
                if self.network_focus!=self.detail[1]:self.trail=[]
                self.network_focus=self.detail[1];self.switch(2)
            elif key in (curses.KEY_DOWN,'j'):self.scroll+=1
            elif key in (curses.KEY_UP,'k'):self.scroll=max(0,self.scroll-1)
            elif key in (curses.KEY_NPAGE,' '):self.scroll+=max(1,self.screen.getmaxyx()[0]-12)
            elif key==curses.KEY_PPAGE:self.scroll=max(0,self.scroll-max(1,self.screen.getmaxyx()[0]-12))
            elif key==curses.KEY_HOME:self.scroll=0
            elif key==curses.KEY_END:self.scroll=10**9
            return True
        if self.tab==2:
            if key=='f':self.immersive=not self.immersive;self.clear();return True
            if key=='z':self.toggle_timelapse();return True
            if key==' ':self.sky.motion=not self.sky.motion;return True
            if key=='l' and self.glow and self.glow.backdrop:self.sky.light=not self.sky.light;return True
            if key in (curses.KEY_BACKSPACE,'\x7f','\b') and self.network_focus:
                self.network_return=self.network_focus;self.network_focus=self.trail.pop() if self.trail else None;self.selection=0;self.top=0;return True
            if key in ('\n','\r',curses.KEY_ENTER) and self.network_focus and self.items:self.trail.append(self.network_focus)
        if key in (curses.KEY_DOWN,'j'):self.selection=min(max(0,len(self.items)-1),self.selection+1)
        elif key in (curses.KEY_UP,'k'):self.selection=max(0,self.selection-1)
        elif key==curses.KEY_NPAGE:self.selection=min(max(0,len(self.items)-1),self.selection+max(1,self.screen.getmaxyx()[0]-12))
        elif key==curses.KEY_PPAGE:self.selection=max(0,self.selection-max(1,self.screen.getmaxyx()[0]-12))
        elif key=='d' and self.network_focus:self.detail=('note',self.network_focus);self.scroll=0
        elif key in ('\n','\r',curses.KEY_ENTER) and self.items:
            selected=self.items[min(self.selection,len(self.items)-1)]
            if selected['kind']=='focus':self.network_focus=selected['id'];self.selection=0;self.top=0
            else:self.detail=(selected['kind'],selected['id']);self.sources=False;self.scroll=0
        return True

    def loop(self):
        if self.automation_entry:
            if __package__:
                from .automation_ui import Panel
            else:
                sys.path.insert(0,str(BASE.parent))
                from hivemind.automation_ui import Panel
            Panel(self).run()
            return 0
        while True:
            try:
                message=self.work.get_nowait();self.busy=False;self.reload();self.notice=message
            except queue.Empty:pass
            try:
                fresh=self.fresh.get_nowait();self.loading=False
                if isinstance(fresh,dict):
                    self.data=fresh;self.note_cache={};self.detail_cache={}
                    if fresh['errors']:self.notice=fresh['errors'][0]
                else:self.notice='Aktualisierung fehlgeschlagen: '+str(fresh)
            except queue.Empty:pass
            if time.monotonic()-self.last_refresh>=10 and not self.modal:
                if self.animated():self.reload_later()
                else:self.reload()
            began=time.monotonic();self.paint()
            # Animation paces frames; a still screen waits for keys as before.
            self.screen.timeout(max(1,int(((GLOW_FRAME if self.glow else FRAME)-(time.monotonic()-began))*1000)) if self.animated() else 150)
            try:key=self.screen.get_wch()
            except curses.error:continue
            self.last_key=time.monotonic()
            if key==curses.KEY_RESIZE:
                self.clear();continue
            if not self.handle(key):return 0


def run(root=None, initial_view='overview'):
    root=Path(root or default_vault()).resolve()
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get('TERM') in (None,'','dumb'):
        data=snapshot(root);sys.stdout.write(render_plain(root,data=data));return 0 if data['valid'] else 1
    try:
        locale.setlocale(locale.LC_ALL,'')
    except locale.Error:pass
    # macOS links ncurses 6.0, which repeats a wide character through terminfo `rep` as a
    # single byte; Ghostty then shows �. Such terminals get the equivalent xterm entry.
    try:
        curses.setupterm()
        if curses.ncurses_version<(6,1) and curses.tigetstr('rep'):os.environ['TERM']='xterm-256color'
    except (curses.error,AttributeError):pass
    colour=backdrop()
    try:
        return curses.wrapper(lambda screen:Interface(screen,root,initial_view,colour).loop())
    except KeyboardInterrupt:
        return 0
    except curses.error as exc:
        print('Das Terminal konnte nicht initialisiert werden: '+str(exc),file=sys.stderr)
        print(render_plain(root),end='');return 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=default_vault())
    parser.add_argument('--plain',action='store_true',help='Einen lokalen Textstand ausgeben, ohne interaktive Oberfläche.')
    args=parser.parse_args()
    if args.plain:
        data=snapshot(args.root);print(render_plain(args.root,data=data),end='');raise SystemExit(0 if data['valid'] else 1)
    raise SystemExit(run(args.root))

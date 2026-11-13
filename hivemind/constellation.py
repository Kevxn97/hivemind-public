#!/usr/bin/env python3
"""HiveMind's Netz: the knowledge as living tissue. Notes are cells, links nerve fibres,
and impulses run along the real links from cell to cell.

Standard library only and independent of curses: `Sky.frame()` returns positioned
text runs with a brightness level or palette name, and the terminal decides how they
look. A terminal with 24-bit colour can instead take the canvas, including the light
that cells, impulses and flashes cast, and paint it cell by cell with its own colour
tables. The shape derives from note IDs and links alone and the impulses from a seeded
generator, so the same knowledge always forms the same body. Nothing here reads files,
starts processes or calls providers.
"""
import datetime as dt
import functools
import hashlib
import math
import random
import time
import unicodedata

if __package__:
    from . import hive
else:
    import hive

LEVELS = 16
REVERSE = 32
DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
AREAS = {'projects': 'PROJEKTE', 'work': 'ARBEIT', 'personal': 'PERSÖNLICH', 'people': 'PERSONEN', 'knowledge': 'WISSEN', 'system': 'BETRIEB'}
AXES = {'projects': (1, 0, 0), 'work': (0, 0, 1), 'personal': (-1, 0, 0), 'people': (0, 1, 0), 'knowledge': (0, -1, 0), 'system': (0, 0, -1)}
TYPES = {'person': 'Person', 'preference': 'Präferenz', 'work': 'Arbeit', 'project': 'Projekt', 'decision': 'Entscheidung', 'lesson': 'Erfahrung', 'system': 'System'}
EVIDENCE = {'confirmed': 'bestätigt', 'reported': 'berichtet', 'inferred': 'abgeleitet', 'disputed': 'umstritten'}
RELATIONS = {'about': 'über', 'related': 'verwandt', 'depends_on': 'hängt ab von', 'supersedes': 'ersetzt', 'contradicts': 'widerspricht'}
# Impulse colours from a deep tail to the icy head; terminal_ui maps them to a blue ramp.
SPARK = ('abyss', 'deep', 'tide', 'azure', 'ice', 'spark')
TAIL = ('spark', 'ice', 'ice', 'azure', 'azure', 'azure', 'tide', 'tide', 'deep')
REFRACTORY, PULSES = 1.4, 160
SECTORS = ('personal', 'people', 'work', 'projects', 'knowledge', 'system')
LINK, CHARGE, COHESION, GRAVITY, DECAY = 30.0, -60.0, .06, .02, .6
ALPHA_DECAY = 1-.001**(1/300)
REPLAY, HOLD = 16.0, 3.0

# Five-row bitmaps; the wordmark's ANSI-shadow face is derived from them below.
BIG = {
    'A': (' ##### ', '##   ##', '#######', '##   ##', '##   ##'),
    'B': ('###### ', '##   ##', '###### ', '##   ##', '###### '),
    'C': (' ######', '##     ', '##     ', '##     ', ' ######'),
    'D': ('###### ', '##   ##', '##   ##', '##   ##', '###### '),
    'E': ('#######', '##     ', '#####  ', '##     ', '#######'),
    'F': ('#######', '##     ', '#####  ', '##     ', '##     '),
    'G': (' ###### ', '##      ', '##   ###', '##    ##', ' ###### '),
    'H': ('##   ##', '##   ##', '#######', '##   ##', '##   ##'),
    'I': ('##', '##', '##', '##', '##'),
    'J': ('     ##', '     ##', '     ##', '##   ##', ' ##### '),
    'K': ('##   ##', '##  ## ', '#####  ', '##  ## ', '##   ##'),
    'L': ('##     ', '##     ', '##     ', '##     ', '#######'),
    'M': ('###    ###', '####  ####', '## #### ##', '##  ##  ##', '##      ##'),
    'N': ('###    ##', '####   ##', '## ##  ##', '##  ## ##', '##   ####'),
    'O': (' ###### ', '##    ##', '##    ##', '##    ##', ' ###### '),
    'P': ('###### ', '##   ##', '###### ', '##     ', '##     '),
    'Q': (' ###### ', '##    ##', '##    ##', '##   ###', ' #######'),
    'R': ('###### ', '##   ##', '###### ', '##   ##', '##   ##'),
    'S': ('#######', '##     ', '#######', '     ##', '#######'),
    'T': ('########', '   ##   ', '   ##   ', '   ##   ', '   ##   '),
    'U': ('##    ##', '##    ##', '##    ##', '##    ##', ' ###### '),
    'V': ('##    ##', '##    ##', '##    ##', ' ##  ## ', '  ####  '),
    'W': ('##     ##', '##     ##', '##  #  ##', '## ### ##', ' ### ### '),
    'X': ('##   ##', ' ## ## ', '  ###  ', ' ## ## ', '##   ##'),
    'Y': ('##    ##', ' ##  ## ', '  ####  ', '   ##   ', '   ##   '),
    'Z': ('#######', '   ### ', '  ###  ', ' ###   ', '#######'),
    '0': (' ##### ', '##   ##', '##   ##', '##   ##', ' ##### '),
    '1': (' ##', '###', ' ##', ' ##', ' ##'),
    '2': ('###### ', '     ##', ' ##### ', '##     ', '#######'),
    '3': ('###### ', '     ##', ' ##### ', '     ##', '###### '),
    '4': ('##   ##', '##   ##', '#######', '     ##', '     ##'),
    '5': ('#######', '##     ', '###### ', '     ##', '###### '),
    '6': (' ##### ', '##     ', '###### ', '##   ##', ' ##### '),
    '7': ('#######', '     ##', '    ## ', '   ##  ', '   ##  '),
    '8': (' ##### ', '##   ##', ' ##### ', '##   ##', ' ##### '),
    '9': (' ##### ', '##   ##', ' ######', '     ##', ' ##### '),
    ' ': ('  ', '  ', '  ', '  ', '  '),
    '.': ('  ', '  ', '  ', '  ', '##'),
    ',': ('  ', '  ', '  ', '##', ' #'),
    ':': ('  ', '##', '  ', '##', '  '),
    '-': ('     ', '     ', '#####', '     ', '     '),
    '+': ('      ', '  ##  ', '######', '  ##  ', '      '),
    '/': ('    ##', '   ## ', '  ##  ', ' ##   ', '##    '),
    '(': (' ##', '## ', '## ', '## ', ' ##'),
    ')': ('## ', ' ##', ' ##', ' ##', '## '),
    "'": ('##', ' #', '  ', '  ', '  '),
    '!': ('##', '##', '##', '  ', '##'),
    '?': ('###### ', '     ##', '  #### ', '       ', '  ##   '),
}
MARKS = {'Ä': ('A', ' ▄▄ ▄▄  '), 'Ö': ('O', ' ▄▄  ▄▄  '), 'Ü': ('U', '▄▄    ▄▄ ')}


def _hash(text):
    """Stable floats in [0, 1); Python's own hash() is salted per process."""
    raw=hashlib.sha256(text.encode()).digest()
    return [int.from_bytes(raw[i:i+4],'big')/4294967296 for i in range(0,32,4)]


@functools.lru_cache(maxsize=8)
def _grain(w, h):
    """Fixed per-dot thresholds (0–255). A dot appears only where its intensity beats the threshold,
    so soft gradients become stable, film-like grain instead of solid blocks."""
    out=bytearray(w*h)
    for i in range(w*h):
        v=(i*2654435761)&0xffffffff;v^=v>>15;v=(v*2246822519)&0xffffffff;v^=v>>13;out[i]=v&255
    return out


@functools.lru_cache(maxsize=8)
def _jitter(cols, rows):
    """Per-cell grain as fractions in [0, 1), for dithering the swell."""
    return [value/256 for value in _grain(cols,rows)]


BRAILLE = [chr(0x2800+bits) for bits in range(256)]
# Light kept at 0–3 quarters, for fading it out around fixed text.
FADE = [bytes(value*k>>2 for value in range(256)) for k in range(4)]


@functools.lru_cache(maxsize=512)
def _disc(outer, hole):
    """Dot offsets (dx, dy, distance) of a disc of radius outer/2 outside hole/2; glows reuse them."""
    reach=outer//2+1
    return tuple((dx,dy,d) for dy in range(-reach,reach+1) for dx in range(-reach,reach+1) for d in (math.sqrt(dx*dx+dy*dy),) if hole/2<=d<=outer/2)


@functools.lru_cache(maxsize=None)
def _mote(index):
    return _hash(f'mote:{index}')


def _ease(value):
    value=min(1.0,max(0.0,value));return value*value*(3-2*value)


def _clean(value):
    """Knowledge is display content, never terminal control (same rule as terminal_ui.clean)."""
    return ''.join(c if ord(c)>=32 and ord(c)!=127 and unicodedata.category(c)!='Cc' else ' ' for c in str(value))


def _span(char):
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W','F') else 1


@functools.lru_cache(maxsize=4096)
def _cells(text):
    """Display cells of a text as (character, width) pairs; frames repeat most texts."""
    return tuple((char,_span(char)) for char in _clean(text) if _span(char))


@functools.lru_cache(maxsize=4096)
def _clip(text, width):
    text=_clean(text)
    if sum(map(_span,text))<=width:return text
    result='';used=0
    for char in text:
        if used+_span(char)>width-1:break
        result+=char;used+=_span(char)
    return result+'…' if width>0 else ''


def _wrap(text, width, limit):
    lines=['']
    for word in _clean(text).split():
        if lines[-1] and sum(map(_span,lines[-1]+' '+word))>width:lines.append('')
        lines[-1]=(lines[-1]+' '+word).strip()
    if len(lines)>limit:lines=lines[:limit];lines[-1]=_clip(lines[-1]+' …',width)
    return [_clip(line,width) for line in lines if line]


def _stale(note):
    try:return hive.freshness(note)=='stale'
    except (KeyError,ValueError,TypeError,AttributeError):return False


def short(title):
    """The name part of a note title: before ' · ' or a colon."""
    head=_clean(title).split(' · ')[0].split(': ')[0].strip()
    return head or _clean(title)


def merge(rows):
    """One row per neighbour: both directions become ↔; relations and reasons are combined."""
    merged={}
    for row in rows:
        entry=merged.setdefault(row['id'],{**row,'direction':row.get('direction',''),'relation':[],'reason':[]})
        if entry['direction']!=row.get('direction',''):entry['direction']='↔'
        for field,value in (('relation',RELATIONS.get(row.get('relation'),row.get('relation',''))),('reason',row.get('reason',''))):
            if value and value not in entry[field]:entry[field].append(value)
    return sorted(({**entry,'relation':' · '.join(entry['relation']),'reason':' · '.join(entry['reason'])} for entry in merged.values()),key=lambda row:(row.get('title','').casefold(),row['id']))


def monumental(text):
    """Capitals in the wordmark's ANSI-shadow face; None when a character has no glyph."""
    glyphs=[];marks=''
    for char in str(text).upper():
        base,mark=MARKS.get(char,(char,None))
        if base not in BIG:base=unicodedata.normalize('NFD',base)[:1]
        if base not in BIG:return None
        glyphs.append(BIG[base]);marks+=mark or ' '*(len(BIG[base][0])+1)
    if not glyphs:return None
    bitmap=[''.join(rows[r]+' ' for rows in glyphs) for r in range(5)]
    block=lambda r,c:0<=r<5 and c>=0 and bitmap[r][c]=='#'
    lines=[]
    for r in range(6):
        line=''
        for c in range(len(bitmap[0])):
            if block(r,c):line+='█';continue
            left,up,corner=block(r,c-1),block(r-1,c),block(r-1,c-1)
            line+='╔' if left and up else ('║' if corner else '╗') if left else ('═' if corner else '╚') if up else '╝' if corner else ' '
        lines.append(line)
    return ([marks] if marks.strip() else [])+lines


class Canvas:
    """Braille dots (2 × 4 per cell) beneath a text layer; each cell keeps its brightest dot,
    and a tinted dot colours its cell with a palette name instead of a gray level. `light`, when
    a terminal can show it, holds the glow behind the dots per half cell (2 × 2 dots), 0–255."""

    def __init__(self, cols, rows):
        self.cols=cols;self.rows=rows;self.w=cols*2;self.h=rows*4;self.text={};self.tint={};self.swell=None;self.grain=_grain(cols*2,rows*4)
        self.mask=bytearray(cols*rows);self.level=bytearray(cols*rows);self.used=bytearray(cols*rows);self.light=None

    def clone(self):
        other=Canvas.__new__(Canvas);other.cols=self.cols;other.rows=self.rows;other.w=self.w;other.h=self.h
        other.swell=self.swell;other.grain=self.grain;other.text=dict(self.text);other.tint=dict(self.tint);other.mask=bytearray(self.mask);other.level=bytearray(self.level);other.used=bytearray(self.used)
        other.light=None if self.light is None else bytearray(self.light)
        return other

    def lamp(self, x, y, radius, power):
        """Soft light around a dot position, `power` (up to 255) at the centre falling smoothly to
        nothing at `radius` dots. Lights combine like screens, so overlaps brighten without clipping."""
        light=self.light
        if light is None or power<1 or radius<1:return
        cols=self.cols;inverse=1/(radius*radius);power=min(255.0,power)
        for py in range(max(0,int((y-radius)/2)),min(self.rows*2,int((y+radius)/2)+1)):
            dy=2*py+1-y;rest=1-dy*dy*inverse
            if rest<=0:continue
            base=py*cols;half=radius*math.sqrt(rest)
            for px in range(max(0,int((x-half)/2)),min(cols,int((x+half)/2)+1)):
                dx=2*px+1-x;q=rest-dx*dx*inverse
                if q>0:value=light[base+px];add=power*q*q;light[base+px]=int(value+add-value*add/255)

    def dot(self, x, y, level, tint=None):
        x=int(x);y=int(y)
        if 0<=x<self.w and 0<=y<self.h and level>=0:
            cell=(y>>2)*self.cols+(x>>1);self.mask[cell]|=DOTS[y&3][x&1]
            if tint:
                if level>=self.level[cell]:self.level[cell]=min(LEVELS-1,level);self.tint[cell]=tint
            elif level>self.level[cell]:self.level[cell]=min(LEVELS-1,level)

    def curve(self, x0, y0, cx, cy, x1, y1, l0, l1, step=1, start=0.0, end=1.0, fade=1.0):
        """Quadratic Bézier in dots, brightness blending from l0 to l1; step > 1 is dotted, and
        fade < 1 thins the dots towards the end into grain."""
        cols=self.cols;w=self.w;h=self.h;mask=self.mask;level=self.level;grain=self.grain
        n=int(max(abs(x1-x0),abs(y1-y0),abs(2*cx-x0-x1),abs(2*cy-y0-y1))*1.1)+2
        for i in range(int(start*n),int(end*n)+1,step):
            t=i/n;u=1-t;a=u*u;b=2*u*t;c=t*t
            x=int(a*x0+b*cx+c*x1);y=int(a*y0+b*cy+c*y1)
            if 0<=x<w and 0<=y<h and (fade>=1 or (1-(1-fade)*t)*255>grain[y*w+x]):
                cell=(y>>2)*cols+(x>>1);mask[cell]|=DOTS[y&3][x&1];value=int(l0+(l1-l0)*t)
                if value>level[cell]:level[cell]=value

    def strand(self, x0, y0, cx, cy, x1, y1, edge, mid, step=1, start=0.0, end=1.0, thin=1.0):
        """A tapered fibre along a quadratic Bézier: `edge` brightness where it meets its cells, `mid`
        between; `thin` is the dot density at the middle, rising to full at the cells."""
        cols=self.cols;w=self.w;h=self.h;mask=self.mask;level=self.level;grain=self.grain
        n=int(max(abs(x1-x0),abs(y1-y0),abs(2*cx-x0-x1),abs(2*cy-y0-y1))*1.1)+2
        for i in range(int(start*n),int(end*n)+1,step):
            t=i/n;u=1-t;a=u*u;b=2*u*t;c=t*t
            x=int(a*x0+b*cx+c*x1);y=int(a*y0+b*cy+c*y1)
            if 0<=x<w and 0<=y<h:
                taper=abs(2*t-1)**1.6
                if thin<1 and (thin+(1-thin)*taper)*255<=grain[y*w+x]:continue
                cell=(y>>2)*cols+(x>>1);mask[cell]|=DOTS[y&3][x&1];value=int(mid+(edge-mid)*taper)
                if value>level[cell]:level[cell]=value

    def ring(self, x, y, radius, level, step=1, phase=0.0, tint=None, strength=1.0):
        """A circle of dots; strength < 1 dissolves it into grain."""
        count=max(8,int(radius*7.5));w=self.w;h=self.h;grain=self.grain
        for i in range(0,count,step):
            angle=phase+6.283185307*i/count;px=x+radius*math.cos(angle);py=y+radius*math.sin(angle)
            if strength>=1 or 0<=px<w and 0<=py<h and strength*255>grain[int(py)*w+int(px)]:self.dot(px,py,level,tint)

    def trace(self, x0, y0, cx, cy, x1, y1, level, tint, density=1.0, start=0.0, end=1.0):
        """Tinted dots along part of a quadratic Bézier; density < 1 lets it fall apart into grain."""
        w=self.w;h=self.h;grain=self.grain
        n=int(max(abs(x1-x0),abs(y1-y0),abs(2*cx-x0-x1),abs(2*cy-y0-y1))*1.1)+2
        for i in range(int(max(0.0,start)*n),int(min(1.0,end)*n)+1):
            t=i/n;u=1-t;x=u*u*x0+2*u*t*cx+t*t*x1;y=u*u*y0+2*u*t*cy+t*t*y1
            if density>=1 or 0<=x<w and 0<=y<h and density*255>grain[int(y)*w+int(x)]:self.dot(x,y,level,tint)

    def glow(self, x, y, inner, outer, level, tint=None, strength=1.0, low=1, hole=0.0):
        """A soft, grainy disc: full inside `inner`, thinning out towards `outer`, empty within `hole`;
        brightness falls from `level` to `low` with the density."""
        grain=self.grain;w=self.w;h=self.h;cols=self.cols;mask=self.mask;lit=self.level;tints=self.tint
        cx=int(x+.5);cy=int(y+.5);span=max(.5,outer-inner)
        for dx,dy,d in _disc(int(outer*2+.5),int(hole*2)):
            px=cx+dx;py=cy+dy
            if 0<=px<w and 0<=py<h:
                v=strength if d<=inner else strength*max(0.0,1-(d-inner)/span)**1.5
                if v*255>grain[py*w+px]:
                    value=min(LEVELS-1,int(low+(level-low)*min(1.0,v)));cell=(py>>2)*cols+(px>>1);mask[cell]|=DOTS[py&3][px&1]
                    if tint:
                        if value>=lit[cell]:lit[cell]=value;tints[cell]=tint
                    elif value>lit[cell]:lit[cell]=value

    def plate(self, row, col, width, height=1, fixed=False):
        """Reserve cells for text: dots beneath vanish; labels avoid them and never cover fixed ones.
        Fixed text (header, caption, panel) also darkens the light around it, fading over three
        half cells, so hidden cells leave no glow behind."""
        for r in range(max(0,row),min(self.rows,row+height)):
            for c in range(max(0,col),min(self.cols,col+width)):
                index=r*self.cols+c;self.used[index]=max(self.used[index],2 if fixed else 1);self.mask[index]=0
        light=self.light;cols=self.cols;left=max(0,col);right=min(cols,col+width);top=2*row;bottom=2*(row+height)
        if not fixed or light is None or left>=right:return
        for py in range(max(0,top-3),min(2*self.rows,bottom+3)):
            base=py*cols;rim=max(top-py,py-bottom+1,0)
            light[base+left:base+right]=light[base+left:base+right].translate(FADE[rim])
            for px in [*range(max(0,left-3),left),*range(right,min(cols,right+3))]:
                light[base+px]=light[base+px]*min(3,max(rim,left-px,px-right+1))>>2

    def free(self, row, col, width, below=1):
        """True when the span lies inside and no cell there is reserved at level `below` or above."""
        if row<0 or row>=self.rows or col<0 or col+width>self.cols:return False
        base=row*self.cols+col
        return max(self.used[base:base+width])<below

    def put(self, row, col, text, style, fixed=False):
        if not 0<=row<self.rows:return 0
        x=col
        for char,span in _cells(text):
            if x+span>self.cols:break
            if x>=0:
                # Covering half of a wide glyph blanks its other half instead of shifting the row.
                first=row*self.cols+x;last=first+span-1;head=self.text.get(last)
                if self.text.get(first,('?',0))[0]=='':self.text[first-1]=(' ',self.text[first-1][1])
                if head and head[0] and _span(head[0])==2:self.text[last+1]=(' ',head[1])
                for k in range(span):
                    index=first+k;self.text[index]=(char if k==0 else '',style);self.used[index]=max(self.used[index],2 if fixed else 1);self.mask[index]=0
            x+=span
        return x-col

    def _breath(self):
        """swell = (phase, amplitude): a slow diagonal wave of brightness that makes the dots breathe;
        per-cell grain dithers its edges so the wave dissolves rather than steps. Returns the shade
        per diagonal, the shifted level ramps and the grain, or None while the dots rest."""
        if not self.swell:return None
        phase,amplitude=self.swell
        return ([amplitude+amplitude*math.sin(phase-i*.11) for i in range(self.cols+2*self.rows+1)],
                [[max(0,min(LEVELS-1,value+offset-amplitude)) for value in range(LEVELS)] for offset in range(2*amplitude+1)],_jitter(self.cols,self.rows))

    def runs(self):
        """Text runs per row; short blank gaps join a run of the same style to save terminal writes."""
        result=[];cols=self.cols;mask=self.mask;level=self.level;used=self.used;text=self.text;tint=self.tint;swell=self._breath();braille=BRAILLE
        if swell:shade,ramp,jitter=swell
        for row in range(self.rows):
            base=row*cols;run=[];style=None;start=0;gap=0
            # Only the stretch between the first and last occupied cell of a row needs a look.
            dots=mask[base:base+cols];held=used[base:base+cols]
            first=min(cols-len(dots.lstrip(b'\0')),cols-len(held.lstrip(b'\0')))
            if first>=cols:continue
            for col in range(first,cols-min(cols-len(dots.rstrip(b'\0')),cols-len(held.rstrip(b'\0')))):
                index=base+col;cell=text.get(index) if used[index] else None
                if cell is not None:
                    char,kind=cell
                    if not char:continue
                elif mask[index]:
                    char=braille[mask[index]];kind=tint.get(index)
                    if kind is None:kind=ramp[int(shade[col+2*row]+jitter[index])][level[index]] if swell else level[index]
                else:
                    gap+=1
                    if run and gap>2:result.append((row,start,''.join(run[:len(run)-gap+1]),style));run=[]
                    if run:run.append(' ')
                    continue
                if run and kind==style:run.append(char)
                else:
                    if run:result.append((row,start,''.join(run[:len(run)-gap] if gap else run),style))
                    run=[char];style=kind;start=col
                gap=0
            if run:result.append((row,start,''.join(run[:len(run)-gap] if gap else run),style))
        return result

    def paint(self, look):
        """Every row as cell tokens (fg, bg, character) from the terminal's colour tables in `look`:
        `dot[level][light>>4]` and `tinted()` colour dots, `text()` gives text its (fg, bg), `wash`
        turns light into a background and `haze` into the upper half of a ▀ where the light alone
        splits a cell into two tones. `look.empty` fills untouched cells; (None, None, '') follows
        a wide glyph."""
        cols=self.cols;mask=self.mask;level=self.level;used=self.used;text=self.text;tint=self.tint;braille=BRAILLE
        dot=look.dot;wash=look.wash;haze=look.haze;tinted=look.tinted;ink=look.text;empty=look.empty;swell=self._breath()
        if swell:shade,ramp,jitter=swell
        light=self.light if self.light is not None else bytes(2*cols*self.rows);result=[]
        for row in range(self.rows):
            base=row*cols;line=[empty]*cols;result.append(line)
            dots=mask[base:base+cols];held=used[base:base+cols];upper=light[2*base:2*base+cols];lower=light[2*base+cols:2*base+2*cols]
            parts=(dots,held,upper,lower)
            for col in range(min(cols-len(part.lstrip(b'\0')) for part in parts),max(len(part.rstrip(b'\0')) for part in parts)):
                index=base+col;top=upper[col];bottom=lower[col];cell=text.get(index) if held[col] else None
                if cell is not None:line[col]=ink(cell[1],(top+bottom)>>1)+(cell[0],) if cell[0] else (None,None,'')
                elif dots[col]:
                    kind=tint.get(index);glow=(top if top>bottom else bottom)>>4
                    if kind is None:fg=dot[ramp[int(shade[col+2*row]+jitter[index])][level[index]] if swell else level[index]][glow]
                    else:fg=tinted(kind,level[index],glow)
                    line[col]=(fg,wash[(top+bottom)>>1],braille[dots[col]])
                elif top>>3!=bottom>>3:line[col]=(haze[top],wash[bottom],'▀')
                elif top or bottom:line[col]=(None,wash[(top+bottom)>>1],' ')
        return result


class Layout:
    """Deterministic 3-D force layout with d3-force semantics, a few ticks per frame."""

    def __init__(self):
        self.ids=[];self.groups=[];self.links=[];self.alpha=0.0
        self.x=[];self.y=[];self.z=[];self.vx=[];self.vy=[];self.vz=[]

    def sync(self, ids, groups, pairs):
        """Known stars keep their place; new ones start beside a linked star or in their area."""
        old={ident:(self.x[i],self.y[i],self.z[i]) for i,ident in enumerate(self.ids)}
        index={ident:i for i,ident in enumerate(ids)};count=[0]*len(ids);partners={}
        for a,b in pairs:
            count[index[a]]+=1;count[index[b]]+=1
            partners.setdefault(a,[]).append(b);partners.setdefault(b,[]).append(a)
        self.links=[(index[a],index[b],1/min(count[index[a]],count[index[b]]),count[index[a]]/(count[index[a]]+count[index[b]])) for a,b in pairs]
        self.x=[];self.y=[];self.z=[]
        for ident,group in zip(ids,groups):
            u=_hash(ident);near=[old[p] for p in partners.get(ident,[]) if p in old]
            if ident in old:point=old[ident]
            elif near:point=tuple(sum(p[k] for p in near)/len(near)+(u[k]-.5)*12 for k in range(3))
            else:point=tuple(c*45+(u[k]-.5)*30 for k,c in enumerate(AXES.get(group,(0,0,0))))
            self.x.append(point[0]);self.y.append(point[1]);self.z.append(point[2])
        self.ids=list(ids);self.groups=list(groups);self.vx=[0.0]*len(ids);self.vy=[0.0]*len(ids);self.vz=[0.0]*len(ids)
        self.alpha=1.0 if not old else max(self.alpha,.3)

    def settled(self):
        return self.alpha<.004

    def tick(self):
        self.alpha+=(0-self.alpha)*ALPHA_DECAY;a=self.alpha
        X,Y,Z,VX,VY,VZ=self.x,self.y,self.z,self.vx,self.vy,self.vz;n=len(X)
        for s,t,strength,bias in self.links:
            dx=X[t]+VX[t]-X[s]-VX[s];dy=Y[t]+VY[t]-Y[s]-VY[s];dz=Z[t]+VZ[t]-Z[s]-VZ[s]
            length=math.sqrt(dx*dx+dy*dy+dz*dz) or 1e-6;k=(length-LINK)/length*a*strength
            dx*=k;dy*=k;dz*=k
            VX[t]-=dx*bias;VY[t]-=dy*bias;VZ[t]-=dz*bias;VX[s]+=dx*(1-bias);VY[s]+=dy*(1-bias);VZ[s]+=dz*(1-bias)
        charge=CHARGE*a
        for i in range(n):
            xi=X[i];yi=Y[i];zi=Z[i];fx=fy=fz=0.0
            for j in range(i+1,n):
                dx=X[j]-xi;dy=Y[j]-yi;dz=Z[j]-zi;d2=dx*dx+dy*dy+dz*dz
                w=charge/(d2 if d2>1 else 1)
                fx+=dx*w;fy+=dy*w;fz+=dz*w;VX[j]-=dx*w;VY[j]-=dy*w;VZ[j]-=dz*w
            VX[i]+=fx;VY[i]+=fy;VZ[i]+=fz
        sums={}
        for i,group in enumerate(self.groups):
            total=sums.setdefault(group,[0.0,0.0,0.0,0]);total[0]+=X[i];total[1]+=Y[i];total[2]+=Z[i];total[3]+=1
        for i,group in enumerate(self.groups):
            total=sums[group];pull=COHESION*a;anchor=GRAVITY*a
            VX[i]+=(total[0]/total[3]-X[i])*pull-X[i]*anchor;VY[i]+=(total[1]/total[3]-Y[i])*pull-Y[i]*anchor;VZ[i]+=(total[2]/total[3]-Z[i])*pull-Z[i]*anchor
        for i in range(n):
            VX[i]*=DECAY;VY[i]*=DECAY;VZ[i]*=DECAY;X[i]+=VX[i];Y[i]+=VY[i];Z[i]+=VZ[i]

    def normalized(self, linked):
        """Centre and size come from linked stars; unlinked ones drift outward and would set the scale."""
        core=[i for i,ident in enumerate(self.ids) if ident in linked] or list(range(len(self.ids)))
        if not core:return {},1.0
        cx,cy,cz=(sum(axis[i] for i in core)/len(core) for axis in (self.x,self.y,self.z))
        points={ident:(self.x[i]-cx,self.y[i]-cy,self.z[i]-cz) for i,ident in enumerate(self.ids)}
        radii=sorted(math.sqrt(sum(v*v for v in points[self.ids[i]])) for i in core)
        return points,radii[int(.92*(len(radii)-1))] or 1.0


class Sky:
    """Netz between frames: the knowledge as living tissue. Notes are cells and links nerve fibres;
    impulses run along the real links, and a focus draws its neighbours close."""

    def __init__(self):
        # `light`: the terminal can show the glow cells and impulses cast (see Canvas.paint).
        self.layout=Layout();self.motion=True;self.timelapse=None;self.light=False
        self.clock=None;self.selected=None;self.selected_at=-9.0;self.focus=None;self.focus_at=-9.0
        self.key=None;self.graph=None;self.edges=[];self.kinds={};self.bends={};self.degree={};self.names={};self.links={};self.areas={};self.hubs=set()
        self.recent=[];self.recent_at=-99.0;self.chart={};self.charted=None;self.still=(None,[])
        # Life: simulated seconds, impulses in flight, recent firings and the shape in motion.
        self.age=0.0;self.tick_at=None;self.rng=random.Random(7);self.pulses=[];self.fired={};self.lit={}
        self.beat=-99.0;self.wave=5.0;self.speed=60.0;self.seen=-1.0;self.sparks=random.Random(11);self.twinkle=(-1.0,[])
        # A train of thought along real links, and the awakening when Netz opens.
        self.thought=None;self.waking=None;self.wake_pending=True
        self.shown={};self.morph=None;self.target=();self.body=(None,None)

    def sync(self, notes):
        """Adopt the visible notes; only a change of IDs, areas or links moves the layout."""
        key=tuple(sorted((ident,note['title'],hive.cluster_for(note),tuple(sorted((link.get('target'),link.get('relation')) for link in note.get('links',[])))) for ident,note in notes.items()))
        if key==self.key:return
        self.key=key;edges=set();heads={}
        for ident,_,_,links in key:
            for target,relation in links:
                if target in notes and target!=ident:edges.add((ident,target,relation))
        self.edges=sorted(edges);self.degree={ident:0 for ident in notes};self.links={ident:set() for ident in notes};self.kinds={}
        for origin,target,relation in self.edges:
            self.degree[origin]+=1;self.degree[target]+=1;self.links[origin].add(target);self.links[target].add(origin)
            pair=(origin,target) if origin<target else (target,origin)
            self.kinds[pair]='related' if relation=='related' and self.kinds.get(pair,'related')=='related' else 'strong'
        self.bends={pair:(_hash(pair[0]+'>'+pair[1])[0]-.5)*.5 for pair in self.kinds}
        self.hubs={ident for ident in sorted(self.degree,key=lambda ident:-self.degree[ident])[:9] if self.degree[ident]>=3}
        ids=sorted(notes);groups=[hive.cluster_for(notes[ident]).split('/')[0] for ident in ids];pairs=sorted(self.kinds)
        self.areas=dict(zip(ids,groups))
        if (ids,groups,pairs)!=self.graph:
            self.graph=(ids,groups,pairs);self.layout.sync(ids,groups,pairs);self.pulses=[];self.body=(None,None)
            # The tissue keeps its shape, so settle up front within a short budget; later frames finish the rest.
            deadline=time.monotonic()+.6
            while not self.layout.settled() and time.monotonic()<deadline:self.layout.tick()
        # Shared prefixes such as "Example" or "Tool" name their cells by the rest of the title.
        for ident in ids:heads.setdefault(short(notes[ident]['title']).casefold(),[]).append(ident)
        self.names={}
        for ident in ids:
            title=_clean(notes[ident]['title']);head=short(title);rest=title[len(head):].lstrip(' ·:') if title.startswith(head) else ''
            shared=len(heads[head.casefold()])>1 and ident!='-'.join(''.join(c if c.isalnum() else ' ' for c in head.casefold()).split())
            self.names[ident]=rest if rest and shared else head
        self.recent_at=-99.0

    def order(self, notes, matches=None):
        """Strongest first: cells by number of connections, then title."""
        return sorted((ident for ident in notes if matches is None or ident in matches),key=lambda ident:(-self.degree.get(ident,0),notes[ident]['title'].casefold(),ident))

    def arrange(self, rows):
        """Focus order: clockwise by area, the most connected first within each."""
        rank={area:index for index,area in enumerate(SECTORS)}
        return sorted(rows,key=lambda row:(rank.get(self.areas.get(row['id']),len(rank)),-self.degree.get(row['id'],0),row.get('title','').casefold()))

    def start_timelapse(self, events, now):
        """events: (time, created IDs, touched IDs, reason, actor) per journaled change, oldest first."""
        events=[event for event in events if event[2]]
        if not events:return False
        first=events[0][0];span=max(1.0,(events[-1][0]-first).total_seconds());born={};timeline=[]
        for at,created,touched,reason,actor in events:
            second=(at-first).total_seconds()/span*REPLAY;timeline.append((second,at,touched,reason,actor))
            for ident in created:born.setdefault(ident,second)
        self.timelapse={'start':now,'first':first,'span':span,'born':born,'events':timeline}
        self.pulses=[];self.lit={};self.seen=-1.0
        return True

    def wake(self):
        """Opening Netz wakes the tissue on the next frame: cells ignite outward from the strongest one."""
        self.wake_pending=True

    def animating(self, awake=True):
        """Whether the next frame differs: the tissue lives while motion is on and someone is near."""
        now=self.clock or 0.0
        return bool(self.timelapse or (self.motion and awake) or self.morph or self.waking or not self.layout.settled() or now-self.selected_at<.6 or now-self.focus_at<.9)

    def frame(self, cols, rows, notes, items, index, focus=None, matches=None, trail=(), now=0.0, awake=True, version=None, framed=True, header=(), cells=False):
        """One frame as (row, column, text, style) runs; style is a gray level (optionally | REVERSE)
        or a palette name. With `cells` the Canvas itself, for terminals that paint each cell.

        `version` names the loaded knowledge; with it, a resting picture repeats its last frame.
        `framed` adds the side panel; without it the tissue fills the screen beneath `header`,
        lines of (text, style[, right-aligned text]) drawn top-left.
        """
        cols=max(1,cols);rows=max(1,rows);canvas=Canvas(cols,rows)
        if not notes:
            canvas.put(rows//2,2,'Noch keine Notizen. Sobald Wissen entsteht, wächst hier sein Netz.',8);return canvas if cells else canvas.runs()
        signature=(version,cols,rows,tuple(row.get('id') for row in items),index,focus,None if matches is None else frozenset(matches),tuple(trail),self.motion and awake,framed,tuple(header),cells,self.light)
        if version is not None and signature==self.still[0] and not self.animating(awake):return self.still[1]
        self.sync(notes);self.clock=now;moving=self.motion and awake
        lapse=self.timelapse
        if lapse and now-lapse['start']>REPLAY+HOLD:self.timelapse=lapse=None
        focus=None if lapse or focus not in notes else focus
        if self.wake_pending:self.wake_pending=False;self.waking=self._waken(now) if moving and not lapse else None
        if self.waking and (lapse or now-self.waking['start']>self.waking['span']):self.waking=None
        grow=lapse or self.waking
        current=items[index]['id'] if items and 0<=index<len(items) and items[index].get('id') in notes else None
        selected=None if lapse or focus else current;chosen=current if focus else None
        if selected!=self.selected:self.selected=selected;self.selected_at=now;self.beat=-99.0
        if focus!=self.focus:self.focus=focus;self.focus_at=now;self.beat=-99.0
        self._recent(notes,now)
        if not self.layout.settled():
            for _ in range(max(1,min(24,32000//len(notes)**2))):self.layout.tick()
        # Full layouts carry monumental type and, when framed, a side panel; the tissue fills what remains.
        full=cols>=96 and rows>=22;panel=min(46,max(30,cols//4)) if full and framed else 0
        story=self._story(notes,focus,selected,items,index,matches,lapse,now)
        hud=self._hud(story,full,cols-panel-4,rows-len(header),now,moving,items,index,framed)
        if not story and matches is not None:hud=[('Keine Treffer. Esc zeigt wieder alle Notizen.',9)]
        # Full screen: the body sits a little right of centre, clear of the name at the bottom left.
        if full and not framed:room=rows-len(header);ox=cols*1.06;oy=(rows+len(header))*2;ry=.44*min(cols*2,room*4);rx=min(ry*1.6,cols*.88)
        else:
            room=rows-len(hud)-len(header);sky=rows-(len(hud)//2 if full else len(hud));ox=cols-panel
            oy=sky*2-len(hud) if full else (len(header)+rows-len(hud))*2;ry=.4*min((cols-panel)*2,sky*4) if full else .42*min(cols*2,room*4);rx=min(ry*1.3,(cols-panel)*.9)
        step=min(.25,max(0.0,now-self.tick_at)) if moving and self.tick_at is not None else 0.0;self.tick_at=now
        place=None;scale=min(1.25,max(.5,ry/80))
        if room>=5:
            place={ident:(ox+x*rx,oy-y*ry,z,alpha) for ident,(x,y,z,alpha) in self._shape(notes,items,focus,now,moving).items()}
            fibers=self._fibers(place)
            if step:self._live(step,place,fibers,focus,selected,lapse,now,ry,self.waking)
            canvas=self._body(cols,rows,notes,place,fibers,scale,selected,focus,chosen,matches,grow,now)
            self._activity(canvas,place,fibers,scale,selected,focus,grow,now,moving)
            canvas.swell=(self.age*.9,2) if full else (self.age*.9,1)
        if panel:self._panel(canvas,cols-panel,panel,rows,notes,focus,items,index,matches,trail,lapse,now)
        for row,line in enumerate(header):
            canvas.plate(row,0,sum(span for _,span in _cells(line[0]))+4,1,True);canvas.put(row,2,line[0],line[1],True)
            if len(line)>2 and line[2]:
                width=sum(span for _,span in _cells(line[2]));canvas.plate(row,cols-width-4,width+4,1,True);canvas.put(row,cols-width-2,line[2],6,True)
        # The caption block reserves its full width, so names never sit beside its lines.
        block=max((sum(span for _,span in _cells(text)) for text,_ in hud),default=0)+4
        for offset,(text,style) in enumerate(hud):
            row=rows-len(hud)+offset;canvas.plate(row,0,block if full else cols-panel,1,True);canvas.put(row,2,text,style,True)
        if place is not None:self._marks(canvas,notes,place,scale,selected,focus,chosen,items,matches,grow)
        thought=self.thought
        if place is not None and thought and not grow and cols>=60:
            # The caption names the train of thought as far as it has come.
            reached=[ident for ident in thought['path'] if ident in thought['reached']];row=len(header)+1 if header else 0
            fade=thought['end'] is not None and self.age-thought['end']>2.2;names='  →  '.join(self.names.get(ident,ident) for ident in reached)
            text=_clip(names,max(10,cols-24));width=sum(span for _,span in _cells(text))+16
            if canvas.free(row,0,min(cols,width+4),below=2):
                canvas.plate(row,0,min(cols,width+4),1,True);canvas.put(row,2,'GEDANKENGANG',8 if fade else 9,True);canvas.put(row,18,text,8 if fade else 'accent',True)
        if full and not framed and place is not None and not lapse:
            legend=[('Mitte Fokus  ·  innen direkte Nachbarn  ·  außen ein Schritt weiter',6),('Impulse und Gedankengänge folgen den echten Verbindungen',5)] if focus else [
                ('● Zelle = Notiz, größer mit mehr Verbindungen',6),('gefüllt bestätigt   hohl berichtet   gebrochen umstritten',6),
                ('Faser = Verbindung, gepunktet: verwandt',6),('Gedankengang: zufälliger Weg über echte Verbindungen  ·  Glimmen: in 72 h bearbeitet',5)]
            for offset,(text,style) in enumerate(legend):
                width=sum(span for _,span in _cells(text));row=rows-len(legend)+offset
                if canvas.free(row,cols-width-3,width+2):canvas.put(row,cols-width-2,text,style)
        self.still=(signature,canvas if cells else canvas.runs())
        return self.still[1]

    def _waken(self, now):
        """Ignition times: breadth-first from the strongest cell, a beat per link; other parts last."""
        first=max(self.degree,key=lambda ident:(self.degree[ident],ident));depth={first:0};queue=[first]
        while queue:
            ident=queue.pop(0)
            for other in sorted(self.links.get(ident,())):
                if other not in depth:depth[other]=depth[ident]+1;queue.append(other)
        far=max(depth.values())+1;born={ident:.22*depth.get(ident,far)+.18*_hash(ident)[7] for ident in self.degree}
        span=max(born.values())+.9;self.wave=self.age+span+1.2
        return {'start':now,'born':born,'span':span,'seen':-1.0}

    def _recent(self, notes, now):
        """Notes edited in the last 72 hours glimmer; generated.at means edited, not verified."""
        if now-self.recent_at<30:return
        self.recent_at=now;cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=72);recent=[]
        for ident,note in notes.items():
            try:
                at=hive.timestamp((note.get('generated') or {}).get('at') or '')
                if at>=cutoff:recent.append((at,ident))
            except (ValueError,TypeError,AttributeError):pass
        self.recent=[ident for _,ident in sorted(recent,reverse=True)[:8]]

    def _positions(self):
        """Unit-disc coordinates from the layout's two widest axes, radii evened out; the third axis is depth in [-1, 1]."""
        if self.charted==(self.graph,True):return self.chart
        points,_=self.layout.normalized({ident for ident,degree in self.degree.items() if degree})
        if not points:return {}
        cov=[[sum(p[a]*p[b] for p in points.values()) for b in range(3)] for a in range(3)]
        def axis(away=None):
            v=[1.0,.6,.3]
            for _ in range(48):
                w=[sum(cov[a][b]*v[b] for b in range(3)) for a in range(3)]
                if away:d=sum(w[k]*away[k] for k in range(3));w=[w[k]-d*away[k] for k in range(3)]
                n=math.sqrt(sum(x*x for x in w)) or 1.0;v=[x/n for x in w]
            return v
        u=axis();v=axis(u);w=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
        flat={ident:(sum(p[k]*u[k] for k in range(3)),sum(p[k]*v[k] for k in range(3))) for ident,p in points.items()}
        depth={ident:sum(p[k]*w[k] for k in range(3)) for ident,p in points.items()};far=max(map(abs,depth.values())) or 1.0
        # Orientation is fixed by the strongest cell (left, slightly low) so the body never mirrors.
        hub=max(flat,key=lambda ident:(self.degree.get(ident,0),ident));sx=-1 if flat[hub][0]>0 else 1;sy=-1 if flat[hub][1]>0 else 1
        ranked=sorted(flat,key=lambda ident:(math.hypot(*flat[ident]),ident));chart={}
        for rank,ident in enumerate(ranked):
            x,y=flat[ident];r=math.hypot(x,y) or 1e-9;q=.92*math.sqrt((rank+.5)/len(ranked));chart[ident]=(sx*x/r*q,sy*y/r*q,depth[ident]/far)
        self.chart=chart;self.charted=(self.graph,self.layout.settled())
        return chart

    def _shape(self, notes, items, focus, now, moving):
        """Unit positions with depth and visibility. Around a focus the tissue gathers: direct
        neighbours on an inner ring, theirs further out, everything else recedes."""
        base={ident:point for ident,point in self._positions().items() if ident in notes}
        if focus in base:
            direct=[row['id'] for row in items if row['id'] in base and row['id']!=focus];angles={}
            target={ident:(x*1.3,y*1.3,z,0.0) for ident,(x,y,z) in base.items()};target[focus]=(0.0,0.0,1.0,1.0)
            for k,ident in enumerate(direct):
                a=math.pi/2-2*math.pi*(k+.5)/len(direct);r=.46 if len(direct)<=10 or k%2==0 else .58
                angles[ident]=a;target[ident]=(r*math.cos(a),r*math.sin(a),.5,1.0)
            wish=[]
            for ident in sorted({other for near in direct for other in self.links.get(near,())}-set(direct)-{focus}):
                toward=[angles[other] for other in self.links.get(ident,()) if other in angles]
                wish.append((math.atan2(sum(map(math.sin,toward)),sum(map(math.cos,toward))),ident))
            gap=min(2*math.pi/max(1,len(wish)),.3);last=None
            for k,(a,ident) in enumerate(sorted(wish)):
                if last is not None and a<last+gap:a=last+gap
                last=a;r=.86 if len(wish)<=16 or k%2==0 else .95;target[ident]=(r*math.cos(a),r*math.sin(a),0.0,.7)
        else:target={ident:(x,y,z,1.0) for ident,(x,y,z) in base.items()}
        if self.target!=(focus,):self.morph=(now,dict(self.shown)) if self.shown and moving else None;self.target=(focus,)
        if self.morph:
            f=_ease((now-self.morph[0])/1.2)
            if f>=1:self.morph=None
            else:target={ident:tuple(s+(t-s)*f for s,t in zip(self.morph[1].get(ident,point[:3]+(0.0,)),point)) for ident,point in target.items()}
        self.shown=target
        return target

    def _fibers(self, place):
        """Each visible link as a gently bent curve: (x0, y0, cx, cy, x1, y1, length in dots)."""
        fibers={}
        for pair,bend in self.bends.items():
            a,b=pair
            if a not in place or b not in place or min(place[a][3],place[b][3])<.04:continue
            (x0,y0,_,_),(x1,y1,_,_)=place[a],place[b];dx=x1-x0;dy=y1-y0
            fibers[pair]=(x0,y0,(x0+x1)/2-dy*bend,(y0+y1)/2+dx*bend,x1,y1,(math.hypot(dx,dy) or 1.0)*(1+bend*bend))
        return fibers

    def _radius(self, ident, z, scale, focus):
        """Cell size in dots: grows with connections, a little with nearness, most for the focus."""
        return (1.3+.5*math.sqrt(self.degree.get(ident,0)))*scale*(1+.25*z)*(1.35 if ident==focus else 1.0)

    def _soma(self, canvas, x, y, r, level, tint, form, blur=0.0):
        """A cell body in soft grain: a dense core fading outwards when confirmed; a thin membrane
        round a faint inside when reported; the membrane broken when disputed. `blur` (0 to 1) widens
        and thins cells in the back, a depth of field."""
        if level<0:return
        low=max(1,level-8)
        if form=='solid' or r<2.2:canvas.glow(x,y,r*.55*(1-.4*blur),r*(1.3+.5*blur),level if form=='solid' else max(0,level-3),tint,1.0-.3*blur,low)
        else:
            canvas.glow(x,y,.6,r*.75,max(0,level-5),tint,.45,low);count=max(8,int(r*7.5))
            for i in range(count):
                a=6.283185307*i/count
                if form!='broken' or int(a*2.5)%2==0:canvas.dot(x+r*math.cos(a),y+r*math.sin(a),level,tint)
            canvas.glow(x,y,r+.4,r+1.8,max(0,level-6),tint,.4,1,hole=r+.4)
        row,col=int(y)>>2,int(x)>>1
        if 0<=row<canvas.rows and 0<=col<canvas.cols:canvas.used[row*canvas.cols+col]=max(canvas.used[row*canvas.cols+col],1)

    def _body(self, cols, rows, notes, place, fibers, scale, selected, focus, chosen, matches, lapse, now):
        """The tissue at rest: tapered fibres, dendrites and cell bodies, kept while nothing reshapes."""
        key=None if lapse else (cols,rows,tuple((i,round(x,1),round(y,1),round(a,2)) for i,(x,y,_,a) in place.items()),selected,focus,chosen,None if matches is None else frozenset(matches),tuple(self.recent),self.graph,self.light)
        if key is not None and key==self.body[0]:return self.body[1].clone()
        canvas=Canvas(cols,rows);born=lapse['born'] if lapse else {};elapsed=now-lapse['start'] if lapse else 0.0;heart=focus or selected
        if self.light:canvas.light=bytearray(cols*rows*2)
        for pair,(x0,y0,cx,cy,x1,y1,_) in fibers.items():
            a,b=pair;start,end=0.0,1.0;alpha=min(place[a][3],place[b][3]);weak=self.kinds[pair]=='related'
            if lapse:
                grow=_ease((elapsed-max(born.get(a,0.0),born.get(b,0.0)))/.8)
                if grow<=0:continue
                if born.get(a,0.0)>=born.get(b,0.0):end=grow
                else:start=1-grow
            if matches is not None:edge,mid=(10,4) if a in matches and b in matches else (2,0)
            elif chosen and {a,b}=={focus,chosen}:edge,mid=14,9
            elif heart in pair:edge,mid=12,6
            else:depth=int(round(place[a][2]+place[b][2]));edge,mid=(7 if weak else 10)+depth,(2 if weak else 3)+depth//2
            if any(notes[i].get('archived') or notes[i].get('status')=='deprecated' for i in pair):edge,mid=min(edge,3),0
            # Depth of field: fibres in the back fall apart into finer grain.
            blur=max(0.0,-(place[a][2]+place[b][2])/2)
            canvas.strand(x0,y0,cx,cy,x1,y1,int(edge*alpha),max(0,int(mid*alpha)),1,start,end,(.35 if weak else .75)*(1-.4*blur))
        for ident,(x,y,z,alpha) in place.items():
            if alpha<.04 or lapse and elapsed<born.get(ident,0.0):continue
            grow=_ease((elapsed-born.get(ident,0.0))/.6) if lapse else 1.0
            note=notes[ident];degree=self.degree.get(ident,0);u=_hash(ident);r=self._radius(ident,z,scale,focus)*grow
            # Dendrites: short, forked, fading branches that make a note read as a cell; they lead nowhere.
            for k in range(2+min(5,degree//2) if scale>=.7 else 1+min(2,degree//4)):
                a=6.2832*u[k%8]+k*1.9;reach=r+(4+8*u[(k+3)%8]+min(degree,12)*.3)*scale*grow;bend=(u[(k+5)%8]-.5)*.8
                x0=x+r*math.cos(a);y0=y+r*math.sin(a);x1=x+reach*math.cos(a+bend*.4);y1=y+reach*math.sin(a+bend*.4)
                canvas.curve(x0,y0,(x0+x1)/2-(y1-y0)*bend*.5,(y0+y1)/2+(x1-x0)*bend*.5,x1,y1,int(5*alpha*grow),0,fade=.15)
                fx=x0+(x1-x0)*.6;fy=y0+(y1-y0)*.6;fork=a+(.7 if u[(k+1)%8]>.5 else -.7);twig=(reach-r)*.45
                canvas.curve(fx,fy,fx+twig*.5*math.cos(fork+bend),fy+twig*.5*math.sin(fork+bend),fx+twig*math.cos(fork),fy+twig*math.sin(fork),int(3*alpha*grow),0,fade=.1)
            ghost=note.get('archived') or note.get('status')=='deprecated';area=self.areas.get(ident)
            if ident==heart:tint,nucleus,level='azure','spark',15
            elif ghost:tint,nucleus,level=None,None,4
            elif matches is not None and ident not in matches:tint,nucleus,level=None,None,3
            else:tint,nucleus,level=None,(area if area in AREAS else None),max(8,min(15,11+degree//4+round(1.5*z)))
            form='solid' if ghost or note.get('evidence')=='confirmed' else 'broken' if note.get('evidence')=='disputed' else 'hollow'
            self._soma(canvas,x,y,r,int(level*min(1.0,alpha*1.3)),tint if alpha>=.5 else None,form,max(0.0,-z))
            # Every living cell casts a faint light of its own; the chosen one's breathes in _activity.
            if level>=8 and ident!=heart:canvas.lamp(x,y,r*2.8+7*scale,(level-6)*3.5*min(1.0,alpha*1.3)*grow*(1-.5*max(0.0,-z)))
            # The nucleus carries the area colour, like a stained cell.
            row,col=int(y)>>2,int(x)>>1
            if nucleus and alpha>=.5 and 0<=row<rows and 0<=col<cols:canvas.tint[row*cols+col]=nucleus
        self.body=(key,canvas)
        return canvas.clone() if key is not None else canvas

    def _live(self, step, place, fibers, focus, selected, lapse, now, ry, waking=None):
        """Advance life by `step` seconds. Impulses travel and fire the cells they reach; cells fire
        on their own, recently edited ones more often; the chosen cell beats; now and then a wave
        runs through everything and a train of thought follows a path of real links. Seeded, so the
        same knowledge lives the same way."""
        self.age+=step;age=self.age;self.speed=max(40.0,ry*1.1);grow=lapse or waking
        born=grow['born'] if grow else {};elapsed=now-grow['start'] if grow else 0.0
        alive={ident for ident,point in place.items() if point[3]>=.5 and born.get(ident,0.0)<=elapsed}
        length={pair:geometry[6] for pair,geometry in fibers.items()};moving=[];arrived=[]
        for pulse in self.pulses:
            pulse[2]+=step
            if pulse[0] in alive and pulse[1] in alive:(arrived if pulse[2]>=pulse[3] else moving).append(pulse)
        self.pulses=moving
        for a,b,_,_,strength in arrived:
            self.lit[b]=(age,strength*.8)
            if strength*.8>=.3:self._fire(b,strength*.8,alive,length,a)
        if waking:
            for ident,second in waking['born'].items():
                if waking['seen']<second<=elapsed and ident in alive:self._fire(ident,.7,alive,length,force=True)
            waking['seen']=elapsed
        if lapse:
            for second,_,touched,_,_ in lapse['events']:
                if self.seen<second<=elapsed:
                    for ident in touched:
                        if ident in alive:self._fire(ident,.75,alive,length,force=True)
            self.seen=elapsed;return
        heart=focus or selected
        if heart in alive and age-self.beat>=(1.7 if focus else 2.4):self.beat=age;self._fire(heart,.55,alive,length,force=True,spread=1.0)
        for ident in sorted(alive):
            if self.rng.random()<(.12+.01*self.degree.get(ident,0)+(.1 if ident in self.recent else 0))*step:self._fire(ident,.62,alive,length,spread=.6)
        if age>=self.wave and not waking:
            self.wave=age+self.rng.uniform(10.0,18.0);pool=[focus] if focus in alive else sorted(alive&self.hubs) or sorted(alive)
            if pool:origin=self.rng.choice(pool);self._fire(origin,1.5,alive,length,force=True,spread=.85);self.thought=self._think(origin,alive,length)
        thought=self.thought
        if thought and not all(ident in alive for ident in thought['path']):self.thought=thought=None
        if thought and thought['end'] is None:
            thought['t']+=step
            while thought['end'] is None and thought['t']>=thought['times'][thought['hop']]:
                thought['t']-=thought['times'][thought['hop']];thought['hop']+=1;ident=thought['path'][thought['hop']];thought['reached'][ident]=age
                self._fire(ident,1.2,alive,length,thought['path'][thought['hop']-1],force=True,spread=.45)
                if thought['hop']>=len(thought['path'])-1:thought['end']=age
        if thought and thought['end'] is not None and age-thought['end']>3.5:self.thought=None
        self.lit={ident:value for ident,value in self.lit.items() if age-value[0]<1.0}

    def _think(self, origin, alive, length):
        """A train of thought: a walk of up to four real links from `origin`, never back to a cell
        it has passed, leaning towards well-connected neighbours."""
        path=[origin]
        for _ in range(4):
            options=[other for other in sorted(self.links.get(path[-1],())) if other in alive and other not in path and ((path[-1],other) if path[-1]<other else (other,path[-1])) in length]
            if not options:break
            weights=[1+self.degree.get(other,0) for other in options];pick=self.rng.random()*sum(weights)
            for other,weight in zip(options,weights):
                pick-=weight
                if pick<=0:break
            path.append(other)
        if len(path)<2:return None
        times=[max(.7,min(2.2,length[(a,b) if a<b else (b,a)]/(self.speed*.7))) for a,b in zip(path,path[1:])]
        return {'path':path,'times':times,'hop':0,'t':0.0,'reached':{origin:self.age},'end':None}

    def _fire(self, ident, strength, alive, length, source=None, force=False, spread=None):
        """A cell fires: it flashes and sends impulses along some of its fibres."""
        if not force and self.age-self.fired.get(ident,-99.0)<REFRACTORY:return
        self.fired[ident]=self.age;self.lit[ident]=(self.age,strength);chance=.15+.32*strength if spread is None else spread
        for other in sorted(self.links.get(ident,())):
            pair=(ident,other) if ident<other else (other,ident)
            if other==source or other not in alive or pair not in length or len(self.pulses)>=PULSES:continue
            if self.rng.random()<chance:self.pulses.append([ident,other,-.15*self.rng.random(),max(.35,min(1.6,length[pair]/self.speed)),strength])

    def _activity(self, canvas, place, fibers, scale, selected, focus, lapse, now, moving):
        """What lives on the tissue: drifting motes, breathing halos, flashing cells and impulses."""
        age=self.age;heart=focus or selected;born=lapse['born'] if lapse else {};elapsed=now-lapse['start'] if lapse else 0.0
        W,H,cols=canvas.w,canvas.h,canvas.cols
        for k in range(min(72,canvas.cols*canvas.rows//150)):
            u=_mote(k);x=(u[0]*W+3*math.sin(age*(.1+.1*u[2])+u[3]*6.3))%W;y=(u[1]*H-age*(.5+1.2*u[2]))%H;cell=(int(y)>>2)*cols+(int(x)>>1)
            if not canvas.mask[cell] and not canvas.used[cell]:canvas.dot(x,y,1 if math.sin(age*(.6+u[4])+u[5]*6.3)<.6 else 3)
        for ident,(x,y,z,alpha) in place.items():
            if alpha<.5 or lapse and elapsed<born.get(ident,0.0):continue
            u=_hash(ident)[6];b=math.sin(age*6.2832/(4.5+2.5*u)+u*6.3);r=self._radius(ident,z,scale,focus)
            # Halos breathe in density, not in size, so they swell softly instead of jumping.
            if ident==heart:
                canvas.ring(x,y,r+2.2,11,1,tint='ice');canvas.glow(x,y,r+2.8,r+7+1.2*b,9,'tide',.38+.12*b,4,hole=r+2.8)
                canvas.lamp(x,y,r*2+8*scale+3,40+14*b)
                since=now-(self.focus_at if focus else self.selected_at)
                if moving and since<.45:shock=r+2+14*(1-_ease(since/.45));canvas.ring(x,y,shock,13,1,tint='ice');self._beam(canvas,x,y,shock,60)
            elif ident in self.recent:canvas.glow(x,y,r+.8,r+4.5,6,None,.28+.12*b,1,hole=r+.6);canvas.lamp(x,y,r*1.8+5*scale+2,14+8*b)
            elif ident in self.hubs:canvas.glow(x,y,r+.6,r+3.2,4,None,.2+.08*b,1,hole=r+.4);canvas.lamp(x,y,r*1.8+4*scale+2,8+5*b)
        for ident,(at,strength) in self.lit.items():
            since=age-at
            if ident not in place or place[ident][3]<.5 or since>.9 or lapse and elapsed<born.get(ident,0.0):continue
            x,y,z,_=place[ident];r=self._radius(ident,z,scale,focus);row,col=int(y)>>2,int(x)>>1;f=since/.9
            # A firing cell lights its nucleus and flares; strong firings and the heartbeat also send out a ring.
            if since<.3 and 0<=row<canvas.rows and 0<=col<cols:canvas.tint[row*cols+col]='spark' if strength>=.55 else 'ice'
            canvas.lamp(x,y,r*1.2+(2+5*min(1.5,strength))*scale+2,150*min(1.0,strength*strength)*(1-f)**2)
            if strength>=1.0 or ident==heart and strength>=.5:
                wave=r+1+(5+5*min(1.5,strength))*scale*f;canvas.ring(x,y,wave,15,1,tint=SPARK[min(5,int((1-f)*4.5))],strength=.9*(1-f));self._beam(canvas,x,y,wave,70*(1-f))
        if moving and fibers:
            # Synaptic shimmer: a few sparks wink along random fibres, new ones 24 times a second
            # however fast the terminal draws.
            if age-self.twinkle[0]>=.04:
                pairs=list(fibers);sparks=self.sparks
                self.twinkle=(age,[(pairs[int(sparks.random()*len(pairs))],sparks.random(),'tide' if sparks.random()<.7 else 'azure') for _ in range(min(8,3+len(pairs)//20))])
            for pair,q,tint in self.twinkle[1]:
                if pair in fibers:x0,y0,cx,cy,x1,y1,_=fibers[pair];v=1-q;x=v*v*x0+2*v*q*cx+q*q*x1;y=v*v*y0+2*v*q*cy+q*q*y1;canvas.dot(x,y,10,tint);canvas.lamp(x,y,3,40)
        thought=self.thought
        if thought and all(ident in place for ident in thought['path']):
            # The train of thought: travelled links glow and later fall apart into grain; the leading
            # impulse is a comet with a bright head.
            fade=1.0 if thought['end'] is None else max(0.0,1-(age-thought['end'])/3.0)
            for k,(a,b) in enumerate(zip(thought['path'],thought['path'][1:])):
                pair=(a,b) if a<b else (b,a)
                if pair not in fibers or k>thought['hop']:continue
                x0,y0,cx,cy,x1,y1,_=fibers[pair];forward=a==pair[0]
                if k<thought['hop']:canvas.trace(x0,y0,cx,cy,x1,y1,14,'ice',.9*fade);self._glide(canvas,fibers[pair],0.0,1.0,4,26*fade)
                elif thought['end'] is None:
                    p=thought['t']/thought['times'][k];span=(max(0.0,p-.5),p) if forward else (1-p,min(1.0,1-p+.5))
                    canvas.trace(x0,y0,cx,cy,x1,y1,11,'tide',.8,*span);canvas.trace(x0,y0,cx,cy,x1,y1,15,'ice',1.0,*((max(0.0,p-.1),p) if forward else (1-p,min(1.0,1-p+.1))))
                    q=p if forward else 1-p;v=1-q;x=v*v*x0+2*v*q*cx+q*q*x1;y=v*v*y0+2*v*q*cy+q*q*y1
                    canvas.glow(x,y,.8,3.6*scale+1,15,'spark',.75,10);canvas.lamp(x,y,6*scale+4,150);self._glide(canvas,fibers[pair],*span,4,30)
            for ident,at in thought['reached'].items():
                x,y,z,_=place[ident];r=self._radius(ident,z,scale,focus);since=age-at;glow=max(0.0,min(fade,1-since/6))
                canvas.glow(x,y,r+1,r+5*scale+2,10,'azure',.55*glow,5,hole=r+.8);canvas.lamp(x,y,r*1.8+5*scale+3,50*glow)
        for a,b,t,duration,strength in self.pulses:
            pair=(a,b) if a<b else (b,a)
            if t<0 or pair not in fibers:continue
            x0,y0,cx,cy,x1,y1,length=fibers[pair];p=t/duration;gap=1.2/length;grain=canvas.grain;w=canvas.w;h=canvas.h
            # A bright head in a little grainy bloom, and a trail cooling from ice to deep blue over a
            # third of the fibre that thins into grain as it fades.
            count=max(8 if scale>=.7 else 4,min(34,int(length*.35/1.2)));shift=0 if strength>=.8 else 1
            for k in range(count):
                q=p-k*gap
                if q<0:break
                if a!=pair[0]:q=1-q
                v=1-q;x=v*v*x0+2*v*q*cx+q*q*x1;y=v*v*y0+2*v*q*cy+q*q*y1;fade=1-k/count
                if fade<.65 and 0<=x<w and 0<=y<h and fade/.65*255<=grain[int(y)*w+int(x)]:continue
                canvas.dot(x,y,max(4,int(5+10*fade)),TAIL[min(len(TAIL)-1,shift+k*len(TAIL)//count)])
                if k==0:
                    canvas.lamp(x,y,3*scale+2.5,40+40*min(1.0,strength))
                    if scale>=.7:canvas.glow(x,y,0,2.2,12,'ice',.5,8)
                elif k==count//3:canvas.lamp(x,y,2.5*scale+2,16*strength)
        if lapse:
            for ident,second in born.items():
                if ident in place and 0<=elapsed-second<.9:
                    f=(elapsed-second)/.9;x,y,_,_=place[ident];canvas.ring(x,y,1+12*f*scale,15,1,tint=SPARK[min(5,int((1-f)*6))],strength=1-f);canvas.lamp(x,y,6+12*f*scale,140*(1-f)**1.5)

    @staticmethod
    def _beam(canvas, x, y, radius, power):
        """Light along a ring: small lamps around the circle."""
        if canvas.light is None or power<1:return
        count=max(6,int(radius*.9))
        for i in range(count):a=6.283185307*i/count;canvas.lamp(x+radius*math.cos(a),y+radius*math.sin(a),4,power)

    @staticmethod
    def _glide(canvas, fiber, start, end, radius, power):
        """Light along part of a fibre: a lamp every few dots between `start` and `end` (0 to 1)."""
        if canvas.light is None or power<1:return
        x0,y0,cx,cy,x1,y1,length=fiber;steps=max(1,int(length*(end-start)/(radius*1.2)))
        for i in range(steps+1):
            t=start+(end-start)*i/steps;u=1-t;canvas.lamp(u*u*x0+2*u*t*cx+t*t*x1,u*u*y0+2*u*t*cy+t*t*y1,radius,power)

    def _marks(self, canvas, notes, place, scale, selected, focus, chosen, items, matches, lapse):
        """Names beside their cells, the most important first; a name never covers another."""
        if lapse:return
        thought=self.thought;latest=None
        if thought and (thought['end'] is None or self.age-thought['end']<3.0):
            latest=max(thought['reached'],key=thought['reached'].get);travelled=[(-1,ident) for ident in thought['path'] if ident in thought['reached']]
        else:travelled=[]
        if focus:wanted=[(0,focus)]+travelled+[(1 if row['id']==chosen else 2,row['id']) for row in items]
        else:
            near=sorted(self.links.get(selected,()),key=lambda ident:(-self.degree.get(ident,0),ident))[:10] if selected else []
            pool=sorted(matches if matches is not None else self.hubs,key=lambda ident:(-self.degree.get(ident,0),ident))
            wanted=([(0,selected)] if selected else [])+travelled+[(1,ident) for ident in near]+[(3,ident) for ident in pool]
        centre=place[focus][0] if focus in place else None;done=set()
        for rank,ident in wanted:
            if ident in done or ident not in place or place[ident][3]<.5:continue
            done.add(ident);x,y,z,_=place[ident];r=self._radius(ident,z,scale,focus);row,col=int(y)//4,int(x)//2;dx=int(r)//2+2;dy=int(r)//4+1
            text=_clip(self.names.get(ident,short(notes[ident]['title'])),22)
            if ident==focus:text=_clip(text.upper(),24)
            width=sum(span for _,span in _cells(text))
            if ident==focus:spots=((row+dy,col-width//2),(row-dy,col-width//2))
            elif centre is not None and x<centre:spots=((row,col-dx-width+1),(row+dy,col-width//2),(row-dy,col-width//2),(row,col+dx))
            else:spots=((row,col+dx),(row,col-dx-width+1),(row-dy,col-width//2),(row+dy,col-width//2))
            style=('accent' if ident==latest else 12) if rank<0 else 15 if rank==0 else (15|REVERSE) if rank==1 and focus else 11 if rank<=2 or matches is not None else 8
            spot=next((spot for spot in spots if canvas.free(spot[0],spot[1]-1,width+2)),None)
            # The chosen names may cover cell bodies, never another name.
            if spot is None and (rank==0 or rank==1 and focus):
                spot=next((spot for spot in spots if canvas.free(spot[0],spot[1]-1,width+2,below=2) and not any((spot[0]*canvas.cols+c) in canvas.text for c in range(spot[1]-1,spot[1]+width+1))),None)
            if spot:canvas.plate(spot[0],spot[1]-1,width+2);canvas.put(spot[0],spot[1],text,style)
        if focus or matches is not None:return
        groups={}
        for ident,(x,y,_,_) in place.items():groups.setdefault(self.areas.get(ident),[]).append((x,y))
        for area,points in sorted(groups.items(),key=lambda item:str(item[0])):
            if len(points)<3 or area not in AREAS:continue
            x=sum(p[0] for p in points)/len(points);y=sum(p[1] for p in points)/len(points)
            label=' '.join(AREAS[area]);row=int(y)//4+1;col=int(x)//2-len(label)//2
            # Generous margins keep two neighbouring regions from reading as one name.
            for r in (row,row+1,row-1,row+2,row-2):
                if canvas.free(r,col-4,len(label)+8) and canvas.free(r-1,col,len(label)) and canvas.free(r+1,col,len(label)):canvas.put(r,col,label,area);break

    def _story(self, notes, focus, selected, items, index, matches, lapse, now):
        if lapse:
            elapsed=min(REPLAY,now-lapse['start']);at=lapse['first']+dt.timedelta(seconds=elapsed/REPLAY*lapse['span'])
            done=[event for event in lapse['events'] if event[0]<=elapsed];current=done[-1] if done else lapse['events'][0]
            count=sum(1 for ident in notes if lapse['born'].get(ident,0.0)<=elapsed)
            return {'kind':'lapse','caption':f'ZEITRAFFER  ·  {count} NOTIZEN  ·  ÄNDERUNG {len(done)} / {len(lapse["events"])}',
                    'head':at.astimezone().strftime('%d.%m.%Y'),'subtitle':_clean(current[3]),'summary':'','changed':lapse['start'],
                    'meta':current[1].astimezone().strftime('%d.%m.%Y, %H:%M Uhr')+'  ·  '+_clean(current[4])}
        ident=focus or selected
        if not ident:return None
        note=notes[ident];title=_clean(note['title']);head=short(title);degree=self.degree.get(ident,0)
        rest=title[len(head):].lstrip(' ·:') if title.startswith(head) else ''
        caption=f'FOKUS  ·  {len(items)} NACHBAR'+('N' if len(items)!=1 else '') if focus else ('TREFFER' if matches is not None else 'NOTIZ')+f'  {index+1:02d} / {len(items):02d}'
        meta=[TYPES.get(note['type'],note['type']),hive.cluster_for(note),EVIDENCE.get(note['evidence'],note['evidence']),
              'Prüfung fällig' if _stale(note) else 'im Prüfzeitraum',f'{degree} Verbindung'+('en' if degree!=1 else '')]
        if note.get('archived'):meta.append('archiviert')
        elif note.get('status')=='deprecated':meta.append('veraltet')
        if focus and items and 0<=index<len(items):
            row=items[index];ribbon=f"NACHBAR {index+1:02d}/{len(items):02d}   {row.get('direction','')} {RELATIONS.get(row.get('relation'),row.get('relation',''))}   {self.names.get(row['id'],short(row.get('title','')))}"+('   —   '+row['reason'] if row.get('reason') else '')
        elif focus:ribbon='Keine aktiven Verbindungen für diese Notiz.'
        else:ribbon='   ·   '.join(part for part in ('↑ '+self.names.get(items[index-1]['id'],'') if index>0 else '','↓ '+self.names.get(items[index+1]['id'],'') if index+1<len(items) else '') if part)
        return {'kind':'focus' if focus else 'star','caption':caption,'head':head,'subtitle':rest,'meta':'  ·  '.join(meta),'ribbon':ribbon,
                'summary':note.get('summary','') if focus else '','changed':self.focus_at if focus else self.selected_at}

    def _hud(self, story, full, width, rows, now, moving, items, index, framed=True):
        """Bottom-left text: a monumental name in full layouts, a compact list otherwise."""
        if not story:return []
        width=max(10,width);kind=story['kind']
        if full:
            big=monumental(story['head'])
            if big and (len(big[-1])>width or rows<30):big=None
            span=len(big[-1]) if big else 0;reveal=int(span*_ease((now-story['changed'])/.45)) if moving else span
            lines=[(story['caption'],8)]+([(line[:reveal],15) for line in big] if big else [(_clip(story['head'].upper(),width),15)])
            if story['subtitle']:lines.append((_clip(story['subtitle'],width),12))
            lines.append((_clip(story['meta'],width),7))
            lines+=[(line,9) for line in _wrap(story['summary'],min(width,76),2)]
            if not framed and story.get('ribbon'):lines+=[('',0),(_clip(story['ribbon'],width),10 if story['kind']=='focus' else 6)]
            if len(lines)<=rows-5:return lines
            return [(story['caption'],8),(_clip(story['head'].upper(),width),15),(_clip(story['meta'],width),7)][:max(1,rows-5)]
        width+=2
        if kind=='lapse':return [(_clip(story['head']+'  ·  '+story['caption'],width),15),(_clip(story['subtitle'],width),8)][:max(1,rows)]
        lines=[(_clip(('FOKUS  ' if kind=='focus' else '◉ ')+story['head']+('  ·  '+story['subtitle'] if story['subtitle'] else ''),width),15)]
        if kind=='focus':
            if not items:lines.append(('Keine aktiven Verbindungen für diese Notiz.',8))
            room=max(1,min(len(items),rows//2-1));first=max(0,min(index-room//2,len(items)-room))
            for i,row in enumerate(items[first:first+room],first):
                text=f"{row.get('direction','')} {RELATIONS.get(row.get('relation'),row.get('relation',''))}  {self.names.get(row['id'],short(row.get('title','')))}"+(' · '+row['reason'] if row.get('reason') else '')
                lines.append((_clip(text,width).ljust(width) if i==index else _clip(text,width),(15|REVERSE) if i==index else 8))
        else:lines.append((_clip(story['meta'],width),7))
        return lines[:max(1,rows)]

    def _panel(self, canvas, x, width, rows, notes, focus, items, index, matches, trail, lapse, now):
        """Right column: the note index, the focused note's connections or the chronicle."""
        canvas.plate(0,x,width,rows,fixed=True);x+=2;width-=4
        if lapse:
            elapsed=min(REPLAY,now-lapse['start']);done=[event for event in lapse['events'] if event[0]<=elapsed];count=f'{len(done)}/{len(lapse["events"])}'
            canvas.put(0,x,'CHRONIK',9);canvas.put(0,x+width-len(count),count,7)
            for i,(_,at,_,reason,_) in enumerate(reversed(done[-max(1,rows-2):])):
                canvas.put(2+i,x,_clip(at.astimezone().strftime('%d.%m.  ')+reason,width),15 if i==0 else max(4,11-i))
            return
        if focus:
            title,count,lines='NACHBARN',str(len(items)),[]
            if trail:lines+=[('PFAD  '+' › '.join(self.names.get(i,i) for i in list(trail)[-3:]+[focus] if i in notes),-2),('',-2)]
            for i,row in enumerate(items):
                relation=RELATIONS.get(row.get('relation'),row.get('relation',''))
                lines.append((f"{row.get('direction','')} {relation:<13}{self.names.get(row['id'],short(row.get('title','')))}" if len(relation)<13 else f"{row.get('direction','')} {relation}  {self.names.get(row['id'],short(row.get('title','')))}",i))
                if row.get('reason'):lines.append(('   '+row['reason'],-1))
            if not items:lines.append(('Keine aktiven Verbindungen.',-1))
        else:
            title,count=('TREFFER' if matches is not None else 'NOTIZEN'),str(len(items))
            lines=[(f"{self.degree.get(row['id'],0):>2}  {notes[row['id']]['title']}",i) for i,row in enumerate(items)]
        canvas.put(0,x,title,9);canvas.put(0,x+width-len(count),count,7)
        legend=[] if focus else [('● Zelle = Notiz, Faser = Verbindung',6),('gefüllt bestätigt  ·  hohl berichtet',6),('Glimmen: in 72 h bearbeitet',5)]
        room=max(1,rows-2-(len(legend)+1 if legend else 0))
        anchor=next((i for i,(_,kind) in enumerate(lines) if kind==index),0);first=max(0,min(anchor-room//2,len(lines)-room))
        for offset,(text,kind) in enumerate(lines[first:first+room]):
            canvas.put(2+offset,x,_clip(text,width).ljust(width) if kind==index else _clip(text,width),(15|REVERSE) if kind==index else 5 if kind<0 else 9 if focus else 8)
        for offset,(text,style) in enumerate(legend):canvas.put(rows-len(legend)+offset,x,_clip(text,width),style)

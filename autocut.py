"""Automatic assembly for sequential, repeated attempts at scripted talking-head lines.

Decisions are edits over original source times. Removed material is never destroyed.
This is a transcript-driven selector, not a guarantee of intended wording.
"""
import re
import uuid
from difflib import SequenceMatcher
import engine

FILLERS={'um','uh','erm','er','umm','uhh'}
STUTTERS={'i','i\'m','we','the','a','it','its','this','that','you','and','to','my'}
DANGLING={'a','an','the','and','but','or','because','with','for','to','of','my','your','our','is','are','was','were','ever','than'}
CUES=[('sorry',),('start','again'),('let','me','start','again'),('let','me','try','again'),('one','more','time'),('no','wait'),('that','was','wrong'),('i','messed','that','up')]
STOP={'i','we','you','it','this','that','is','are','a','an','the','and','or','to','for','of','my','your','on','in','so','really'}

def text(words): return ' '.join(w['word'].strip() for w in words).strip()
def token(w): return ' '.join(engine.tokens(w['word']))
def uid(): return uuid.uuid4().hex

def clean_words(words):
    """Remove explicit hesitation sounds and adjacent repeated word/phrase starts."""
    keep=[True]*len(words); reasons={}
    for i,w in enumerate(words):
        if token(w) in FILLERS:
            keep[i]=False; reasons[i]='Hesitation removed automatically'
    # Iterate so triple restarts collapse too. Preserve expressive 'very very' / 'no no'.
    for width in range(6,0,-1):
        for i in range(len(words)-2*width+1):
            left=words[i:i+width]; right=words[i+width:i+2*width]
            if any(not keep[j] for j in range(i,i+width)): continue
            if [token(w) for w in left]!=[token(w) for w in right]: continue
            if width==1 and token(left[0]) not in STUTTERS: continue
            if right[0]['start']-left[-1]['end']>1.2: continue
            if any(re.search(r'[.!?]',w['word']) for w in left): continue
            for j in range(i,i+width):keep[j]=False;reasons[j]='Repeated word or phrase start removed automatically'
    removed=[]
    for i,w in enumerate(words):
        if not keep[i]:removed.append(dict(id=uid(),start=w['start'],end=w['end'],text=w['word'].strip(),reason=reasons[i],review=False))
    return [dict(w,source_index=i) for i,w in enumerate(words) if keep[i]],removed

def attempts(words):
    groups=[];current=[]
    for i,w in enumerate(words):
        head=engine.tokens(text(current[:3]));following=engine.tokens(text(words[i:i+3]))
        repeated_start=(len(current)>=3 and len(head)>=2 and len(following)>=2
                        and head[:2]==following[:2] and token(current[-1]) not in {'and','but','or'})
        boundary=current and (w['start']-current[-1]['end']>.5 or re.search(r'[.!?]$',current[-1]['word'].strip()) or repeated_start)
        if boundary:groups.append(current);current=[]
        current.append(w)
    if current:groups.append(current)
    return [dict(start=g[0]['start'],end=g[-1]['end'],text=text(g),confidence=sum(w.get('probability',.5) for w in g)/len(g),words=g) for g in groups]

def is_cue(line):
    t=engine.tokens(line['text'])
    # Only standalone recording directions. 'Sorry you had a bad experience' is dialogue.
    return tuple(t) in CUES or t in [['okay','start','again'],['sorry','let','me','try','again'],['sorry','start','again']]

def relation(a,b,script_lines):
    aa,bb=engine.tokens(a['text']),engine.tokens(b['text'])
    if not aa or not bb:return False
    sim=engine.similarity(a['text'],b['text'])
    if script_lines:
        sa=[engine.similarity(a['text'],s) for s in script_lines]
        sb=[engine.similarity(b['text'],s) for s in script_lines]
        if sa.index(max(sa))==sb.index(max(sb)) and min(max(sa),max(sb))>=.55 and max(max(sa),max(sb))>=.8:return True
    # Without a script, do not infer which of two opposite/numerically different claims is right.
    if (set(aa)&{'not','never','no'})!=(set(bb)&{'not','never','no'}):return False
    if re.findall(r'\d+',a['text'])!=re.findall(r'\d+',b['text']):return False
    shorter,longer=(aa,bb) if len(aa)<len(bb) else (bb,aa)
    if len(shorter)>=2 and shorter==longer[:len(shorter)]:return True
    common_prefix=0
    for x,y in zip(aa,bb):
        if x!=y:break
        common_prefix+=1
    # A repeated start plus substantial common content distinguishes a retake from a new line.
    content_a=set(aa)-STOP;content_b=set(bb)-STOP
    shared=len(content_a&content_b)
    if common_prefix>=3 and sim>=.62 and shared>=2:return True
    if common_prefix>=2 and sim>=.75 and shared>=2:return True
    return sim>=.9 and min(len(aa),len(bb))>=5 and shared>=3

def choose(lines,script=''):
    script_lines=[s.strip() for s in re.split(r'\n+|(?<=[.!?])\s+',script) if s.strip()]
    # Preserve proven paragraph matching, then actually select among near retakes.
    lines=engine.choose_takes(lines,script)
    for line in lines:
        if is_cue(line):line.update(keep=False,reason='Recording direction removed automatically',review=False)
    candidates=[i for i,x in enumerate(lines) if x['keep']]
    groups=[]
    for idx in candidates:
        if groups:
            group=groups[-1];last=lines[group[-1]]
            intervening=lines[group[-1]+1:idx]
            allowed=all(not x['keep'] for x in intervening)
            if allowed and lines[idx]['start']-last['end']<90 and all(relation(lines[j],lines[idx],script_lines) for j in group):
                group.append(idx);continue
        groups.append([idx])
    for group in groups:
        if len(group)<2:continue
        longest=max(len(engine.tokens(lines[i]['text'])) for i in group)
        def quality(i):
            x=lines[i];t=engine.tokens(x['text'])
            match=max((engine.similarity(x['text'],s) for s in script_lines),default=0)
            complete=bool(t and t[-1] not in DANGLING and len(t)>=longest*.78)
            # Transcript punctuation alone is not evidence of a complete delivery.
            return (round(match,2) if script_lines else 0,int(complete),round(min(len(t)/max(longest,1),1)*4),round(x['confidence']/.2),i)
        winner=max(group,key=quality)
        lines[winner].update(review=False,reason='Selected complete take automatically')
        for i in group:
            if i!=winner:lines[i].update(keep=False,review=False,reason='Alternative attempt removed automatically',alternative=winner)
    return lines

def assemble(words,ranges,duration,script='',padding=.06,gap=.18):
    cleaned,removed=clean_words(words)
    lines=choose(attempts(cleaned),script)
    clips=[];discarded=list(removed)
    for i,line in enumerate(lines):
        if not line['keep']:
            discarded.append(dict(id=uid(),start=line['start'],end=line['end'],text=line['text'],reason=line['reason'],review=False));continue
        # Missing source indices are words removed above. Never pad back over those words.
        groups=[];current=[]
        for w in line['words']:
            if current and (w['source_index']!=current[-1]['source_index']+1 or w['start']-current[-1]['end']>gap):
                groups.append(current);current=[]
            current.append(w)
        if current:groups.append(current)
        for group in groups:
            first,last=group[0],group[-1];a=max(0,first['start']-padding);b=min(duration,last['end']+padding)
            fi,li=first['source_index'],last['source_index']
            if fi:a=max(a,(words[fi-1]['end']+first['start'])/2)
            if li+1<len(words):b=min(b,(last['end']+words[li+1]['start'])/2)
            portions=[(max(a,s),min(b,e)) for s,e in ranges if min(b,e)-max(a,s)>=.025]
            if not portions:portions=[(a,b)] # Keep speech when VAD disagrees instead of losing an entire line.
            for start,end in portions:
                if end-start>=.025:clips.append(dict(id=uid(),start=start,end=end,text=text(group),reason=line['reason'],review=line['review']))
    # VAD intersections and adjacent padding must remain monotonic and exportable.
    clips.sort(key=lambda c:c['start'])
    for previous,current in zip(clips,clips[1:]):
        if previous['end']>current['start']:
            cut=(previous['end']+current['start'])/2;previous['end']=cut;current['start']=cut
    clips=[c for c in clips if c['end']-c['start']>=.025]
    if not clips:raise ValueError('No usable speech found. The original recording is preserved.')
    engine.validate_clips(clips,duration)
    summary=dict(attempts=len(lines),selected_takes=sum(x['keep'] for x in lines),removed_attempts=sum(not x['keep'] for x in lines),removed_stumbles=len(removed),original_seconds=duration,edited_seconds=sum(c['end']-c['start'] for c in clips))
    return clips,sorted(discarded,key=lambda c:c['start']),summary

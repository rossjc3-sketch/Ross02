"""Non-destructive timeline analysis and native FFmpeg rendering."""
import json, math, os, re, subprocess, wave
from difflib import SequenceMatcher
from pathlib import Path
import numpy as np

FFMPEG = os.environ.get('QUICKCUT_FFMPEG', 'ffmpeg')
FFPROBE = os.environ.get('QUICKCUT_FFPROBE', 'ffprobe')

def run(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode: raise RuntimeError(p.stderr[-2500:])
    return p.stdout

def probe(path):
    d = json.loads(run([FFPROBE, '-v','error','-show_format','-show_streams','-of','json',str(path)]))
    v = next((s for s in d['streams'] if s['codec_type']=='video'), None)
    if not v: raise ValueError('This file has no video track.')
    fps = v.get('avg_frame_rate','30/1').split('/')
    rate = float(fps[0])/max(float(fps[1]),1)
    duration = float(d['format'].get('duration',v.get('duration',0)))
    if not math.isfinite(duration) or duration <= 0: raise ValueError('Invalid video duration.')
    return dict(duration=duration, width=v['width'], height=v['height'], fps=rate or 30,
                audio=any(s['codec_type']=='audio' for s in d['streams']),
                hdr=v.get('color_transfer') in ['smpte2084','arib-std-b67'])

def audio_file(source, dest):
    run([FFMPEG,'-v','error','-y','-i',str(source),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(dest)])

def samples(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32)/32768

def waveform(audio, count=2400):
    hop=max(1, math.ceil(len(audio)/count))
    return [round(float(np.max(np.abs(audio[i:i+hop]))),4) for i in range(0,len(audio),hop)]

def merge_ranges(ranges, duration, padding=.12, gap=.25):
    result=[]
    for s,e in sorted(ranges):
        s=max(0,s-padding); e=min(duration,e+padding)
        if e<=s: continue
        if result and s-result[-1][1]<=gap: result[-1][1]=max(e,result[-1][1])
        else: result.append([s,e])
    return result

def energy_ranges(audio, duration, padding, gap):
    # Explicit fallback, not represented as speech recognition. Adaptive noise floor.
    hop=320
    rms=np.array([np.sqrt(np.mean(audio[i:i+hop]**2)) for i in range(0,len(audio),hop)])
    if not len(rms) or float(rms.max())<.0001: return []
    floor=float(np.percentile(rms,15)); peak=float(np.percentile(rms,90))
    threshold=max(.001,min(floor*3,peak*.3))
    active=rms>threshold
    ranges=[]; start=None
    for i,on in enumerate(np.r_[active,False]):
        if on and start is None: start=i
        if not on and start is not None:
            if i-start>=3: ranges.append((start*.02,i*.02))
            start=None
    return merge_ranges(ranges,duration,padding,gap)

def speech_ranges(audio, duration, padding, gap):
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    stamps=get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=int(gap*1000),speech_pad_ms=0))
    return merge_ranges([(s['start']/16000,s['end']/16000) for s in stamps],duration,padding,0)

def tokens(text): return re.findall(r"[a-z0-9]+(?:'[a-z]+)?",text.lower())

def similarity(a,b): return SequenceMatcher(None,tokens(a),tokens(b),autojunk=False).ratio()

def utterances(words, ranges):
    lines=[]; current=[]
    for index,w in enumerate(words):
        restart=(len(current)>=6 and len(words)-index>=4 and similarity(' '.join(x['word'] for x in current[:4]),' '.join(x['word'] for x in words[index:index+4]))>=.75)
        if current and (restart or w['start']-current[-1]['end']>.42 or re.search(r'[.!?]$',current[-1]['word'].strip())):
            lines.append(current); current=[]
        current.append(w)
    if current: lines.append(current)
    return [dict(start=g[0]['start'],end=g[-1]['end'],text=''.join(w['word'] for w in g).strip(),
                 confidence=sum(w['probability'] for w in g)/len(g),words=g) for g in lines]

def choose_takes(lines, script=''):
    """Conservative local fuzzy alignment. Uncertain pairs stay kept for review."""
    lines=[dict(x,keep=True,reason='',review=False) for x in lines]
    script_lines=[x.strip() for x in re.split(r'\n+|(?<=[.!?])\s+',script) if x.strip()]
    for item in lines:
        scores=[similarity(item['text'],s) for s in script_lines]
        item['script_match']=max(scores,default=0)
        item['script_line']=scores.index(max(scores)) if scores else None
    # Match adjacent passages even when ASR splits the two takes differently.
    # A repeated isolated line across unrelated content remains untouched.
    for left_size in range(4, 0, -1):
        for right_size in range(4, 0, -1):
            for i in range(len(lines)-left_size-right_size+1):
                first=lines[i:i+left_size]; second=lines[i+left_size:i+left_size+right_size]
                if not all(x['keep'] for x in first+second): continue
                if second[0]['start']-first[-1]['end']>15: continue
                a=' '.join(x['text'] for x in first); b=' '.join(x['text'] for x in second)
                aa,bb=tokens(a),tokens(b)
                if min(len(aa),len(bb))<8: continue
                same_meaning=(set(aa)&{'not','never','no'})==(set(bb)&{'not','never','no'}) and re.findall(r'\d+',a)==re.findall(r'\d+',b)
                if not same_meaning or similarity(a,b)<.94: continue
                if len(bb)<len(aa)*.95: continue
                ca=sum(x['confidence']*len(tokens(x['text'])) for x in first)/len(aa)
                cb=sum(x['confidence']*len(tokens(x['text'])) for x in second)/len(bb)
                if cb<ca-.15: continue
                if script_lines and max(similarity(b,s) for s in script_lines)+.02<max(similarity(a,s) for s in script_lines): continue
                for x in first:
                    x['keep']=False; x['reason']='Repeated consecutive passage; later take retained'
    for i,a in enumerate(lines):
        if not a['keep']: continue
        for j in range(i+1,min(i+4,len(lines))):
            b=lines[j]
            if b['start']-a['end']>20: break
            # Do not jump over a clearly different complete line.
            if any(similarity(a['text'],x['text'])<.55 for x in lines[i+1:j]): break
            aa,bb=tokens(a['text']),tokens(b['text'])
            if min(len(aa),len(bb))<3: continue
            sim=similarity(a['text'],b['text'])
            prefix=aa==bb[:len(aa)] and len(bb)>len(aa)
            # Opposite claims/numbers are not confidently interchangeable without a script.
            meaning_change=(set(aa)&{'not','never','no'})!=(set(bb)&{'not','never','no'}) or re.findall(r'\d+',a['text'])!=re.findall(r'\d+',b['text'])
            script_good=bool(script_lines and a['script_line']==b['script_line'] and b['script_match']>.88 and a['script_match']>.55)
            exact=aa==bb
            certain=(exact or (prefix and len(aa)>=4) or script_good) and (not meaning_change or script_good)
            if not certain:
                if sim>.65:
                    a['review']=b['review']=True
                    a['reason']=b['reason']='Possible retake — kept for your review'
                continue
            def rank(x):
                t=tokens(x['text']); complete=bool(re.search(r'[.!?]$',x['text']))
                return (round(x['script_match'],2) if script_lines else 0, len(t) if prefix else int(complete), round(x['confidence']/.15), x['start'])
            winner=max([a,b],key=rank); loser=b if winner is a else a
            loser['keep']=False; loser['reason']='Earlier/redundant take; alternative retained'
            loser['alternative']=j if winner is b else i
            if loser is a: break
    return lines

def validate_clips(clips, duration):
    if not isinstance(clips,list) or not 0<len(clips)<=500: raise ValueError('Keep between 1 and 500 clips.')
    previous=0
    for c in clips:
        a,b=float(c['start']),float(c['end'])
        if not all(math.isfinite(x) for x in [a,b]) or a<previous-.00001 or b>duration+.001 or b-a<.025:
            raise ValueError('Clips must be ordered, non-overlapping and within the original video.')
        previous=b
    return clips

def video_filter(hdr, preview):
    filters=[]
    if hdr: filters+=['zscale=t=linear:npl=100','format=gbrpf32le','tonemap=tonemap=hable:desat=0','zscale=p=bt709:t=bt709:m=bt709:r=tv']
    if preview: filters+=['scale=540:960:force_original_aspect_ratio=decrease:force_divisible_by=2']
    else: filters+=['scale=trunc(iw/2)*2:trunc(ih/2)*2']
    return ','.join(filters+['setsar=1','format=yuv420p'])

def render(source, clips, dest, meta, progress=lambda x:None, preview=False):
    validate_clips(clips,meta['duration'])
    work=Path(dest).parent/(Path(dest).stem+'-parts'); work.mkdir(exist_ok=True)
    # Sequential parts bound memory for long/4K inputs. Each track starts at zero.
    parts=[]; total=sum(c['end']-c['start'] for c in clips); done=0
    try:
        for i,c in enumerate(clips):
            out=work/f'{i:04d}.mp4'; length=c['end']-c['start']
            args=[FFMPEG,'-v','error','-y','-ss',str(c['start']),'-i',str(source),'-t',str(length),'-map','0:v:0','-map','0:a:0?',
                  '-vf',video_filter(meta.get('hdr'),preview),'-c:v','libx264','-preset','ultrafast' if preview else 'fast','-crf','25' if preview else '18',
                  '-r',str(meta['fps']),'-fps_mode','cfr','-c:a','aac','-b:a','192k','-af','aresample=async=1:first_pts=0','-movflags','+faststart',str(out)]
            run(args); parts.append(out); done+=length; progress(done/total*.95)
        listing=work/'concat.txt'; listing.write_text('\n'.join("file '"+p.name+"'" for p in parts))
        run([FFMPEG,'-v','error','-y','-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(dest)])
        progress(1)
    finally:
        import shutil
        shutil.rmtree(work,ignore_errors=True)

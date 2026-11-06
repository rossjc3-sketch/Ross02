import json, os, secrets, shutil, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
import engine
import autocut

ROOT=Path(__file__).parent
DATA=Path(os.environ.get('QUICKCUT_DATA',ROOT/'data')); DATA.mkdir(parents=True,exist_ok=True)
PAIR=os.environ.get('QUICKCUT_TOKEN') or secrets.token_urlsafe(12)
SESSIONS=set(); JOBS={}; BUSY=set(); LOCK=threading.RLock(); POOL=ThreadPoolExecutor(max_workers=1)
MODEL=None
app=FastAPI(docs_url=None,redoc_url=None)

@app.middleware('http')
async def protect(request, call_next):
    path=request.url.path
    if request.method not in ['GET','HEAD']:
        origin=request.headers.get('origin')
        if origin and origin!=str(request.base_url).rstrip('/'):
            return JSONResponse({'detail':'Open QuickCut using its original address.'},403)
    if path.startswith(('/api/','/media/')) and path not in ['/api/pair','/api/health']:
        if request.cookies.get('quickcut') not in SESSIONS:
            return JSONResponse({'detail':'Enter your QuickCut access code first.'},401)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    if path.startswith(('/api/','/media/')): response.headers['Cache-Control']='no-store'
    return response

class Pairing(BaseModel): code:str
@app.post('/api/pair')
def pair(body:Pairing, request:Request):
    if not secrets.compare_digest(body.code,PAIR): raise HTTPException(403,'Incorrect access code.')
    token=secrets.token_urlsafe(32); SESSIONS.add(token)
    response=JSONResponse({'ok':True}); response.set_cookie('quickcut',token,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=86400*30)
    return response

@app.get('/api/health')
def health(): return {'ok':True,'ffmpeg':bool(shutil.which(engine.FFMPEG))}

def folder(pid):
    try: uuid.UUID(pid)
    except ValueError: raise HTTPException(404,'Project not found.')
    p=DATA/pid
    if not p.is_dir(): raise HTTPException(404,'Project not found.')
    return p

def read(pid): return json.loads((folder(pid)/'project.json').read_text())
def save(p):
    dest=folder(p['id'])/'project.json'; tmp=dest.with_suffix('.tmp'); tmp.write_text(json.dumps(p)); tmp.replace(dest)

def job(pid, fn):
    with LOCK:
        if pid in BUSY: raise HTTPException(409,'This project is already processing.')
        BUSY.add(pid)
    jid=uuid.uuid4().hex; JOBS[jid]={'id':jid,'project':pid,'state':'queued','progress':0,'message':'Queued'}
    def update(progress,message=None):
        JOBS[jid]['progress']=round(progress*100)
        if message: JOBS[jid]['message']=message
    def task():
        try:
            JOBS[jid]['state']='running'; fn(update); JOBS[jid].update(state='done',progress=100,message='Ready')
        except Exception as e: JOBS[jid].update(state='error',message=str(e))
        finally:
            with LOCK: BUSY.discard(pid)
    POOL.submit(task); return {'job':jid,'project':pid}

@app.get('/api/jobs/{jid}')
def get_job(jid:str):
    if jid not in JOBS: raise HTTPException(404,'Job no longer available; reopen the project.')
    return JOBS[jid]

@app.get('/api/projects')
def projects():
    result=[]
    for p in DATA.glob('*/project.json'):
        d=json.loads(p.read_text()); result.append({k:d[k] for k in ['id','name','created','ready']})
    return sorted(result,key=lambda x:x['created'],reverse=True)

@app.get('/api/projects/{pid}')
def project(pid:str): return read(pid)

@app.post('/api/import')
async def upload(file:UploadFile=File(...), mode:str=Form('ai'), script:str=Form(''), padding:float=Form(.06), gap:float=Form(.18)):
    try: body=Analyze(mode=mode,script=script,padding=padding,gap=gap)
    except ValidationError: raise HTTPException(422,'Invalid editing settings.')
    if mode not in ['ai','review','silence']: raise HTTPException(400,'Unknown editing mode.')
    pid=str(uuid.uuid4()); p=DATA/pid; p.mkdir()
    try:
        size=0
        with (p/'source').open('wb') as f:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>4*1024**3: raise ValueError('Maximum upload is 4 GB.')
                f.write(chunk)
        meta=engine.probe(p/'source')
        if meta['duration']>1800: raise ValueError('Please use a recording under 30 minutes.')
        state=dict(id=pid,name=Path(file.filename or 'Recording').name,created=time.time(),ready=False,meta=meta,clips=[],discarded=[],words=[],peaks=[],revision=0,preview_revision=-1,export_revision=-1,warnings=[])
        save(state)
    except Exception as e:
        shutil.rmtree(p,ignore_errors=True); raise HTTPException(400,str(e))
    finally: await file.close()
    def prepare(update):
        update(.05,'Preparing phone preview')
        engine.render(p/'source',[{'start':0,'end':meta['duration']}],p/'source.mp4',meta,lambda x:update(x*.75),True)
        state=read(pid)
        if meta['audio']:
            engine.audio_file(p/'source',p/'audio.wav'); state['peaks']=engine.waveform(engine.samples(p/'audio.wav'))
        else: state['warnings'].append('No audio track: automatic speech editing is unavailable.')
        state['clips']=[dict(id=uuid.uuid4().hex,start=0,end=meta['duration'],text='Original recording')]
        state['ready']=True; save(state)
        if meta['audio']:
            analyze_work(pid,body,update)
    return job(pid,prepare)

class Analyze(BaseModel):
    mode:str='ai'
    script:str=Field(default='',max_length=20000)
    padding:float=Field(default=.06,ge=0,le=.5)
    gap:float=Field(default=.18,ge=.1,le=2)

@app.post('/api/projects/{pid}/analyze')
def analyze(pid:str,body:Analyze):
    state=read(pid)
    if not state['ready'] or not state['meta']['audio']: raise HTTPException(400,'Import a video with audio first.')
    if body.mode not in ['ai','review','silence']: raise HTTPException(400,'Unknown editing mode.')
    return job(pid,lambda update: analyze_work(pid,body,update))

def analyze_work(pid,body,update):
    global MODEL
    state=read(pid); state.pop('auto_summary',None); p=folder(pid); audio=engine.samples(p/'audio.wav'); warnings=[]
    update(.05,'Detecting speech')
    try: ranges=engine.speech_ranges(audio,state['meta']['duration'],body.padding,body.gap)
    except ImportError:
        ranges=engine.energy_ranges(audio,state['meta']['duration'],body.padding,body.gap)
        warnings.append('Using amplitude detection because Silero VAD is unavailable.')
    discarded=[]; words=[]
    if body.mode in ['ai','review']:
        update(.12,'Loading Whisper (first use downloads the model)')
        from faster_whisper import WhisperModel
        if MODEL is None: MODEL=WhisperModel(os.environ.get('QUICKCUT_MODEL','base.en'),device='cpu',compute_type='int8')
        segments,info=MODEL.transcribe(audio,word_timestamps=True,vad_filter=True,condition_on_previous_text=False,beam_size=5,temperature=0)
        for segment in segments:
            for w in segment.words or []:
                if w.end>w.start: words.append(dict(start=w.start,end=w.end,word=w.word,probability=w.probability))
            update(.2+.5*min(segment.end/state['meta']['duration'],1),'Transcribing speech')
        if not words: raise ValueError('Whisper found no words. Try Silence only or retain the original.')
        if body.mode=='ai':
            clips,discarded,summary=autocut.assemble(words,ranges,state['meta']['duration'],body.script,body.padding,body.gap)
            state['auto_summary']=summary
            warnings.append('Automatic cut assembled. Removed attempts are recoverable in Removed; check the result before publishing.')
        else:
            lines=engine.choose_takes(engine.utterances(words,ranges),body.script)
            clips=[]
            for i,line in enumerate(lines):
                start=max(0,line['start']-body.padding); end=min(state['meta']['duration'],line['end']+body.padding)
                # Split padding at adjacent utterance midpoint, including discarded takes.
                if i: start=max(start,(lines[i-1]['end']+line['start'])/2)
                if i+1<len(lines): end=min(end,(line['end']+lines[i+1]['start'])/2)
                clip=dict(id=uuid.uuid4().hex,start=start,end=end,text=line['text'],reason=line['reason'],review=line['review'])
                if not line['keep']: discarded.append(clip)
                else:
                    # VAD trims intra-sentence pauses that word alignments sometimes span.
                    portions=[(max(start,a),min(end,b)) for a,b in ranges if min(end,b)-max(start,a)>=.025]
                    if not portions: portions=[(start,end)]
                    for a,b in portions:
                        clips.append(dict(clip,id=uuid.uuid4().hex,start=a,end=b))
            warnings.append('Take selection uses conservative transcript matching. Review wording and delivery; uncertain matches remain in the edit.')
    else: clips=[dict(id=uuid.uuid4().hex,start=s,end=e,text=f'Speech {i+1}') for i,(s,e) in enumerate(ranges)]
    if not clips: raise ValueError('No speech found. Your current edit has been preserved.')
    engine.validate_clips(clips,state['meta']['duration'])
    update(.75,'Saving non-destructive edit')
    state.update(clips=clips,discarded=discarded,words=words,warnings=warnings,revision=state['revision']+1)
    save(state)
    # Finish the one-action workflow with a real stitched preview, not the source video.
    filename=f'preview-{state["revision"]}.mp4'
    update(.78,'Assembling your finished cut')
    try:
        engine.render(p/'source',clips,p/filename,state['meta'],lambda x:update(.78+.22*x,'Assembling your finished cut'),True)
        state.update(preview_revision=state['revision'],preview_file=filename)
    except Exception as exc:
        state['warnings'].append('Cuts were saved, but preview rendering failed. Use Render smooth preview to retry. '+str(exc))
    save(state)

class Edits(BaseModel):
    revision:int
    clips:list[dict]
    discarded:list[dict]=[]
@app.put('/api/projects/{pid}/edit')
def edit(pid:str,body:Edits):
    with LOCK:
        state=read(pid)
        if pid in BUSY: raise HTTPException(409,'Wait for processing to finish.')
        if state['revision']!=body.revision: raise HTTPException(409,'Project changed. Reopen it before editing.')
        try: engine.validate_clips(body.clips,state['meta']['duration'])
        except (ValueError,KeyError,TypeError) as e: raise HTTPException(400,str(e))
        state.update(clips=body.clips,discarded=body.discarded,revision=state['revision']+1); save(state)
    return state

@app.post('/api/projects/{pid}/render')
def render(pid:str,preview:bool=False):
    folder(pid)
    def process(update):
        state=read(pid); p=folder(pid)
        key='preview' if preview else 'export'; filename=f'{key}-{state["revision"]}.mp4'
        update(.01,'Rendering '+key)
        engine.render(p/'source',state['clips'],p/filename,state['meta'],update,preview)
        state[key+'_revision']=state['revision']; state[key+'_file']=filename; save(state)
    return job(pid,process)

@app.get('/media/{pid}/{name}')
def media(pid:str,name:str):
    p=folder(pid)
    if not (name=='source.mp4' or __import__('re').fullmatch(r'(export|preview)-\d+\.mp4',name)): raise HTTPException(404)
    if not (p/name).is_file(): raise HTTPException(404)
    return FileResponse(p/name,media_type='video/mp4',filename='QuickCut.mp4' if name.startswith('export') else None)

@app.delete('/api/projects/{pid}')
def delete(pid:str):
    with LOCK:
        if pid in BUSY: raise HTTPException(409,'Wait for processing to finish.')
        shutil.rmtree(folder(pid))
    return {'ok':True}

app.mount('/',StaticFiles(directory=ROOT/'web',html=True),name='web')
if __name__=='__main__':
    import socket, uvicorn
    scheme='https' if os.environ.get('QUICKCUT_CERT') else 'http'
    print('\nQUICKCUT — Pairing code: '+PAIR+f'\nOpen {scheme}://localhost:8765 on this PC.')
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('192.0.2.1',80)); ip=s.getsockname()[0]; s.close()
        print(f'On your iPhone (same Wi-Fi): {scheme}://{ip}:8765\n')
    except OSError: pass
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765)
    args,_=parser.parse_known_args()
    uvicorn.run(app,host=os.environ.get('QUICKCUT_HOST','127.0.0.1'),port=args.port,ssl_certfile=os.environ.get('QUICKCUT_CERT'),ssl_keyfile=os.environ.get('QUICKCUT_KEY'))

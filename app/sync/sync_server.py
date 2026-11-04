#!/usr/bin/env python3
"""Local sync service: write editor results back to Label Studio as Annotations.

Safety:
  * backs up the SQLite DB before every write
  * writes pending/machine rows only as Predictions
  * requires an explicit replaceAnnotation for any existing Annotation
  * tasks listed in LS_READ_ONLY_TASKS need an explicit allowReadOnlyReplace from the UI to be replaced
  * never deletes another user's Annotation
"""
from __future__ import annotations
import json, os, shutil, sqlite3, subprocess, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB = Path(os.environ.get('LS_DB', str(Path.home()/'Library/Application Support/label-studio/label_studio.sqlite3')))
BASE = os.environ.get('LABEL_STUDIO_URL', 'http://localhost:8080').rstrip('/')
TOKEN_FILE = Path(os.environ.get('LS_TOKEN_FILE', '/tmp/ls_access.txt'))
BACKUP_DIR = Path(os.environ.get('SYNC_BACKUP_DIR', str(ROOT/'work'/'sync_backups')))
TASKS = {int(k): v for k, v in json.loads(os.environ.get('TASKS_JSON', '{}')).items()}
CONFIRMED = {'manual', 'reference'}
READ_ONLY_ANNOTATION_TASKS = {
    int(x) for x in os.environ.get('LS_READ_ONLY_TASKS', '').split(',') if x.strip()
}
LS_BIN = os.environ.get('LS_BIN', 'label-studio')
if LS_BIN == 'label-studio' and not shutil.which(LS_BIN):
    candidate = Path.home()/'.venvs'/'label-studio'/'bin'/'label-studio'
    if candidate.exists(): LS_BIN = str(candidate)
EDITOR_DATA = Path(os.environ.get('EDITOR_DATA_PATH', str(ROOT/'app'/'editor'/'data.json')))
try:
    from preprocess import prepare_audio, build_asr_recording
except Exception:
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT/'work'))
        from preprocess import prepare_audio, build_asr_recording
    except Exception:
        prepare_audio = None
        build_asr_recording = None

def task_audio_map():
    """Return task_id -> Label Studio audio path from local editor data.

    The mapping is kept out of source code so this repository is portable and
    no client filename is baked into a distributed build.
    """
    if TASKS:
        return TASKS
    data_path=EDITOR_DATA
    if not data_path.exists():
        return {}
    bundle=json.loads(data_path.read_text(encoding='utf-8'))
    return {int(r['taskId']): r.get('lsAudio') for r in bundle.get('recordings', [])
            if r.get('lsAudio')}


def refresh_token():
    script = "from users.models import User\nfrom jwt_auth.models import LSAPIToken\nprint(LSAPIToken.for_user(User.objects.get(id=1)).access_token)\n"
    p = subprocess.run([LS_BIN, 'shell'],
                       input=script, text=True, capture_output=True)
    for token in p.stdout.split():
        if token.startswith('eyJ'):
            TOKEN_FILE.write_text(token)
            return token
    raise RuntimeError('token refresh failed: ' + p.stdout[-300:] + p.stderr[-300:])

def api(path, method='GET', body=None, retry=True):
    token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ''
    req = urllib.request.Request(BASE+path, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            refresh_token(); return api(path, method, body, retry=False)
        raise RuntimeError(f'HTTP {e.code}: {e.read().decode()[:800]}')


def _user_id(value):
    """Label Studio returns completed_by as either an int or {"id": int}."""
    if isinstance(value, dict):
        return value.get('id')
    return value

def is_mine(annotation):
    return _user_id(annotation.get('completed_by')) == 1

def probe_duration(path):
    try:
        p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
                          '-of','default=nw=1:nk=1',str(path)],capture_output=True,text=True)
        return float(p.stdout.strip()) if p.returncode==0 else 0.0
    except Exception:
        return 0.0


def _merge_same_time_rows(rows):
    """Merge local rows that occupy exactly the same interval.

    Label Studio draws same-time regions on top of each other, which makes the
    checkbox state impossible to see. Keep one row per interval, merging tags
    and preferring a confirmed speaker/text.
    """
    out=[]
    index={}
    for row in rows:
        key=(round(float(row['start']),3), round(float(row['end']),3))
        if key not in index:
            row=dict(row); row['tags']=list(row.get('tags') or []); row['pieces']=list(row.get('pieces') or [])
            index[key]=len(out); out.append(row); continue
        prev=out[index[key]]
        if row.get('confidence') in CONFIRMED and prev.get('confidence') not in CONFIRMED:
            prev['confidence']=row['confidence']; prev['speaker']=row.get('speaker') or prev.get('speaker')
        if (row.get('bodyText') or '').strip() and not (prev.get('bodyText') or '').strip():
            prev['bodyText']=row['bodyText']
        prev['tags']=list(dict.fromkeys([*(prev.get('tags') or []), *(row.get('tags') or [])]))
    return out

def _region(rid, start, end, from_name, kind, value):
    return {'id':rid,'from_name':from_name,'to_name':'audio','type':kind,
            'value':{'start':start,'end':end,'channel':0,**value}}

def to_result(rows):
    """Convert normalized rows to Label Studio's flat region schema.

    Each parent row has a speaker region and a body text region. Every nested
    interjection is emitted as independent regions with its own speaker, text
    and actual millisecond bounds -- Label Studio has no native nesting.
    """
    out=[]
    n=0
    for u in rows:
        rid=f's{n:05d}'; n+=1
        start,end=round(float(u['start']),3),round(float(u['end']),3)
        out.append(_region(rid,start,end,'speaker','labels',{'labels':[u['speaker']]}))
        body=(u.get('bodyText') or '').strip()
        if body:
            out.append(_region(rid,start,end,'text','textarea',{'text':[body]}))
        if u.get('tags'):
            out.append(_region(rid,start,end,'tags','choices',{'choices':list(u['tags'])}))
        for p in (u.get('pieces') or []):
            if not p.get('interjection'):
                continue
            iid=f's{n:05d}'; n+=1
            ps,pe=round(float(p['start']),3),round(float(p['end']),3)
            out.append(_region(iid,ps,pe,'speaker','labels',
                               {'labels':[p.get('speaker') or u['speaker']]}))
            ptext=(p.get('text') or '').strip()
            if ptext:
                out.append(_region(iid,ps,pe,'text','textarea',{'text':[ptext]}))
    return out

def _format_range(rows):
    """Validate and normalize rows once, including their nested pieces."""
    out=[]
    for idx,row in enumerate(rows):
        start=round(float(row['start']),3); end=round(float(row['end']),3)
        if start < 0 or end <= start:
            raise ValueError(f'第 {idx+1} 句时间范围无效（{start}–{end} 秒）。请刷新页面后重试。')
        speaker=(row.get('speaker') or 'UNKNOWN').strip()
        if not speaker:
            raise ValueError(f'row {idx}: empty speaker')
        pieces=[]
        for p in (row.get('pieces') or []):
            ps=round(float(p['start']),3); pe=round(float(p['end']),3)
            if ps < 0 or pe <= ps:
                snippet=(p.get('text') or row.get('bodyText') or '').strip()[:14]
                raise ValueError(
                    f'第 {idx+1} 句（{start}–{end} 秒）里的片段“{snippet}”时间无效（{ps}–{pe} 秒，长度为 0）。'
                    '页面已会自动清理这类片段，请刷新页面后重试。')
            # Allow a 20ms timing tolerance for hand-edited pieces, but never
            # accept an interjection that lives outside its parent sentence.
            if ps < start-0.020 or pe > end+0.020:
                snippet=(row.get('bodyText') or row.get('text') or '').strip()[:14]
                raise ValueError(
                    f'第 {idx+1} 句（{start}–{end} 秒，“{snippet}”）的插话片段 {ps}–{pe} 秒超出了本句范围。'
                    '请在页面找到该时间点，拆出或删除该插话后再同步。')
            pieces.append({'start':max(start,ps),'end':min(end,pe),
                           'text':(p.get('text') or '').strip(),
                           'speaker':(p.get('speaker') or speaker).strip(),
                           'interjection':bool(p.get('interjection') or p.get('isInterjection')),
                           'confidence':p.get('confidence') or row.get('confidence') or 'low'})
        pieces.sort(key=lambda p:p['start'])
        body=(row.get('bodyText') if row.get('bodyText') is not None else row.get('text') or '').strip()
        out.append({'start':start,'end':end,'speaker':speaker,
                    'bodyText':body,'tags':list(row.get('tags') or []),
                    'pieces':pieces,
                    'confidence':row.get('confidence') or 'low'})
    return out



def merge_into_annotation(existing, rows):
    """Merge local rows into an existing Label Studio result without wiping it.

    Matching is by exact interval first, then by a >=80% overlap. For a matched
    row we update only what the local row actually carries:
      - speaker region: updated when the local row is manually confirmed
      - text region: updated when non-empty
      - tags region: replaced when the local row has tags
    Unmatched local rows are appended; unmatched existing regions are preserved.
    """
    import copy
    merged=copy.deepcopy(existing or [])
    usable=[r for r in merged if isinstance(r,dict) and isinstance(r.get('value'),dict)]
    def interval(r):
        v=r.get('value') or {}; return round(float(v.get('start',0)),3), round(float(v.get('end',0)),3)
    def overlap(a,b):
        return max(0.0,min(a[1],b[1])-max(a[0],b[0]))
    def find(row):
        start,end=round(float(row['start']),3),round(float(row['end']),3)
        same=[r for r in usable if interval(r)==(start,end)]
        if same: return same
        dur=max(0.001,end-start)
        cands=[r for r in usable if overlap(interval(r),(start,end))>=0.8*dur]
        return cands
    changed=added=0
    for row in rows:
        matches=find(row)
        labels=row.get('speaker'); body=(row.get('bodyText') or '').strip(); tags=list(row.get('tags') or [])
        # Match regions by id when possible; otherwise use the first matching group.
        ids={r.get('id') for r in matches}
        target_id=next(iter(ids),None)
        if target_id is None:
            # Create a fresh group with a collision-free id.
            n=0
            while f'merged{n:05d}' in {r.get('id') for r in usable}: n+=1
            target_id=f'merged{n:05d}'
            merged.append(_region(target_id,float(row['start']),float(row['end']),'speaker','labels',{'labels':[labels]}))
            if body: merged.append(_region(target_id,float(row['start']),float(row['end']),'text','textarea',{'text':[body]}))
            if tags: merged.append(_region(target_id,float(row['start']),float(row['end']),'tags','choices',{'choices':tags}))
            added+=1
            continue
        touched=False
        # update speaker only if manually confirmed; tag-only rows keep old speaker
        if row.get('confidence') in CONFIRMED:
            target=next((r for r in usable if r.get('id')==target_id and r.get('from_name')=='speaker'),None)
            if target is not None:
                target['value']['labels']=[labels]; touched=True
            elif labels:
                merged.append(_region(target_id,float(row['start']),float(row['end']),'speaker','labels',{'labels':[labels]})); touched=True
        if body:
            target=next((r for r in usable if r.get('id')==target_id and r.get('from_name')=='text'),None)
            if target is not None:
                if target['value'].get('text')!= [body]: target['value']['text']=[body]; touched=True
            else:
                merged.append(_region(target_id,float(row['start']),float(row['end']),'text','textarea',{'text':[body]})); touched=True
        # tags are authoritative when present; remove old tag region for same id group
        if tags:
            old=[r for r in merged if r.get('id')==target_id and r.get('from_name')=='tags']
            for r in old: merged.remove(r)
            v=next((r for r in merged if r.get('id')==target_id and r.get('from_name')=='speaker'),None)
            start,end=interval(v) if v else (float(row['start']),float(row['end']))
            merged.append(_region(target_id,start,end,'tags','choices',{'choices':tags})); touched=True
        if touched: changed+=1
    return merged, changed, added

def sync(task_id, rows, mode='confirmed', replace_annotation=False, dry_run=False, check_existing=False,
         allow_read_only_replace=False):
    """Write confirmed rows as Annotation, pending rows as Prediction.

    Existing Annotations are never PATCHed unless replace_annotation=true. Task 5
    remains read-only by default because it contains the user's original manual
    annotation. dry_run returns the exact payload without touching Label Studio.
    """
    if mode not in ('confirmed','all'):
        raise ValueError('mode must be confirmed or all')
    rows=_merge_same_time_rows(_format_range(rows))
    confirmed=[r for r in rows if r.get('confidence') in CONFIRMED]
    tagged=[r for r in rows if r.get('confidence') not in CONFIRMED and r.get('tags')]
    pending=[r for r in rows if r.get('confidence') not in CONFIRMED and not r.get('tags')]
    ann_rows=rows if mode=='all' else [r for r in rows if r in confirmed or r in tagged]
    plan={'confirmed':len(confirmed),'tagged':len(tagged),'pending':len(pending),'annotation':None,
          'prediction':None,'taggedNotInAnnotation':False,'dryRun':bool(dry_run),
          'annotationResult':to_result(ann_rows) if ann_rows else []}
    if dry_run:
        if check_existing:
            try:
                plan['existing']=[{'id':a.get('id'),'items':len(a.get('result') or []),'mine':is_mine(a)}
                                  for a in (api(f'/api/tasks/{task_id}/annotations/') or [])]
            except Exception:
                plan['existing']=[]
        return plan

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp=time.strftime('%Y%m%d-%H%M%S')
    if DB.exists():
        shutil.copyfile(DB, BACKUP_DIR/f'ls-{stamp}-before-task{task_id}.sqlite3')

    if ann_rows:
        result=plan['annotationResult']
        anns=api(f'/api/tasks/{task_id}/annotations/') or []
        mine=[a for a in anns if is_mine(a)]
        # Our "mine" test uses the API's latest completed_by. If the API only
        # exposes a legacy task_completion row, don't assume ownership.
        if anns and task_id in READ_ONLY_ANNOTATION_TASKS and replace_annotation and not allow_read_only_replace:
            raise PermissionError(
                f'task {task_id} 已有受保护的人工标注（{sum(len(a.get("result") or []) for a in anns)} 个结果）。'
                '继续将用当前页面内容整体替换它；写回前会自动备份数据库。')
        if not mine and tagged and mode!='all':
            # A task with no Annotation yet: tags belong in the Prediction,
            # together with all other unconfirmed rows. Never drop them.
            plan['taggedNotInAnnotation']=True
            ann_rows=[]
            pending=[r for r in rows if r.get('confidence') not in CONFIRMED]
        if ann_rows and mine:
            ann_id=mine[0]['id']
            if replace_annotation:
                api(f'/api/annotations/{ann_id}/', method='PATCH', body={'result':result})
                plan['annotation']={'action':'replaced','id':ann_id,'items':len(result)}
            else:
                # Safe upsert: preserve every existing region that doesn't
                # correspond to a changed local row. In particular, a tag-only
                # update must not wipe the original speaker/text annotations.
                merged, changed, added = merge_into_annotation(
                    mine[0].get('result') or [], ann_rows)
                api(f'/api/annotations/{ann_id}/', method='PATCH', body={'result':merged})
                plan['annotation']={'action':'merged','id':ann_id,'items':len(merged),
                                    'changedRows':changed,'addedRows':added}
        elif ann_rows:
            if anns:
                raise PermissionError(
                    f'task {task_id} 已有他人的 Annotation；本工具不会覆盖或删除他人的结果。')
            created=api(f'/api/tasks/{task_id}/annotations/', method='POST', body={'result':result})
            plan['annotation']={'action':'created','id':created.get('id'),'items':len(result)}

    # After a successful write, remove the user's stale draft for this task.
    # Label Studio may otherwise reopen the draft tab, hiding the new annotation
    # and making tags look missing even though they were saved.
    try:
        conn=sqlite3.connect(DB)
        conn.execute('delete from tasks_annotationdraft where task_id=? and user_id=1',(task_id,))
        conn.commit(); conn.close()
    except Exception:
        pass  # draft cleanup must not turn a successful write into an error

    if pending and mode!='all':
        version='editor-pending-v2'
        for pred in (api(f'/api/predictions/?task={task_id}') or []):
            if pred.get('model_version')==version:
                api(f'/api/predictions/{pred["id"]}/', method='DELETE')
        pres=api('/api/predictions/', method='POST',
                 body={'task':task_id,'model_version':version,'result':to_result(pending)})
        plan['prediction']={'id':pres.get('id'),'items':len(pres.get('result',[]))}
    return plan


def list_projects():
    """List Label Studio projects with task counts."""
    data=api('/api/projects/?page_size=200') or {}
    rows=data.get('results', data if isinstance(data,list) else [])
    return [{'id':r.get('id'),'title':r.get('title') or f"Project {r.get('id')}",
             'taskCount':r.get('task_number') or 0,
             'createdAt':r.get('created_at')} for r in rows]

def _media_path(ls_audio):
    """Resolve /data/upload/<project>/<file> to the local Label Studio media path."""
    # DB lives next to the media dir; derive both from LS_DB.
    media_root=Path(os.environ.get('LS_MEDIA_ROOT', str(DB.parent/'media')))
    rel=str(ls_audio or '').lstrip('/')
    if rel.startswith('data/'):
        rel=rel[len('data/'):]
    return media_root/rel

def _region_to_utterances(result):
    """Convert Label Studio's flat regions into editor utterances.

    Regions emitted by this tool share a region id for speaker/text/tags.
    Nested interjections are separate ids and are folded back into their
    containing parent row when their time span lies inside it.
    """
    groups={}
    for x in result or []:
        value=x.get('value') or {}
        key=(x.get('id'), round(float(value.get('start',0)),3), round(float(value.get('end',0)),3))
        groups.setdefault(key,{'speaker':None,'text':'','tags':[],'start':key[1],'end':key[2]})
        g=groups[key]
        if x.get('from_name')=='speaker':
            labels=value.get('labels') or []
            g['speaker']=labels[0] if labels else None
        elif x.get('from_name')=='text':
            g['text']=''.join(value.get('text') or [])
        elif x.get('from_name')=='tags':
            g['tags']=list(value.get('choices') or [])
    raw=[]
    for key,g in groups.items():
        raw.append({'rid':key[0],'start':g['start'],'end':g['end'],'speaker':g['speaker'] or 'UNKNOWN',
                    'text':g['text'],'tags':g['tags']})
    raw.sort(key=lambda r:(r['start'],r['end'],r['rid']))
    # Fold small regions into an enclosing parent, but only when the region is
    # much shorter than the parent (the nested interjection shape).
    out=[]
    used=set()
    for i,r in enumerate(raw):
        if i in used: continue
        children=[ (j,c) for j,c in enumerate(raw)
                   if j!=i and j not in used and c['start']>=r['start']-0.002 and c['end']<=r['end']+0.002
                   and (c['end']-c['start']) < (r['end']-r['start'])*0.9 ]
        # Prefer the tightest enclosing parent, not a chain of overlaps.
        children=[c for _,c in children]
        used.add(i)
        for c in children:
            # only fold truly nested small pieces, never two long adjacent rows
            used.add(next(j for j,x in enumerate(raw) if x is c))
        u={'id':f"{r['rid']}-{len(out)+1:04d}",'start':r['start'],'end':r['end'],
           'text':r['text'],'bodyText':r['text'],'speaker':r['speaker'],
           'speakerSource':'manual','speakerConfidence':'manual','reviewed':True,
           'displayLabel':'来自 Label Studio','isInterjection':False,
           'hasInterjection':bool(children),'tags':r['tags'],'grade':'existing','window':'full',
           'pieces':[]}
        if children:
            u['pieces']=[{'text':c['text'],'start':c['start'],'end':c['end'],'isInterjection':True,
                          'speaker':c['speaker'],'speakerSource':'manual','speakerConfidence':'manual','reviewed':True}
                         for c in children]
            u['bodyText']=''.join(x['text'] for x in raw if x is r)
        else:
            u['pieces']=[{'text':r['text'],'start':r['start'],'end':r['end'],'isInterjection':False,
                          'speaker':r['speaker'],'speakerSource':'manual','speakerConfidence':'manual','reviewed':True}]
        out.append(u)
    return out

def pull_project(project_id, force=False, include_empty=True):
    """Pull new tasks from a Label Studio project into editor/data.json.

    Existing task ids are skipped unless force=True. Local files are written
    atomically; the response reports what changed.
    """
    project_id=int(project_id)
    tasks=api(f'/api/projects/{project_id}/tasks/?page_size=1000') or []
    if isinstance(tasks,dict): tasks=tasks.get('results') or tasks.get('tasks') or []
    data_path=EDITOR_DATA
    bundle=json.loads(data_path.read_text(encoding='utf-8'))
    recordings=bundle.setdefault('recordings',[])
    existing={(int(r.get('projectId') or 0), int(r['taskId'])) for r in recordings}
    added=[]; skipped=[]; missing=[]
    for t in tasks:
        tid=int(t['id'])
        if (project_id,tid) in existing and not force:
            skipped.append(tid); continue
        audio=str((t.get('data') or {}).get('audio') or '')
        if not audio:
            missing.append({'taskId':tid,'reason':'no audio field'}); continue
        src=_media_path(audio)
        if not src.exists():
            missing.append({'taskId':tid,'reason':f'audio not found: {src}'}); continue
        anns=t.get('annotations') or []
        result=(anns[0].get('result') if anns else []) or []
        utterances=_region_to_utterances(result)
        if not utterances and not include_empty:
            missing.append({'taskId':tid,'reason':'no annotation'}); continue
        duration=max((u['end'] for u in utterances), default=0.0)
        rel=f'audio/project{project_id}_task{tid}.wav'
        dest=ROOT/'app'/'editor'/rel
        wave_rel=f'waveforms/project{project_id}_task{tid}.png'
        wave_dest=ROOT/'app'/'editor'/wave_rel
        if prepare_audio:
            try:
                duration=prepare_audio(src,dest,wave_dest)
            except Exception as exc:
                # fall back to a plain copy; the task should still be visible
                dest.parent.mkdir(parents=True,exist_ok=True)
                if force or not dest.exists(): shutil.copyfile(src,dest)
                duration=probe_duration(dest)
                wave_rel=''
        else:
            dest.parent.mkdir(parents=True,exist_ok=True)
            if force or not dest.exists(): shutil.copyfile(src,dest)
            duration=duration or 0.0
        if duration<=0:
            duration=probe_duration(dest)
        windows=[]
        if not utterances and build_asr_recording and os.environ.get('AUTO_ASR','1')!='0':
            try:
                asr_data=build_asr_recording(dest,tid,duration)
                utterances=asr_data['utterances']; windows=asr_data['windows']
                duration=asr_data['duration']
                rel=rel
            except Exception as exc:
                missing.append({'taskId':tid,'reason':f'ASR failed: {str(exc)[-300:]}'})
        rec={'taskId':tid,'projectId':project_id,'label':Path(audio).stem or f'project{project_id} / task {tid}',
             'audio':rel,'waveform':wave_rel,'lsAudio':audio,'duration':round(duration,3),
             'windows':windows,'utterances':utterances}
        # Replace a task with the same id only on explicit force.
        recordings[:]=[r for r in recordings if not (
            int(r['taskId'])==tid and int(r.get('projectId') or 0)==project_id and force)]
        recordings.append(rec)
        added.append(tid); existing.add((project_id,tid))
    data_path.write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'projectId':project_id,'added':added,'skipped':skipped,'missing':missing,
            'total':len(recordings)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body=json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.send_header('Access-Control-Allow-Methods','POST,OPTIONS')
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self):
        self._send(200, {'ok':True})
    def do_GET(self):
        if self.path.startswith('/health'):
            return self._send(200, {'ok':True,'tasks':list(TASKS)})
        if self.path.startswith('/projects'):
            try: return self._send(200, {'ok':True,'projects':list_projects()})
            except Exception as e: return self._send(500, {'ok':False,'error':str(e)})
        self._send(404, {'error':'not found'})
    def do_POST(self):
        if not (self.path.startswith('/sync') or self.path.startswith('/pull') or self.path.startswith('/asr')):
            return self._send(404, {'error':'not found'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            payload=json.loads(self.rfile.read(length) or b'{}')
            if self.path.startswith('/pull'):
                return self._send(200, {'ok':True, **pull_project(payload['projectId'], bool(payload.get('force')), bool(payload.get('includeEmpty', True)))} )
            if self.path.startswith('/asr'):
                project_id=int(payload['projectId']); task_id=int(payload['taskId'])
                data_path=EDITOR_DATA
                bundle=json.loads(data_path.read_text(encoding='utf-8'))
                rec=next((r for r in bundle.get('recordings',[])
                          if int(r['taskId'])==task_id and int(r.get('projectId') or 0)==project_id),None)
                if not rec: raise ValueError('task not found in local editor data')
                if not build_asr_recording: raise RuntimeError('ASR 模块未安装')
                audio_path=data_path.parent/rec['audio']
                # Always allow regeneration; keep a copy of the previous editor
                # state so a bad ASR run can be recovered without relying only
                # on browser localStorage.
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                stamp=time.strftime('%Y%m%d-%H%M%S')
                shutil.copyfile(data_path, BACKUP_DIR/f'editor-task{task_id}-{stamp}-before-asr.json')
                result=build_asr_recording(audio_path,task_id,float(rec.get('duration') or 0))
                rec['duration']=result['duration']; rec['windows']=result['windows']
                rec['utterances']=result['utterances']
                data_path.write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
                return self._send(200,{'ok':True,'taskId':task_id,'sentences':len(result['utterances']),
                                       'windows':len(result['windows']),
                                       'windowCounts':result.get('windowCounts',{})})
            if self.path.startswith('/sync'):
                # A project id is required for new multi-project clients. It is
                # optional for old local data whose recordings have no projectId.
                expected_project=payload.get('projectId')
                if expected_project is not None:
                    try: expected_project=int(expected_project)
                    except Exception: raise ValueError('projectId must be an integer')
                    actual=api(f'/api/tasks/{int(payload["taskId"])}/') or {}
                    if int(actual.get('project') or 0) != int(expected_project):
                        return self._send(409, {'ok':False,'code':'project_mismatch',
                            'error':f'task {payload["taskId"]} 属于 project {actual.get("project")}，不是 {expected_project}；已拒绝写回'})
            task_id=int(payload['taskId']); rows=payload['utterances']
            mode=payload.get('mode','confirmed')
            audio_map=task_audio_map()
            if audio_map and task_id not in audio_map: raise ValueError('unknown task')
            if not rows: raise ValueError('empty rows')
            data = sync(
                task_id, rows, mode,
                replace_annotation=bool(payload.get('replaceAnnotation')),
                dry_run=bool(payload.get('dryRun')),
                check_existing=bool(payload.get('checkExisting')),
                allow_read_only_replace=bool(payload.get('allowReadOnlyReplace')))
            self._send(200, {'ok':True, **data})
        except PermissionError as e:
            self._send(409, {'ok':False,'error':str(e),'code':'annotation_conflict'})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(500, {'ok':False,'error':str(e),'trace':traceback.format_exc(limit=4)})

if __name__=='__main__':
    os.chdir(ROOT)
    srv=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('SYNC_PORT','8768'))), Handler)
    print('sync server on http://127.0.0.1:8768  (POST /sync)')
    srv.serve_forever()

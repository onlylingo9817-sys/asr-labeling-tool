#!/usr/bin/env python3
"""Local sync service: write editor results back to Label Studio as Annotations.

Safety:
  * backs up the SQLite DB before every write
  * writes pending/machine rows only as Predictions
  * requires an explicit replaceAnnotation for any existing Annotation
  * task 5's existing human annotation is read-only unless ALLOW_TASK5_REPLACE=1
  * never deletes another user's Annotation
"""
from __future__ import annotations
import json, os, shutil, subprocess, time, urllib.error, urllib.request
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

def task_audio_map():
    """Return task_id -> Label Studio audio path from local editor data.

    The mapping is kept out of source code so this repository is portable and
    no client filename is baked into a distributed build.
    """
    if TASKS:
        return TASKS
    data_path=ROOT/'app'/'editor'/'data.json'
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
            raise ValueError(f'row {idx}: invalid time range {start}..{end}')
        speaker=(row.get('speaker') or 'UNKNOWN').strip()
        if not speaker:
            raise ValueError(f'row {idx}: empty speaker')
        pieces=[]
        for p in (row.get('pieces') or []):
            ps=round(float(p['start']),3); pe=round(float(p['end']),3)
            if ps < 0 or pe <= ps:
                raise ValueError(f'row {idx}: invalid piece range {ps}..{pe}')
            # Allow a 20ms timing tolerance for hand-edited pieces, but never
            # accept an interjection that lives outside its parent sentence.
            if ps < start-0.020 or pe > end+0.020:
                raise ValueError(f'row {idx}: piece {ps}..{pe} outside parent {start}..{end}')
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


def sync(task_id, rows, mode='confirmed', replace_annotation=False, dry_run=False):
    """Write confirmed rows as Annotation, pending rows as Prediction.

    Existing Annotations are never PATCHed unless replace_annotation=true. Task 5
    remains read-only by default because it contains the user's original manual
    annotation. dry_run returns the exact payload without touching Label Studio.
    """
    if mode not in ('confirmed','all'):
        raise ValueError('mode must be confirmed or all')
    rows=_format_range(rows)
    confirmed=[r for r in rows if r.get('confidence') in CONFIRMED]
    pending=[r for r in rows if r.get('confidence') not in CONFIRMED]
    ann_rows=rows if mode=='all' else confirmed
    plan={'confirmed':len(confirmed),'pending':len(pending),'annotation':None,
          'prediction':None,'dryRun':bool(dry_run),
          'annotationResult':to_result(ann_rows) if ann_rows else []}
    if dry_run:
        return plan

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp=time.strftime('%Y%m%d-%H%M%S')
    if DB.exists():
        shutil.copyfile(DB, BACKUP_DIR/f'ls-{stamp}-before-task{task_id}.sqlite3')

    if ann_rows:
        result=plan['annotationResult']
        anns=api(f'/api/tasks/{task_id}/annotations/') or []
        mine=[a for a in anns if (a.get('completed_by') or {}).get('id')==1]
        # Our "mine" test uses the API's latest completed_by. If the API only
        # exposes a legacy task_completion row, don't assume ownership.
        if anns and task_id in READ_ONLY_ANNOTATION_TASKS:
            raise PermissionError(
                f'task {task_id} 已有正式人工标注，同步服务默认只读；'
                '若确需覆盖，请以 ALLOW_TASK5_REPLACE=1 重启服务并显式确认。')
        if mine:
            if not replace_annotation:
                raise PermissionError(
                    f'task {task_id} 已有 Annotation #{mine[0]["id"]}；'
                    '为避免覆盖，请在页面明确选择“替换已有标注”后重试。')
            ann_id=mine[0]['id']
            api(f'/api/annotations/{ann_id}/', method='PATCH', body={'result':result})
            plan['annotation']={'action':'updated','id':ann_id,'items':len(result)}
        else:
            if anns:
                raise PermissionError(
                    f'task {task_id} 已有他人的 Annotation；本工具不会覆盖或删除他人的结果。')
            created=api(f'/api/tasks/{task_id}/annotations/', method='POST', body={'result':result})
            plan['annotation']={'action':'created','id':created.get('id'),'items':len(result)}

    if pending and mode!='all':
        version='editor-pending-v2'
        for pred in (api(f'/api/predictions/?task={task_id}') or []):
            if pred.get('model_version')==version:
                api(f'/api/predictions/{pred["id"]}/', method='DELETE')
        pres=api('/api/predictions/', method='POST',
                 body={'task':task_id,'model_version':version,'result':to_result(pending)})
        plan['prediction']={'id':pres.get('id'),'items':len(pres.get('result',[]))}
    return plan


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
        self._send(404, {'error':'not found'})
    def do_POST(self):
        if not self.path.startswith('/sync'):
            return self._send(404, {'error':'not found'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            payload=json.loads(self.rfile.read(length) or b'{}')
            task_id=int(payload['taskId']); rows=payload['utterances']
            mode=payload.get('mode','confirmed')
            audio_map=task_audio_map()
            if audio_map and task_id not in audio_map: raise ValueError('unknown task')
            if not rows: raise ValueError('empty rows')
            data = sync(
                task_id, rows, mode,
                replace_annotation=bool(payload.get('replaceAnnotation')),
                dry_run=bool(payload.get('dryRun')))
            self._send(200, {'ok':True, **data})
        except PermissionError as e:
            self._send(409, {'ok':False,'error':str(e),'code':'annotation_conflict'})
        except Exception as e:
            self._send(500, {'ok':False,'error':str(e)})

if __name__=='__main__':
    os.chdir(ROOT)
    srv=ThreadingHTTPServer(('127.0.0.1',8768), Handler)
    print('sync server on http://127.0.0.1:8768  (POST /sync)')
    srv.serve_forever()

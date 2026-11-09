#!/usr/bin/env python3
"""Re-recognise ONE sentence: cut [start,end] from a recording and run ASR on just that clip.

Keeps the model loaded between requests (first call loads it, later calls take a few seconds).

  GET  /health
  POST /recognize  {taskId, projectId, start, end, pad? (default 0.05s of context on each side)}  ->
       {ok, text, bodyText, pieces:[{text,start,end,isInterjection}], seconds}
Only text/pieces are returned; speaker, tags and the sentence's own start/end are left to the editor.
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE/'asr_clip_server.py').exists() and (HERE.parent/'scripts').is_dir() and (HERE.parent/'work').is_dir() else HERE.parent.parent
for _cand in (HERE, ROOT/'app', ROOT/'work'):
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
import interjection_nested as inn

EDITOR_DATA = Path(os.environ.get('EDITOR_DATA_PATH') or os.environ.get('EDITOR_DATA_FILE') or
                   str((ROOT/'work' if (ROOT/'work'/'editor').is_dir() else ROOT/'app')/'editor'/'data.json'))
PORT = int(os.environ.get('ASR_CLIP_PORT', '8770'))
DEVICE = os.environ.get('ASR_DEVICE', '')
LOCK = threading.Lock()
MODEL = None
MAX_SECONDS = 60.0

def get_model():
    global MODEL
    if MODEL is None:
        from funasr import AutoModel
        dev = DEVICE or ('mps' if sys.platform == 'darwin' else 'cpu')
        MODEL = AutoModel(model='paraformer-zh', vad_model='fsmn-vad', punc_model='ct-punc',
                          device=dev, disable_update=True, disable_pbar=True, log_level='ERROR')
    return MODEL

def audio_for(task_id, project_id):
    data = json.loads(EDITOR_DATA.read_text(encoding='utf-8'))
    rows = [r for r in data.get('recordings', []) if int(r.get('taskId', -1)) == int(task_id)]
    if project_id not in (None, ''):
        rows = [r for r in rows if int(r.get('projectId') or 0) == int(project_id)]
    if not rows:
        raise ValueError(f'找不到 task {task_id}')
    if len(rows) > 1:
        raise ValueError(f'task {task_id} 属于多个项目，请传 projectId')
    p = Path(rows[0].get('audio') or '')
    return p if p.is_absolute() else EDITOR_DATA.parent/p

def recognize(audio, start, end, pad=0.05):
    start, end = float(start), float(end)
    if end <= start:
        raise ValueError('起止时间无效')
    if end - start > MAX_SECONDS:
        raise ValueError(f'这一句超过 {int(MAX_SECONDS)} 秒，请先拆分再重识别')
    t0 = max(0.0, start - pad); t1 = end + pad
    with tempfile.TemporaryDirectory(prefix='clip-') as tmp:
        wav = Path(tmp)/'clip.wav'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-ss',f'{t0:.3f}','-t',f'{t1-t0:.3f}',
                        '-i',str(audio),'-ac','1','-ar','16000','-c:a','pcm_s16le','-y',str(wav)], check=True)
        with LOCK:
            started = time.time()
            result = get_model().generate(input=str(wav), batch_size_s=300,
                                          return_spk_res=True, sentence_timestamp=True)
        rec = result[0] if isinstance(result, list) and result else {}
    sents = []
    for s in rec.get('sentence_info') or []:
        text = str(s.get('text') or '').strip()
        if not text:
            continue
        stamps = [[t0 + a/1000.0, t0 + b/1000.0] for a, b in (s.get('timestamp') or [])]
        sents.append({'start': round(t0 + float(s.get('start', 0))/1000.0, 3),
                      'end': round(t0 + float(s.get('end', 0))/1000.0, 3),
                      'text': text, 'timestamps': stamps})
    if not sents:
        return {'text': '', 'bodyText': '', 'pieces': [], 'seconds': round(time.time()-started, 1), 'empty': True}
    # The clip may be cut into several ASR sentences: join them into one text and keep every piece.
    pieces = []
    for s in sents:
        pieces.extend(inn.pieces_for(s))
    # clamp to the sentence bounds the user is editing
    out = []
    for p in pieces:
        ps = max(start, min(end, p['start'])); pe = max(start, min(end, p['end']))
        if pe - ps < 0.01:
            continue
        out.append({'text': p['text'], 'start': round(ps, 3), 'end': round(pe, 3), 'isInterjection': bool(p['isInterjection'])})
    body = ''.join(p['text'] for p in out if not p['isInterjection'])
    return {'text': ''.join(p['text'] for p in out), 'bodyText': body, 'pieces': out,
            'seconds': round(time.time()-started, 1), 'empty': not out}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self): self._send(200, {'ok': True})
    def do_GET(self):
        if self.path.split('?')[0] == '/health':
            return self._send(200, {'ok': True, 'modelLoaded': MODEL is not None})
        self._send(404, {'ok': False, 'error': 'not found'})
    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', '0') or 0)
            p = json.loads(self.rfile.read(n) or b'{}')
            if self.path.split('?')[0] != '/recognize':
                return self._send(404, {'ok': False, 'error': 'not found'})
            audio = audio_for(p['taskId'], p.get('projectId'))
            out = recognize(audio, p['start'], p['end'], float(p.get('pad', 0.05)))
            self._send(200, {'ok': True, **out})
        except subprocess.CalledProcessError:
            self._send(400, {'ok': False, 'error': '切音频失败，请确认 ffmpeg 可用、起止时间在录音范围内'})
        except Exception as exc:
            self._send(400, {'ok': False, 'error': str(exc)})

def main():
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'asr clip server on http://127.0.0.1:{PORT}', flush=True)
    srv.serve_forever()

if __name__ == '__main__':
    main()

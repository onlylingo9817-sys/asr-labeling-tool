#!/usr/bin/env python3
"""Global voiceprint enrollment + identification service.

Profiles are stored once in work/voiceprints.json and reused across recordings.

Endpoints (JSON):
  GET  /state                 -> profiles, settings
  POST /enroll                -> {role, taskId, start, end}   add one sample
  POST /delete                -> {role, index?}               remove one sample or whole role
  POST /settings              -> {threshold, maxProfiles}
  POST /apply                 -> {taskId, rows:[{id,start,end}]} -> [{id, speaker, margin}]
"""
from __future__ import annotations
import json, os, pickle, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf
import sherpa_onnx
from funasr import AutoModel

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_FILE = Path(os.environ.get('EDITOR_DATA_FILE') or str(ROOT/'app/editor/data.json'))
def _env_json(name):
    raw = os.environ.get(name)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{name} 必须是合法 JSON') from exc

AUDIO_OVERRIDES = {int(k): ROOT/v for k, v in _env_json('VOICE_AUDIO_JSON').items()}
STORE = Path(os.environ.get('VOICEPRINTS_FILE') or str(ROOT/'work/voiceprints.json'))
SEG_MODEL = Path(os.environ.get('SEG_MODEL') or str(ROOT/'work/models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx'))
EMB_MODEL = Path(os.environ.get('EMB_MODEL') or str(ROOT/'work/models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx'))
DIAR_CACHE = Path(os.environ.get('DIAR_CACHE') or str(ROOT/'work/outputs'))
LOCK = threading.Lock()
MODEL = None
DATA_CACHE = {'key': None, 'recordings': []}

DEFAULT_SETTINGS = {"threshold": 0.06, "maxProfiles": 6}

def recordings():
    """Load editor recordings, refreshing when data.json changes."""
    if not DATA_FILE.exists():
        return []
    stat = DATA_FILE.stat()
    key = (str(DATA_FILE), stat.st_mtime_ns, stat.st_size)
    if DATA_CACHE['key'] != key:
        data = json.loads(DATA_FILE.read_text(encoding='utf-8'))
        DATA_CACHE['recordings'] = data.get('recordings', [])
        DATA_CACHE['key'] = key
    return DATA_CACHE['recordings']

def audio_for(task_id, project_id=None):
    """Resolve a task's audio from editor data, with optional explicit overrides."""
    tid = int(task_id)
    if tid in AUDIO_OVERRIDES:
        return AUDIO_OVERRIDES[tid]
    matches = [rec for rec in recordings() if int(rec.get('taskId', -1)) == tid]
    if project_id not in (None, ''):
        pid = int(project_id)
        matches = [rec for rec in matches if int(rec.get('projectId', 0)) == pid]
    if not matches:
        suffix = f'（项目 {project_id}）' if project_id not in (None, '') else ''
        raise ValueError(f'task {tid}{suffix} 没有可用的音频映射')
    if len(matches) > 1:
        raise ValueError(f'task {tid} 属于多个项目，请传 projectId 后重试')
    raw = str(matches[0].get('audio') or '').strip()
    if not raw:
        raise ValueError(f'task {tid} 的 data.json 缺少 audio 字段')
    src = Path(raw)
    return src if src.is_absolute() else DATA_FILE.parent/src

def model():
    global MODEL
    if MODEL is None:
        MODEL = AutoModel(model='cam++', device='mps', disable_update=True,
                          disable_pbar=True, log_level='ERROR')
    return MODEL

def load():
    if STORE.exists():
        return json.loads(STORE.read_text(encoding='utf-8'))
    return {"settings": dict(DEFAULT_SETTINGS), "profiles": {}}

def save(state):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

def norm(v):
    return v/(np.linalg.norm(v)+1e-9)

WAV_CACHE = {}
EMB_CACHE = {}
SR = 16000
MIN_SPAN = 0.3
PAD = 0.04

def load_wav(src):
    """Return mono 16k float32 samples for src, or None when it needs ffmpeg."""
    src = str(src)
    stat = Path(src).stat()
    key = (src, stat.st_mtime_ns, stat.st_size)
    hit = WAV_CACHE.get(src)
    if hit and hit[0] == key:
        return hit[1]
    samples, rate = sf.read(src, dtype='float32')
    if samples.ndim > 1:
        samples = samples.mean(1)
    if rate != SR:
        samples = None
    WAV_CACHE.clear()
    WAV_CACHE[src] = (key, samples)
    return samples

def _vec(raw):
    return norm(raw.detach().cpu().numpy().reshape(-1))

def embed_file(src, start, end):
    end = max(end, start+MIN_SPAN)
    samples = load_wav(src)
    if samples is not None:
        a = max(0, int((start-PAD)*SR)); b = min(len(samples), int((end+PAD)*SR))
        if b - a < int(0.1*SR):
            b = min(len(samples), a + int(0.1*SR))
        return _vec(model().generate(input=samples[a:b])[0]['spk_embedding'])
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        tmp = Path(f.name)
    try:
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error',
                        '-ss',f'{max(0,start-PAD):.3f}','-to',f'{end+PAD:.3f}',
                        '-i',str(src),'-ac','1','-ar','16000','-c:a','pcm_s16le','-y',str(tmp)],
                       check=True)
        return _vec(model().generate(input=str(tmp))[0]['spk_embedding'])
    finally:
        tmp.unlink(missing_ok=True)

def _cache_path(src):
    stat = Path(src).stat()
    tag = f'{Path(src).stem}_{stat.st_size}_{int(stat.st_mtime)}'
    return DIAR_CACHE/f'vp_emb_{tag}.pkl'

def emb_store(src):
    key = str(src)
    store = EMB_CACHE.get(key)
    if store is None:
        path = _cache_path(src)
        store = {}
        if path.exists():
            try: store = pickle.loads(path.read_bytes())
            except Exception: store = {}
        EMB_CACHE[key] = store
    return store

def save_emb_store(src):
    store = EMB_CACHE.get(str(src))
    if store is None: return
    path = _cache_path(src)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(store))

def embed_cached(src, start, end):
    store = emb_store(src)
    key = (round(float(start), 3), round(float(end), 3))
    vec = store.get(key)
    if vec is None:
        vec = embed_file(src, float(start), float(end))
        store[key] = vec
    return vec

def embed(task_id, start, end, project_id=None):
    src = audio_for(task_id, project_id)
    vec = embed_cached(src, start, end)
    save_emb_store(src)
    return vec

def diarize(task_id, k, project_id=None):
    """Diarize one recording; cache the turns. k<=0 means auto."""
    cache = DIAR_CACHE/f'diar_task{task_id}_k{k}.json'
    if cache.exists():
        return json.loads(cache.read_text(encoding='utf-8'))
    wav = audio_for(task_id, project_id)
    samples, sr = sf.read(str(wav), dtype='float32')
    if samples.ndim > 1: samples = samples.mean(1)
    seg = sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
        num_threads=2)
    emb = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=2)
    cl = sherpa_onnx.FastClusteringConfig(num_clusters=int(k), threshold=0.5)
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=seg, embedding=emb, clustering=cl,
        min_duration_on=0.4, min_duration_off=0.4)
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    turns = [{'start': float(t.start), 'end': float(t.end), 'cluster': int(t.speaker)}
             for t in sd.process(samples).sort_by_start_time()]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(turns, ensure_ascii=False), encoding='utf-8')
    return turns

def normv(v): return v/(np.linalg.norm(v)+1e-9)

LONG_SECONDS = 1.5
SURE_SECONDS = 1.0
SURE_MARGIN = 0.12

def cluster_rows(rows, k):
    """Cluster sentence embeddings.

    Ward clustering on the long sentences (reliable embeddings) defines the
    speakers; every sentence, including short ones, is then assigned to the
    nearest speaker centroid. Each row gets a margin and a `sure` flag so the
    UI can separate confident members from ones that need listening.
    """
    from scipy.cluster.hierarchy import linkage, fcluster
    if not rows:
        raise ValueError('没有可分组的句子')
    src = audio_for(rows[0]['taskId'], rows[0].get('projectId'))
    X = np.stack([embed_cached(src, r['start'], r['end']) for r in rows])
    save_emb_store(src)
    dur = np.array([r['end']-r['start'] for r in rows])
    k = max(1, min(int(k), len(rows)))
    base = np.where(dur >= LONG_SECONDS)[0]
    if len(base) < k*3:
        base = np.where(dur >= SURE_SECONDS)[0]
    if len(base) < max(k, 2):
        base = np.arange(len(rows))
    if k == 1 or len(base) < 2:
        labels = np.zeros(len(rows), dtype=int); margin = np.ones(len(rows))
    else:
        sub = fcluster(linkage(X[base], 'ward'), k, 'maxclust') - 1
        groups = sorted(set(sub.tolist()))
        C = np.stack([normv(X[base][sub == g].mean(0)) for g in groups])
        for _ in range(3):
            lab = (X[base] @ C.T).argmax(1)
            for j in range(len(C)):
                m = lab == j
                if m.sum(): C[j] = normv(X[base][m].mean(0))
        S = X @ C.T
        order = np.argsort(-S, axis=1)
        labels = order[:, 0]
        margin = S[np.arange(len(X)), order[:, 0]] - S[np.arange(len(X)), order[:, 1]]
    return [{**r, 'cluster': int(c), 'margin': round(float(m), 4),
             'sure': bool(d >= SURE_SECONDS and m >= SURE_MARGIN)}
            for r, c, m, d in zip(rows, labels, margin, dur)]

def centroid(vectors, weights):
    w = np.array(weights, dtype='float32')
    m = (np.stack(vectors)*w[:,None]).sum(0)
    return norm(m)

def summarize(state):
    out = {}
    for role, samples in state['profiles'].items():
        out[role] = {"count": len(samples),
                     "seconds": round(sum(s['end']-s['start'] for s in samples), 2),
                     "samples": [{k: v for k, v in s.items() if k != 'vector'}
                                  for s in samples]}
    return {"settings": state['settings'], "profiles": out}

def handle(path, payload):
    with LOCK:
        state = load()
        if path == '/health':
            return {'ok': True}
        if path == '/state':
            return summarize(state)
        if path == '/settings':
            for k in ('threshold','maxProfiles'):
                if k in payload: state['settings'][k] = payload[k]
            save(state)
            return summarize(state)
        if path == '/enroll':
            role = payload['role']; tid = int(payload['taskId'])
            pid = payload.get('projectId')
            items = payload.get('items')
            if items is None:
                items = [{'start': payload['start'], 'end': payload['end']}]
            src = audio_for(tid, pid)
            added = 0
            samples = state['profiles'].setdefault(role, [])
            for item in items:
                start = float(item['start']); end = float(item['end'])
                if end-start < 0.15:
                    continue
                vec = embed_cached(src, start, end)
                samples.append({"taskId": tid, "projectId": pid, "start": start, "end": end,
                                "vector": vec.tolist(), "added": time.strftime('%Y-%m-%d %H:%M:%S')})
                added += 1
            if not added:
                if not samples: state['profiles'].pop(role, None)
                raise ValueError('样本太短（<150ms），请选长一点的句子')
            save_emb_store(src)
            save(state)
            return {**summarize(state), 'added': added}
        if path == '/delete':
            role = payload.get('role')
            if role and role in state['profiles']:
                idx = payload.get('index')
                if idx is None:
                    state['profiles'].pop(role)
                else:
                    state['profiles'][role].pop(int(idx))
                    if not state['profiles'][role]:
                        state['profiles'].pop(role)
                save(state)
            return summarize(state)
        if path == '/group':
            tid = int(payload['taskId'])
            pid = payload.get('projectId')
            k = int(payload.get('k') or 3)
            sentences = payload['sentences']
            rows = [{'id': s_['id'], 'start': float(s_['start']), 'end': float(s_['end']),
                     'text': s_.get('text',''), 'taskId': tid, 'projectId': pid} for s_ in sentences]
            clustered = cluster_rows(rows, max(1, min(6, k)))
            groups = {}
            for r in clustered:
                groups.setdefault(r['cluster'], []).append(r)
            out = []
            for c, rows_c in groups.items():
                dur = sum(r['end']-r['start'] for r in rows_c)
                sure = [r for r in rows_c if r['sure']]
                cands = sorted(sure or rows_c, key=lambda r: -(r['end']-r['start']))
                picked = []
                for r in cands:
                    if len(picked) >= 3: break
                    if r['end']-r['start'] < 1.2 and len(cands) > 3: continue
                    if any(abs(r['start']-p_['start']) < 8 for p_ in picked): continue
                    picked.append(r)
                if not picked and cands: picked = cands[:1]
                picked.sort(key=lambda r: r['start'])
                out.append({'cluster': c, 'count': len(rows_c), 'sure': len(sure), 'seconds': round(dur, 1),
                            'members': [{'id': r['id'], 'start': r['start'], 'end': r['end'],
                                         'sure': r['sure'], 'margin': r['margin']} for r in rows_c],
                            'samples': [{'id': r['id'], 'start': r['start'], 'end': r['end'],
                                         'text': r['text']} for r in picked]})
            out.sort(key=lambda g: -g['seconds'])
            return {'groups': out, 'turns': len(rows), 'clusters': len(groups),
                    'sure': sum(g['sure'] for g in out)}
        if path == '/candidates':
            tid = int(payload['taskId'])
            pid = payload.get('projectId')
            rows = payload['rows']
            scene = payload.get('scene') or {}
            max_customers = scene.get('customers')
            if not state['profiles']:
                raise ValueError('还没有注册声纹样本')
            roles, cents = [], []
            for role, samples in state['profiles'].items():
                if max_customers and role.startswith('CUSTOMER_'):
                    try:
                        if int(role.split('_')[1]) > int(max_customers):
                            continue
                    except ValueError:
                        pass
                if role == 'CAR_VOICE' and scene.get('carVoice') is False:
                    continue
                vecs = [np.array(s['vector'], dtype='float32') for s in samples]
                ws = [s['end']-s['start'] for s in samples]
                roles.append(role); cents.append(centroid(vecs, ws))
            C = np.stack(cents)
            out = []
            for r in rows:
                if float(r['end'])-float(r['start']) < 0.15:
                    out.append({"id": r['id'], "candidates": []}); continue
                v = embed(tid, float(r['start']), float(r['end']), pid)
                sims = C @ v
                order = np.argsort(-sims)[:3]
                out.append({"id": r['id'],
                            "candidates": [{"role": roles[i], "score": round(float(sims[i]), 3)} for i in order]})
            return {"results": out}
        if path == '/apply':
            tid = int(payload['taskId'])
            pid = payload.get('projectId')
            rows = payload['rows']
            scene = payload.get('scene') or {}
            max_customers = scene.get('customers')
            if not state['profiles']:
                raise ValueError('还没有注册任何声纹样本')
            roles, cents = [], []
            for role, samples in state['profiles'].items():
                # scene hint: hide extra customer slots that this recording does not have
                if max_customers:
                    if role.startswith('CUSTOMER_'):
                        try:
                            if int(role.split('_')[1]) > int(max_customers):
                                continue
                        except ValueError:
                            pass
                    if role == 'CAR_VOICE' and scene.get('carVoice') is False:
                        continue
                vecs = [np.array(s['vector'], dtype='float32') for s in samples]
                ws = [s['end']-s['start'] for s in samples]
                roles.append(role); cents.append(centroid(vecs, ws))
            C = np.stack(cents)
            th = float(state['settings'].get('threshold', 0.06))
            out = []
            for r in rows:
                if float(r['end'])-float(r['start']) < 0.15:
                    out.append({"id": r['id'], "speaker": "UNKNOWN", "margin": 0.0})
                    continue
                v = embed(tid, float(r['start']), float(r['end']), pid)
                sims = C @ v
                order = np.argsort(-sims)
                best = roles[order[0]]
                margin = float(sims[order[0]]-sims[order[1]]) if len(roles) > 1 else 1.0
                out.append({"id": r['id'],
                            "speaker": best if margin >= th else "UNKNOWN",
                            "margin": round(margin, 4)})
            return {"results": out, "profiles": summarize(state)['profiles']}
    raise ValueError('unknown endpoint '+path)

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.send_header('Access-Control-Allow-Methods','POST,GET,OPTIONS')
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self): self._send(200, {"ok": True})
    def do_GET(self):
        try: self._send(200, {"ok": True, **handle(self.path.split('?')[0], {})})
        except Exception as e: self._send(500, {"ok": False, "error": str(e)})
    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length','0'))
            payload = json.loads(self.rfile.read(n) or b'{}')
            self._send(200, {"ok": True, **handle(self.path.split('?')[0], payload)})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('VOICEPRINT_PORT', '8769'))
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'voiceprint server on http://127.0.0.1:{port}', flush=True)
    srv.serve_forever()

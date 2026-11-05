#!/usr/bin/env python3
"""Environment & service control for the ASR labeling tool.

Runs on 127.0.0.1:8766 and powers the in-page "环境与服务" panel:
detects ffmpeg / ASR stack / models / Label Studio / services, and performs
install / start / stop / configure actions as background jobs with live logs.
"""
from __future__ import annotations
import json, os, platform, re, shutil, signal, socket, subprocess, sys, threading, time, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('APP_ROOT') or (HERE.parent if (HERE.parent/'scripts').is_dir() else HERE.parent.parent))
APP_DIR = ROOT/'app' if (ROOT/'app').is_dir() else ROOT/'work'
EDITOR_DIR = APP_DIR/'editor'
ENV_FILE = ROOT/'.env'
VENV_DIR = ROOT/'.venv'
STATE_DIR = ROOT/'work'
LOG_DIR = STATE_DIR/'logs'
PID_DIR = STATE_DIR/'pids'
JOB_DIR = STATE_DIR/'jobs'
SYSTEM = platform.system()
PORT = int(os.environ.get('SETUP_PORT', '8766'))

SERVICES = {
    'editor':     {'port': 8765, 'script': 'serve_editor.py',      'label': '编辑器',       'always': True},
    'sync':       {'port': 8768, 'script': 'sync_server.py',       'label': 'Label Studio 同步', 'flag': 'ENABLE_SYNC'},
    'voiceprint': {'port': 8769, 'script': 'voiceprint_server.py', 'label': '声纹分组',      'flag': 'ENABLE_VOICEPRINT'},
}
SECRET_KEYS = {'LS_TOKEN'}
ENV_KEYS = ['VOICE_MODEL', 'ENABLE_SYNC', 'ENABLE_VOICEPRINT', 'LABEL_STUDIO_URL', 'LS_BIN', 'LS_DB', 'LS_MEDIA_ROOT',
            'LS_READ_ONLY_TASKS', 'AUTO_ASR', 'ASR_PYTHON', 'VOICE_PY']
LOCK = threading.RLock()
JOBS: dict[str, dict] = {}


# ---------------------------------------------------------------- .env
def read_env() -> dict:
    out = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out

def write_env(updates: dict) -> None:
    """Update keys in .env, preserving comments and unrelated lines. Values are quoted when needed."""
    lines = ENV_FILE.read_text(encoding='utf-8').splitlines() if ENV_FILE.exists() else []
    seen = set(); out = []
    def fmt(v):
        v = str(v)
        return f'"{v}"' if re.search(r'[\s"\'{}$`\\]', v) else v
    for raw in lines:
        key = raw.split('=', 1)[0].strip() if '=' in raw and not raw.lstrip().startswith('#') else None
        if key in updates:
            seen.add(key)
            if updates[key] is not None:
                out.append(f'{key}={fmt(updates[key])}')
        else:
            out.append(raw)
    for k, v in updates.items():
        if k not in seen and v is not None:
            out.append(f'{k}={fmt(v)}')
    ENV_FILE.write_text('\n'.join(out).rstrip('\n') + '\n', encoding='utf-8')

def env_value(key, default=''):
    """Read a setting. The .env file wins over the inherited process environment,
    because the panel edits .env while this process keeps the values it started with."""
    file_env = read_env()
    if key in file_env and file_env[key] != '':
        return file_env[key]
    return os.environ.get(key) or default


# ---------------------------------------------------------------- detection helpers
def run(cmd, timeout=15, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as exc:
        return 1, str(exc)

def port_open(port) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(('127.0.0.1', port)) == 0

def http_ok(url, timeout=2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False

def python_candidates():
    env = read_env()
    cands = [env.get('ASR_PYTHON') or os.environ.get('ASR_PYTHON'), env.get('VOICE_PY'),
             str(VENV_DIR/'bin'/'python'), str(ROOT/'work'/'asr-venv'/'bin'/'python'),
             str(APP_DIR/'asr-venv'/'bin'/'python')]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and Path(c).exists():
            seen.add(c); out.append(c)
    return out

def probe_python(py):
    code = ("import importlib.util as u,json,sys;"
            "mods=['funasr','torch','torchaudio','numpy','scipy','soundfile','modelscope'];"
            "print(json.dumps({'version':sys.version.split()[0],'mods':{m:bool(u.find_spec(m)) for m in mods}}))")
    rc, out = run([py, '-c', code], timeout=20)
    if rc != 0:
        return None
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return None

def best_python():
    best = None
    for py in python_candidates():
        info = probe_python(py)
        if not info:
            continue
        score = sum(info['mods'].values())
        if best is None or score > best[0]:
            best = (score, py, info)
    return best

VOICE_MODELS = {
    'cam': {'label': 'Cam++（默认，轻量）', 'desc': '体积小（约 28 MB），默认使用。', 'parts': ['cam'], 'size_mb': 28},
    'eres2net_large': {'label': 'ERes2Net-large', 'desc': '模型更大（约 116 MB）。在我们的评测里与 Cam++ 的差异没有统计显著性。', 'parts': ['eres2net_large'], 'size_mb': 116},
    'fusion': {'label': 'Cam++ + ERes2Net 融合', 'desc': '同时运行两个模型，耗时最长；评测里数字上最高，但差异没有统计显著性。', 'parts': ['cam', 'eres2net_large'], 'size_mb': 144},
}
VOICE_PARTS = {
    'cam': {'model_id': 'iic/speech_campplus_sv_zh-cn_16k-common', 'dir': 'speech_campplus_sv_zh-cn_16k-common', 'file': 'campplus_cn_common.bin'},
    'eres2net_large': {'model_id': 'iic/speech_eres2net_large_200k_sv_zh-cn_16k-common', 'dir': 'speech_eres2net_large_200k_sv_zh-cn_16k-common', 'file': 'pretrained_eres2net.pt'},
}

def voice_part_present(part) -> bool:
    spec = VOICE_PARTS[part]
    base = Path(os.environ.get('MODELSCOPE_CACHE') or Path.home()/'.cache'/'modelscope')
    for root in (base/'models', base/'hub'/'models', base/'hub', base):
        for cand in (root/('iic--'+spec['dir'])/'snapshots'/'master'/spec['file'], root/'iic'/spec['dir']/spec['file'], root/spec['dir']/spec['file']):
            if cand.exists():
                return True
    return False

def voice_model_status():
    cur = env_value('VOICE_MODEL', 'cam')
    if cur not in VOICE_MODELS: cur = 'cam'
    return {'current': cur, 'models': [
        {'key': k, 'label': m['label'], 'desc': m['desc'], 'size_mb': m['size_mb'],
         'downloaded': all(voice_part_present(p) for p in m['parts']), 'current': k == cur}
        for k, m in VOICE_MODELS.items()]}

MODEL_IDS = {
    'asr':  ['speech_paraformer-large', 'speech_seaco_paraformer'],
    'vad':  ['speech_fsmn_vad_zh-cn-16k'],
    'punc': ['punc_ct-transformer_cn-en-common-vocab471067-large', 'punc_ct-transformer'],
    'spk':  ['speech_campplus_sv_zh-cn_16k-common'],
}
def model_cache_dirs():
    base = Path(os.environ.get('MODELSCOPE_CACHE') or Path.home()/'.cache'/'modelscope')
    return [base/'models'/'iic', base/'models', base/'hub'/'models'/'iic', base/'hub']

def model_present(keys) -> bool:
    for d in model_cache_dirs():
        if d.is_dir():
            names = [p.name for p in d.iterdir()]
            if any(any(k in n for k in keys) for n in names):
                return True
    return False

def find_label_studio_bin():
    env = env_value('LS_BIN')
    cands = [env, shutil.which('label-studio'), str(Path.home()/'.venvs'/'label-studio'/'bin'/'label-studio'),
             str(VENV_DIR/'bin'/'label-studio')]
    for c in cands:
        if c and Path(c).exists():
            return c
    return ''

def find_ls_data_dir():
    env_db = env_value('LS_DB')
    if env_db and Path(env_db).exists():
        return Path(env_db)
    homes = [Path.home()/'Library'/'Application Support'/'label-studio', Path.home()/'.local'/'share'/'label-studio',
             Path.home()/'.label-studio']
    for h in homes:
        db = h/'label_studio.sqlite3'
        if db.exists():
            return db
    return None

def service_running(name) -> dict:
    spec = SERVICES[name]
    up = http_ok(f"http://127.0.0.1:{spec['port']}/health")
    pid = None
    pf = PID_DIR/f'{name}.pid'
    if pf.exists():
        try: pid = int(pf.read_text().strip() or 0) or None
        except Exception: pid = None
    return {'up': up, 'port_open': port_open(spec['port']), 'pid': pid}


# ---------------------------------------------------------------- status
def status() -> dict:
    env = read_env()
    sync_on = env.get('ENABLE_SYNC', '0') == '1'
    voice_on = env.get('ENABLE_VOICEPRINT', '0') == '1'
    items = []
    def add(key, title, ok, detail='', fix=None, optional=False, group='base'):
        items.append({'key': key, 'title': title, 'state': 'ok' if ok else ('optional' if optional else 'missing'),
                      'detail': detail, 'fix': fix, 'group': group})

    pyv = sys.version.split()[0]
    add('python', f'Python {pyv}', sys.version_info >= (3, 9), '编辑器与同步服务只需标准库', group='base')
    ff = shutil.which('ffmpeg'); fp = shutil.which('ffprobe')
    brew = shutil.which('brew'); apt = shutil.which('apt-get')
    ff_fix = None
    if not (ff and fp):
        if SYSTEM == 'Darwin' and brew: ff_fix = {'action': 'install_ffmpeg', 'label': '一键安装 ffmpeg（Homebrew）'}
        elif SYSTEM == 'Darwin': ff_fix = {'manual': '请先安装 Homebrew（https://brew.sh），再回到这里一键安装 ffmpeg'}
        elif apt: ff_fix = {'manual': '终端运行：sudo apt install ffmpeg（需要管理员权限）'}
        else: ff_fix = {'manual': '请安装 ffmpeg 并确保 ffmpeg / ffprobe 在 PATH 中'}
    add('ffmpeg', 'ffmpeg / ffprobe', bool(ff and fp), ff or '用于音频转码、波形和时长检测', ff_fix, group='base')
    screen = shutil.which('screen')
    add('screen', 'screen（后台托管服务）', bool(screen), screen or 'Linux 需 apt install screen；缺失时仍可前台运行', None, optional=True, group='base')

    best = best_python()
    py_info = best[2] if best else None
    mods = (py_info or {}).get('mods', {})
    asr_ready = bool(py_info and all(mods.get(m) for m in ('funasr', 'torch', 'numpy', 'soundfile', 'scipy')))
    missing_mods = [m for m in ('funasr', 'torch', 'numpy', 'scipy', 'soundfile') if not mods.get(m)]
    if asr_ready:
        add('asr_env', 'ASR / 声纹运行环境', True, f"{best[1]}（Python {py_info['version']}）", group='asr')
    else:
        detail = '未找到可用的 Python 环境' if not best else f"{best[1]} 缺少：{', '.join(missing_mods)}"
        add('asr_env', 'ASR / 声纹运行环境', False, detail,
            {'action': 'install_asr', 'label': '一键安装（约 1–3 GB，需联网）'}, group='asr')
    models_ok = {k: model_present(v) for k, v in MODEL_IDS.items()}
    asr_models = models_ok['asr'] and models_ok['vad'] and models_ok['punc']
    add('asr_models', 'ASR 模型（语音识别/断句/标点）', asr_models,
        '已缓存' if asr_models else '首次使用时会自动下载（约 1–2 GB），也可现在预下载',
        None if asr_models or not asr_ready else {'action': 'download_models', 'label': '预下载模型'}, optional=True, group='asr')
    vm = voice_model_status(); cur = next(m for m in vm['models'] if m['current'])
    add('spk_model', f"声纹模型（{cur['label']}）", cur['downloaded'],
        '已下载' if cur['downloaded'] else f"尚未下载（约 {cur['size_mb']} MB），首次分组时会自动下载",
        None, optional=True, group='asr')
    add('auto_asr', '拉取时自动生成 ASR 底稿', env.get('AUTO_ASR', '1') != '0',
        '没有 Annotation 的 task 拉取后自动识别' if env.get('AUTO_ASR', '1') != '0' else '已关闭：拉取后只带音频，需手动点“生成 ASR 底稿”',
        {'action': 'set_env', 'label': '关闭' if env.get('AUTO_ASR', '1') != '0' else '开启',
         'env': {'AUTO_ASR': '0' if env.get('AUTO_ASR', '1') != '0' else '1'}}, optional=True, group='asr')

    ls_bin = find_label_studio_bin(); ls_url = env_value('LABEL_STUDIO_URL', 'http://localhost:8080').rstrip('/')
    ls_up = http_ok(ls_url + '/health')
    ls_fix = None
    if not ls_up:
        if ls_bin: ls_fix = {'action': 'start_label_studio', 'label': '启动 Label Studio'}
        else: ls_fix = {'action': 'install_label_studio', 'label': '一键安装 Label Studio'}
    add('label_studio', 'Label Studio 连接', ls_up, f'{ls_url} 可访问' if ls_up else (f'{ls_url} 无响应' + ('；已找到程序，可直接启动' if ls_bin else '；尚未安装')),
        ls_fix, optional=not sync_on, group='sync')
    db = find_ls_data_dir()
    add('ls_db', 'Label Studio 数据库', bool(db), str(db) if db else '未找到；非默认位置请在下方“高级配置”填写 LS_DB / LS_MEDIA_ROOT', None, optional=True, group='sync')

    services = []
    for name, spec in SERVICES.items():
        st = service_running(name)
        enabled = spec.get('always') or env.get(spec.get('flag', ''), '0') == '1'
        if name == 'voiceprint':
            blocked = '' if asr_ready else '需先安装 ASR / 声纹运行环境'
        else:
            blocked = ''
        services.append({'name': name, 'label': spec['label'], 'port': spec['port'], 'enabled': bool(enabled),
                         'running': st['up'], 'port_conflict': (st['port_open'] and not st['up']),
                         'blocked': blocked})
    summary_ready = bool(ff and fp)
    return {'ok': True, 'voiceModels': voice_model_status(), 'system': SYSTEM, 'root': str(ROOT), 'items': items, 'services': services,
            'env': {k: env.get(k, '') for k in ENV_KEYS}, 'ready': {'basic': summary_ready, 'asr': asr_ready,
            'sync': ls_up and sync_on, 'voiceprint': voice_on and asr_ready}}


# ---------------------------------------------------------------- jobs
def new_job(title):
    jid = uuid.uuid4().hex[:8]
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    job = {'id': jid, 'title': title, 'state': 'running', 'log': str(JOB_DIR/f'{jid}.log'), 'started': time.time(), 'ended': None, 'ok': None}
    Path(job['log']).write_text('', encoding='utf-8')
    with LOCK:
        JOBS[jid] = job
    return job

def job_view(job, tail=60000):
    p = Path(job['log'])
    text = p.read_text(encoding='utf-8', errors='replace') if p.exists() else ''
    return {**{k: job[k] for k in ('id', 'title', 'state', 'ok', 'started', 'ended')}, 'log': text[-tail:]}

def run_job(job, steps):
    def worker():
        ok = True
        with open(job['log'], 'a', encoding='utf-8') as log:
            for title, cmd, kw in steps:
                log.write(f'\n$ {title}\n'); log.flush()
                try:
                    if callable(cmd):
                        cmd(log)
                    else:
                        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True, cwd=str(ROOT), **kw)
                        rc = p.wait()
                        if rc != 0:
                            log.write(f'\n[失败] 退出码 {rc}\n'); ok = False; break
                except Exception as exc:
                    log.write(f'\n[失败] {exc}\n'); ok = False; break
            log.write('\n[完成]\n' if ok else '\n[未完成]\n')
        with LOCK:
            job['ok'] = ok; job['state'] = 'done' if ok else 'failed'; job['ended'] = time.time()
    threading.Thread(target=worker, daemon=True).start()

def system_python():
    for c in ('/opt/homebrew/bin/python3.12', '/opt/homebrew/bin/python3.11', '/opt/homebrew/bin/python3.10',
              shutil.which('python3.12'), shutil.which('python3.11'), shutil.which('python3.10'), shutil.which('python3'), sys.executable):
        if c and Path(c).exists():
            return c
    return sys.executable

def action_install_asr():
    job = new_job('安装 ASR / 声纹运行环境')
    py = system_python()
    venv_py = str(VENV_DIR/'bin'/'python')
    req = ROOT/'requirements.txt'
    steps = []
    if not Path(venv_py).exists():
        steps.append((f'创建虚拟环境 {VENV_DIR}（使用 {py}）', [py, '-m', 'venv', str(VENV_DIR)], {}))
    steps.append(('升级 pip', [venv_py, '-m', 'pip', 'install', '--upgrade', 'pip'], {}))
    steps.append(('安装依赖（funasr / torch 等，首次约几分钟到十几分钟）', [venv_py, '-m', 'pip', 'install', '-r', str(req)], {}))
    def finish(log):
        write_env({'ASR_PYTHON': venv_py, 'VOICE_PY': venv_py})
        log.write(f'已写入 .env：ASR_PYTHON / VOICE_PY = {venv_py}\n')
    steps.append(('写入配置', finish, {}))
    run_job(job, steps); return job

def action_install_ffmpeg():
    job = new_job('安装 ffmpeg')
    if SYSTEM == 'Darwin' and shutil.which('brew'):
        run_job(job, [('brew install ffmpeg', [shutil.which('brew'), 'install', 'ffmpeg'], {})])
    else:
        def nope(log): raise RuntimeError('当前系统无法自动安装，请按提示手动安装 ffmpeg')
        run_job(job, [('检查系统', nope, {})])
    return job

def action_install_label_studio():
    job = new_job('安装 Label Studio')
    ls_venv = Path.home()/'.venvs'/'label-studio'
    py = system_python()
    steps = []
    if not (ls_venv/'bin'/'python').exists():
        steps.append((f'创建虚拟环境 {ls_venv}', [py, '-m', 'venv', str(ls_venv)], {}))
    steps.append(('升级 pip', [str(ls_venv/'bin'/'python'), '-m', 'pip', 'install', '--upgrade', 'pip'], {}))
    steps.append(('安装 label-studio（体积较大，请耐心等待）', [str(ls_venv/'bin'/'python'), '-m', 'pip', 'install', 'label-studio'], {}))
    def finish(log):
        write_env({'LS_BIN': str(ls_venv/'bin'/'label-studio')})
        log.write('已写入 .env：LS_BIN\n')
    steps.append(('写入配置', finish, {}))
    run_job(job, steps); return job

def action_start_label_studio():
    ls_bin = find_label_studio_bin()
    if not ls_bin:
        raise RuntimeError('未找到 label-studio，请先安装')
    url = env_value('LABEL_STUDIO_URL', 'http://localhost:8080')
    m = re.search(r':(\d+)', url.split('//')[-1]); port = m.group(1) if m else '8080'
    job = new_job('启动 Label Studio')
    log_path = LOG_DIR/'label-studio.log'; LOG_DIR.mkdir(parents=True, exist_ok=True)
    def start(log):
        out = open(log_path, 'ab')
        subprocess.Popen([ls_bin, 'start', '--no-browser', '--port', port], stdout=out, stderr=out, start_new_session=True, cwd=str(ROOT))
        log.write(f'已在后台启动，日志：{log_path}\n等待就绪（首次启动需要初始化，可能要 1–2 分钟）…\n'); log.flush()
        for i in range(120):
            if http_ok(url.rstrip('/') + '/health'):
                log.write(f'Label Studio 已就绪：{url}\n'); return
            time.sleep(2)
        raise RuntimeError('等待超时，请查看日志：' + str(log_path))
    run_job(job, [('启动 Label Studio', start, {})]); return job

def action_download_voice_model(model_key):
    if model_key not in VOICE_MODELS:
        raise RuntimeError('未知的声纹模型')
    job = new_job('下载声纹模型：' + VOICE_MODELS[model_key]['label'])
    best = best_python()
    if not best:
        def nope(log): raise RuntimeError('请先安装 ASR / 声纹运行环境')
        run_job(job, [('检查环境', nope, {})]); return job
    ids = [VOICE_PARTS[p]['model_id'] for p in VOICE_MODELS[model_key]['parts']]
    code = ("from modelscope import snapshot_download\n"
            f"for mid in {ids!r}:\n"
            "    print('下载', mid, flush=True)\n"
            "    print('完成', snapshot_download(mid), flush=True)\n")
    run_job(job, [('下载模型文件（需联网）', [best[1], '-c', code], {})]); return job

def action_download_models():
    job = new_job('预下载 ASR / 声纹模型')
    best = best_python()
    if not best:
        def nope(log): raise RuntimeError('请先安装 ASR / 声纹运行环境')
        run_job(job, [('检查环境', nope, {})]); return job
    code = ("from funasr import AutoModel\n"
            "print('下载 ASR（paraformer + VAD + 标点）…',flush=True)\n"
            "AutoModel(model='paraformer-zh',vad_model='fsmn-vad',punc_model='ct-punc',device='cpu',disable_update=True,disable_pbar=False)\n"
            "print('下载声纹模型（Cam++）…',flush=True)\n"
            "AutoModel(model='cam++',device='cpu',disable_update=True,disable_pbar=False)\n"
            "print('全部模型已就绪')\n")
    run_job(job, [('下载模型（首次需要几分钟）', [best[1], '-c', code], {})]); return job


# ---------------------------------------------------------------- services
def load_env_for_child():
    env = os.environ.copy()
    file_env = read_env()
    for key in ENV_KEYS:
        if key not in file_env:
            env.pop(key, None)
    env.update(file_env)
    return env

def service_command(name):
    spec = SERVICES[name]
    env = read_env()
    if name == 'editor':
        return [sys.executable, str(EDITOR_DIR/'serve_editor.py'), '--port', str(spec['port'])], EDITOR_DIR
    if name == 'sync':
        return [sys.executable, str(APP_DIR/'sync_server.py' if (APP_DIR/'sync_server.py').exists() else APP_DIR/'sync'/'sync_server.py')], (APP_DIR if (APP_DIR/'sync_server.py').exists() else APP_DIR/'sync')
    best = best_python()
    py = env.get('VOICE_PY') or (best[1] if best else sys.executable)
    vp = APP_DIR/'voiceprint_server.py' if (APP_DIR/'voiceprint_server.py').exists() else APP_DIR/'voiceprint'/'voiceprint_server.py'
    return [py, str(vp)], vp.parent

def start_service(name):
    spec = SERVICES[name]
    st = service_running(name)
    if st['up']:
        return {'ok': True, 'message': f"{spec['label']} 已在运行"}
    if st['port_open']:
        raise RuntimeError(f"端口 {spec['port']} 被其他程序占用，请先关闭它")
    if name == 'voiceprint':
        b = best_python()
        if not (b and all(b[2]['mods'].get(m) for m in ('funasr', 'torch', 'soundfile', 'scipy'))):
            raise RuntimeError('声纹分组需要先安装 ASR / 声纹运行环境')
    cmd, cwd = service_command(name)
    LOG_DIR.mkdir(parents=True, exist_ok=True); PID_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR/f'{name}.log', 'ab')
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=load_env_for_child(), stdout=out, stderr=out, start_new_session=True)
    (PID_DIR/f'{name}.pid').write_text(str(proc.pid))
    deadline = time.time() + (90 if name == 'voiceprint' else 20)
    while time.time() < deadline:
        if http_ok(f"http://127.0.0.1:{spec['port']}/health"):
            return {'ok': True, 'message': f"{spec['label']} 已启动"}
        if proc.poll() is not None:
            tail = (LOG_DIR/f'{name}.log').read_text(errors='replace')[-800:]
            raise RuntimeError(f"{spec['label']} 启动失败：\n{tail}")
        time.sleep(0.5)
    raise RuntimeError(f"{spec['label']} 启动超时，请查看 {LOG_DIR/(name+'.log')}")

def stop_service(name):
    spec = SERVICES[name]
    if name == 'editor':
        raise RuntimeError('编辑器就是当前页面所在的服务，不能在这里停止')
    pids = set()
    rc, out = run(['lsof', '-nP', f"-iTCP:{spec['port']}", '-sTCP:LISTEN', '-t'])
    for tok in out.split():
        if tok.isdigit(): pids.add(int(tok))
    killed = 0
    for pid in pids:
        _, cmd = run(['ps', '-p', str(pid), '-o', 'command='])
        if spec['script'] in cmd:
            try: os.kill(pid, signal.SIGTERM); killed += 1
            except ProcessLookupError: pass
    for _ in range(30):
        if not port_open(spec['port']): break
        time.sleep(0.2)
    return {'ok': True, 'message': f"{spec['label']} 已停止" if killed else f"{spec['label']} 没有在运行"}

def enable_service(name, enabled):
    spec = SERVICES[name]; flag = spec.get('flag')
    if not flag: raise RuntimeError('该服务始终启用')
    write_env({flag: '1' if enabled else '0'})
    if enabled:
        try: res = start_service(name)
        except RuntimeError as exc:
            return {'ok': False, 'message': f'已启用，但启动失败：{exc}'}
        return {'ok': True, 'message': '已启用并启动。' + res['message']}
    try: stop_service(name)
    except RuntimeError: pass
    return {'ok': True, 'message': f"已关闭 {spec['label']}"}


# ---------------------------------------------------------------- http
ACTIONS = {'install_asr': action_install_asr, 'install_ffmpeg': action_install_ffmpeg,
           'install_label_studio': action_install_label_studio, 'start_label_studio': action_start_label_studio,
           'download_models': action_download_models}

def handle(method, path, payload):
    if path == '/health':
        return 200, {'ok': True}
    if path == '/status':
        return 200, status()
    if path.startswith('/jobs/'):
        job = JOBS.get(path.split('/')[2])
        return (200, {'ok': True, **job_view(job)}) if job else (404, {'ok': False, 'error': '任务不存在'})
    if method != 'POST':
        return 404, {'ok': False, 'error': 'not found'}
    if path == '/action':
        name = payload.get('action')
        if name == 'set_env':
            env = {k: v for k, v in (payload.get('env') or {}).items() if k in ENV_KEYS}
            write_env(env); return 200, {'ok': True, 'message': '已保存配置'}
        if name not in ACTIONS: return 400, {'ok': False, 'error': f'未知操作 {name}'}
        with LOCK:
            running = [j for j in JOBS.values() if j['state'] == 'running']
        if running: return 409, {'ok': False, 'error': f"已有任务在进行：{running[0]['title']}"}
        try: job = ACTIONS[name]()
        except Exception as exc: return 400, {'ok': False, 'error': str(exc)}
        return 200, {'ok': True, 'job': job['id']}
    if path == '/service':
        name = payload.get('name'); op = payload.get('op')
        if name not in SERVICES: return 400, {'ok': False, 'error': '未知服务'}
        try:
            if op == 'start': res = start_service(name)
            elif op == 'stop': res = stop_service(name)
            elif op == 'restart':
                try: stop_service(name)
                except RuntimeError: pass
                res = start_service(name)
            elif op == 'enable': res = enable_service(name, True)
            elif op == 'disable': res = enable_service(name, False)
            else: return 400, {'ok': False, 'error': '未知操作'}
        except Exception as exc:
            return 400, {'ok': False, 'error': str(exc)}
        return 200, res
    if path == '/voice_model':
        key = payload.get('model')
        if key not in VOICE_MODELS:
            return 400, {'ok': False, 'error': '未知的声纹模型'}
        missing = [p for p in VOICE_MODELS[key]['parts'] if not voice_part_present(p)]
        if missing:
            with LOCK:
                running = [j for j in JOBS.values() if j['state'] == 'running']
            if running: return 409, {'ok': False, 'error': f"已有任务在进行：{running[0]['title']}"}
            try: job = action_download_voice_model(key)
            except Exception as exc: return 400, {'ok': False, 'error': str(exc)}
            return 200, {'ok': True, 'job': job['id'], 'needsDownload': True,
                         'message': '模型尚未下载，已开始下载。下载完成后请再点一次「使用」。'}
        write_env({'VOICE_MODEL': key})
        restarted = False
        if service_running('voiceprint')['up']:
            try:
                try: stop_service('voiceprint')
                except RuntimeError: pass
                start_service('voiceprint'); restarted = True
            except Exception as exc:
                return 200, {'ok': True, 'message': f'已切换为 {VOICE_MODELS[key]["label"]}，但声纹服务重启失败：{exc}', 'restarted': False}
        return 200, {'ok': True, 'restarted': restarted,
                     'message': f'已切换为 {VOICE_MODELS[key]["label"]}' + ('，声纹服务已重启。' if restarted else '；下次启用声纹服务时生效。') + '更换模型后，需要重新点一次「开始分组」。'}
    if path == '/config':
        env = {k: (v if v != '' else None) for k, v in (payload.get('env') or {}).items() if k in ENV_KEYS}
        write_env(env); return 200, {'ok': True, 'message': '已保存。部分设置需重启对应服务后生效。'}
    return 404, {'ok': False, 'error': 'not found'}

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
    def _dispatch(self, method):
        try:
            n = int(self.headers.get('Content-Length', '0') or 0)
            payload = json.loads(self.rfile.read(n) or b'{}') if method == 'POST' else {}
            code, obj = handle(method, self.path.split('?')[0], payload)
            self._send(code, obj)
        except Exception as exc:
            self._send(500, {'ok': False, 'error': str(exc)})
    def do_GET(self): self._dispatch('GET')
    def do_POST(self): self._dispatch('POST')

def main():
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'setup server on http://127.0.0.1:{PORT}', flush=True)
    srv.serve_forever()

if __name__ == '__main__':
    main()

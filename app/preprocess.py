#!/usr/bin/env python3
"""Audio preparation + optional FunASR draft generation.

This module is intentionally usable from both the development tree
(`work/`) and the standalone delivery tree (`app/`).
"""
from __future__ import annotations

import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent


def _first_existing(items):
    for item in items:
        if item and Path(item).exists():
            return str(item)
    return None


def asr_python() -> str:
    env = os.environ.get('ASR_PYTHON')
    if env:
        return env
    candidates = [
        HERE / 'asr-venv' / 'bin' / 'python',
        PROJECT_ROOT / 'work' / 'asr-venv' / 'bin' / 'python',
        PROJECT_ROOT / '.venv' / 'bin' / 'python',
    ]
    found = _first_existing(candidates)
    return found or sys.executable


def asr_script() -> str:
    env = os.environ.get('ASR_SCRIPT')
    if env:
        return env
    candidates = [
        HERE / 'interjection_nested.py',
        PROJECT_ROOT / 'work' / 'interjection_nested.py',
    ]
    found = _first_existing(candidates)
    if not found:
        raise RuntimeError('找不到 interjection_nested.py；请设置 ASR_SCRIPT')
    return found


def probe_duration(path: Path) -> float:
    p = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=nw=1:nk=1', str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def prepare_audio(source: Path, dest: Path, waveform: Path) -> float:
    """Normalize to 16-bit mono 16k WAV and render a waveform PNG."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    waveform.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(source),
         '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-y', str(dest)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(dest),
         '-filter_complex', 'showwavespic=s=1400x160:colors=#1d4ed8',
         '-frames:v', '1', '-y', str(waveform)],
        check=True, capture_output=True,
    )
    return probe_duration(dest)


def run_asr(audio: Path, timeout: float | None = None) -> dict:
    """Run interjection_nested.py and return its JSON payload."""
    py = asr_python()
    script = asr_script()
    timeout = timeout or float(os.environ.get('ASR_TIMEOUT', '3600'))
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        out = Path(f.name)
    try:
        p = subprocess.run(
            [py, script, '--audio', str(audio), '--out', str(out)],
            capture_output=True, text=True, timeout=timeout,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or 'ASR subprocess failed')[-1600:])
        payload = json.loads(out.read_text(encoding='utf-8'))
        if not payload.get('sentences'):
            raise RuntimeError('ASR 没有识别出句子')
        return payload
    finally:
        out.unlink(missing_ok=True)


def _segments(duration: float):
    """Import the shared 3-window rule without depending on cwd."""
    try:
        from make_segments import segments
        return segments(duration)
    except Exception:
        # Minimal fallback if the helper is not packaged.
        if duration <= 0:
            return []
        length = 180.0 if duration >= 540 else max(1.0, duration * 0.6 / 3)
        if duration < length * 3:
            gap = max(0.0, (duration - length * 3) / 2)
        else:
            gap = 0.0
        return [
            {'name': 'front', 'start': round(gap, 3), 'end': round(gap + length, 3)},
            {'name': 'middle', 'start': round(duration / 2 - length / 2, 3),
             'end': round(duration / 2 + length / 2, 3)},
            {'name': 'tail', 'start': round(duration - gap - length, 3),
             'end': round(duration - gap, 3)},
        ]


def asr_utterances(payload: dict, task_id: int, duration: float):
    """Convert nested-ASR output into editor utterances."""
    windows = _segments(duration)
    rows = []
    for s in payload.get('sentences') or []:
        start = round(float(s.get('start', 0)), 3)
        end = round(float(s.get('end', start)), 3)
        if end <= start:
            continue
        pieces = []
        for p in s.get('pieces') or []:
            ps = round(float(p.get('start', start)), 3)
            pe = round(float(p.get('end', end)), 3)
            if pe <= ps:
                continue
            pieces.append({
                'text': str(p.get('text') or ''),
                'start': ps, 'end': pe,
                'isInterjection': bool(p.get('isInterjection')),
                'speaker': 'UNKNOWN',
                'speakerSource': 'auto',
                'speakerConfidence': 'low',
                'reviewed': False,
            })
        if not pieces:
            pieces = [{'text': str(s.get('text') or ''), 'start': start, 'end': end,
                       'isInterjection': False, 'speaker': 'UNKNOWN',
                       'speakerSource': 'auto', 'speakerConfidence': 'low',
                       'reviewed': False}]
        body = ''.join(p['text'] for p in pieces if not p['isInterjection'])
        has_int = any(p['isInterjection'] for p in pieces)
        mid = (start + end) / 2
        window = next((w['name'] for w in windows if w['start'] <= mid < w['end']), 'full')
        rows.append({
            'id': f'{task_id}-{len(rows)+1:04d}',
            'start': start, 'end': end,
            'text': str(s.get('text') or ''),
            'bodyText': body or str(s.get('text') or ''),
            'pieces': pieces,
            'speaker': 'UNKNOWN',
            'speakerSource': 'auto',
            'speakerConfidence': 'low',
            'isInterjection': False,
            'hasInterjection': has_int,
            'tags': [], 'reviewed': False,
            'grade': 'risky' if has_int else ('likely' if end - start >= 3 else 'unknown'),
            'displayLabel': '含插话' if has_int else ('机器建议' if end - start >= 3 else '待核'),
            'window': window,
        })
    return rows, windows


def build_asr_recording(audio: Path, task_id: int, duration: float | None = None):
    payload = run_asr(audio)
    duration = duration or float(payload.get('duration') or probe_duration(audio))
    rows, windows = asr_utterances(payload, task_id, duration)
    return {'duration': round(duration, 3), 'windows': windows, 'utterances': rows,
            'engine': payload.get('engine', 'FunASR')}

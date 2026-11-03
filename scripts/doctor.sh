#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then set -a; source "$ROOT/.env"; set +a; fi
ok=0
echo "== ASR Labeling Tool doctor =="
check_cmd(){ if command -v "$1" >/dev/null 2>&1; then echo "[ok] $2: $(command -v "$1")"; else echo "[missing] $2"; ok=1; fi; }
check_cmd python3 "Python 3"
check_cmd ffmpeg "ffmpeg (audio normalization)"
check_cmd ffprobe "ffprobe (duration)"
if command -v screen >/dev/null 2>&1; then echo "[ok] screen (background service)"; else echo "[optional] screen: on Linux install screen or run editor in foreground"; fi
echo
echo "-- editor sample --"
python3 - "$ROOT" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); p=root/'app/editor/data.json'
try:
 d=json.loads(p.read_text()); rs=d['recordings']; print(f'[ok] data.json: {len(rs)} recordings, {sum(len(r["utterances"]) for r in rs)} utterances')
 a=root/'app/editor'/rs[0]['audio'] if rs else None
 print('[ok] sample audio' if a and a.exists() else '[missing] sample audio')
except Exception as e:
 print('[error] data.json:',e); raise SystemExit(1)
PY
echo
echo "-- optional: ASR --"
ASR_PY="${ASR_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$ASR_PY" ]]; then ASR_PY="$(command -v python3)"; fi
if "$ASR_PY" - <<'PY' >/dev/null 2>&1
import funasr
PY
then echo "[ok] funasr available via $ASR_PY"; else echo "[optional] funasr missing (ASR disabled; run scripts/setup.sh asr to enable)"; fi
echo
echo "-- optional: Label Studio --"
if [[ "${ENABLE_SYNC:-0}" == "1" ]]; then
  if curl -fsS --max-time 2 "${LABEL_STUDIO_URL:-http://localhost:8080}/health" >/dev/null 2>&1; then
    echo "[ok] Label Studio: ${LABEL_STUDIO_URL:-http://localhost:8080}"
  else
    echo "[missing] Label Studio not reachable at ${LABEL_STUDIO_URL:-http://localhost:8080}"; ok=1
  fi
  if [[ -x "$(command -v "${LS_BIN:-label-studio}")" ]] || command -v "${LS_BIN:-label-studio}" >/dev/null 2>&1; then echo "[ok] label-studio CLI"; else echo "[missing] label-studio CLI (set LS_BIN)"; ok=1; fi
else
  echo "[disabled] sync (set ENABLE_SYNC=1 in .env to use Label Studio)"
fi
echo
if (( ok )); then echo "RESULT: NOT READY (see missing items above)"; exit 1; else echo "RESULT: READY"; fi

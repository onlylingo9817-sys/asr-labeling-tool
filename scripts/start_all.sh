#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/work/logs"
PID_DIR="$ROOT/work/pids"
RUN_DIR="$ROOT/work/run"
mkdir -p "$LOG_DIR" "$PID_DIR" "$RUN_DIR"
if [[ -f "$ROOT/.env" ]]; then set -a; source "$ROOT/.env"; set +a; fi
source "$ROOT/scripts/screen_lib.sh"

start_one() {
  local name="$1" port="$2" workdir="$3" python="$4" script="$5"; shift 5
  local session="asr-lab-${name}-${port}" launcher="$RUN_DIR/$name.sh" pidfile="$PID_DIR/$name.pid" logfile="$LOG_DIR/$name.log"
  if session_running "$session"; then
    if curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      echo "[ok] $name already running ($session, port $port)"
      lsof -nP -iTCP:"$port" -sTCP:LISTEN -t | head -1 >"$pidfile"
      return 0
    fi
    echo "[error] $name session exists but health failed; run scripts/stop_all.sh" >&2; return 1
  fi
  if curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "[ok] $name already running (port $port)"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t | head -1 >"$pidfile"; return 0
  fi
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[error] port $port is occupied by another process" >&2; return 1
  fi
  {
    printf '#!/usr/bin/env bash\ncd %q\nexec ' "$workdir"
    printf '%q ' "$python" "$script"
    if (( $# )); then printf '%q ' "$@"; fi
    printf '>> %q 2>&1\n' "$logfile"
  } >"$launcher"
  chmod +x "$launcher"
  screen -dmS "$session" "$launcher"
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      lsof -nP -iTCP:"$port" -sTCP:LISTEN -t | head -1 >"$pidfile"
      echo "[ok] $name started ($session, port $port)"; return 0
    fi
    if ! session_running "$session"; then
      echo "[error] $name exited; see $logfile" >&2; tail -n 30 "$logfile" >&2 || true; return 1
    fi
    sleep 1
  done
  echo "[error] $name did not become healthy on port $port" >&2; return 1
}

start_one editor 8765 "$ROOT/app/editor" python3 serve_editor.py --port 8765
start_one setup 8766 "$ROOT/app" python3 setup_server.py
if [[ "${ENABLE_SYNC:-0}" == "1" ]]; then
  start_one sync 8768 "$ROOT/app/sync" python3 sync_server.py
else
  echo "[skip] sync (set ENABLE_SYNC=1 in .env to write back to Label Studio)"
fi
if [[ "${ENABLE_VOICEPRINT:-0}" == "1" ]]; then
  VOICE_PY="${VOICE_PY:-${ASR_PYTHON:-}}"
  if [[ -z "$VOICE_PY" && -x "$ROOT/.venv/bin/python" ]]; then VOICE_PY="$ROOT/.venv/bin/python"; fi
  start_one voiceprint 8769 "$ROOT/app/voiceprint" "${VOICE_PY:-python3}" voiceprint_server.py
else
  echo "[skip] voiceprint (set ENABLE_VOICEPRINT=1 in .env)"
fi
echo "Editor: http://127.0.0.1:8765/  （右上角「⚙ 环境与服务」可检测/安装/启用各项能力）"

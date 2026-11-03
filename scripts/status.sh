#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/screen_lib.sh"
if [[ -f "$ROOT/.env" ]]; then set -a; source "$ROOT/.env"; set +a; fi
check() {
  local name="$1" port="$2" enabled="$3"; local session="asr-lab-${name}-${port}"
  if [[ "$enabled" != "1" ]]; then echo "[disabled] $name"; return; fi
  if session_running "$session" && curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "[running] $name port=$port"
  elif lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[external] $name port=$port occupied"
  else echo "[stopped] $name port=$port"; fi
}
check editor 8765 1
check sync 8768 "${ENABLE_SYNC:-0}"
check voiceprint 8769 "${ENABLE_VOICEPRINT:-0}"

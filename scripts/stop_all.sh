#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/screen_lib.sh"
if [[ -f "$ROOT/.env" ]]; then set -a; source "$ROOT/.env"; set +a; fi

stop_one() {
  local name="$1" port="$2" script="$3" session="asr-lab-$1-$2"
  if session_running "$session"; then
    screen -S "$session" -X quit 2>/dev/null || true
  fi
  # screen can leave the child alive on this macOS build; verify by port and
  # only kill the exact expected script.
  for _ in $(seq 1 20); do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || break
    for pid in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true); do
      cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      if [[ "$cmd" == *"$script"* ]]; then kill "$pid" 2>/dev/null || true; fi
    done
    sleep 0.2
  done
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[error] $name still listening on port $port" >&2; return 1
  fi
  echo "[stopped] $name (port $port)"
}

stop_one editor 8765 serve_editor.py
[[ "${ENABLE_SYNC:-0}" == "1" ]] && stop_one sync 8768 sync_server.py || true
[[ "${ENABLE_VOICEPRINT:-0}" == "1" ]] && stop_one voiceprint 8769 voiceprint_server.py || true

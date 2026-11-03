#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:-all}"
case "$target" in
  asr|all)
    python3 -m venv "$ROOT/.venv"
    "$ROOT/.venv/bin/python" -m pip install --upgrade pip
    "$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
    echo "ASR Python environment: $ROOT/.venv/bin/python"
    ;;
  base) ;;
  *) echo "usage: $0 [base|asr|all]" >&2; exit 2 ;;
esac
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg missing. On macOS: brew install ffmpeg; Ubuntu: sudo apt install ffmpeg" >&2
fi
echo "Next: cp .env.example .env; ./scripts/doctor.sh; ./scripts/start_all.sh"

#!/usr/bin/env bash
# screen's macOS build emits CRLF and spaces ("There is a screen on:"); use
# fixed, grep -a matching to avoid locale/regex surprises.
session_running() {
  local name="$1" out
  out="$(screen -ls 2>/dev/null | tr -d '\r' || true)"
  [[ "$out" == *"$name"* ]]
}

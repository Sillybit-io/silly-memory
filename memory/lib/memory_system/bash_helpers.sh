#!/usr/bin/env bash
set -euo pipefail
LC_ALL=C
export LC_ALL
export MEM_HELPERS_LOADED=1

# ---------------------------------------------------------------------------
# Internal: nanosecond timestamp via python3 (darwin-portable)
# ---------------------------------------------------------------------------
_mem_ns() {
  python3 -c "import time;print(int(time.time()*1e9))"
}

# ---------------------------------------------------------------------------
# Step-logging trio
# ---------------------------------------------------------------------------
mem_step_start() {
  # $1 = "K/M"  $2 = description
  __MEM_STEP_T0=$(_mem_ns)
  __MEM_STEP_LABEL="$2"
  printf '[%s] %s… ' "$1" "$2" >&2
}

mem_step_ok() {
  local now ms
  now=$(_mem_ns)
  ms=$(( (now - __MEM_STEP_T0) / 1000000 ))
  printf '✅ (%sms)\n' "$ms" >&2
  return 0
}

mem_step_fail() {
  local now ms
  now=$(_mem_ns)
  ms=$(( (now - __MEM_STEP_T0) / 1000000 ))
  printf '❌ (%sms)\n' "$ms" >&2
  return 1
}

# mem_run_step "K/M" "description" command...
# Returns the command's own status, so callers can pass a failure's exit code on.
mem_run_step() {
  local km desc rc=0
  km="$1"; desc="$2"; shift 2
  mem_step_start "$km" "$desc"
  "$@" || rc=$?
  if [ "$rc" -eq 0 ]; then
    mem_step_ok
  else
    mem_step_fail || true
  fi
  return "$rc"
}

# ---------------------------------------------------------------------------
# Root guard
# ---------------------------------------------------------------------------
mem_refuse_root() {
  if [[ $(id -u) -eq 0 ]]; then
    echo "refuse to run as root" >&2
    exit 64
  fi
}

# ---------------------------------------------------------------------------
# mkdir-based atomic lock / unlock
# ---------------------------------------------------------------------------
mem_lock() {
  # $1 = lock_path  $2 = timeout_seconds
  local lock_path="$1"
  local timeout_sec="$2"
  local elapsed=0

  while ! mkdir "$lock_path" 2>/dev/null; do
    sleep 0.1
    elapsed=$(( elapsed + 1 ))
    # timeout_sec * 10 deciseconds
    if [[ $elapsed -ge $(( timeout_sec * 10 )) ]]; then
      echo "mem_lock: timed out waiting for $lock_path" >&2
      exit 73
    fi
  done
  return 0
}

mem_unlock() {
  rmdir "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------
mem_atomic_write() {
  # $1 = dest  $2 = content
  local dest="$1"
  local content="$2"
  printf '%s' "$content" > "${dest}.new"
  python3 -c "
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
os.fsync(fd)
os.close(fd)
" "${dest}.new"
  mv -f "${dest}.new" "${dest}"
}

# ---------------------------------------------------------------------------
# Symlink escape guard
# ---------------------------------------------------------------------------
mem_check_symlink_escape() {
  # $1 = root  $2 = path
  local root="$1"
  local path="$2"
  local real_root real_path
  real_root=$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$root")
  real_path=$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$path")
  case "$real_path" in
    "$real_root"/*|"$real_root")
      ;;
    *)
      echo "mem_check_symlink_escape: $path escapes root $root" >&2
      exit 65
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Backup management
# ---------------------------------------------------------------------------
mem_count_backups() {
  # $1 = dir  $2 = pattern
  find "$1" -maxdepth 1 -name "$2" -type d 2>/dev/null | wc -l | tr -d ' '
}

mem_prune_backups() {
  # $1 = dir  $2 = pattern  $3 = keep_n
  local dir="$1"
  local pattern="$2"
  local keep_n="$3"

  # Build mtime-sorted list: newest first
  # darwin stat: -f "%m %N"
  local sorted
  sorted=$(
    find "$dir" -maxdepth 1 -name "$pattern" -type d -print0 2>/dev/null \
      | xargs -0 stat -f "%m %N" 2>/dev/null \
      | sort -rn
  )

  if [[ -z "$sorted" ]]; then
    return 0
  fi

  # Skip first keep_n lines, rm -rf the rest
  echo "$sorted" \
    | tail -n "+$(( keep_n + 1 ))" \
    | cut -d' ' -f2- \
    | xargs -I{} rm -rf "{}"
}

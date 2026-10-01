#!/usr/bin/env bash
# Wire (or remove) the silly-memory zsh helpers into ~/.zshrc.
# Idempotent: re-running is a no-op once the managed block is current.
# Honours --dry-run, --remove, and --rc <path> overrides.
set -euo pipefail
LC_ALL=C
export LC_ALL

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./lib/memory_system/bash_helpers.sh disable=SC1091
source "${SELF_DIR}/lib/memory_system/bash_helpers.sh"

mem_refuse_root

DRY_RUN=0
REMOVE=0
RC="${HOME}/.zshrc"
# The block sources the installed helpers, wherever this script runs from.
MEMORY_HOME_DIR="${SILLY_MEMORY_HOME:-${HOME}/.silly-memory}"
TX_PY="${SELF_DIR}/lib/memory_system/system/install_transaction.py"
OWNERSHIP="${SILLY_MEMORY_OWNERSHIP:-}"

usage() {
  cat >&2 <<'EOF'
Usage: install-zsh.sh [--dry-run] [--remove] [--rc <path>] [-h|--help]

  --dry-run    Print all 4 steps without writing to the rc file.
  --remove     Remove the silly-memory block.
  --rc <path>  Override target rc file (default: $HOME/.zshrc).
  -h, --help   Show this help.

The block sources memory.zsh from SILLY_MEMORY_HOME (default ~/.silly-memory).
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --remove)  REMOVE=1;  shift ;;
    --rc)
      shift
      if [ "$#" -eq 0 ]; then
        echo "error: --rc requires <path>" >&2
        exit 64
      fi
      RC="$1"
      shift
      ;;
    --rc=*) RC="${1#--rc=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown flag: $1" >&2; usage; exit 64 ;;
  esac
done

step1_detect_rc() {
  [ -e "$RC" ] || printf '  %s does not exist yet\n' "$RC" >&2
  return 0
}

step2_check_existing() {
  # A malformed block fails here, before anything is written.
  python3 "$TX_PY" validate --check zsh "$RC" >/dev/null
}

step3_apply() {
  local args=(zsh --rc "$RC")
  if [ "$REMOVE" = "1" ]; then
    args+=(--remove)
  else
    args+=(--memory-zsh "${MEMORY_HOME_DIR}/memory.zsh")
  fi
  [ "$DRY_RUN" = "1" ] && args+=(--dry-run)
  [ -n "$OWNERSHIP" ] && args+=(--ownership "$OWNERSHIP")
  python3 "$TX_PY" "${args[@]}" >/dev/null
}

step4_report_hint() {
  return 0
}

mem_run_step "1/4" "Detecting target rc file" step1_detect_rc || exit 1
mem_run_step "2/4" "Checking existing helper block" step2_check_existing || exit 3
mem_run_step "3/4" "Appending (or removing) source line atomically" step3_apply || exit 1
mem_run_step "4/4" "Reporting next-step hint (source $RC, run memhelp)" step4_report_hint || exit 1

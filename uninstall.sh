#!/bin/bash
# Uninstall silly-memory on this machine.
# Removes only install artifacts: the engine in the memory home; the hooks,
# recall rules, skills, and OpenCode plugin the installer added to Cursor, Claude
# Code, and OpenCode, unless you changed them; and the silly-memory MCP server
# each tracked project's own config gained at its first session. Never touches
# user memory data (config.json, _global/, per-workspace dirs) or project markers.
set -euo pipefail
LC_ALL=C
export LC_ALL

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./memory/lib/memory_system/bash_helpers.sh disable=SC1091
source "${HERE}/memory/lib/memory_system/bash_helpers.sh"
export TOTAL_STEPS=7

mem_refuse_root

# Same precedence as the engine: SILLY_MEMORY_HOME, else ~/.silly-memory.
DEST="${SILLY_MEMORY_HOME:-${HOME}/.silly-memory}"
UNINSTALL_PY="${HERE}/memory/lib/memory_system/system/uninstall_transaction.py"

CONFIRM=0
DRY_RUN=0
REMOVE_ZSH_HELPER=0
REMOVE_BACKUPS=0
REMOVE_CACHE=0
KEEP_USER_DATA=0
ARTIFACTS=()
BACKUPS=()
REMOVED_COUNT=0
STEP_EXIT_CODE=1

usage() {
  cat >&2 <<'EOF'
Usage: ./uninstall.sh [--confirm] [--dry-run] [--remove-zsh-helper] [--remove-backups] [--remove-cache] [--keep-user-data]

Removes only install artifacts: the engine in the memory home (SILLY_MEMORY_HOME,
default ~/.silly-memory); the hooks, recall rules, skills, and OpenCode plugin
installed into Cursor, Claude Code, and OpenCode; and the silly-memory MCP server
in each tracked project's .mcp.json, .cursor/mcp.json, and opencode.json. A rule
or skill you edited, and every setting that is not silly-memory's, is kept.

User memory data is always preserved:
  <home>/config.json
  <home>/_global/
  <home>/<workspace-id>/ (memory-bank, events.jsonl, observations.md,
                          work-state.md, context-pack.md, .meta.json,
                          .lock, staging, queues)
  .silly-memory/memory-id in every project

Options:
  --confirm              Required for any filesystem delete.
  --dry-run              Print artifacts that would be removed; touch nothing.
  --remove-zsh-helper    Also remove the silly-memory block from ~/.zshrc.
  --remove-backups       Also remove numbered upgrade backups
                         (<home>.upgrade-backup-*).
  --remove-cache         Also remove the <home>/_embeddings/ cache.
  --keep-user-data       No-op: user data (config.json, _global/, workspaces) is
                         preserved by default. Accepted for explicit/script-friendly
                         invocation.
  -h, --help             Show this help.
EOF
}

die() {
  local code="$1"
  shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$code"
}

log_sub() {
  printf '  %s\n' "$*" >&2
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --confirm) CONFIRM=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --remove-zsh-helper) REMOVE_ZSH_HELPER=1 ;;
      --remove-backups) REMOVE_BACKUPS=1 ;;
      --remove-cache) REMOVE_CACHE=1 ;;
      --keep-user-data)
        # No-op: user data is preserved by default. This flag exists for explicit/script-friendly invocation.
        KEEP_USER_DATA=1
        ;;
      -h|--help) usage; exit 0 ;;
      *)
        usage
        die 64 "unknown argument: $1"
        ;;
    esac
    shift
  done
}

check_symlink_safe() {
  if [ ! -L "$DEST" ]; then
    return 0
  fi
  local real_target real_home
  real_target="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$DEST")"
  real_home="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$HOME")"
  case "$real_target" in
    "$real_home"/*|"$real_home") ;;
    *)
      die 65 "refusing: ${DEST} is a symlink whose target (${real_target}) is outside \$HOME (${real_home})"
      ;;
  esac
}

tool_plan() {
  local args=(--home "$DEST" --repo "$HERE")
  [ "$REMOVE_ZSH_HELPER" = "1" ] && args+=(--remove-zsh)
  python3 "$UNINSTALL_PY" "$@" "${args[@]}"
}

# === Step 1: detect installed components =====================================
step_detect() {
  if [ ! -d "$DEST" ]; then
    log_sub "no install found at ${DEST}"
    return 0
  fi
  log_sub "memory home: ${DEST}"
  if [ -f "${DEST}/VERSION" ]; then
    log_sub "VERSION present: $(tr -d '\r\n' < "${DEST}/VERSION")"
  fi
  if [ -d "${DEST}/bin" ];          then log_sub "found: bin/"; fi
  if [ -d "${DEST}/lib" ];          then log_sub "found: lib/"; fi
  if [ -d "${DEST}/hooks" ];        then log_sub "found: hooks/"; fi
  if [ -d "${DEST}/cursor-extras" ]; then log_sub "found: cursor-extras/"; fi
  if [ -d "${DEST}/_embeddings" ];  then log_sub "found: _embeddings/ (cache)"; fi
}

# === Step 2: compute artifacts list ==========================================
step_compute() {
  ARTIFACTS=()
  BACKUPS=()
  local candidate line plan
  if [ -d "$DEST" ]; then
    for candidate in \
      "${DEST}/bin" \
      "${DEST}/lib" \
      "${DEST}/tests" \
      "${DEST}/hooks" \
      "${DEST}/cursor-extras" \
      "${DEST}/memory.zsh" \
      "${DEST}/install-zsh.sh" \
      "${DEST}/README.md" \
      "${DEST}/VERSION" \
      "${DEST}/.installed-artifacts.json" \
      "${DEST}/.first-run-seen" \
      "${DEST}/.upgraded-from" \
      "${DEST}/.install.lock" \
      "${DEST}/.upgrade.lock"
    do
      if [ -e "$candidate" ] || [ -L "$candidate" ]; then
        ARTIFACTS+=("$candidate")
      fi
    done
    if [ "$REMOVE_CACHE" = "1" ]; then
      if [ -e "${DEST}/_embeddings" ] || [ -L "${DEST}/_embeddings" ]; then
        ARTIFACTS+=("${DEST}/_embeddings")
      fi
    fi
  fi
  if [ "$REMOVE_BACKUPS" = "1" ] && [ -d "$(dirname "$DEST")" ]; then
    while IFS= read -r -d '' backup; do
      BACKUPS+=("$backup")
    done < <(find "$(dirname "$DEST")" -maxdepth 1 -name "$(basename "$DEST").upgrade-backup-*" -type d -print0 2>/dev/null)
  fi

  log_sub "install artifacts to remove: ${#ARTIFACTS[@]}"
  if [ "${#ARTIFACTS[@]}" -gt 0 ]; then
    local item
    for item in "${ARTIFACTS[@]}"; do
      log_sub "  - ${item}"
    done
  fi
  if [ "$REMOVE_BACKUPS" = "1" ]; then
    log_sub "numbered backups to remove: ${#BACKUPS[@]}"
    if [ "${#BACKUPS[@]}" -gt 0 ]; then
      local b
      for b in "${BACKUPS[@]}"; do
        log_sub "  - ${b}"
      done
    fi
  fi

  # Tool artifacts: every shared file is parsed and every removal decided here,
  # before anything is deleted. A malformed file stops the whole uninstall.
  plan="$(tool_plan plan)" || return
  log_sub "tool artifacts (Cursor, Claude Code, OpenCode):"
  while IFS= read -r line; do
    log_sub "  ${line}"
  done <<< "$plan"
}

# === Step 3: confirm intent ==================================================
step_confirm() {
  if [ "$DRY_RUN" = "1" ]; then
    log_sub "dry-run: leaving filesystem unchanged"
    return 0
  fi
  if [ "$CONFIRM" != "1" ]; then
    printf 'ERROR: --confirm required for uninstall. Re-run with --dry-run to preview, or --confirm to proceed.\n' >&2
    return 1
  fi
  log_sub "confirmed: proceeding with uninstall"
}

# === Step 4: remove tool artifacts and settings entries ======================
step_remove_tool_artifacts() {
  if [ "$DRY_RUN" = "1" ]; then
    log_sub "dry-run: not removing"
    return 0
  fi
  # Runs before the engine goes: the ownership record lives in the memory home.
  tool_plan apply >/dev/null || return
  log_sub "removed silly-memory hooks, rules, skills, plugin, and MCP entries"
}

# === Step 5: remove install artifacts (atomic mv -> tmp -> rm) ===============
step_remove_artifacts() {
  if [ "$DRY_RUN" = "1" ]; then
    log_sub "dry-run: not removing"
    return 0
  fi
  REMOVED_COUNT=0
  if [ "${#ARTIFACTS[@]}" -eq 0 ] && [ "${#BACKUPS[@]}" -eq 0 ]; then
    log_sub "nothing to remove"
    return 0
  fi
  local stage
  stage="$(mktemp -d "${TMPDIR:-/tmp}/silly-memory-uninstall.XXXXXX")"
  if [ -z "$stage" ] || [ ! -d "$stage" ]; then
    die 73 "failed to create staging dir"
  fi
  log_sub "staging removals at: ${stage}"
  local idx=0
  local item count base staged
  for item in ${ARTIFACTS[@]+"${ARTIFACTS[@]}"} ${BACKUPS[@]+"${BACKUPS[@]}"}; do
    [ -n "$item" ] || continue
    if [ ! -e "$item" ] && [ ! -L "$item" ]; then
      continue
    fi
    idx=$(( idx + 1 ))
    base="$(basename "$item")"
    staged="${stage}/${base}.${idx}"
    mv "$item" "$staged"
    if [ -d "$staged" ] && [ ! -L "$staged" ]; then
      count=$(find "$staged" -type f 2>/dev/null | wc -l | tr -d ' ')
    else
      count=1
    fi
    REMOVED_COUNT=$(( REMOVED_COUNT + count ))
  done
  if [ -n "$stage" ] && [ -d "$stage" ]; then
    rm -rf -- "$stage"
  fi
  log_sub "removed ${REMOVED_COUNT} install-artifact files"
}

# === Step 6: report the ~/.zshrc helper block ================================
step_report_zsh_helper() {
  if [ "$REMOVE_ZSH_HELPER" != "1" ]; then
    log_sub "skipped (no --remove-zsh-helper)"
  elif [ "$DRY_RUN" = "1" ]; then
    log_sub "dry-run: the tool plan above lists the ~/.zshrc block"
  else
    log_sub "silly-memory block removed from ~/.zshrc (if it was there)"
  fi
}

# === Step 7: verify user memory dir untouched ================================
step_verify_user_data() {
  if [ ! -d "$DEST" ]; then
    log_sub "memory home no longer exists"
    return 0
  fi
  local preserved=()
  if [ -f "${DEST}/config.json" ]; then
    preserved+=("${DEST}/config.json")
  fi
  if [ -d "${DEST}/_global" ]; then
    preserved+=("${DEST}/_global/")
  fi
  if [ -d "${DEST}/_embeddings" ]; then
    preserved+=("${DEST}/_embeddings/ (cache)")
  fi
  local entry name
  while IFS= read -r -d '' entry; do
    name="$(basename "$entry")"
    case "$name" in
      bin|lib|tests|hooks|cursor-extras|_global|_embeddings|queues)
        ;;
      *)
        preserved+=("${entry}/")
        ;;
    esac
  done < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)

  log_sub "user-data items preserved: ${#preserved[@]}"
  if [ "${#preserved[@]}" -gt 0 ]; then
    local p
    for p in "${preserved[@]}"; do
      log_sub "  + ${p}"
    done
  fi
}

# === Main ====================================================================
parse_args "$@"
[ "${KEEP_USER_DATA:-0}" = "1" ] && printf '%s\n' "→ --keep-user-data: user data will be preserved (default behavior)."
check_symlink_safe

mem_run_step "1/7" "Detecting installed components" step_detect || exit "$STEP_EXIT_CODE"
mem_run_step "2/7" "Computing artifacts to remove (and checking shared settings)" step_compute || exit $?
mem_run_step "3/7" "Confirming intent" step_confirm || exit "$STEP_EXIT_CODE"
mem_run_step "4/7" "Removing tool hooks, rules, skills, plugin, and MCP entries" step_remove_tool_artifacts || exit $?
mem_run_step "5/7" "Removing install artifacts (atomic mv to tmp + final rm)" step_remove_artifacts || exit "$STEP_EXIT_CODE"
mem_run_step "6/7" "Optionally removing the ~/.zshrc helper block" step_report_zsh_helper || exit "$STEP_EXIT_CODE"
mem_run_step "7/7" "Verifying user memory dir untouched" step_verify_user_data || exit "$STEP_EXIT_CODE"

printf 'Removed: %s install-artifact files. User memory preserved at: %s/{config.json, _global/, <workspace-id>/memory-bank/, <workspace-id>/events.jsonl, <workspace-id>/observations.md, <workspace-id>/work-state.md, <workspace-id>/context-pack.md}\n' "$REMOVED_COUNT" "$DEST"

#!/bin/bash
# Upgrade silly-memory in place behind a verified backup that --rollback (or a
# failed upgrade) restores together with every tool setting the installer changed.
set -euo pipefail
LC_ALL=C
export LC_ALL

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./memory/lib/memory_system/bash_helpers.sh disable=SC1091
source "${HERE}/memory/lib/memory_system/bash_helpers.sh"

mem_refuse_root

SYSTEM_PY="${HERE}/memory/lib/memory_system/system"
LOCK_PATH="${HOME}/.silly-memory-upgrade.lock"
LOCK_TIMEOUT="${SILLY_MEMORY_UPGRADE_LOCK_TIMEOUT:-30}"
DRY_RUN=0
ROLLBACK=0
CONFIRM=0
FROM_VERSION=""
TARGET_N=""

usage() {
  cat >&2 <<'EOF'
Usage: ./upgrade.sh [--dry-run] [--from-version VERSION]
       ./upgrade.sh --rollback [--target N] [--confirm]

Upgrades the installation at SILLY_MEMORY_HOME, else ~/.silly-memory, in place.
A failed upgrade rolls back.

Options:
  --dry-run              Print the upgrade steps in order; change nothing.
  --rollback             Restore the latest numbered upgrade backup, its home,
                         and the tool settings the upgrade changed.
  --target N             With --rollback, restore backup number N.
  --confirm              Required for rollback.
  --from-version VERSION Override installed version detection.
  -h, --help             Show this help.

Environment: SILLY_MEMORY_HOME (the home to upgrade in place),
SILLY_MEMORY_UPGRADE_LOCK_TIMEOUT (seconds to wait for another upgrade; default 30).
EOF
}

die() {
  local code="$1"
  shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$code"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --rollback) ROLLBACK=1 ;;
      --confirm) CONFIRM=1 ;;
      --from-version)
        shift
        [ "$#" -gt 0 ] || die 64 "--from-version requires a value"
        FROM_VERSION="$1"
        ;;
      --target)
        shift
        [ "$#" -gt 0 ] || die 64 "--target requires a numeric backup number"
        TARGET_N="$1"
        case "$TARGET_N" in
          ''|*[!0-9]*) die 64 "--target must be a numeric backup number" ;;
        esac
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        usage
        die 64 "unknown argument: $1"
        ;;
    esac
    shift
  done

  if [ "$DRY_RUN" = "1" ] && [ "$ROLLBACK" = "1" ]; then
    die 64 "--dry-run and --rollback cannot be combined"
  fi
  if [ -n "$TARGET_N" ] && [ "$ROLLBACK" != "1" ]; then
    die 64 "--target is only valid with --rollback"
  fi
}

upgrade_tx() {
  python3 "${SYSTEM_PY}/upgrade_transaction.py" "$@"
}

parse_args "$@"

version_args=()
[ -n "$FROM_VERSION" ] && version_args=(--from-version "$FROM_VERSION")

# Read-only paths take no lock and write nothing.
if [ "$DRY_RUN" = "1" ]; then
  upgrade_tx dry-run --repo "$HERE" ${version_args[@]+"${version_args[@]}"}
  exit $?
fi
if [ "$ROLLBACK" = "1" ] && [ "$CONFIRM" != "1" ]; then
  upgrade_tx list-backups
  printf 'ERROR: rollback replaces the memory home; require --confirm to proceed.\n' >&2
  exit 2
fi

# Everything below runs while holding the upgrade lock. The kernel releases it
# when the last process holding it exits, so a killed upgrade never wedges it.
if [ -z "${SILLY_MEMORY_UPGRADE_LOCK_FD:-}" ]; then
  exec python3 "${SYSTEM_PY}/install_transaction.py" run-locked --lock "$LOCK_PATH" --timeout "$LOCK_TIMEOUT" \
    -- bash "${HERE}/$(basename "$0")" "$@"
fi

if [ "$ROLLBACK" = "1" ]; then
  upgrade_tx list-backups
  target_args=()
  [ -n "$TARGET_N" ] && target_args=(--target "$TARGET_N")
  upgrade_tx rollback ${target_args[@]+"${target_args[@]}"}
  exit $?
fi
upgrade_tx upgrade --repo "$HERE" ${version_args[@]+"${version_args[@]}"}

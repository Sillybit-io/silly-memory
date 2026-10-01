#!/bin/bash
# bootstrap.sh — one-liner fresh install of silly-memory on a new machine.
#
# Default-offline policy: --from <local-path> installs from an existing checkout
# without touching the network. --from <URL> requires --allow-network.
set -euo pipefail
LC_ALL=C
export LC_ALL

SELF_DIR="$(cd "$(dirname "$0")" && pwd)" || SELF_DIR=""

DEFAULT_TARGET="${HOME}/src/cursor-memory-system"
DEFAULT_BRANCH="main"

FROM="${MEMORY_REPO_URL:-}"
BRANCH="$DEFAULT_BRANCH"
TARGET="$DEFAULT_TARGET"
ALLOW_NETWORK=0
SOURCE_KIND=""
SOURCE_DIR=""

usage() {
  cat >&2 <<'EOF'
Usage: bootstrap.sh [--from <url-or-path>] [--branch <ref>]
                    [--target <dir>] [--allow-network]

Default-offline policy:
  --from <local-path>  installs from a directory; no network needed.
  --from <URL>         requires --allow-network (opt-in).
  --from omitted       falls back to $MEMORY_REPO_URL.

Options:
  --from <url-or-path>  Git URL or local directory of the cursor-memory-system
                        repo. Defaults to $MEMORY_REPO_URL.
  --branch <ref>        Git branch / tag / sha (default: main).
  --target <dir>        Where to clone (default: ~/src/cursor-memory-system).
  --allow-network       Opt in to network clone.
  -h, --help            Show this help.
EOF
}

die() {
  local code="$1"
  shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$code"
}

log_sub() {
  printf '  %s\n' "$*"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --from)
        shift
        [ "$#" -gt 0 ] || die 64 "--from requires a value"
        FROM="$1"
        ;;
      --branch)
        shift
        [ "$#" -gt 0 ] || die 64 "--branch requires a value"
        BRANCH="$1"
        ;;
      --target)
        shift
        [ "$#" -gt 0 ] || die 64 "--target requires a value"
        TARGET="$1"
        ;;
      --allow-network)
        ALLOW_NETWORK=1
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
}

# Returns 0 if $1 is a usable local directory; 1 if URL-like, missing, or empty.
is_local_path() {
  case "$1" in
    http://*|https://*|git://*|git@*|ssh://*|"") return 1 ;;
  esac
  [ -d "$1" ]
}

# ---------------------------------------------------------------------------
# Argument parsing + early validation
# ---------------------------------------------------------------------------
parse_args "$@"

if [ "$(id -u)" -eq 0 ]; then
  die 64 "refuse to run as root"
fi

if [ -z "$FROM" ]; then
  cat >&2 <<'EOF'
ERROR: no source specified.

Pass --from <url-or-path> or set MEMORY_REPO_URL.

Network install (requires explicit opt-in):
  ./bootstrap.sh --from https://example.com/cursor-memory-system --allow-network

Offline install from a local checkout:
  ./bootstrap.sh --from /path/to/cursor-memory-system
EOF
  exit 64
fi

if is_local_path "$FROM"; then
  SOURCE_KIND="local"
else
  SOURCE_KIND="url"
fi

if [ "$SOURCE_KIND" = "url" ] && [ "$ALLOW_NETWORK" != "1" ]; then
  cat >&2 <<EOF
ERROR: --from "${FROM}" looks like a network URL, but --allow-network was not passed.

silly-memory bootstrap is default-offline. To opt in to network use:

  ./bootstrap.sh --from "${FROM}" --allow-network

Or pass a local checkout instead:

  ./bootstrap.sh --from /path/to/cursor-memory-system

(Local paths must exist as directories. URLs are detected by scheme
prefix: http://, https://, git://, git@, ssh://.)
EOF
  exit 64
fi

# ---------------------------------------------------------------------------
# Find bash_helpers.sh — prefer --from <local-path>, fall back to SELF_DIR,
# and pre-clone if --from is a URL with --allow-network and helpers absent.
# ---------------------------------------------------------------------------
HELPERS=""
if [ "$SOURCE_KIND" = "local" ] \
   && [ -f "${FROM}/memory/lib/memory_system/bash_helpers.sh" ]; then
  HELPERS="${FROM}/memory/lib/memory_system/bash_helpers.sh"
elif [ -n "$SELF_DIR" ] \
   && [ -f "${SELF_DIR}/memory/lib/memory_system/bash_helpers.sh" ]; then
  HELPERS="${SELF_DIR}/memory/lib/memory_system/bash_helpers.sh"
elif [ "$SOURCE_KIND" = "url" ] && [ "$ALLOW_NETWORK" = "1" ]; then
  command -v git >/dev/null 2>&1 || die 66 "git required to clone --from <url>"
  printf 'Pre-cloning %s to %s to obtain helpers…\n' "$FROM" "$TARGET" >&2
  mkdir -p "$TARGET"
  if [ -d "${TARGET}/.git" ]; then
    git -C "$TARGET" fetch --depth 1 origin "$BRANCH"
    git -C "$TARGET" checkout "$BRANCH"
    git -C "$TARGET" reset --hard "origin/${BRANCH}"
  elif [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null || true)" ]; then
    die 1 "target exists and is not empty: ${TARGET}"
  else
    git clone --depth 1 --branch "$BRANCH" "$FROM" "$TARGET"
  fi
  HELPERS="${TARGET}/memory/lib/memory_system/bash_helpers.sh"
  SOURCE_DIR="$TARGET"
fi

if [ -z "$HELPERS" ] || [ ! -f "$HELPERS" ]; then
  die 66 "bash_helpers.sh not found. Run bootstrap.sh from a checkout, or pass --from <local-path>."
fi

# shellcheck source=memory/lib/memory_system/bash_helpers.sh disable=SC1090,SC1091
source "$HELPERS"
export TOTAL_STEPS=5

mem_refuse_root

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
step_preflight() {
  if ! command -v git >/dev/null 2>&1; then
    if [ "$SOURCE_KIND" = "url" ]; then
      printf 'ERROR: git not installed; required for --from <url>\n' >&2
      return 1
    fi
    log_sub "git not installed; OK (local --from skips clone)"
  else
    log_sub "git: $(command -v git)"
  fi
  if ! python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    print(
        "python3 too old: "
        f"{sys.version_info.major}.{sys.version_info.minor} "
        "(need >= 3.10)",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
  then
    return 1
  fi
  log_sub "python3: $(python3 -c 'import sys;print(".".join(str(p) for p in sys.version_info[:3]))')"

  if [ "$SOURCE_KIND" = "url" ]; then
    local parent
    parent="$(dirname "$TARGET")"
    mkdir -p "$parent"
    mem_check_symlink_escape "$parent" "$parent"
    if [ -e "$TARGET" ]; then
      mem_check_symlink_escape "$parent" "$TARGET"
    fi
    if [ ! -w "$parent" ]; then
      printf 'ERROR: target parent not writable: %s\n' "$parent" >&2
      return 1
    fi
    log_sub "target parent writable: $parent"
  fi
}

step_acquire_source() {
  if [ "$SOURCE_KIND" = "local" ]; then
    SOURCE_DIR="$(cd "$FROM" && pwd)"
    log_sub "using local checkout: $SOURCE_DIR (no network)"
    return 0
  fi
  # SOURCE_KIND=url, ALLOW_NETWORK=1; may already be pre-cloned to TARGET.
  if [ -d "${TARGET}/.git" ]; then
    log_sub "target already a git repo; refreshing to ${BRANCH}"
    git -C "$TARGET" fetch --depth 1 origin "$BRANCH"
    git -C "$TARGET" checkout "$BRANCH"
    git -C "$TARGET" reset --hard "origin/${BRANCH}"
  elif [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null || true)" ]; then
    printf 'ERROR: target exists and is not empty: %s\n' "$TARGET" >&2
    return 1
  else
    mkdir -p "$TARGET"
    git clone --depth 1 --branch "$BRANCH" "$FROM" "$TARGET"
  fi
  SOURCE_DIR="$TARGET"
  log_sub "source ready at $SOURCE_DIR"
}

step_run_install() {
  if [ ! -f "${SOURCE_DIR}/install.sh" ]; then
    printf 'ERROR: install.sh missing in %s\n' "$SOURCE_DIR" >&2
    return 1
  fi
  log_sub "running ${SOURCE_DIR}/install.sh"
  bash "${SOURCE_DIR}/install.sh"
}

memory_home() {
  printf '%s\n' "${SILLY_MEMORY_HOME:-${HOME}/.silly-memory}"
}

step_source_into_zshrc() {
  local home
  home="$(memory_home)"
  if [ ! -f "${home}/memory.zsh" ] || [ ! -f "${home}/install-zsh.sh" ]; then
    printf 'ERROR: memory.zsh not installed in %s\n' "$home" >&2
    return 1
  fi
  # install.sh already wrote the one managed block; this confirms it (a no-op when current).
  bash "${home}/install-zsh.sh" >/dev/null || return
  log_sub "silly-memory block present in ${HOME}/.zshrc"
}

step_run_memdoctor() {
  local cli
  cli="$(memory_home)/bin/memory"
  if [ ! -f "$cli" ]; then
    printf 'ERROR: memory CLI not found at %s\n' "$cli" >&2
    return 1
  fi
  local tmp rc status
  tmp="$(mktemp "${TMPDIR:-/tmp}/bootstrap-doctor.XXXXXX")"
  rc=0
  PYTHONDONTWRITEBYTECODE=1 python3 "$cli" doctor --json >"$tmp" || rc=$?
  cat "$tmp"
  status="$(python3 -c 'import json,sys;print(json.loads(open(sys.argv[1]).read()).get("status","error"))' "$tmp")"
  rm -f "$tmp"
  if [ "$status" = "error" ] || [ "$rc" -ge 2 ]; then
    printf 'ERROR: memdoctor status=%s exit=%s\n' "$status" "$rc" >&2
    return 1
  fi
  log_sub "memdoctor status=$status (exit $rc)"
}

mem_run_step "1/5" "Pre-flight (git, python3 >=3.10, no root, target writable)" step_preflight || exit 1
mem_run_step "2/5" "Acquiring source ($SOURCE_KIND)" step_acquire_source || exit 1
mem_run_step "3/5" "Running install.sh" step_run_install || exit 1
mem_run_step "4/5" "Sourcing memory.zsh into ~/.zshrc (idempotent)" step_source_into_zshrc || exit 1
mem_run_step "5/5" "Running memdoctor" step_run_memdoctor || exit 1

cat <<'EOF'

Bootstrap complete. Next:
  1) source ~/.zshrc   (or open a new terminal)
  2) Try:  memhelp
EOF

#!/bin/bash
# Install silly-memory on this machine and wire it into Cursor, Claude Code, and
# OpenCode (every one found, or the ones named with --tools).
# Safe to re-run: it never deletes existing memory stores, keeps config.json and
# customized recall rules or skills, and changes only its own entries in shared
# tool settings.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./memory/lib/memory_system/bash_helpers.sh disable=SC1091
source "${HERE}/memory/lib/memory_system/bash_helpers.sh"
export TOTAL_STEPS=11

mem_refuse_root

# Same precedence as the engine: SILLY_MEMORY_HOME, else ~/.silly-memory.
DEST="${SILLY_MEMORY_HOME:-${HOME}/.silly-memory}"
CURSOR="${HOME}/.cursor"
CLAUDE="${HOME}/.claude"
OPENCODE="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode"
LOCK_PATH="${DEST}/.install.lock"
OWNERSHIP="${DEST}/.installed-artifacts.json"
TX_PY="${HERE}/memory/lib/memory_system/system/install_transaction.py"
LOCK_HELD=0
SELECTED_BACKEND="noop"
WEIGHT_VERIFY_DEFERRED_ERROR=""
WEIGHT_VERIFY_REQUIRED=0
SUPPORTED_TOOLS="cursor claude-code opencode"

# --- Flags --------------------------------------------------------------------
usage() {
  cat <<'EOF'
Usage: ./install.sh [--tools LIST] [--mcp | --no-mcp] [--no-torch] [--import BUNDLE]

Options:
  --tools LIST     Comma-separated tools to wire: cursor, claude-code, opencode.
                   Default: every tool found on this machine (its config folder
                   ~/.cursor, ~/.claude, ~/.config/opencode, or its command on PATH).
  --mcp            Also give each project the silly-memory MCP server (off by
                   default; the memory skills do the same without MCP).
  --no-mcp         Turn the MCP server off again and remove it from every
                   tracked project. Without either flag, a rerun keeps the setting.
  --no-torch       Skip the sentence-transformers/torch embedding backend.
  --import BUNDLE  After installing, restore a `memory export` bundle
                   (any existing store is snapshotted first).
  -h, --help       Show this help.

Environment: SILLY_MEMORY_HOME (memory home, default ~/.silly-memory),
MEMORY_ALLOW_NETWORK, MEMORY_SKIP_MODEL_DOWNLOAD, MEMORY_EMBEDDING_BACKEND
(see README).
EOF
}

NO_TORCH=0
IMPORT_BUNDLE=""
TOOLS_ARG=""
TOOLS_GIVEN=0
MCP_ARG=""
MCP_STATE="off"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --no-torch) NO_TORCH=1 ;;
    --mcp) MCP_ARG="on" ;;
    --no-mcp) MCP_ARG="off" ;;
    --import)
      shift
      IMPORT_BUNDLE="${1:-}"
      if [ -z "$IMPORT_BUNDLE" ]; then
        echo "install.sh: --import requires a bundle path" >&2
        exit 2
      fi
      ;;
    --import=*) IMPORT_BUNDLE="${1#--import=}" ;;
    --tools)
      shift
      TOOLS_ARG="${1:-}"
      TOOLS_GIVEN=1
      ;;
    --tools=*) TOOLS_ARG="${1#--tools=}"; TOOLS_GIVEN=1 ;;
    *)
      # Never ignore an unknown flag: `--dry-run` used to run a real install.
      echo "install.sh: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done
SKIP_DOWNLOAD="${MEMORY_SKIP_MODEL_DOWNLOAD:-0}"
ALLOW_NETWORK="${MEMORY_ALLOW_NETWORK:-0}"

# --- Tool selection (before anything is written) ------------------------------
SELECTED=""
select_tool() {
  case " $SELECTED " in
    *" $1 "*) ;;
    *) SELECTED="${SELECTED:+$SELECTED }$1" ;;
  esac
}

if [ "$TOOLS_GIVEN" = "1" ]; then
  if [ -z "$TOOLS_ARG" ]; then
    echo "install.sh: --tools needs at least one of: ${SUPPORTED_TOOLS}" >&2
    exit 2
  fi
  IFS=',' read -r -a requested <<< "$TOOLS_ARG"
  for tool in "${requested[@]}"; do
    case " $SUPPORTED_TOOLS " in
      *" $tool "*) select_tool "$tool" ;;
      *)
        echo "install.sh: unknown tool: '${tool}' (expected: ${SUPPORTED_TOOLS// /, })" >&2
        exit 2
        ;;
    esac
  done
else
  { [ -d "$CURSOR" ] || command -v cursor >/dev/null 2>&1; } && select_tool cursor
  { [ -d "$CLAUDE" ] || command -v claude >/dev/null 2>&1; } && select_tool claude-code
  { [ -d "$OPENCODE" ] || command -v opencode >/dev/null 2>&1; } && select_tool opencode
fi

has_tool() {
  case " $SELECTED " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

tx() {
  python3 "$TX_PY" "$@"
}

# Shared files must be mergeable (and inside HOME) before any of them is touched.
checks=(--check zsh "${HOME}/.zshrc")
if has_tool cursor; then
  checks+=(--check cursor-hooks "${CURSOR}/hooks.json")
fi
if has_tool claude-code; then
  checks+=(--check claude-hooks "${CLAUDE}/settings.json")
fi
validate_rc=0
tx validate --within "$HOME" "${checks[@]}" >/dev/null || validate_rc=$?
if [ "$validate_rc" -ne 0 ]; then
  echo "install.sh: nothing was changed." >&2
  exit "$validate_rc"
fi

cleanup_install_lock() {
  if [ "${LOCK_HELD}" = "1" ]; then
    mem_unlock "${LOCK_PATH}"
  fi
}
trap cleanup_install_lock EXIT

log_sub() {
  printf '  %s\n' "$*"
}

atomic_copy_file() {
  local src="$1"
  local dest="$2"
  local content
  content="$(python3 - "$src" <<'PY'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
sys.stdout.write(text)
sys.stdout.write("\n__MEM_ATOMIC_COPY_EOF__")
PY
)"
  content="${content%$'\n'__MEM_ATOMIC_COPY_EOF__}"
  mem_atomic_write "$dest" "$content"
}

copy_tree_into_memory() {
  local src="$1"
  local target="$2"
  mkdir -p "$target"
  mem_check_symlink_escape "$DEST" "$target"
  # --delete: lib/bin/tests are engine-owned; modules removed upstream must not linger.
  rsync -a --delete --exclude '__pycache__' "$src" "$target"
}

step_preflight() {
  log_sub "installing into ${DEST}"
  log_sub "tools: ${SELECTED:-none found}"
  mkdir -p "${DEST}"
  mem_check_symlink_escape "$DEST" "$DEST"
  mkdir -p "${DEST}/hooks" "${DEST}/queues"

  if ! python3 - "${HERE}/memory/lib" <<'PY'
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from memory_system.preflight import check_fts5_available

ok, msg = check_fts5_available()
if not ok:
    print(f"FTS5 unavailable: {msg}", file=sys.stderr)
    sys.exit(2)

home = Path(os.environ["HOME"])
usage = shutil.disk_usage(home)
if usage.free < 100 * 1024 * 1024:
    print("disk space below 100MB", file=sys.stderr)
    sys.exit(3)
PY
  then
    log_sub "ERROR: FTS5 preflight failed. SQLite in your python3 lacks FTS5 support."
    log_sub "Try: brew reinstall python3 --build-from-source"
    log_sub "or pyenv install <version> --with-fts5"
    return 2
  fi
  log_sub "preflight: FTS5 OK"

  python3 - "${HERE}/memory/lib" "${DEST}" <<'PY' || true
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from memory_system.preflight import check_sync_filesystem
ok, msg = check_sync_filesystem(Path(sys.argv[2]))
if not ok:
    print(f"  WARNING: {msg}", file=sys.stderr)
PY
}

step_refuse_root() {
  mem_refuse_root
  log_sub "root guard: OK"
}

step_acquire_lock() {
  mkdir -p "$DEST"
  mem_check_symlink_escape "$DEST" "$DEST"
  # Captured chats and bank files are private: owner-only, also on reinstall/upgrade.
  chmod 700 "$DEST"
  log_sub "memory home permissions: 0700 (owner only)"
  mem_lock "$LOCK_PATH" 30
  LOCK_HELD=1
  log_sub "install lock acquired"
}

step_copy_engine_files() {
  copy_tree_into_memory "${HERE}/memory/lib/" "${DEST}/lib/" || return
  copy_tree_into_memory "${HERE}/memory/bin/" "${DEST}/bin/" || return
  copy_tree_into_memory "${HERE}/memory/tests/" "${DEST}/tests/" || return
  # The OpenCode plugin reads the recall rule from the engine, whatever tools are wired.
  copy_tree_into_memory "${HERE}/cursor-extras/rules/" "${DEST}/cursor-extras/rules/" || return
  mem_check_symlink_escape "$DEST" "${DEST}/memory.zsh"
  cp "${HERE}/memory/memory.zsh" "${DEST}/memory.zsh" || return
  mem_check_symlink_escape "$DEST" "${DEST}/install-zsh.sh"
  cp "${HERE}/memory/install-zsh.sh" "${DEST}/install-zsh.sh" || return
  mem_check_symlink_escape "$DEST" "${DEST}/README.md"
  cp "${HERE}/memory/README.md" "${DEST}/README.md" || return
  chmod +x "${DEST}/bin/memory" "${DEST}/bin/memory-mcp" "${DEST}/install-zsh.sh" || return
  log_sub "engine files refreshed"
}

step_seed_config() {
  local existed=0
  [ -f "${DEST}/config.json" ] && existed=1
  if [ "$existed" = "0" ]; then
    mem_check_symlink_escape "$DEST" "${DEST}/config.json"
    atomic_copy_file "${HERE}/memory/config.json" "${DEST}/config.json" || return
    log_sub "seeded config.json"
  else
    log_sub "kept existing config.json"
  fi
  # Rendering, doctor, and uninstall read the wired tools from here; session
  # start reads "mcp".
  MCP_STATE="$(python3 - "${DEST}/config.json" "$SELECTED" "$MCP_ARG" "$existed" "$DEST" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path, selected, mcp_arg, existed, home = sys.argv[1:6]
path = Path(path)
config = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(config, dict):
    raise SystemExit(f"{path} is not a JSON object")
# An install from before the setting existed registered the server in every project.
was_on = existed == "1" and config.get("mcp", True) is True
wanted = {"on": True, "off": False}.get(mcp_arg, config.get("mcp") is True)
config["tools"] = selected.split()
config["mcp"] = wanted
fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.json.")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(config, indent=2) + "\n")
os.chmod(tmp, path.stat().st_mode & 0o777)
os.replace(tmp, path)

if not wanted and (was_on or mcp_arg == "off"):
    sys.path.insert(0, str(Path(home) / "lib"))
    from memory_system.project_mcp import project_roots, remove_project_mcp

    for root in project_roots(Path(home)):
        try:
            changed = remove_project_mcp(root)
        except OSError as exc:
            print(f"  WARNING: could not remove the MCP server from {root}: {exc}", file=sys.stderr)
            continue
        for changed_path in changed:
            print(f"  MCP off: removed the silly-memory server from {changed_path}", file=sys.stderr)
print("on" if wanted else "off")
PY
)" || return
  log_sub "config.json tools: ${SELECTED:-none}"
  log_sub "MCP server: ${MCP_STATE}"
}

step_seed_profile() {
  # profile.md is a local-only SOURCE file — it surfaces only after
  # `memory profile-sync` routes it into the recognized global bank files.
  local profile_dest="${DEST}/_global/memory-bank/profile.md"
  if [ ! -f "${profile_dest}" ]; then
    mkdir -p "${DEST}/_global/memory-bank"
    mem_check_symlink_escape "$DEST" "${profile_dest}"
    atomic_copy_file "${HERE}/memory/templates/profile.md" "${profile_dest}"
    log_sub "seeded profile.md"
  else
    log_sub "kept existing profile"
  fi
}

# Owned files stay inside their tool's folder; shared settings may be dotfile
# links, but only into HOME.
step_wire_hooks() {
  local status
  if has_tool cursor; then
    status="$(tx put-file --target "${CURSOR}/hooks/memory-hook.sh" --source "${HERE}/cursor-extras/hooks/memory-hook.sh" \
      --mode 755 --ownership "$OWNERSHIP" --kind shim --tool cursor --within "$CURSOR")" || return
    log_sub "cursor hook shim: ${status}"
    status="$(tx merge --kind cursor-hooks --target "${CURSOR}/hooks.json" --template "${HERE}/cursor-extras/hooks.json" \
      --ownership "$OWNERSHIP" --within "$HOME")" || return
    log_sub "cursor hooks.json: ${status}"
  fi
  if has_tool claude-code; then
    status="$(tx put-file --target "${CLAUDE}/hooks/silly-memory-hook.sh" \
      --source "${HERE}/claude-code-extras/hooks/silly-memory-hook.sh" --mode 755 --ownership "$OWNERSHIP" \
      --kind shim --tool claude-code --within "$CLAUDE")" || return
    log_sub "claude-code hook shim: ${status}"
    status="$(tx merge --kind claude-hooks --target "${CLAUDE}/settings.json" \
      --template "${HERE}/claude-code-extras/hooks.json" --ownership "$OWNERSHIP" --within "$HOME")" || return
    log_sub "claude-code settings.json hooks: ${status}"
  fi
  if has_tool opencode; then
    status="$(tx put-file --target "${OPENCODE}/plugins/silly-memory.js" \
      --source "${HERE}/opencode-extras/plugins/silly-memory.js" --ownership "$OWNERSHIP" --kind plugin --tool opencode \
      --within "$OPENCODE")" || return
    log_sub "opencode plugin: ${status}"
  fi
  return 0
}

# install_skills TOOL TOOL_DIR: every shipped skill into TOOL_DIR/skills.
install_skills() {
  local tool="$1" tool_dir="$2" line output
  output="$(tx put-skills --source-dir "${HERE}/cursor-extras/skills" --target-dir "${tool_dir}/skills" \
    --ownership "$OWNERSHIP" --tool "$tool" --within "$tool_dir")" || return
  while IFS= read -r line; do
    log_sub "${tool} skill ${line}"
  done <<< "$output"
}

step_install_rules_and_skills() {
  local status
  if has_tool cursor; then
    status="$(tx put-artifact --target "${CURSOR}/rules/memory-recall.mdc" --source "${HERE}/cursor-extras/rules/memory-recall.mdc" \
      --kind rule --ownership "$OWNERSHIP" --tool cursor --within "$CURSOR")" || return
    log_sub "cursor memory-recall rule: ${status}"
    install_skills cursor "$CURSOR" || return
  fi
  if has_tool claude-code; then
    status="$(tx put-artifact --target "${CLAUDE}/rules/memory-recall.md" \
      --source "${HERE}/claude-code-extras/rules/memory-recall.md" --kind rule \
      --ownership "$OWNERSHIP" --tool claude-code --within "$CLAUDE")" || return
    log_sub "claude-code memory-recall rule: ${status}"
    install_skills claude-code "$CLAUDE" || return
  fi
  if has_tool opencode; then
    install_skills opencode "$OPENCODE" || return
  fi
  return 0
}

step_select_embedding_backend() {
  SELECTED_BACKEND="$(python3 - "${DEST}/lib" "${NO_TORCH}" "${SKIP_DOWNLOAD}" "${ALLOW_NETWORK}" <<'PY'
import sys

lib_path = sys.argv[1]
no_torch = sys.argv[2] == "1"
skip_download = sys.argv[3] == "1"
allow_network = sys.argv[4] == "1"
sys.path.insert(0, lib_path)


def _verify_st(path):
    from memory_system.backends.embedding.weights_manifest import verify_weights
    verify_weights("sentence-transformers/all-MiniLM-L6-v2", path)


def _verify_fe(path):
    from memory_system.backends.embedding.weights_manifest import verify_weights
    verify_weights("BAAI/bge-small-en-v1.5", path)


def _try_st():
    try:
        from memory_system.backends.embedding.sentence_transformers_backend import (
            SentenceTransformersBackend,
        )
    except ImportError as exc:
        print(f"  sentence-transformers backend import failed: {exc}", file=sys.stderr)
        return None
    try:
        backend = SentenceTransformersBackend()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  sentence-transformers init failed: {exc}", file=sys.stderr)
        return None
    if backend.is_available():
        try:
            _verify_st(backend.model_path)
        except RuntimeError:
            print("  sentence-transformers verify failed; preseed weights or set MEMORY_ALLOW_NETWORK=1", file=sys.stderr)
            if not allow_network:
                return None
        print(
            f"  embedding: sentence-transformers (cache hit; skipping download)",
            file=sys.stderr,
        )
        return "sentence-transformers"
    try:
        import torch  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError as exc:
        print(f"  sentence-transformers/torch unavailable: {exc}", file=sys.stderr)
        return None
    if skip_download:
        print(
            "  embedding: sentence-transformers libs OK; MEMORY_SKIP_MODEL_DOWNLOAD=1 — skipping download",
            file=sys.stderr,
        )
        return "sentence-transformers"
    if not allow_network:
        print(
            "  embedding: sentence-transformers unavailable offline; preseed weights or set MEMORY_ALLOW_NETWORK=1",
            file=sys.stderr,
        )
        return None
    try:
        path = backend.download_model()
        _verify_st(path)
        print("  embedding: sentence-transformers downloaded and verified", file=sys.stderr)
        return "sentence-transformers"
    except Exception as exc:
        print(f"  sentence-transformers download/verify failed: {exc}", file=sys.stderr)
        return None


def _try_fe():
    try:
        from memory_system.backends.embedding.fastembed_backend import FastembedBackend
    except ImportError as exc:
        print(f"  fastembed backend import failed: {exc}", file=sys.stderr)
        return None
    try:
        backend = FastembedBackend()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  fastembed init failed: {exc}", file=sys.stderr)
        return None
    if backend.is_available():
        try:
            _verify_fe(backend.model_dir)
        except RuntimeError:
            print("  fastembed verify failed; preseed weights or set MEMORY_ALLOW_NETWORK=1", file=sys.stderr)
            if not allow_network:
                return None
        print("  embedding: fastembed (cache hit; skipping download)", file=sys.stderr)
        return "fastembed"
    try:
        import fastembed  # noqa: F401
    except ImportError as exc:
        print(f"  fastembed unavailable: {exc}", file=sys.stderr)
        return None
    if skip_download:
        print(
            "  embedding: fastembed libs OK; MEMORY_SKIP_MODEL_DOWNLOAD=1 — skipping download",
            file=sys.stderr,
        )
        return "fastembed"
    if not allow_network:
        print(
            "  embedding: fastembed unavailable offline; preseed weights or set MEMORY_ALLOW_NETWORK=1",
            file=sys.stderr,
        )
        return None
    try:
        path = backend.download_model()
        _verify_fe(path)
        print("  embedding: fastembed downloaded and verified", file=sys.stderr)
        return "fastembed"
    except Exception as exc:
        print(f"  fastembed download/verify failed: {exc}", file=sys.stderr)
        return None


selected = None
if no_torch:
    print("  embedding: --no-torch passed; skipping sentence-transformers", file=sys.stderr)
else:
    selected = _try_st()
if selected is None:
    selected = _try_fe()
if selected is None:
    print(
        "  WARNING: no embedding backend available; falling back to noop "
        "(recall stays FTS5-only until weights/libs are installed)",
        file=sys.stderr,
    )
    selected = "noop"
print(selected)
PY
)"
  log_sub "embedding backend selected: ${SELECTED_BACKEND}"
}

copy_preseed_model() {
  local preseed="$1"
  local backend_cache="$2"
  local hf_cache="$3"
  local copied=0

  if [ -d "$preseed" ] && find "$preseed" -type f ! -name README.md ! -name LICENSE -print -quit | grep -q .; then
    mkdir -p "$backend_cache" "$hf_cache"
    mem_check_symlink_escape "$DEST" "$backend_cache"
    rsync -a "$preseed/" "$backend_cache/"
    mem_check_symlink_escape "${HOME}/.cache/huggingface" "$hf_cache"
    rsync -a "$preseed/" "$hf_cache/"
    copied=1
  fi
  if [ "$copied" = "1" ]; then
    return 0
  fi
  return 1
}

step_verify_weight_integrity() {
  local st_preseed="${HERE}/memory/weights/all-MiniLM-L6-v2"
  local st_cache="${DEST}/_embeddings/sentence-transformers-all-MiniLM-L6-v2"
  local st_hf="${HOME}/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/preseed"
  local fe_preseed="${HERE}/memory/weights/bge-small-en-v1.5"
  local fe_cache="${DEST}/_embeddings/fastembed-bge-small-en-v1.5"
  local fe_hf="${HOME}/.cache/huggingface/hub/models--BAAI--bge-small-en-v1.5/snapshots/preseed"

  mkdir -p "${HOME}/.cache/huggingface/hub"
  mem_check_symlink_escape "${HOME}/.cache/huggingface" "${HOME}/.cache/huggingface/hub"

  if [ "$ALLOW_NETWORK" != "1" ]; then
    if copy_preseed_model "$st_preseed" "$st_cache" "$st_hf"; then
      log_sub "copied preseeded weights: all-MiniLM-L6-v2"
    elif [ "$NO_TORCH" != "1" ]; then
      WEIGHT_VERIFY_REQUIRED=1
      WEIGHT_VERIFY_DEFERRED_ERROR="preseeded weights missing at memory/weights/all-MiniLM-L6-v2; add the model there or rerun with MEMORY_ALLOW_NETWORK=1"
      log_sub "preseeded weights missing; will report after final selftest"
    fi
    if copy_preseed_model "$fe_preseed" "$fe_cache" "$fe_hf"; then
      log_sub "copied preseeded weights: bge-small-en-v1.5"
    fi
  fi

  local verify_target="sentence-transformers/all-MiniLM-L6-v2"
  local verify_cache="$st_cache"
  if [ "$SELECTED_BACKEND" = "fastembed" ]; then
    verify_target="BAAI/bge-small-en-v1.5"
    verify_cache="$fe_cache"
  fi

  if [ "$SELECTED_BACKEND" = "noop" ] && [ "$SKIP_DOWNLOAD" = "1" ]; then
    log_sub "weight integrity: skipped for MEMORY_SKIP_MODEL_DOWNLOAD=1"
    return 0
  fi

  if python3 - "${DEST}/lib" "$verify_target" "$verify_cache" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from memory_system.backends.embedding.weights_manifest import verify_weights
try:
    verify_weights(sys.argv[2], sys.argv[3])
except RuntimeError:
    raise SystemExit(1)
PY
  then
    log_sub "weight integrity: verified"
    WEIGHT_VERIFY_DEFERRED_ERROR=""
    WEIGHT_VERIFY_REQUIRED=0
  else
    if [ "$ALLOW_NETWORK" = "1" ]; then
      return 1
    fi
    WEIGHT_VERIFY_REQUIRED=1
    if [ -z "$WEIGHT_VERIFY_DEFERRED_ERROR" ]; then
      WEIGHT_VERIFY_DEFERRED_ERROR="weight verification failed; preseed memory/weights or rerun with MEMORY_ALLOW_NETWORK=1"
    fi
    log_sub "weight integrity: deferred failure until final step"
  fi
}

step_write_version_and_selftest() {
  mem_check_symlink_escape "$DEST" "${DEST}/VERSION"
  atomic_copy_file "${HERE}/VERSION" "${DEST}/VERSION" || return
  log_sub "VERSION marker written"

  local zsh_log rc=0
  zsh_log="$(SILLY_MEMORY_HOME="$DEST" SILLY_MEMORY_OWNERSHIP="$OWNERSHIP" bash "${DEST}/install-zsh.sh" 2>&1)" || rc=$?
  while IFS= read -r line; do
    log_sub "$line"
  done <<< "$zsh_log"
  if [ "$rc" -ne 0 ]; then
    log_sub "ERROR: could not update the silly-memory block in ~/.zshrc"
    return "$rc"
  fi
  if [ "$SKIP_DOWNLOAD" = "1" ]; then
    log_sub "selftest: skipped for MEMORY_SKIP_MODEL_DOWNLOAD=1"
  elif python3 "${DEST}/bin/memory" selftest >/dev/null 2>&1; then
    log_sub "selftest: PASS"
  else
    log_sub "selftest: see 'memory selftest' for details"
  fi

  if [ "$WEIGHT_VERIFY_REQUIRED" = "1" ] && [ "$SKIP_DOWNLOAD" != "1" ]; then
    log_sub "ERROR: ${WEIGHT_VERIFY_DEFERRED_ERROR}"
    return 1
  fi
}

print_next_steps() {
  printf '\nDone. Final steps:\n'
  if [ -z "$SELECTED" ]; then
    printf '  - No Cursor, Claude Code, or OpenCode found. Rerun with --tools cursor,claude-code,opencode\n'
    printf '    (any subset) to wire one.\n'
  fi
  has_tool cursor && printf '  - Cursor: restart Cursor so it loads the hooks.\n'
  has_tool claude-code && printf '  - Claude Code: start a new session (hooks load at startup).\n'
  has_tool opencode && printf '  - OpenCode: the plugin reloads automatically; if not, run: opencode service restart\n'
  if [ "$MCP_STATE" = "on" ]; then
    printf '  - Memory tools (MCP): each project gets the silly-memory server in its own config\n'
    printf '    (.mcp.json, .cursor/mcp.json, opencode.json) at its first session; they load from\n'
    printf '    the next one. Cursor asks once per project to approve it.\n'
  else
    printf '  - Memory tools: the add-memory and query-memory skills (MCP server off; add --mcp\n'
    printf '    to give each project the server as well).\n'
  fi
  printf '  - Shell: run "source ~/.zshrc" (or open a new terminal), then try memhelp.\n'
  if [ "$DEST" != "${HOME}/.silly-memory" ]; then
    printf '  - Custom home: hooks find %s through SILLY_MEMORY_HOME. ~/.zshrc now exports it;\n' "$DEST"
    printf '    an app started outside that shell needs it set as well.\n'
  fi
  printf '  - Check everything with memdoctor.\n\n'
  printf 'Your memory data is stored locally under %s/<workspace-id>/ and is\n' "$DEST"
  printf 'never shared. Nothing here uploads anywhere.\n'
}

mem_run_step "1/11" "Preflight checks (FTS5, disk space, perms)" step_preflight || exit $?
mem_run_step "2/11" "Refusing to run as root" step_refuse_root || exit $?
mem_run_step "3/11" "Acquiring install lock" step_acquire_lock || exit $?
mem_run_step "4/11" "Copying engine files to ${DEST}" step_copy_engine_files || exit $?
if [ -n "$IMPORT_BUNDLE" ]; then
  log_sub "importing memory bundle: ${IMPORT_BUNDLE}"
  python3 "${DEST}/bin/memory" import "$IMPORT_BUNDLE" --confirm-import YES-IMPORT --relink \
    || { echo "install.sh: bundle import failed" >&2; exit 1; }
fi
mem_run_step "5/11" "Seeding config.json and recording the wired tools and MCP setting" step_seed_config || exit $?
mem_run_step "6/11" "Seeding private profile (or keeping existing)" step_seed_profile || exit $?
mem_run_step "7/11" "Wiring hooks and the OpenCode plugin" step_wire_hooks || exit $?
mem_run_step "8/11" "Installing rules and skills" step_install_rules_and_skills || exit $?
mem_run_step "9/11" "Selecting embedding backend" step_select_embedding_backend || exit $?
mem_run_step "10/11" "Verifying weight integrity (SHA-pinned)" step_verify_weight_integrity || exit $?
mem_run_step "11/11" "Writing VERSION marker, shell helpers, and running selftest" step_write_version_and_selftest || exit $?

print_next_steps

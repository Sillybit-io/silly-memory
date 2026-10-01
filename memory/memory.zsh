# silly-memory — zsh helpers
# Source this from your ~/.zshrc:  source ~/.silly-memory/memory.zsh
# (or run ~/.silly-memory/install-zsh.sh once to wire it up automatically)

# Resolve the memory CLI and a python interpreter once. An explicit MEMORY_BIN
# wins; otherwise use the same home as the engine: SILLY_MEMORY_HOME, else
# ~/.silly-memory.
MEMORY_BIN="${MEMORY_BIN:-${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory}"
: "${MEMORY_PY:=$(command -v python3 || command -v python)}"

# Core dispatcher: `mem <subcommand> ...` -> passes through to the CLI.
# Always operates on the current working directory's workspace.
mem() {
  if [[ ! -f "$MEMORY_BIN" ]]; then
    print -u2 "mem: memory CLI not found at $MEMORY_BIN"
    return 1
  fi
  "$MEMORY_PY" "$MEMORY_BIN" "$@"
}

# --- Inspection ---------------------------------------------------------------
# Dashboard of the current workspace's memory store.
memstatus() { mem status --workspace "$PWD" "$@"; }

# Inspect a single view: pack | observations | staging | bank | events
# Usage: memshow <view> [--limit N]
memshow() {
  if [[ -z "$1" ]]; then
    print -u2 "usage: memshow <pack|observations|staging|bank|events> [--limit N]"
    return 2
  fi
  local view="$1"; shift
  mem show "$view" --workspace "$PWD" "$@"
}

# Shortcuts for each view.
mempack()  { mem show pack --workspace "$PWD" "$@"; }
memobs()   { mem show observations --workspace "$PWD" "$@"; }
memstage() { mem show staging --workspace "$PWD" "$@"; }
membank()  { mem show bank --workspace "$PWD" "$@"; }
memevents(){ mem show events --workspace "$PWD" "$@"; }

# Full-text recall across the store. Usage: memrecall [--all] [--limit N] <query words...>
# Query words are joined into one query; recognized options pass through to the CLI.
memrecall() {
  local -a words opts
  while (( $# )); do
    case "$1" in
      --all|--limit=*) opts+=("$1") ;;
      --limit) opts+=("$1" "$2"); shift ;;
      *) words+=("$1") ;;
    esac
    (( $# )) && shift
  done
  if (( ${#words} == 0 )); then
    print -u2 "usage: memrecall [--all] [--limit N] <query>"
    return 2
  fi
  mem recall "${words[*]}" --workspace "$PWD" "${opts[@]}"
}

# Action items as a task list. Flags: --status open|done|all --tag X --owner Y --all --json
memtasks() { mem tasks --workspace "$PWD" "$@"; }

# List tracked workspaces so you can pick one to inspect.
memws() { mem workspaces "$@"; }

# List all memory markdown files with absolute paths (scope: workspace|global|all).
mempaths() { mem paths --workspace "$PWD" "$@"; }

# --- Processing / maintenance -------------------------------------------------
memprocess() { mem process --workspace "$PWD" "$@"; }   # run the full pipeline now
memrender()  { mem render  --workspace "$PWD" "$@"; }   # regenerate the injected rule file
memobserve() { mem observe --workspace "$PWD" "$@"; }   # add --force to bypass thresholds
memreflect() { mem reflect --workspace "$PWD" "$@"; }
memreindex() { mem reindex --workspace "$PWD" "$@"; }

# --- Lifecycle (user-approved mutations) --------------------------------------
# Store one fact at full confidence. Usage: memadd [--scope auto|workspace|global] <fact words...>
memadd() {
  if (( $# == 0 )); then
    print -u2 "usage: memadd [--scope auto|workspace|global] <fact>"
    return 2
  fi
  mem add --workspace "$PWD" "$@"
}
memdelete()       { mem delete       --workspace "$PWD" "$@"; }   # needs --confirm
memprune-review() { mem prune-review --workspace "$PWD" "$@"; }   # interactive decay review

# --- Learning / inspection ----------------------------------------------------
meminspect()      { mem inspect      --workspace "$PWD" "$@"; }   # score/topic/vector for one entry
memwhy()          { mem why          --workspace "$PWD" "$@"; }   # explain why each pack entry was included
memlearn-status() { mem learn-status --workspace "$PWD" "$@"; }   # learning loop metrics

# Sync _global/memory-bank/profile.md into the recognized global bank files.
memprofile-sync() { mem profile-sync "$@"; }

# --- Dev ----------------------------------------------------------------------
memtest() { mem selftest "$@"; }                        # regression suite

# Health check: 10 read-only checks, exit 0 healthy / 1 warn / 2 error.
memdoctor() { mem doctor "$@"; }

# --- Backup / migration (cross-machine move + backup) ------------------------
# Export the WHOLE memory home (~/.silly-memory) to a portable .tgz bundle.
# This is BOTH your backup and how you MOVE memory to a new machine. Contrast
# upgrade.sh, which is a same-machine in-place engine upgrade only.
# Usage: memexport [OUT.tgz]   (default: ~/silly-memory-export-<date>.tgz)
memexport() {
  local out="${1:-$HOME/silly-memory-export-$(date +%Y-%m-%d).tgz}"
  [[ $# -gt 0 ]] && shift
  mem export --out "$out" "$@"
}

# Restore a bundle produced by memexport (the CLI snapshots any existing store
# first). Writing REQUIRES an explicit --confirm-import YES-IMPORT token — it is
# deliberately NOT injected for you; you pass it. --relink re-plants each
# project's .silly-memory/memory-id; --map-workspace OLD=NEW relinks a moved project.
# Usage: memimport <bundle> --confirm-import YES-IMPORT [--relink] [--map-workspace OLD=NEW]
memimport() {
  if [[ -z "$1" ]]; then
    print -u2 "usage: memimport <bundle> --confirm-import YES-IMPORT [--relink] [--map-workspace OLD=NEW]"
    return 2
  fi
  mem import "$@"
}

# Quick help listing the helpers, with detailed descriptions.
memhelp() {
  cat <<'EOF'
silly-memory helpers — all act on the CURRENT directory's workspace ($PWD).

HOW THE MEMORY PIPELINE WORKS (so the commands below make sense):
  Your chats/edits/commands are captured as raw EVENTS. The "observer" turns
  recent events into dated OBSERVATIONS (short notes). The "reflector" condenses
  many observations into tighter summaries. The "distiller" promotes the durable
  ones into your BANK (the markdown files: actionItems, decisions, stakeholders…).
  A bounded slice of the bank is rendered into the PACK that your AI tool
  (Cursor, Claude Code, or OpenCode) loads into every new chat. Normally this
  all runs automatically at session boundaries — the commands below are for
  inspecting it or forcing a step by hand.

── INSPECT (read-only, safe to run anytime) ─────────────────────────────────
  memstatus
      One-screen dashboard: how many events/observations/staged items you have,
      how big each bank file is, and the size of the injected pack vs its cap.
      Start here to see the overall state of memory for this project.

  memtasks [--status open|done|all] [--tag X] [--owner NAME] [--all] [--json]
      Show action items from the bank as a checkbox task list. Defaults to OPEN
      items in this workspace. --tag/--owner filter; --all spans every workspace;
      --json emits structured JSON (for scripts/agents) instead of text.

  memws
      List every project that has memory, with open/done task counts and last
      activity. Use it to find a project, then pass its path to other commands.

  memrecall [--all] [--limit N] <words...>
      Full-text search across everything stored (events, observations, bank).
      Use when you remember a topic but not where it lives. e.g. memrecall release checklist.
      --all searches every tracked project, not just this one.

  memshow <view> [--limit N]   (views: pack observations staging bank events)
      Print the raw contents of one layer. Shortcuts:
        mempack    what your AI tool actually loads into each chat (the PACK)
        memobs     recent OBSERVATIONS (dated notes the observer wrote)
        memstage   items waiting to be promoted into the bank (low-confidence)
        membank    the durable BANK files in full (your canonical memory)
        memevents  the most recent raw captured events

  mempaths [--scope workspace|global|all]
      List every memory markdown file with its ABSOLUTE path (bank files +
      observations/work-state/pack + the injected rule), with fact counts. Use it
      to grab a path and open/cat a file directly. Defaults to all scopes.

  meminspect <file:line>
      Score, topic assignment, recency factor, and vector-store presence for one
      entry. Find the id from mempaths output, e.g.: meminspect actionItems.md:12

  memwhy [--json]
      Explain why each entry in the current context pack was included: its
      composite score (sidecar score, recency factor), topic bucket, and rank
      among peers. Use this to understand what your AI tool is shown and why.

  memlearn-status
      Learning loop summary: corrections detected, memories reinforced/demoted,
      contradictions flagged, and clarifications asked. Focused subset of memstatus
      aimed at the self-teaching layer.

── LIFECYCLE (user-approved mutations) ──────────────────────────────────────
  memadd [--scope auto|workspace|global] <fact...>
      Store one fact at full confidence right away, e.g. memadd we deploy only
      from main. auto (the default) files preferences, people, conventions, and
      hard rules in global memory and the rest in this project; --scope forces one.
      Text inside <private>…</private> is never stored.

  memdelete <file:line> --confirm
      Permanently remove one entry from the bank and re-index. Requires --confirm
      to prevent accidents. Use mempaths or meminspect to find the entry id.
      Run memreindex after large prune sessions to resync the FTS5 index.

  memprune-review
      Interactive review of all entries at or below the prune floor (score ≤ 0.1).
      Shows each candidate with its score and text; prompts Y/n before each delete.
      Pass --confirm-all to approve all without prompting (batch/CI mode).

  memprofile-sync
      Copy the facts from your private _global/memory-bank/profile.md into the
      global bank files the context pack reads. Re-run after editing the profile.

── PROCESS BY HAND (you rarely need these; they run automatically) ───────────
  memprocess
      Run the WHOLE pipeline now: observe → reflect → distill → render → reindex.
      Use after bulk-editing notes when you don't want to wait for a session end.

  memobserve [--force]
      Run only the OBSERVER: read new raw events and write dated observations.
      Normally it waits until enough new activity has piled up; --force makes it
      run immediately even below that threshold.

  memreflect [--force]
      Run only the REFLECTOR: condense/merge existing observations into tighter
      summaries (reduces noise before distillation). --force ignores the threshold.

  memrender
      Rebuild only the injected pack files (.cursor/rules/_memory-context.mdc,
      .claude/rules/_memory-context.md) from the current bank. Use if the pack looks stale but the bank is correct.

  memreindex
      Rebuild the full-text search index. Use if memrecall seems to miss things.

── BACKUP / MIGRATION (cross-machine move + backup) ─────────────────────────
  memexport [OUT.tgz]
                Export the WHOLE memory home (~/.silly-memory) to a portable
                .tgz bundle — your BACKUP and the way you MOVE memory to a new
                machine. Defaults to ~/silly-memory-export-<date>.tgz.
                (upgrade.sh is same-machine in-place; memexport is the
                cross-machine move + backup.)

  memimport <bundle> --confirm-import YES-IMPORT [--relink] [--map-workspace OLD=NEW]
                Restore a memexport bundle (existing store snapshotted first).
                --confirm-import YES-IMPORT is REQUIRED to write and is never
                injected for you — you pass it. --relink re-plants each project's
                .silly-memory/memory-id; --map-workspace OLD=NEW relinks a project
                whose absolute path changed on this machine.

── MAINTENANCE / DEV ─────────────────────────────────────────────────────────
  memdoctor [--json]
                Run 10 health checks (home perms, SQLite integrity, VERSION,
                weights, privacy, tool wiring, MCP, recall p95, locks, backup
                retention).
                Exit code 0 healthy, 1 at least one warn, 2 at least one error.
  memtest       Run the regression suite (isolated; never touches real data).
  mem <args>    Raw passthrough to the underlying CLI (advanced/escape hatch).
EOF
}

# --- Completion ---------------------------------------------------------------
# Tab-complete views for memshow / subcommands for mem.
# Guarded: compdef only exists after compinit (interactive shells). Never let
# this block change the source return status.
if [[ -n "$ZSH_VERSION" ]] && whence compdef >/dev/null 2>&1; then
  _memshow_complete() { compadd pack observations staging bank events; }
  _mem_complete() {
    compadd recall add render observe reflect process reindex status show tasks workspaces paths selftest doctor \
      delete prune-review inspect why learn-status export import profile-sync
  }
  compdef _memshow_complete memshow 2>/dev/null
  compdef _mem_complete mem 2>/dev/null
fi

true

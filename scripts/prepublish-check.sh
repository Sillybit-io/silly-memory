#!/usr/bin/env bash
#
# scripts/prepublish-check.sh — pre-publish safety gate for the TRACKED tree.
#
#   exit 0  = safe to publish (tracked tree is clean)
#   exit 1  = at least one offender; every offender is named above the summary
#   exit 2  = harness error (not a git repo, or the scanner is missing)
#
# It inspects ONLY the tracked tree — `git ls-files` and
# `git ls-files --cached --ignored`. Untracked / ignored-on-disk files that are
# NOT staged are invisible to every check here, by design (a stray ignored
# `.omo/` on disk must not trip the gate). Three independent checks:
#
#   [1] Tracked-but-ignored artifacts (Oracle O-1, generic): any path git both
#       tracks AND .gitignore matches. No hard-coded list — it catches `.omo/`,
#       `.playwright-mcp/`, `.remember/`, and any FUTURE artifact dir the moment
#       it lands in .gitignore.
#
#   [2] Sensitive-prefix denylist (Oracle Or4-1, belt-and-suspenders): any
#       tracked path under a known private runtime dir (`.omo/`, `.remember/`,
#       `.cursor/`, `.silly-memory/`, `.playwright-mcp/`) or a `memory/<16-hex-workspace-hash>/`
#       store — even if that dir is somehow NOT gitignored (so [1] would miss
#       it). Overlap with [1] is intentional: a file caught by both nets is
#       reported by both.
#
#   [3] Secret / real-username path-leak scan: `scripts/secret_scan.py
#       --tracked`, the project's stdlib, no-network secret gate. That scanner
#       derives the current OS username itself via `id -un` (never
#       `git config user.name`), so `/Users/<you>/…` leaks are caught without
#       this script hard-coding any name. If `gitleaks` is on PATH it is run in
#       addition, with `.gitleaks.toml`; otherwise it is skipped silently
#       (gitleaks is NOT a dependency of this project).
#
set -uo pipefail

# --- locate the repo root and this script's dir (run correctly from anywhere) ---
if ! REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "prepublish-check: FATAL — not inside a git repository" >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! cd "$REPO_ROOT"; then
  echo "prepublish-check: FATAL — cannot cd to $REPO_ROOT" >&2
  exit 2
fi

SECRET_SCAN="${SCRIPT_DIR}/secret_scan.py"
if [ ! -f "$SECRET_SCAN" ]; then
  echo "prepublish-check: FATAL — missing scanner: $SECRET_SCAN" >&2
  exit 2
fi

# --- colours (only when stdout is a TTY) ---
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_BLD=''; C_RST=''
fi

offenders=()        # consolidated "[rule] path" list, printed in the summary
gitleaks_ok=1

echo "${C_BLD}prepublish-check${C_RST} — scanning TRACKED tree at ${REPO_ROOT}"

# === [1] tracked-but-ignored artifacts =======================================
echo
echo "[1/3] tracked-but-ignored artifacts  (git ls-files --cached --ignored --exclude-standard)"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  echo "      ${C_RED}x offender${C_RST}  [tracked-but-ignored]  $f"
  offenders+=("[tracked-but-ignored] $f")
done < <(git ls-files --cached --ignored --exclude-standard)
if [ "${#offenders[@]}" -eq 0 ]; then
  echo "      ${C_GRN}ok${C_RST} none — no tracked file is gitignored"
fi

# === [2] sensitive-prefix denylist (belt-and-suspenders) =====================
echo
echo "[2/3] sensitive-prefix denylist      (.omo/ .remember/ .cursor/ .silly-memory/ .playwright-mcp/ memory/<16-hex>/)"
c2_before="${#offenders[@]}"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  echo "      ${C_RED}x offender${C_RST}  [sensitive-prefix]  $f"
  offenders+=("[sensitive-prefix] $f")
done < <(
  {
    git ls-files -- .omo/ .remember/ .cursor/ .silly-memory/ .playwright-mcp/
    # memory/<16-hex-workspace-hash>/… — a length-exact hex dir a glob can't
    # express, so filter the full memory/ listing.
    git ls-files -- memory/ | grep -E '^memory/[0-9a-f]{16}/' || true
  } | sort -u
)
if [ "${#offenders[@]}" -eq "$c2_before" ]; then
  echo "      ${C_GRN}ok${C_RST} none — no tracked file under a private runtime dir"
fi

# === [3] secret / real-username path-leak scan ===============================
echo
echo "[3/3] secret / real-username path-leak scan  (python3 scripts/secret_scan.py --tracked)"
if scan_out="$(python3 "$SECRET_SCAN" --tracked 2>&1)"; then
  echo "      ${C_GRN}ok${C_RST} secret_scan.py: clean"
else
  echo "      ${C_RED}x offender${C_RST}  [secret_scan] provider-secret / real-username path leak(s):"
  printf '%s\n' "$scan_out" | sed 's/^/        /'
  n_before="${#offenders[@]}"
  # Fold each "path:line: [rule] snippet" detail into the consolidated list.
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    offenders+=("[secret_scan] $line")
  done < <(printf '%s\n' "$scan_out" | grep -E ':[0-9]+: \[' | sed 's/^[[:space:]]*//')
  if [ "${#offenders[@]}" -eq "$n_before" ]; then
    offenders+=("[secret_scan] (non-zero exit; see [3/3] output above)")
  fi
fi

# Optional: gitleaks, only if present on PATH. No network install, ever.
if command -v gitleaks >/dev/null 2>&1; then
  echo "      gitleaks found on PATH — running with .gitleaks.toml (supplementary)"
  if gl_out="$(gitleaks detect --no-banner --redact --config .gitleaks.toml 2>&1)"; then
    echo "      ${C_GRN}ok${C_RST} gitleaks: clean"
  else
    gitleaks_ok=0
    echo "      ${C_RED}x offender${C_RST}  [gitleaks] reported leak(s):"
    printf '%s\n' "$gl_out" | sed 's/^/        /'
    offenders+=("[gitleaks] see [3/3] output above")
  fi
else
  : # gitleaks not installed — skipped silently (it is not a project dependency)
fi

# === summary =================================================================
echo
if [ "${#offenders[@]}" -gt 0 ] || [ "$gitleaks_ok" -eq 0 ]; then
  echo "${C_RED}${C_BLD}prepublish-check: FAIL${C_RST} — do NOT publish. ${#offenders[@]} offender(s):"
  for o in "${offenders[@]}"; do
    echo "  ${C_RED}-${C_RST} $o"
  done
  exit 1
fi

echo "${C_GRN}${C_BLD}prepublish-check: PASS${C_RST} — tracked tree is safe to publish."
echo "  ${C_GRN}ok${C_RST} no tracked-but-ignored artifacts"
echo "  ${C_GRN}ok${C_RST} no tracked files under private runtime dirs (.omo/ .remember/ .cursor/ .silly-memory/ .playwright-mcp/ memory/<hash>/)"
echo "  ${C_GRN}ok${C_RST} no provider secrets or real-username path leaks in tracked content"
exit 0

#!/usr/bin/env bash
set -euo pipefail

die() {
  echo "publish-clean: ERROR — $*" >&2
  exit 1
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"
cd "$repo_root"

if [ -z "${PUBLIC_GIT_NAME:-}" ]; then
  die "PUBLIC_GIT_NAME is required (recommended: your GitHub handle)"
fi
if [ -z "${PUBLIC_GIT_EMAIL:-}" ]; then
  die "PUBLIC_GIT_EMAIL is required (recommended: <handle>@users.noreply.github.com)"
fi

echo "publish-clean: verifying current tracked tree with scripts/prepublish-check.sh"
bash scripts/prepublish-check.sh

echo "publish-clean: running pre-archive tracked-private-dir assertions"
private_paths="$(git ls-files ':(glob).omo/**' '.playwright-mcp/**' '.remember/**')"
if [ -n "$private_paths" ]; then
  echo "publish-clean: private tracked path(s) would enter git archive HEAD:" >&2
  printf '%s\n' "$private_paths" >&2
  die "refusing to publish while .omo/.playwright-mcp/.remember paths are tracked"
fi

tracked_ignored="$(git ls-files --cached --ignored --exclude-standard)"
if [ -n "$tracked_ignored" ]; then
  echo "publish-clean: tracked-but-ignored path(s) would enter git archive HEAD:" >&2
  printf '%s\n' "$tracked_ignored" >&2
  die "refusing to publish while tracked files are matched by .gitignore"
fi

DEST="$(mktemp -d)"
echo "publish-clean: exporting HEAD to $DEST"
git archive --format=tar HEAD | tar -x -C "$DEST"

(
  cd "$DEST"
  git -c init.defaultBranch=main init -q
  git add -A
  GIT_AUTHOR_NAME="$PUBLIC_GIT_NAME" \
    GIT_AUTHOR_EMAIL="$PUBLIC_GIT_EMAIL" \
    GIT_COMMITTER_NAME="$PUBLIC_GIT_NAME" \
    GIT_COMMITTER_EMAIL="$PUBLIC_GIT_EMAIL" \
    git commit -q -m "Initial public release"
)

echo "publish-clean: re-running privacy gate inside clean public repo"
(
  cd "$DEST"
  bash scripts/prepublish-check.sh
)

echo
echo "publish-clean: clean single-commit public repository is ready."
echo "DEST=$DEST"
echo
cat <<'INSTRUCTIONS'
Manual publish steps (review before running; this script never pushes):

  cd "$DEST"
  git log --oneline
  git remote add origin git@github.com:<owner>/<public-repo>.git
  git push -u origin main

If GitHub push protection ever flags a synthetic fixture, inspect the exact
secret alert. T4 made this unlikely; if it is truly a documented non-secret test
fixture, use GitHub's per-secret "allow" bypass for that one alert only.
INSTRUCTIONS

# Publishing a clean public repository

This project is published by creating a new single-commit repository from the
current `HEAD`. The private working repository history is not rewritten, and
the script never pushes.

## Prerequisites

Set an explicit public Git identity before running the script. Do not rely on
your local `git config user.name` or `git config user.email`.

```bash
export PUBLIC_GIT_NAME="<github-handle>"
export PUBLIC_GIT_EMAIL="<handle>@users.noreply.github.com"
```

The privacy gate always flags `/Users/<your current account>` paths. To also
flag other local account names (for example a previous account on this
machine), list them one per line in `.secret-scan-usernames` at the repo root.
That file is gitignored, so the published scanner names nobody.

## Build the clean repository

```bash
MEMORY_ALLOW_NETWORK=0 bash scripts/publish-clean.sh
```

The script prints `DEST=/tmp/...` when it succeeds. That directory is the clean
public repository.

## Verify before pushing

Replace `$DEST` with the path printed by the script.

```bash
git -C "$DEST" log --oneline | wc -l
```

Expected: `1`.

```bash
git -C "$DEST" ls-files | grep -cE '^\.omo/|^\.playwright-mcp/|^\.remember/|/Users/'
```

Expected: `0`.

```bash
git -C "$DEST" ls-files --cached --ignored --exclude-standard
```

Expected: no output.

```bash
git -C "$DEST" log -1 --format='author=%an <%ae>%ncommitter=%cn <%ce>'
git config user.email
```

Expected: both author and committer equal your `PUBLIC_GIT_NAME` /
`PUBLIC_GIT_EMAIL`, not the local `git config user.email`.

```bash
test -d "$DEST/memory/weights/all-MiniLM-L6-v2"
du -sh "$DEST/memory/weights/all-MiniLM-L6-v2"
```

Expected: the shipped weights are present. About 91 MB is normal and intended.

```bash
git -C "$DEST" remote
```

Expected: no output until you add the public remote manually.

## Manual push

Only after reviewing the verification output, add the public remote and push
from the clean `$DEST` repository:

```bash
cd "$DEST"
git remote add origin git@github.com:<owner>/<public-repo>.git
git push -u origin main
```

The `publish-clean.sh` script prints these commands but never executes them.

## GitHub push-protection fallback

The committed fixtures were rewritten to avoid provider-secret alerts, so a
push-protection block is unlikely. If GitHub still flags a fixture, inspect the
exact alert. If it is truly a documented non-secret test fixture, use GitHub's
per-secret allow bypass for that single alert only. Do not disable protection
globally and do not bypass unknown or real-looking secrets.

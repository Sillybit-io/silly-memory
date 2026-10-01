"""The upgrade transaction: back up the home, install over it in place, and recover.

Read this first if you are new to the codebase:
  - upgrade.sh runs every mutating command here while holding the upgrade lock
    (``install_transaction.py run-locked``) on ``~/.silly-memory-upgrade.lock``;
    ``upgrade`` and ``rollback`` refuse to run without it.
  - A transaction lives inside its numbered backup. ``<backup>/.upgrade-transaction/``
    holds ``state.json`` (the home, versions, tools, the engine image digest, the
    phase, and recovery paths) and ``journal.jsonl`` (the installer's external
    writes, see install_transaction.py). Everything else in the backup is the
    verified copy of the home.
  - Backups sit beside the home they copy as ``<home>.upgrade-backup-N-<time>``.
    A copy is made under ``<home>.upgrade-staging-*`` and renamed to its numbered
    name only once its digest matches and its state is written.
  - Every mutating run first finishes a transaction whose phase is neither
    ``complete`` nor ``recovered`` by recovering it: the current home is moved
    aside, the home is rebuilt from a staged copy of the backup, and the
    installer's external writes are reverted. Each rename is recorded before it
    happens, so an interrupted recovery resumes, even with the home absent.
  - The home is ``SILLY_MEMORY_HOME`` when set, otherwise ``~/.silly-memory``.
  - ``MEMORY_FORCE_UPGRADE_FAILURE=<step>`` and ``MEMORY_FORCE_UPGRADE_HANG=<point>``
    are test hooks that fail a step or stop at a point.

Public interface (imported elsewhere): the CLI subcommands below.
Depends on: install_transaction (sibling, stdlib only); the repository's
    memory_system.preflight for the FTS5 probe.
Used by: upgrade.sh.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_transaction as tx  # noqa: E402
from install_transaction import InstallError  # noqa: E402

STATE_DIR = ".upgrade-transaction"
STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
LOCK_NAME = ".silly-memory-upgrade.lock"
BACKUP_KEEP = 5
FINAL_PHASES = frozenset({"complete", "recovered"})
FAILURE_ENV = "MEMORY_FORCE_UPGRADE_FAILURE"
HANG_ENV = "MEMORY_FORCE_UPGRADE_HANG"

EXIT_FAILED = 1
EXIT_MISSING = 66
EXIT_UNSAFE = 65


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _log(message: str) -> None:
    print(f"  {message}", file=sys.stderr, flush=True)


def _test_hook(point: str) -> None:
    if os.environ.get(HANG_ENV) == point:
        _log(f"test hook: stopping at {point}")
        time.sleep(600)
    if os.environ.get(FAILURE_ENV) == point:
        raise InstallError(f"test hook: failing at {point}", 70)


# --- homes and backups ------------------------------------------------------------


def default_home() -> Path:
    return Path.home() / ".silly-memory"


def override_home() -> Path | None:
    value = os.environ.get("SILLY_MEMORY_HOME")
    return Path(value).expanduser().absolute() if value else None


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def backup_locations() -> list[tuple[Path, str]]:
    """``(parent, home name)`` for every place a backup can sit."""
    homes = [default_home()]
    override = override_home()
    if override is not None:
        homes.append(override)
    return list(dict.fromkeys((home.parent, home.name) for home in homes))


def list_backups() -> list[tuple[int, Path]]:
    """Published numbered backups, oldest number first; staging copies are not backups."""
    found: list[tuple[int, Path]] = []
    for parent, name in backup_locations():
        if not parent.is_dir():
            continue
        prefix = f"{name}.upgrade-backup-"
        for child in parent.iterdir():
            match = re.match(r"(\d+)-", child.name[len(prefix):]) if child.name.startswith(prefix) else None
            if match and child.is_dir() and not child.is_symlink():
                found.append((int(match.group(1)), child))
    return sorted(found, key=lambda item: (item[0], item[1].name))


def _unique(path: Path) -> Path:
    candidate, n = path, 1
    while _present(candidate):
        n += 1
        candidate = path.with_name(f"{path.name}-{n}")
    return candidate


def tree_digest(root: Path) -> str:
    """Content, mode, and symlink digest of a home, without its transaction record."""
    digest = hashlib.sha256()
    for current, dirnames, filenames in os.walk(root):
        base = Path(current)
        if base == root and STATE_DIR in dirnames:
            dirnames.remove(STATE_DIR)
        links = sorted(d for d in dirnames if (base / d).is_symlink())
        dirnames[:] = sorted(d for d in dirnames if d not in links)
        digest.update(f"D\0{base.relative_to(root).as_posix()}\0".encode())
        for name in sorted(filenames + links):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update(f"L\0{rel}\0{os.readlink(path)}\0".encode())
                continue
            digest.update(f"F\0{rel}\0".encode())
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(f"\0{path.stat().st_mode & 0o777:o}\0".encode())
    return digest.hexdigest()


def _copy_home(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest, symlinks=True, ignore=lambda d, _: [STATE_DIR] if Path(d) == source else [])
    os.chmod(dest, 0o700)


# --- transaction state ------------------------------------------------------------


def state_path(backup: Path) -> Path:
    return backup / STATE_DIR / STATE_FILE


def journal_path(backup: Path) -> Path:
    return backup / STATE_DIR / JOURNAL_FILE


def load_state(backup: Path) -> dict[str, Any] | None:
    path = state_path(backup)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"unreadable upgrade journal {path} ({exc}); nothing was changed", tx.EXIT_INVALID)
    home = state.get("home") if isinstance(state, dict) else None
    if not (isinstance(home, str) and Path(home).is_absolute()) or not isinstance(state.get("digest"), str):
        raise InstallError(f"upgrade journal {path} names invalid paths; nothing was changed", tx.EXIT_INVALID)
    return state


def save_state(backup: Path, state: dict[str, Any], phase: str | None = None) -> None:
    if phase is not None:
        state["phase"] = phase
        state.setdefault("history", []).append([phase, _now()])
    data = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tx.atomic_write_bytes(state_path(backup), data, 0o600)


def pending_transactions() -> list[Path]:
    """Backups whose transaction neither completed nor was recovered."""
    pending = []
    for _, backup in list_backups():
        state = load_state(backup)
        if state is not None and state.get("phase") not in FINAL_PHASES:
            pending.append(backup)
    return pending


def single_pending() -> Path | None:
    pending = pending_transactions()
    if len(pending) > 1:
        names = ", ".join(str(journal_path(b)) for b in pending)
        raise InstallError(f"more than one unfinished upgrade ({names}); resolve them by hand", tx.EXIT_INVALID)
    return pending[0] if pending else None


# --- selection and pre-flight -----------------------------------------------------


def _require_home(home: Path, label: str) -> None:
    if home.is_symlink():
        raise InstallError(f"{label} {home} is a symlink; point SILLY_MEMORY_HOME at the real folder", EXIT_UNSAFE)
    if not home.is_dir():
        raise InstallError(f"{label} {home} is not an installation; move it aside, then rerun", EXIT_MISSING)


def select_home() -> Path:
    """The home to upgrade in place."""
    home = override_home() or default_home()
    if not _present(home):
        raise InstallError(f"no installation at {home}; run ./install.sh first", EXIT_MISSING)
    _require_home(home, "memory home")
    return home


def installed_version(home: Path, from_version: str | None) -> str:
    if from_version:
        return from_version
    marker = home / "VERSION"
    if not marker.is_file():
        raise InstallError(
            f"installed VERSION missing at {marker}\n"
            "Hint: run ./install.sh first, or pass --from-version VERSION if you know the installed version.",
            EXIT_MISSING,
        )
    return marker.read_text(encoding="utf-8").strip() or "0.0.0+unknown"


def recorded_tools(home: Path) -> list[str] | None:
    """The tools the installation was wired for, when its config names them."""
    try:
        tools = json.loads((home / "config.json").read_text(encoding="utf-8")).get("tools")
    except (OSError, ValueError, AttributeError):
        return None
    known = [t for t in tools if t in ("cursor", "claude-code", "opencode")] if isinstance(tools, list) else []
    return known or None


def shared_files(tools: list[str] | None) -> list[tuple[str, Path]]:
    home = Path.home()
    cursor, claude = home / ".cursor", home / ".claude"
    wanted = tools or [t for t, d in (("cursor", cursor), ("claude-code", claude)) if d.is_dir()]
    files = [("zsh", home / ".zshrc")]
    if "cursor" in wanted:
        files.append(("cursor-hooks", cursor / "hooks.json"))
    if "claude-code" in wanted:
        files.append(("claude-hooks", claude / "settings.json"))
    return files


def preflight(repo: Path, home: Path, tools: list[str] | None) -> None:
    sys.path.insert(0, str(repo / "memory" / "lib"))
    from memory_system.preflight import check_fts5_available, check_sync_filesystem

    ok, message = check_fts5_available()
    if not ok:
        raise InstallError(f"FTS5 unavailable: {message}", 2)
    if shutil.disk_usage(Path.home()).free < 100 * 1024 * 1024:
        raise InstallError("disk space below 100MB", EXIT_FAILED)
    if not os.access(home, os.R_OK | os.W_OK | os.X_OK):
        raise InstallError(f"memory home lacks rwx perms: {home}", EXIT_FAILED)
    install_lock = home / ".install.lock"
    if install_lock.exists() and time.time() - install_lock.stat().st_mtime > 300:
        raise InstallError(f"stale install lock present: {install_lock}", EXIT_FAILED)
    for kind, path in shared_files(tools):
        if kind == "zsh":
            data = tx.read_bytes(path)
            tx.zsh_owned_blocks(data.decode("utf-8") if data is not None else "")
        else:
            tx.validate_json(kind, path)
    sync_ok, sync_message = check_sync_filesystem(home)
    if not sync_ok and sync_message:
        _log(f"WARNING: {sync_message}")


# --- the upgrade ------------------------------------------------------------------


def create_backup(home: Path, versions: tuple[str, str], tools: list[str] | None) -> Path:
    number = max((n for n, _ in list_backups()), default=0) + 1
    stamp = _now()
    final = home.parent / f"{home.name}.upgrade-backup-{number}-{stamp}"
    staging = home.parent / f"{home.name}.upgrade-staging-{number}-{stamp}"
    if _present(final) or _present(staging):
        raise InstallError(f"backup path already exists: {final}", 73)
    digest = tree_digest(home)
    try:
        _copy_home(home, staging)
        copied = tree_digest(staging)
        if copied != digest:
            raise InstallError(f"backup integrity mismatch\nhome={digest}\nbackup={copied}", EXIT_FAILED)
        (staging / STATE_DIR).mkdir(mode=0o700)
        journal_path(staging).touch(mode=0o600)
        state = {
            "format": 1,
            "number": number,
            "home": str(home),
            "original_version": versions[0],
            "target_version": versions[1],
            "tools": tools,
            "digest": digest,
        }
        save_state(staging, state, "backed-up")
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _log(f"backup created: {final}")
    _log(f"backup digest: {digest}")
    return final


def _engine_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in (tx.JOURNAL_ENV, tx.LOCK_FD_ENV)}
    env["SILLY_MEMORY_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(argv: list[str], env: dict[str, str], what: str) -> None:
    rc = subprocess.run(argv, env=env, cwd=str(Path.home())).returncode
    if rc != 0:
        raise InstallError(f"{what} failed (exit {rc})", rc)


def run_install(repo: Path, backup: Path, home: Path, tools: list[str] | None) -> None:
    env = _engine_env(home)
    env[tx.JOURNAL_ENV] = str(journal_path(backup))
    env.setdefault("MEMORY_SKIP_MODEL_DOWNLOAD", "1")
    argv = ["bash", str(repo / "install.sh")] + (["--tools", ",".join(tools)] if tools else [])
    _run(argv, env, "install.sh")


def run_doctor(home: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, str(home / "bin" / "memory"), "doctor", "--json"],
        env=env, cwd=str(Path.home()), capture_output=True, text=True,
    )
    try:
        status = json.loads(result.stdout).get("status", "error")
    except (ValueError, AttributeError):
        status = "error"
    if status == "error" or result.returncode >= 2:
        sys.stderr.write(result.stdout[-4000:])
        raise InstallError(f"memdoctor status={status} exit={result.returncode}", EXIT_FAILED)
    _log(f"memdoctor status={status}")


def prune_backups(keep_backup: Path) -> None:
    protected = {keep_backup, *pending_transactions()}
    backups = list_backups()
    removable = [b for _, b in backups if b not in protected]
    for backup in removable[: max(0, len(backups) - BACKUP_KEEP)]:
        shutil.rmtree(backup)
        _log(f"pruned old backup: {backup}")
    _log(f"backup retention OK: {min(len(backups), BACKUP_KEEP)}/{BACKUP_KEEP} kept")


class Steps:
    """Numbered step output in the installer's style; returns the failing exit code."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.n = 0

    def run(self, description: str, action: Callable[[], None]) -> None:
        self.n += 1
        print(f"[{self.n}/{self.total}] {description}… ", end="", file=sys.stderr, flush=True)
        started = time.monotonic()
        try:
            action()
        except InstallError:
            print(f"❌ ({int((time.monotonic() - started) * 1000)}ms)", file=sys.stderr, flush=True)
            raise
        print(f"✅ ({int((time.monotonic() - started) * 1000)}ms)", file=sys.stderr, flush=True)


def upgrade(repo: Path, from_version: str | None) -> int:
    steps = Steps(7)
    context: dict[str, Any] = {}

    def resume() -> None:
        pending = single_pending()
        if pending is None:
            _log("no unfinished upgrade")
            return
        _log(f"finishing the interrupted upgrade recorded in {pending}")
        recover(pending, "failed-upgrade")

    def detect() -> None:
        home = select_home()
        context.update(home=home, version=installed_version(home, from_version))
        context["repo_version"] = (repo / "VERSION").read_text(encoding="utf-8").strip()
        context["tools"] = recorded_tools(home)
        _log(f"installed version: {context['version']}; repo version: {context['repo_version']}")
        _log(f"home: {home}")

    try:
        steps.run("Finishing any interrupted upgrade", resume)
        steps.run("Detecting the installation", detect)
        home: Path = context["home"]
        steps.run("Pre-flight (FTS5, disk, permissions, shared settings)",
                  lambda: preflight(repo, home, context["tools"]))

        def backup_step() -> None:
            context["backup"] = create_backup(home, (context["version"], context["repo_version"]), context["tools"])

        steps.run("Numbered backup", backup_step)
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code

    backup: Path = context["backup"]
    state = load_state(backup)
    assert state is not None

    def install() -> None:
        save_state(backup, state, "installing")
        run_install(repo, backup, home, context["tools"])
        save_state(backup, state, "installed")
        _test_hook("after-install")

    def doctor() -> None:
        run_doctor(home, _engine_env(home))
        save_state(backup, state, "checked")

    def finish() -> None:
        tx.atomic_write_bytes(home / ".upgraded-from", context["version"].encode("utf-8"))
        _log(".upgraded-from marker written")
        save_state(backup, state, "complete")
        prune_backups(backup)

    try:
        steps.run(f"Running install.sh for {home}", install)
        steps.run("Running memdoctor", doctor)
        steps.run("Upgrade marker and backup retention", finish)
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"ERROR: upgrade failed after backup; rolling back from {backup}", file=sys.stderr)
        try:
            recover(backup, "failed-upgrade")
        except InstallError as recovery_error:
            print(f"ERROR: rollback failed: {recovery_error}", file=sys.stderr)
            print(f"The backup and its journal stay at {backup}; rerun ./upgrade.sh to retry.", file=sys.stderr)
            return recovery_error.code
        return exc.code
    print(f"Upgrade complete: {context['version']} -> {context['repo_version']}")
    return 0


# --- recovery -----------------------------------------------------------------------


def entry_point_problems(state: dict[str, Any]) -> list[str]:
    """Tool entry points that would not reach the restored engine."""
    user_home = Path.home()
    engine = Path(state["home"]) / "bin" / "memory"
    problems: list[str] = []
    rc = tx.read_bytes(user_home / ".zshrc")
    for block in tx.zsh_owned_blocks(rc.decode("utf-8") if rc is not None else ""):
        for sourced in re.findall(r'source "([^"]+)"', block):
            path = Path(sourced.replace("$HOME", str(user_home)).replace("${HOME}", str(user_home)))
            if not path.is_file():
                problems.append(f"~/.zshrc sources missing {path}")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or user_home / ".config")
    for shim in (user_home / ".cursor" / "hooks" / tx.CURSOR_SHIM, user_home / ".claude" / "hooks" / tx.CLAUDE_SHIM,
                 config / "opencode" / "plugins" / "silly-memory.js"):
        if shim.is_file() and not engine.is_file():
            problems.append(f"{shim} reaches missing {engine}")
    return problems


def restored_doctor_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("SILLY_MEMORY_HOME", tx.JOURNAL_ENV, tx.LOCK_FD_ENV)}
    if home != default_home():
        env["SILLY_MEMORY_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def recover(backup: Path, reason: str) -> None:
    """Restore the home and wiring recorded in ``backup``; safe to resume."""
    state = load_state(backup)
    if state is None:
        raise InstallError(f"{backup} has no upgrade journal", tx.EXIT_INVALID)
    home = Path(state["home"])
    entries = tx.load_journal(journal_path(backup))
    recovery = state.get("recovery") if state.get("phase") == "recovering" else None

    if recovery is None:
        if tree_digest(backup) != state["digest"]:
            raise InstallError(f"{backup} no longer matches its recorded digest; nothing was changed", tx.EXIT_INVALID)
        tx.revert_entries(entries, dry_run=True)  # a changed owned entry stops recovery here
        recovery = {"reason": reason, "step": "aside"}
        if _present(home):
            recovery["aside"] = str(_unique(home.parent / f"{home.name}.{reason}-{_now()}"))
        state["recovery"] = recovery
        save_state(backup, state, "recovering")

    if recovery["step"] == "aside":
        aside = recovery.get("aside")
        if aside and _present(home) and not _present(Path(aside)):
            os.rename(home, aside)
            _log(f"moved {home} to {aside}")
        recovery["step"] = "stage"
        save_state(backup, state)
        _test_hook("recovery-after-aside")

    if recovery["step"] == "stage":
        staging = Path(recovery.get("staging") or _unique(home.parent / f"{home.name}.restore-staging-{_now()}"))
        recovery["staging"] = str(staging)
        save_state(backup, state)
        if _present(staging):
            shutil.rmtree(staging)
        _copy_home(backup, staging)
        if tree_digest(staging) != state["digest"]:
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"restored copy of {backup} does not match its digest", EXIT_FAILED)
        recovery["step"] = "restore"
        save_state(backup, state)

    if recovery["step"] == "restore":
        staging = Path(recovery["staging"])
        if _present(staging):
            if _present(home):
                raise InstallError(f"{home} appeared during recovery; {staging} holds the restored copy", tx.EXIT_CONFLICT)
            os.rename(staging, home)
            _log(f"restored {backup.name} to {home}")
        recovery["step"] = "rewire"
        save_state(backup, state)

    if recovery["step"] == "rewire":
        restored = tx.revert_entries(entries)
        _log(f"restored {len(restored)} tool file(s) and setting(s)")
        recovery["step"] = "check"
        save_state(backup, state)

    problems = entry_point_problems(state)
    if problems:
        raise InstallError("restored wiring does not reach the restored engine:\n  " + "\n  ".join(problems), EXIT_FAILED)
    run_doctor(home, restored_doctor_env(home))
    save_state(backup, state, "recovered")
    _log(f"{home} is back at {state.get('original_version', 'its previous version')}")


# --- rollback -----------------------------------------------------------------------


def select_rollback(target_number: int | None) -> Path:
    backups = [(n, b) for n, b in list_backups() if target_number is None or n == target_number]
    if not backups:
        suffix = f" for --target {target_number}" if target_number is not None else ""
        raise InstallError(f"no numbered upgrade backup found{suffix}", EXIT_MISSING)
    return backups[-1][1]


def rollback(target_number: int | None) -> int:
    try:
        pending = single_pending()
        if pending is not None:
            _log(f"finishing the interrupted upgrade or rollback recorded in {pending}")
            recover(pending, "failed-upgrade")
            print(f"Rollback complete: restored {pending.name}")
            return 0
        backup = select_rollback(target_number)
        print(f"Selected backup: {backup}", file=sys.stderr)
        recover(backup, "discard")
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    print(f"Rollback complete: restored {backup.name}")
    return 0


# --- read-only views ----------------------------------------------------------------


def print_backups() -> None:
    print("Available numbered backups:", file=sys.stderr)
    for number, backup in list_backups():
        state = load_state(backup)
        phase = state.get("phase") if state else "no journal"
        print(f"  {number}: {backup} [{phase}]", file=sys.stderr)


def dry_run(repo: Path, from_version: str | None) -> int:
    """Print what an upgrade would do, in order; writes nothing."""
    try:
        pending = single_pending()
        if pending is not None:
            print(f"1. would first recover the unfinished upgrade recorded in {journal_path(pending)}")
        home = select_home()
        version = installed_version(home, from_version)
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    number = max((n for n, _ in list_backups()), default=0) + 1
    tools = recorded_tools(home)
    lines = [
        f"detect {home} at version {version}; repo version {(repo / 'VERSION').read_text(encoding='utf-8').strip()}",
        f"take the upgrade lock at {Path.home() / LOCK_NAME}",
        "check FTS5, disk space, permissions, and shared tool settings",
        f"copy {home} to {home.parent / f'{home.name}.upgrade-backup-{number}-<time>'} and verify its digest",
        f"run install.sh with SILLY_MEMORY_HOME={home} (tools: {', '.join(tools) if tools else 'detected'})",
        "run memdoctor",
        f"write .upgraded-from and keep the newest {BACKUP_KEEP} backups",
    ]
    start = 2 if pending is not None else 1
    for offset, line in enumerate(lines):
        print(f"{start + offset}. (dry-run) would: {line}")
    return 0


# --- CLI ----------------------------------------------------------------------------


def _require_lock() -> None:
    """Refuse to mutate unless this process holds the upgrade lock."""
    value = os.environ.get(tx.LOCK_FD_ENV, "")
    lock = Path.home() / LOCK_NAME
    try:
        fd = int(value)
        held = os.fstat(fd)
        on_disk = os.stat(lock)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ValueError, OSError):
        raise InstallError(f"run this through upgrade.sh; it holds the upgrade lock at {lock}", 2)
    if (held.st_dev, held.st_ino) != (on_disk.st_dev, on_disk.st_ino):
        raise InstallError(f"the inherited lock is not {lock}", 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="upgrade_transaction", description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("upgrade", "dry-run"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--from-version")
    p = sub.add_parser("rollback")
    p.add_argument("--target", type=int)
    sub.add_parser("list-backups")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "list-backups":
            print_backups()
            return 0
        if args.cmd == "dry-run":
            return dry_run(Path(args.repo), args.from_version)
        _require_lock()
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    if args.cmd == "upgrade":
        return upgrade(Path(args.repo), args.from_version)
    return rollback(args.target)


if __name__ == "__main__":
    raise SystemExit(main())

"""upgrade.sh: upgrade a home in place and roll it back.

Guards:
  - An upgrade keeps the stores, replaces the engine so it matches a fresh
    install, and updates only the tool files the installer owns. Explicit and
    automatic rollback bring back the home byte for byte together with the
    tool settings the upgrade changed; a hand-changed owned setting stops the
    rollback before anything moves.
  - Interruptions: a killed upgrade releases its lock and the next run recovers
    it; a recovery killed with the home absent resumes.
  - Malformed shared settings, several unfinished journals, a damaged backup, a
    backup without a journal, and an invalid or missing home all fail before
    anything changes; --dry-run writes nothing.
  - A custom home upgrades and rolls back in place; a reinstall after an
    upgrade matches a fresh install. Upgrades and reinstalls keep the MCP
    setting.

Isolation: throwaway HOMEs and projects, SILLY_MEMORY_HOME unset unless a case
sets it, and a PATH with no real tool commands.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = PROJECT_ROOT / "install.sh"
UPGRADE_SH = PROJECT_ROOT / "upgrade.sh"
UPGRADE_TX = PROJECT_ROOT / "memory" / "lib" / "memory_system" / "system" / "upgrade_transaction.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
HOME_VARS = ("SILLY_MEMORY_HOME", "SILLY_MEMORY_UPGRADE_JOURNAL", "SILLY_MEMORY_UPGRADE_LOCK_FD",
             "SILLY_MEMORY_UPGRADE_LOCK_TIMEOUT", "XDG_CONFIG_HOME", "MEMORY_BIN", "MEMORY_FORCE_UPGRADE_FAILURE",
             "MEMORY_FORCE_UPGRADE_HANG")
STATE_DIR = ".upgrade-transaction"
OLD_VERSION = "0.9.0"
# Written by doctor, banners, and locks during a run; not part of a home's content.
TRANSIENT = {
    ".doctor-cache.json", ".first-run-seen", ".upgraded-from", ".install.lock", ".upgrade.lock", "__pycache__",
    ".silly-memory-upgrade.lock",
}
MANAGED_PREFIXES = ("bin/", "lib/", "hooks/", "cursor-extras/")
SHARED_JSON = (".cursor/hooks.json", ".claude/settings.json")


def content_digest(root: Path) -> str:
    """Digest of every file's path and content (links by target), minus transients."""
    outer = hashlib.sha256()
    for current, dirnames, filenames in os.walk(root):
        base = Path(current)
        dirnames[:] = sorted(d for d in dirnames if d not in TRANSIENT and not (base == root and d == STATE_DIR))
        for name in sorted(filenames):
            if name in TRANSIENT:
                continue
            path = base / name
            rel = path.relative_to(root).as_posix()
            value = "L:" + os.readlink(path) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()
            outer.update(f"{rel}\0{value}\0".encode())
    return outer.hexdigest()


def footprint(home: Path) -> dict[str, str]:
    rows = {}
    for path in sorted(p for p in home.rglob("*") if p.is_file()):
        rel = path.relative_to(home).as_posix()
        if (rel == "VERSION" or rel.startswith(MANAGED_PREFIXES)) and "__pycache__" not in rel:
            rows[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class _UpgradeCase(unittest.TestCase):
    """One fixed temporary root per class, so absolute paths in a template stay valid."""

    root: Path
    home: Path
    projects: Path
    py_bin: Path

    @classmethod
    def _make_root(cls, prefix: str) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix=prefix))
        cls.home = cls.root / "home"
        cls.projects = cls.root / "projects"
        cls.py_bin = cls.root / "py-bin"
        cls.py_bin.mkdir()
        python = cls.py_bin / "python3"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        python.chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _env(cls, **overrides: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in HOME_VARS}
        env.update(
            {
                "HOME": str(cls.home),
                "PATH": f"{cls.py_bin}:{SYSTEM_PATH}",
                "MEMORY_ALLOW_NETWORK": "0",
                "MEMORY_EMBEDDING_BACKEND": "noop",
                "MEMORY_SKIP_MODEL_DOWNLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        env.update(overrides)
        return env

    @classmethod
    def _run(cls, argv: list[str], timeout: int = 900, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, cwd=str(cwd or cls.home), capture_output=True, text=True, env=cls._env(**env), timeout=timeout
        )

    @classmethod
    def _install(cls, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        result = cls._run(["bash", str(INSTALL_SH), *args], **env)
        assert result.returncode == 0, f"install.sh failed:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
        return result

    def _upgrade(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return self._run(["bash", str(UPGRADE_SH), *args], **env)

    def _ok(self, result: subprocess.CompletedProcess[str], what: str = "upgrade.sh") -> None:
        self.assertEqual(result.returncode, 0, msg=f"{what} failed:\nstdout=\n{result.stdout[-3000:]}\nstderr=\n{result.stderr[-4000:]}")

    @classmethod
    def _cursor_hook(cls, project: Path) -> dict[str, Any]:
        payload = {"hook_event_name": "sessionStart", "conversation_id": "c-1", "workspace_roots": [str(project)]}
        result = subprocess.run(
            ["bash", str(cls.home / ".cursor" / "hooks" / "memory-hook.sh")], input=json.dumps(payload),
            capture_output=True, text=True, env=cls._env(), cwd=str(project), timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def _backups(self, parent: Path, name: str) -> list[Path]:
        return sorted(p for p in parent.glob(f"{name}.upgrade-backup-*") if p.is_dir())

    @staticmethod
    def _number(backup: Path) -> int:
        match = re.search(r"\.upgrade-backup-(\d+)-", backup.name)
        assert match is not None, backup
        return int(match.group(1))

    def _state(self, backup: Path) -> dict[str, Any]:
        return _json(backup / STATE_DIR / "state.json")

    def _wiring(self) -> dict[str, bytes]:
        """Every tool file outside the memory home and its backups."""
        files: dict[str, bytes] = {}
        roots = [self.home / ".cursor", self.home / ".claude", self.home / ".config", self.home / ".claude.json", self.home / ".zshrc"]
        for root in roots:
            paths = [root] if root.is_file() else sorted(root.rglob("*")) if root.is_dir() else []
            for path in paths:
                if path.is_file():
                    files[path.relative_to(self.home).as_posix()] = path.read_bytes()
        return files

    def _assert_wiring_equal(self, before: dict[str, bytes], after: dict[str, bytes]) -> None:
        self.assertEqual(sorted(after), sorted(before), msg="tool files added or removed")
        for rel, data in before.items():
            if rel in SHARED_JSON:
                self.assertEqual(json.loads(after[rel]), json.loads(data), msg=rel)
            else:
                self.assertEqual(after[rel], data, msg=rel)

    def _wait_for(self, predicate: Any, what: str, timeout: float = 600) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.2)

    def _start_detached(self, *args: str, **env: str) -> subprocess.Popen[bytes]:
        log = (self.root / "detached.log").open("wb")
        return subprocess.Popen(
            ["bash", str(UPGRADE_SH), *args], env=self._env(**env), cwd=str(self.home),
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=30)


@unittest.skipUnless(INSTALL_SH.exists() and UPGRADE_SH.exists(), "install.sh and upgrade.sh must be adjacent to the source tree")
class TestUpgrade(_UpgradeCase):
    """Upgrades of an installation at ~/.silly-memory (or a custom home)."""

    OLD_RULE = "# memory-recall from an older release\n"

    @classmethod
    def setUpClass(cls) -> None:
        cls._make_root("upgrade_")
        cls.home.mkdir()
        (cls.home / ".zshrc").write_text("export EDITOR=vim\n", encoding="utf-8")
        (cls.home / ".cursor").mkdir()
        cls._install()
        project = cls.projects / "alpha"
        (project / ".git").mkdir(parents=True)
        cls._cursor_hook(project)
        neutral = cls.home / ".silly-memory"
        cls.workspace_id = (project / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip()
        bank = neutral / cls.workspace_id / "memory-bank"
        bank.mkdir(parents=True, exist_ok=True)
        (bank / "test-note.md").write_text("# Test Note\n\n- alpha deploys on Fridays\n", encoding="utf-8")
        cls.fresh = footprint(neutral)
        cls.template = cls.root / "template"
        shutil.copytree(cls.home, cls.template / "home", symlinks=True)
        shutil.copytree(cls.projects, cls.template / "projects", symlinks=True)

    def setUp(self) -> None:
        for name in ("home", "projects", "custom"):
            shutil.rmtree(self.root / name, ignore_errors=True)
        for name in ("home", "projects"):
            shutil.copytree(self.template / name, self.root / name, symlinks=True)
        self.neutral = self.home / ".silly-memory"

    def _age(self, home: Path) -> None:
        """Make an installation look like an older release: older VERSION, an older
        owned rule and hook shim, and a leftover module."""
        (home / "VERSION").write_text(f"{OLD_VERSION}\n", encoding="utf-8")
        (home / "lib" / "memory_system" / "worker.py").write_text("# leftover of an older layout\n", encoding="utf-8")
        rule = self.home / ".cursor" / "rules" / "memory-recall.mdc"
        rule.write_text(self.OLD_RULE, encoding="utf-8")
        ownership_path = home / ".installed-artifacts.json"
        ownership = _json(ownership_path)
        ownership["files"][str(rule)]["sha256"] = hashlib.sha256(self.OLD_RULE.encode("utf-8")).hexdigest()
        ownership_path.write_text(json.dumps(ownership), encoding="utf-8")
        with self._shim().open("a", encoding="utf-8") as handle:
            handle.write("# shipped by an older release\n")

    def _shim(self) -> Path:
        return self.home / ".cursor" / "hooks" / "memory-hook.sh"

    def _events(self) -> int:
        events = self.neutral / self.workspace_id / "events.jsonl"
        return len(events.read_text(encoding="utf-8").splitlines()) if events.exists() else 0

    def _assert_restored(self, backup: Path, home_digest: str, wiring: dict[str, bytes]) -> None:
        self.assertEqual(content_digest(self.neutral), home_digest)
        self.assertEqual(content_digest(backup), home_digest, msg="backup changed")
        self._assert_wiring_equal(wiring, self._wiring())
        self.assertEqual((self.neutral / "VERSION").read_text().strip(), OLD_VERSION)
        before = self._events()
        self._cursor_hook(self.projects / "alpha")
        self.assertGreater(self._events(), before, msg="the restored hook does not reach the restored engine")

    def test_upgrade_and_rollback_restore_the_home_and_the_settings_it_changed(self) -> None:
        self._age(self.neutral)
        home_digest, wiring = content_digest(self.neutral), self._wiring()

        # Dry run: the plan, in order, and not one byte written.
        everything = content_digest(self.home)
        dry = self._upgrade("--dry-run")
        self._ok(dry, "upgrade.sh --dry-run")
        self.assertIn(f"detect {self.neutral} at version {OLD_VERSION}", dry.stdout)
        self.assertIn(f"run install.sh with SILLY_MEMORY_HOME={self.neutral}", dry.stdout)
        self.assertEqual(content_digest(self.home), everything)
        self.assertFalse((self.home / ".silly-memory-upgrade.lock").exists())

        self._ok(self._upgrade())
        self.assertEqual((self.neutral / "VERSION").read_text().strip(), (PROJECT_ROOT / "VERSION").read_text().strip())
        self.assertEqual((self.neutral / ".upgraded-from").read_text().strip(), OLD_VERSION)
        self.assertFalse((self.neutral / "lib" / "memory_system" / "worker.py").exists())
        self.assertEqual(footprint(self.neutral), self.fresh)
        note = self.neutral / self.workspace_id / "memory-bank" / "test-note.md"
        self.assertIn("alpha deploys on Fridays", note.read_text(encoding="utf-8"))
        for installed, shipped in (
            (self.home / ".cursor" / "rules" / "memory-recall.mdc", PROJECT_ROOT / "cursor-extras" / "rules" / "memory-recall.mdc"),
            (self._shim(), PROJECT_ROOT / "cursor-extras" / "hooks" / "memory-hook.sh"),
        ):
            self.assertEqual(installed.read_bytes(), shipped.read_bytes(), msg=str(installed))
        (backup,) = self._backups(self.home, ".silly-memory")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(content_digest(backup), home_digest)
        state = self._state(backup)
        self.assertEqual((state["phase"], state["home"], state["original_version"]), ("complete", str(self.neutral), OLD_VERSION))

        # A hand-changed owned file stops the rollback before anything moves.
        upgraded_shim = self._shim().read_bytes()
        self._shim().write_bytes(upgraded_shim + b"# my own tweak\n")
        before_refusal = content_digest(self.home)
        refused = self._upgrade("--rollback", "--confirm")
        self.assertEqual(refused.returncode, 4, msg=refused.stderr[-3000:])
        self.assertIn(str(self._shim()), refused.stderr)
        self.assertEqual(content_digest(self.home), before_refusal)
        self._shim().write_bytes(upgraded_shim)

        self._ok(self._upgrade("--rollback", "--confirm"), "rollback")
        self.assertTrue((self.home / ".silly-memory-upgrade.lock").is_file())
        self.assertTrue(list(self.home.glob(".silly-memory.discard-*")))
        self.assertEqual(self._state(backup)["phase"], "recovered")
        self._assert_restored(backup, home_digest, wiring)

    def test_failure_after_install_rolls_back_automatically(self) -> None:
        self._age(self.neutral)
        home_digest, wiring = content_digest(self.neutral), self._wiring()
        failed = self._upgrade(MEMORY_FORCE_UPGRADE_FAILURE="after-install")
        self.assertEqual(failed.returncode, 70, msg=failed.stderr[-3000:])
        self.assertIn("rolling back", failed.stderr)
        (backup,) = self._backups(self.home, ".silly-memory")
        self.assertEqual(self._state(backup)["phase"], "recovered")
        self.assertTrue(list(self.home.glob(".silly-memory.failed-upgrade-*")))
        self._assert_restored(backup, home_digest, wiring)

    def test_a_killed_upgrade_is_recovered_by_the_next_run(self) -> None:
        self._age(self.neutral)
        home_digest, wiring = content_digest(self.neutral), self._wiring()
        proc = self._start_detached(MEMORY_FORCE_UPGRADE_HANG="after-install")
        try:
            def installed() -> bool:
                backups = self._backups(self.home, ".silly-memory")
                return bool(backups) and self._state(backups[0]).get("phase") == "installed"

            self._wait_for(installed, "the upgrade to finish install.sh")
            before = content_digest(self.home)
            busy = self._upgrade("--rollback", "--confirm", SILLY_MEMORY_UPGRADE_LOCK_TIMEOUT="1")
            self.assertEqual(busy.returncode, 73, msg=busy.stderr[-3000:])
            self.assertEqual(content_digest(self.home), before)
        finally:
            self._kill(proc)

        # The lock died with its owner. The next run finds the journal first: it
        # recovers the old home, then upgrades it afresh.
        self._ok(self._upgrade(), "upgrade after a killed upgrade")
        first, second = sorted(self._backups(self.home, ".silly-memory"), key=self._number)
        self.assertEqual(self._state(first)["phase"], "recovered")
        self.assertEqual(self._state(second)["phase"], "complete")
        self.assertEqual(content_digest(second), home_digest)
        self.assertEqual(footprint(self.neutral), self.fresh)

        self._ok(self._upgrade("--rollback", "--confirm"), "rollback")
        self.assertEqual(self._state(second)["phase"], "recovered")
        self._assert_restored(second, home_digest, wiring)

    def test_old_backups_are_pruned_and_a_reinstall_changes_nothing(self) -> None:
        self._age(self.neutral)
        config = self.neutral / "config.json"
        config.write_text(json.dumps({**_json(config), "mcp": True}), encoding="utf-8")
        # Five older backups: with the new one that is six, so the oldest goes.
        for number in range(1, 6):
            (self.home / f".silly-memory.upgrade-backup-{number}-2025010{number}T000000Z" / "bin").mkdir(parents=True)
        self._ok(self._upgrade())
        backups = sorted(self._backups(self.home, ".silly-memory"), key=self._number)
        self.assertEqual([self._number(b) for b in backups], [2, 3, 4, 5, 6])
        self.assertEqual(self._state(backups[-1])["home"], str(self.neutral))
        self.assertEqual(footprint(self.neutral), self.fresh)
        self.assertIs(_json(config)["mcp"], True, "an upgrade keeps the MCP setting")

        wiring = self._wiring()
        self._install()
        self.assertEqual(footprint(self.neutral), self.fresh)
        self.assertEqual(self._wiring(), wiring)
        self.assertIs(_json(config)["mcp"], True)

    def test_custom_home_upgrades_and_rolls_back_in_place(self) -> None:
        custom = self.root / "custom"
        self._install(SILLY_MEMORY_HOME=str(custom))
        neutral_digest = content_digest(self.neutral)
        custom_digest = content_digest(custom)
        (custom / "VERSION").write_text(f"{OLD_VERSION}\n", encoding="utf-8")
        self._ok(self._upgrade(SILLY_MEMORY_HOME=str(custom)))
        (backup,) = self._backups(self.root, "custom")
        self.assertEqual(self._state(backup)["home"], str(custom))
        self.assertEqual(self._backups(self.home, ".silly-memory"), [])

        self._ok(self._upgrade("--rollback", "--confirm", SILLY_MEMORY_HOME=str(custom)), "rollback")
        self.assertTrue(list(self.root.glob("custom.discard-*")))
        self.assertEqual((custom / "VERSION").read_text().strip(), OLD_VERSION)
        (custom / "VERSION").write_text((PROJECT_ROOT / "VERSION").read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(content_digest(custom), custom_digest)
        self.assertEqual(content_digest(self.neutral), neutral_digest)

    def test_recovery_resumes_with_the_home_absent(self) -> None:
        proc = self._start_detached(MEMORY_FORCE_UPGRADE_FAILURE="after-install", MEMORY_FORCE_UPGRADE_HANG="recovery-after-aside")
        try:
            def aside() -> bool:
                backups = self._backups(self.home, ".silly-memory")
                return bool(backups) and self._state(backups[0]).get("recovery", {}).get("step") == "stage"

            self._wait_for(aside, "the recovery to move the failed home aside")
        finally:
            self._kill(proc)
        self.assertFalse(self.neutral.exists())

        self._ok(self._upgrade("--rollback", "--confirm"), "resumed recovery")
        (backup,) = self._backups(self.home, ".silly-memory")
        self.assertEqual(self._state(backup)["phase"], "recovered")
        self.assertEqual(content_digest(self.neutral), content_digest(backup))


@unittest.skipUnless(UPGRADE_SH.exists(), "upgrade.sh must be adjacent to the source tree")
class TestUpgradeRefusals(_UpgradeCase):
    """Inputs that must stop the upgrade before it changes anything."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._make_root("upgrade_refusal_")

    def setUp(self) -> None:
        for name in ("home", "projects"):
            shutil.rmtree(self.root / name, ignore_errors=True)
        self.home.mkdir()
        self.neutral = self.home / ".silly-memory"
        (self.neutral / "ws-alpha").mkdir(parents=True)
        (self.neutral / "VERSION").write_text(f"{OLD_VERSION}\n", encoding="utf-8")

    def _assert_refused(self, code: int, text: str, *args: str) -> None:
        before = (content_digest(self.home), sorted(self.home.rglob("*.upgrade-*")))
        result = self._upgrade(*args)
        self.assertEqual(result.returncode, code, msg=result.stderr[-3000:])
        self.assertIn(text, result.stderr)
        after = (content_digest(self.home), sorted(self.home.rglob("*.upgrade-*")))
        self.assertEqual(after, before)

    def _backup_with_state(self, number: int, **state: str) -> Path:
        backup = self.home / f".silly-memory.upgrade-backup-{number}-20260101T000000Z"
        shutil.copytree(self.neutral, backup)
        (backup / STATE_DIR).mkdir()
        (backup / STATE_DIR / "journal.jsonl").write_text("", encoding="utf-8")
        (backup / STATE_DIR / "state.json").write_text(json.dumps({"home": str(self.neutral), **state}), encoding="utf-8")
        return backup

    def test_malformed_shared_settings(self) -> None:
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "hooks.json").write_text("{not json", encoding="utf-8")
        self._assert_refused(3, "hooks.json is not valid JSON")

    def test_a_home_that_is_not_a_folder(self) -> None:
        shutil.rmtree(self.neutral)
        self.neutral.write_text("not a folder\n", encoding="utf-8")
        self._assert_refused(66, "is not an installation")

    def test_no_installation(self) -> None:
        shutil.rmtree(self.neutral)
        self._assert_refused(66, "run ./install.sh first")

    def test_several_unfinished_journals(self) -> None:
        for number in (1, 2):
            self._backup_with_state(number, digest="0", phase="installing")
        before = content_digest(self.home)
        result = self._upgrade()
        self.assertEqual(result.returncode, 3, msg=result.stderr[-3000:])
        self.assertIn("more than one unfinished upgrade", result.stderr)
        self.assertEqual(content_digest(self.home), before)

    def test_a_damaged_backup_is_not_restored(self) -> None:
        self._backup_with_state(1, digest="not-this-backup", phase="complete")
        self._assert_refused(3, "no longer matches its recorded digest", "--rollback", "--confirm")

    def test_the_transaction_refuses_to_run_without_the_upgrade_lock(self) -> None:
        before = content_digest(self.home)
        for spoofed in ({}, {"SILLY_MEMORY_UPGRADE_LOCK_FD": "0"}):
            with self.subTest(env=spoofed):
                result = self._run([sys.executable, str(UPGRADE_TX), "upgrade", "--repo", str(PROJECT_ROOT)], **spoofed)
                self.assertEqual(result.returncode, 2, msg=result.stderr)
                self.assertIn("run this through upgrade.sh", result.stderr)
        self.assertEqual(content_digest(self.home), before)

    def test_restored_wiring_is_checked_against_the_restored_engine(self) -> None:
        (self.home / ".zshrc").write_text(
            '# >>> silly-memory >>>\n[ -f "$HOME/.silly-memory/memory.zsh" ] && source "$HOME/.silly-memory/memory.zsh"\n'
            "# <<< silly-memory <<<\n",
            encoding="utf-8",
        )
        shim = self.home / ".cursor" / "hooks" / "memory-hook.sh"
        shim.parent.mkdir(parents=True)
        shim.write_text("#!/bin/bash\n", encoding="utf-8")
        code = (
            "import json, sys; sys.path.insert(0, sys.argv[1]); import upgrade_transaction as u;"
            "print(json.dumps(u.entry_point_problems({'home': sys.argv[2]})))"
        )
        result = self._run([sys.executable, "-c", code, str(UPGRADE_TX.parent), str(self.neutral)])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        problems = json.loads(result.stdout)
        self.assertTrue(any("memory.zsh" in p for p in problems), msg=problems)
        self.assertTrue(any(str(shim) in p and "bin/memory" in p for p in problems), msg=problems)

    def test_rollback_needs_confirm_and_a_backup_without_a_journal_is_refused(self) -> None:
        shutil.copytree(self.neutral, self.home / ".silly-memory.upgrade-backup-1-20260101T000000Z")
        unconfirmed = self._upgrade("--rollback")
        self.assertEqual(unconfirmed.returncode, 2)
        self.assertIn("[no journal]", unconfirmed.stderr)
        self.assertFalse((self.home / ".silly-memory-upgrade.lock").exists())
        self._assert_refused(3, "has no upgrade journal", "--rollback", "--confirm")


if __name__ == "__main__":
    unittest.main(verbosity=2)

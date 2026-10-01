"""``memory add``: the command-line twin of the MCP tool ``memory_add``.

The add-memory skill runs it when MCP is off, so it must behave like the tool.

Guards:
  - A fact is stored once, at score 1.0 with the ``explicit`` tag, in the
    project's store, and is recallable and in the project rule right away.
  - Run from a subfolder, ``add``, ``recall``, and ``tasks`` all use the
    project root (the nearest ``.git``), as the hooks and the MCP server do;
    the subfolder never gets a store of its own.
  - ``add -`` reads the fact from stdin, keeping quotes, ``$``, and backticks.
  - ``--scope`` routes like ``memory_add``: auto by category, or forced.
  - Empty or entirely private text stores nothing (exit 1); a busy store
    stores nothing (exit 75, retry).

Isolation: every test uses a throwaway memory home and project.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN", "MEMORY_EMBEDDING_BACKEND")


class CliAddTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_cli_add_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.home = self.tmp / "stores"
        self.home.mkdir()
        shutil.copy2(MEM_HOME / "config.json", self.home / "config.json")
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        self.subdir = self.project / "packages" / "api"
        self.subdir.mkdir(parents=True)

    def tearDown(self) -> None:
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args: str, stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        env.update(SILLY_MEMORY_HOME=str(self.home), MEMORY_EMBEDDING_BACKEND="noop", PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, str(CLI), *args], input=stdin, cwd=str(cwd or self.subdir),
            capture_output=True, text=True, env=env, timeout=120,
        )

    def store(self) -> Path:
        return self.home / (self.project / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip()

    def explicit_lines(self, bank: Path) -> list[tuple[str, dict]]:
        """Each line of ``bank`` with its score-sidecar entry."""
        sidecar = bank.with_name(f"{bank.name}.score.json")
        scores = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
        lines = bank.read_text(encoding="utf-8").splitlines()
        return [(line, scores.get(f"{bank.name}:{n}", {})) for n, line in enumerate(lines, start=1) if line.startswith("- ")]


class TestAdd(CliAddTestBase):
    def test_a_fact_is_stored_once_at_full_confidence_and_recallable_at_once(self) -> None:
        first = self.cli("add", "we", "deploy", "only", "from", "the", "zephyr", "branch")
        self.assertEqual(first.returncode, 0, first.stderr)
        bank = self.store() / "memory-bank" / "domainContext.md"
        self.assertEqual(first.stdout.strip(), f"saved to {bank} (the memory for {self.project})")
        again = self.cli("add", "We deploy only from the zephyr branch.", cwd=self.project)
        self.assertEqual(again.returncode, 0, again.stderr)

        (line, entry), = self.explicit_lines(bank)
        self.assertIn("#explicit", line)
        self.assertIn("we deploy only from the zephyr branch", line)
        self.assertEqual(entry.get("score"), 1.0)
        self.assertIn("explicit", entry.get("tags", []))
        rule = self.project / ".cursor" / "rules" / "_memory-context.mdc"
        self.assertIn("zephyr branch", rule.read_text(encoding="utf-8"), "the project rule is refreshed at once")

        other = self.project / "docs"
        other.mkdir()
        recall = self.cli("recall", "zephyr", cwd=other)
        self.assertEqual(recall.returncode, 0, recall.stderr)
        self.assertIn("domainContext.md", recall.stdout)
        self.assertIn("zephyr", recall.stdout)
        tasks = self.cli("tasks", cwd=other)
        self.assertIn(f"Action items — {self.project.name}", tasks.stdout)
        stores = [p.name for p in self.home.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))]
        self.assertEqual(stores, [self.store().name])
        for folder in (self.subdir, other):
            self.assertFalse((folder / ".silly-memory").exists(), f"{folder} got a store of its own")

    def test_stdin_keeps_quotes_dollars_and_backticks(self) -> None:
        text = """the "release" check runs `make verify` with $HOME/bin first\n"""
        proc = self.cli("add", "--json", "-", stdin=text)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["scope"], "workspace")
        self.assertEqual(result["workspace"], str(self.project))
        (line, _), = self.explicit_lines(Path(result["file"]))
        self.assertTrue(line.endswith(text.strip()), line)

    def test_scope_routes_like_memory_add(self) -> None:
        preference = self.cli("add", "--json", "I prefer short commit subjects")
        self.assertEqual(json.loads(preference.stdout)["scope"], "global", preference.stderr)
        forced = self.cli("add", "--json", "--scope", "workspace", "I prefer tabs in this repository")
        self.assertEqual(json.loads(forced.stdout)["scope"], "workspace", forced.stderr)
        everywhere = self.cli("add", "--scope", "global", "the shared VPN is vpn-7")
        self.assertEqual(everywhere.returncode, 0, everywhere.stderr)
        self.assertIn("(global memory)", everywhere.stdout)
        global_bank = self.home / "_global" / "memory-bank"
        stored = "".join(p.read_text(encoding="utf-8") for p in global_bank.glob("*.md"))
        self.assertIn("I prefer short commit subjects", stored)
        self.assertIn("the shared VPN is vpn-7", stored)
        self.assertNotIn("tabs", stored)
        bad = self.cli("add", "--scope", "everywhere", "x")
        self.assertEqual(bad.returncode, 2)

    def test_private_or_empty_text_stores_nothing(self) -> None:
        for args, stdin in ((("<private>the door code is 4711</private>",), None), (("-",), "  \n")):
            with self.subTest(args=args):
                proc = self.cli("add", *args, stdin=stdin)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("nothing stored", proc.stderr)
                self.assertEqual(proc.stdout, "")
        mixed = self.cli("add", "the office opens at nine <private>door code 4711</private>")
        self.assertEqual(mixed.returncode, 0, mixed.stderr)
        stored = "".join(p.read_text(encoding="utf-8") for p in self.home.rglob("memory-bank/*.md"))
        self.assertIn("the office opens at nine", stored)
        self.assertNotIn("4711", stored)

    def test_a_busy_store_exits_75_and_stores_nothing(self) -> None:
        from memory_system.paths import ensure_layout, lock_path, workspace_store
        from memory_system.safety import file_lock

        os.environ["SILLY_MEMORY_HOME"] = str(self.home)
        store = workspace_store(self.project)
        ensure_layout(store)
        with file_lock(lock_path(store), timeout=5):
            proc = self.cli("add", "the staging database is db-9")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn("busy", proc.stderr)
        self.assertFalse((store / "memory-bank" / "domainContext.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

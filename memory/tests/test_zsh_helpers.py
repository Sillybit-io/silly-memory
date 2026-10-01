"""The zsh helpers in ``memory.zsh`` must stay in sync with the CLI.

Guards against helpers that call subcommands the CLI never registered (e.g.
``memdelete`` calling ``mem delete`` while the parser only knew ``memdelete``),
tab completion offering dead subcommands, and ``memhelp`` documenting helpers
that do not exist.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MEM_HOME = Path(__file__).resolve().parents[1]
ZSH = MEM_HOME / "memory.zsh"
CLI = MEM_HOME / "bin" / "memory"

HELPER_CALL_RE = re.compile(r"(?:^\s*|\{\s*)mem\s+([a-z][a-z0-9-]*)")
FUNCTION_DEF_RE = re.compile(r"^([a-z][a-z0-9_-]*)\(\)", re.MULTILINE)


def _registered_subcommands() -> set[str]:
    """Subcommand names and aliases, read from the CLI's own usage line."""
    out = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    match = re.search(r"\{([a-z0-9,-]+)\}", out)
    assert match, f"no subcommand list in CLI usage:\n{out}"
    return set(match.group(1).split(","))


def _memhelp_text(src: str) -> str:
    match = re.search(r"memhelp\(\) \{\n  cat <<'EOF'\n(.*?)\nEOF", src, re.DOTALL)
    assert match, "memhelp heredoc not found"
    return match.group(1)


class TestZshHelpersMatchCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.src = ZSH.read_text(encoding="utf-8")
        cls.registered = _registered_subcommands()

    def test_every_helper_calls_a_registered_subcommand(self) -> None:
        """Helpers must not call CLI subcommands that argparse rejects."""
        help_text = _memhelp_text(self.src)
        code = self.src.replace(help_text, "")
        called = {
            m.group(1)
            for line in code.splitlines()
            if not line.lstrip().startswith("#")
            for m in HELPER_CALL_RE.finditer(line)
        }
        self.assertIn("status", called, "helper-call parsing found nothing; regex drifted")
        self.assertEqual(sorted(called - self.registered), [])

    def test_completion_lists_only_registered_subcommands(self) -> None:
        """Tab completion must not offer subcommands the CLI does not know."""
        match = re.search(r"_mem_complete\(\) \{\n(.*?)\n\s*\}", self.src, re.DOTALL)
        self.assertIsNotNone(match, "_mem_complete not found")
        assert match is not None
        offered = set(match.group(1).replace("\\", " ").split()) - {"compadd"}
        self.assertIn("status", offered, "completion parsing found nothing; regex drifted")
        self.assertEqual(sorted(offered - self.registered), [])

    def test_memhelp_documents_only_defined_helpers(self) -> None:
        """memhelp must not advertise helpers that memory.zsh never defines."""
        defined = set(FUNCTION_DEF_RE.findall(self.src))
        documented = set(re.findall(r"^\s+(mem[a-z-]+)\b", _memhelp_text(self.src), re.MULTILINE))
        self.assertIn("memstatus", documented, "memhelp parsing found nothing; regex drifted")
        self.assertEqual(sorted(documented - defined), [])

    def test_memhelp_uses_the_neutral_product_name(self) -> None:
        help_text = _memhelp_text(self.src)
        self.assertIn("silly-memory helpers", help_text)
        self.assertNotIn("Cursor Memory", help_text)


_RECORDING_STUB = """\
import json, os, sys
with open(os.environ["MEMTEST_RECORD"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
"""


@unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
class TestZshHelpersRunAgainstStubCli(unittest.TestCase):
    """Run the real helpers in ``zsh -f`` against a CLI stub that records its argv."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_zsh_"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.record = self.tmp / "argv.jsonl"
        self.stub = self.tmp / "memory-stub"
        self.stub.write_text(_RECORDING_STUB, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zsh(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {"HOME": str(self.tmp), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        return subprocess.run(
            ["zsh", "-f", "-c", f"source {ZSH}; {script}"],
            cwd=self.project,
            env={**base, **env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _calls(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def _run_helpers(self, script: str) -> subprocess.CompletedProcess[str]:
        return self._zsh(
            script,
            MEMORY_BIN=str(self.stub),
            MEMORY_PY=sys.executable,
            MEMTEST_RECORD=str(self.record),
        )

    def test_memrecall_joins_words_and_forwards_options(self) -> None:
        proc = self._run_helpers(
            "memrecall deploy target; memrecall --all deploy; memrecall --limit 3 zephyr stage --all"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ws = str(self.project.resolve())
        self.assertEqual(
            self._calls(),
            [
                ["recall", "deploy target", "--workspace", ws],
                ["recall", "deploy", "--workspace", ws, "--all"],
                ["recall", "zephyr stage", "--workspace", ws, "--limit", "3", "--all"],
            ],
        )

    def test_memrecall_without_query_words_is_a_usage_error(self) -> None:
        proc = self._run_helpers("memrecall --all")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage: memrecall", proc.stderr)
        self.assertEqual(self._calls(), [])

    def test_memadd_forwards_the_fact_and_scope(self) -> None:
        proc = self._run_helpers("memadd we deploy from main; memadd --scope global I prefer tabs; memadd")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage: memadd", proc.stderr)
        ws = str(self.project.resolve())
        self.assertEqual(
            self._calls(),
            [
                ["add", "--workspace", ws, "we", "deploy", "from", "main"],
                ["add", "--workspace", ws, "--scope", "global", "I", "prefer", "tabs"],
            ],
        )

    def test_memtasks_forwards_all(self) -> None:
        proc = self._run_helpers("memtasks --all --json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self._calls(), [["tasks", "--workspace", str(self.project.resolve()), "--all", "--json"]]
        )

    def test_memory_bin_follows_the_engine_home_precedence(self) -> None:
        show = "print -r -- $MEMORY_BIN"
        cases = [
            ({}, f"{self.tmp}/.silly-memory/bin/memory"),
            ({"SILLY_MEMORY_HOME": "/custom/home"}, "/custom/home/bin/memory"),
            ({"MEMORY_BIN": str(self.stub), "SILLY_MEMORY_HOME": "/custom/home"}, str(self.stub)),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                proc = self._zsh(show, **env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), expected)


if __name__ == "__main__":
    _ = unittest.main()

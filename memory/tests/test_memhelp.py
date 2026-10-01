"""Smoke-test: every new v2 command name appears in the memhelp heredoc."""
import unittest
from pathlib import Path

_MEMZSH = Path(__file__).resolve().parents[1] / "memory.zsh"

_NEW_COMMANDS = [
    "memadd",
    "memdelete",
    "memprune-review",
    "meminspect",
    "memwhy",
    "memlearn-status",
    "memprofile-sync",
    "memexport",
    "memimport",
    "memdoctor",
]


class TestMemhelpDocumentsNewCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.text: str = _MEMZSH.read_text()

    def test_new_v2_commands_appear_in_memhelp_heredoc(self) -> None:
        for cmd in _NEW_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, self.text, f"memhelp heredoc missing '{cmd}'")


if __name__ == "__main__":
    unittest.main()

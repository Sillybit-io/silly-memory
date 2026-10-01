"""`memory selftest` must load every test module from the INSTALLED layout.

install.sh copies ``bin/``, ``lib/`` and ``tests/`` into the memory home
without the repo's ``memory/__init__.py``. Discovery rooted at the tests dir
left ``memory.tests.perf_harness`` unimportable there, so five perf modules
errored in every user's ``memtest``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MEM_HOME = Path(__file__).resolve().parents[1]

_COUNT_IMPORT_FAILURES = """
import sys, unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

cli_path, tests_dir = sys.argv[1], Path(sys.argv[2])
loader = SourceFileLoader("memory_cli", cli_path)
spec = spec_from_loader("memory_cli", loader)
cli = module_from_spec(spec)
loader.exec_module(cli)

def flat(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flat(item)
        else:
            yield item

tests = list(flat(cli._selftest_suite(tests_dir)))
failed = [t.id() for t in tests if type(t).__name__ == "_FailedTest"]
print(len(tests))
print("\\n".join(failed))
"""


class TestSelftestInstalledLayout(unittest.TestCase):
    def test_installed_layout_imports_every_test_module(self) -> None:
        """Perf tests failed to import from the installed tests folder (no `memory` package there)."""
        for layout in (Path(".silly-memory"), Path("custom-home") / "memory"):
            with self.subTest(layout=str(layout)):
                self._check_layout(layout)

    def _check_layout(self, layout: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="selftest_layout_") as tmp:
            installed = Path(tmp) / layout
            ignore = shutil.ignore_patterns("__pycache__")
            for sub in ("bin", "lib", "tests"):
                shutil.copytree(MEM_HOME / sub, installed / sub, ignore=ignore)
            self.assertFalse((installed / "__init__.py").exists())

            env = {k: v for k, v in os.environ.items() if k not in ("SILLY_MEMORY_HOME")}
            env.update(
                {
                    "HOME": tmp,
                    "SILLY_MEMORY_HOME": str(Path(tmp) / "memory-home"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _COUNT_IMPORT_FAILURES,
                    str(installed / "bin" / "memory"),
                    str(installed / "tests"),
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            count, _, failed = result.stdout.partition("\n")
            self.assertGreater(int(count), 100, msg=result.stdout)
            self.assertEqual(failed.strip(), "", msg=f"modules failed to import:\n{failed}")


if __name__ == "__main__":
    _ = unittest.main()

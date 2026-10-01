from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path


FORBIDDEN_IMPORTS = {
    "openai",
    "anthropic",
    "transformers",
    "langchain",
    "urllib.request",
    "requests",
    "socket",
    "http.client",
}


@dataclass(frozen=True)
class ImportViolation:
    path: Path
    line: int
    import_name: str

    def format(self) -> str:
        return f"{self.path.as_posix()}:{self.line}:{self.import_name}"


class TestNoForbiddenImports(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path("memory/lib/memory_system")
        self.files: list[Path] = sorted(self.root.rglob("*.py"))

    def _scan_file(self, path: Path) -> list[ImportViolation]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        violations: list[ImportViolation] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if self._is_forbidden(alias.name):
                        violations.append(ImportViolation(path, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.module and self._is_forbidden(node.module):
                    violations.append(ImportViolation(path, node.lineno, node.module))

        return violations

    @staticmethod
    def _is_forbidden(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_IMPORTS
        )

    def test_no_forbidden_imports_in_memory_system(self) -> None:
        violations = []
        for path in self.files:
            violations.extend(self._scan_file(path))

        self.assertFalse(
            violations,
            "\n".join(v.format() for v in violations),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

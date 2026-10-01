# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportMissingParameterType=false
"""TDD: memory bundle IMPORT — round-trip, backup-first, confirm-gate, relink.

Guards T7 ``import_bundle()`` (paired with T6 ``create_bundle()``):

  - **round-trip** — export home A -> ``import_bundle`` into an EMPTY home B ->
    B's ``_global`` and every workspace store are byte-equal to A (transient
    SQLite journals / ``.lock`` files are the only intentional omissions).
  - **backup-first** — importing into a NON-empty home snapshots the store via
    ``backup.snapshot_store`` BEFORE extraction; a pre-existing sentinel file and
    a colliding ``config.json`` are recoverable from the ``.backup-*`` snapshot.
  - **confirm-gate** — ``import_bundle(confirm_token="")`` RAISES and writes
    NOTHING (store byte-unchanged, no snapshot), mirroring
    ``backup.restore_snapshot``'s ``YES-RESTORE`` guard.
  - **different-path relink** — ``map_workspace={P1: P2}`` re-plants
    ``P2/.silly-memory/memory-id`` = store id (so ``resolve_workspace_id(P2)`` is
    not orphaned) AND rewrites the store's ``.meta.json:workspace_root`` to P2
    (because ``paths.workspace_store`` only writes ``.meta.json`` when absent).
    A stale id in that marker is overwritten.

Runner: ``python3 -m unittest discover -s memory/tests -p 'test_import_bundle.py'``.
Isolation: every memory home and project root lives under a
``tempfile.TemporaryDirectory``; the library takes explicit ``memory_home``
paths, so the real ``~/.silly-memory`` is never referenced.
``ImportRealHomeGuardTest`` pins that contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.lifecycle.export_bundle import create_bundle, import_bundle

WS_ID = "a1b2c3d4e5f60718"  # a valid 16-hex workspace store id
_TRANSIENT = (".sqlite-wal", ".sqlite-shm", ".lock")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(root: Path) -> dict[str, str]:
    """``{relpath: sha256}`` for every non-transient, non-backup file under ``root``.

    Skips the transient journals/locks that ``create_bundle`` intentionally
    excludes, so a byte-for-byte A-vs-B comparison is apples-to-apples.
    """
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".backup-")]
        for fname in filenames:
            if any(fname.endswith(suffix) for suffix in _TRANSIENT):
                continue
            full = Path(dirpath) / fname
            out[full.relative_to(root).as_posix()] = _sha(full.read_bytes())
    return out


def _digest_all(root: Path) -> dict[str, str]:
    """``{relpath: sha256}`` for EVERY file under ``root`` (nothing skipped)."""
    out: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            full = Path(dirpath) / fname
            out[full.relative_to(root).as_posix()] = _sha(full.read_bytes())
    return out


def _seed_home(home: Path, ws_id: str, project_root: Path) -> None:
    """Hand-build a realistic memory home: config + ``_global`` + one workspace.

    The workspace carries a ``.meta.json`` whose ``workspace_root`` points at
    ``project_root`` (so ``create_bundle`` auto-derives the project marker) plus
    two transient artifacts that must never enter the bundle.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        '{"bank_files": ["learned-memories.md"]}\n', encoding="utf-8"
    )

    gstore = home / "_global"
    (gstore / "memory-bank").mkdir(parents=True, exist_ok=True)
    (gstore / "memory-bank" / "learned-memories.md").write_text(
        "# Global\n\n- [2026-07-09] #preference: import must be lossless.\n",
        encoding="utf-8",
    )
    (gstore / "events.jsonl").write_text('{"hook":"seed","n":1}\n', encoding="utf-8")
    (gstore / "observations.md").write_text(
        "# Observations\n\n- global obs\n", encoding="utf-8"
    )

    ws = home / ws_id
    (ws / "memory-bank").mkdir(parents=True, exist_ok=True)
    (ws / "memory-bank" / "domainContext.md").write_text(
        "# Domain\n\n- [2026-07-09] #decision: keep bytes identical on import.\n",
        encoding="utf-8",
    )
    (ws / ".meta.json").write_text(
        json.dumps(
            {"workspace_id": ws_id, "workspace_root": str(project_root.resolve())},
            indent=2,
        ),
        encoding="utf-8",
    )
    (ws / "events.jsonl").write_text('{"hook":"seed","n":2}\n', encoding="utf-8")
    (ws / "observations.md").write_text(
        "# Observations\n\n- ws obs\n", encoding="utf-8"
    )
    (ws / "work-state.md").write_text("# Work State\n\n- branch main\n", encoding="utf-8")
    (ws / "context-pack.md").write_text("# Context Pack\n\n- pack\n", encoding="utf-8")
    (ws / "memory.sqlite").write_bytes(b"SQLite format 3\x00payload-bytes")
    # transient artifacts — must NOT travel in the bundle:
    (ws / "memory.sqlite-wal").write_bytes(b"wal-junk-should-not-travel")
    (ws / ".lock").write_text("", encoding="utf-8")


class ImportRoundTripTest(unittest.TestCase):
    def test_roundtrip_bytes_equal_into_empty_home(self) -> None:
        """export A -> import into EMPTY B -> _global + workspace byte-equal A."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            project = base / "proj"
            project.mkdir()
            _seed_home(home_a, WS_ID, project)

            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            home_b = base / "homeB"  # does not exist yet -> truly empty target
            summary = import_bundle(bundle, home_b, "YES-IMPORT")

            self.assertEqual(
                _digest(home_a / "_global"),
                _digest(home_b / "_global"),
                "_global must be byte-equal after round-trip",
            )
            self.assertEqual(
                _digest(home_a / WS_ID),
                _digest(home_b / WS_ID),
                "workspace store must be byte-equal after round-trip",
            )
            self.assertEqual(
                (home_a / "config.json").read_bytes(),
                (home_b / "config.json").read_bytes(),
            )
            # transient journals/locks are the only intentional omissions
            self.assertFalse((home_b / WS_ID / "memory.sqlite-wal").exists())
            self.assertFalse((home_b / WS_ID / ".lock").exists())
            # empty target -> nothing to snapshot
            self.assertIsNone(summary["snapshot"])
            self.assertFalse(
                [p for p in home_b.iterdir() if p.name.startswith(".backup-")]
            )
            # the bundle's own manifest.json must not land in the memory home root
            self.assertEqual(
                sorted(p.name for p in home_b.iterdir()),
                sorted(["_global", WS_ID, "config.json"]),
            )
            markers = summary["project_markers"]
            assert isinstance(markers, dict)
            self.assertEqual(markers.get(str(project.resolve())), WS_ID)


class ImportBackupFirstTest(unittest.TestCase):
    def test_backup_taken_before_extraction(self) -> None:
        """import into a NON-empty home snapshots first; old state recoverable."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            project = base / "proj"
            project.mkdir()
            _seed_home(home_a, WS_ID, project)
            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            # NON-empty target: a sentinel + a colliding config.json
            home_b = base / "homeB"
            home_b.mkdir()
            (home_b / "leftover-sentinel.txt").write_text("KEEP-ME-42", encoding="utf-8")
            (home_b / "config.json").write_text("OLD-CONFIG-BYTES", encoding="utf-8")

            summary = import_bundle(bundle, home_b, "YES-IMPORT")

            snaps = [
                p
                for p in home_b.iterdir()
                if p.is_dir()
                and p.name.startswith(".backup-")
                and not p.name.endswith(".partial")
            ]
            self.assertEqual(
                len(snaps), 1, f"expected one .backup-* dir, got {[s.name for s in snaps]}"
            )
            snap = snaps[0]
            # pre-existing state recoverable from the snapshot
            self.assertEqual(
                (snap / "leftover-sentinel.txt").read_text(encoding="utf-8"), "KEEP-ME-42"
            )
            self.assertEqual(
                (snap / "config.json").read_text(encoding="utf-8"), "OLD-CONFIG-BYTES"
            )
            # live store now carries the imported config, not the old bytes
            self.assertEqual(
                (home_b / "config.json").read_bytes(),
                (home_a / "config.json").read_bytes(),
            )
            self.assertEqual(summary["snapshot"], str(snap))


class ImportConfirmGateTest(unittest.TestCase):
    def test_missing_token_raises_and_writes_nothing(self) -> None:
        """import_bundle(confirm_token="") RAISES; store byte-unchanged, no snapshot."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            project = base / "proj"
            project.mkdir()
            _seed_home(home_a, WS_ID, project)
            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            home_b = base / "homeB"
            home_b.mkdir()
            (home_b / "sentinel.txt").write_text("UNTOUCHED", encoding="utf-8")
            before = _digest_all(home_b)

            with self.assertRaises(ValueError):
                import_bundle(bundle, home_b, "")

            self.assertEqual(
                before,
                _digest_all(home_b),
                "store must be byte-unchanged when the confirm token is refused",
            )
            self.assertFalse(
                (home_b / "_global").exists(), "no data extracted without the token"
            )
            self.assertFalse(
                [p for p in home_b.iterdir() if p.name.startswith(".backup-")],
                "no snapshot taken when the token is refused",
            )


class ImportRelinkTest(unittest.TestCase):
    def test_map_workspace_relinks_to_new_path(self) -> None:
        """--map-workspace P1=P2 -> resolve_workspace_id(P2)==id AND meta root==P2."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            p1 = base / "proj-old"
            p1.mkdir()
            _seed_home(home_a, WS_ID, p1)
            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            home_b = base / "homeB"
            p2 = base / "proj-new"
            p2.mkdir()

            summary = import_bundle(
                bundle,
                home_b,
                "YES-IMPORT",
                map_workspace={str(p1.resolve()): str(p2)},
            )

            from memory_system.paths import resolve_workspace_id

            # the moved project resolves to the imported store id (not orphaned)
            self.assertEqual(resolve_workspace_id(p2), WS_ID)
            self.assertEqual(
                (p2 / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip(), WS_ID
            )
            self.assertEqual(
                (p2 / ".silly-memory" / ".gitignore").read_text(encoding="utf-8").splitlines()[-1], "*"
            )
            self.assertFalse((p2 / ".cursor").exists(), "import writes only the silly-memory marker")
            # the store's .meta.json:workspace_root now reads the NEW path
            meta = json.loads((home_b / WS_ID / ".meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["workspace_root"], str(p2.resolve()))
            self.assertEqual(meta["workspace_id"], WS_ID)
            mapped = summary["mapped"]
            assert isinstance(mapped, dict)
            self.assertEqual(mapped.get(str(p2.resolve())), WS_ID)

    def test_mapped_import_overrides_a_stale_id(self) -> None:
        """An auto-planted id must not shadow the explicitly mapped store."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            p1 = base / "proj-old"
            p1.mkdir()
            _seed_home(home_a, WS_ID, p1)
            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            p2 = base / "proj-new"
            (p2 / ".silly-memory").mkdir(parents=True)
            (p2 / ".silly-memory" / "memory-id").write_text("0000stale0000000\n", encoding="utf-8")

            import_bundle(
                bundle, base / "homeB", "YES-IMPORT", map_workspace={str(p1.resolve()): str(p2)}
            )

            from memory_system.paths import resolve_workspace_id

            self.assertEqual(resolve_workspace_id(p2), WS_ID)

    def test_relink_in_place_writes_neutral_marker(self) -> None:
        """--relink plants the neutral marker for a same-path project on this machine."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            project = base / "proj"
            project.mkdir()
            _seed_home(home_a, WS_ID, project)
            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            summary = import_bundle(bundle, base / "homeB", "YES-IMPORT", relink=True)

            relinked = summary["relinked"]
            assert isinstance(relinked, dict)
            self.assertEqual(relinked.get(str(project.resolve())), WS_ID)
            self.assertEqual(
                (project / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip(), WS_ID
            )

    def test_bundle_names_its_schema(self) -> None:
        """The import summary reports the bundle's schema id."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            project = base / "proj"
            project.mkdir()
            _seed_home(home_a, WS_ID, project)
            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            summary = import_bundle(bundle, base / "homeB", "YES-IMPORT")

            self.assertEqual(summary["schema"], "silly-memory-export/v1")
            self.assertEqual(summary["workspaces"], [WS_ID])


class ImportTamperTest(unittest.TestCase):
    def test_sha_mismatch_refuses_before_writing(self) -> None:
        """A tampered bundle member fails sha256 verification before any write."""
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            base = Path(td)
            home_a = base / "homeA"
            project = base / "proj"
            project.mkdir()
            _seed_home(home_a, WS_ID, project)
            bundle = base / "bundle.tar.gz"
            create_bundle(home_a, bundle)

            # Rewrite one member's bytes without updating manifest sha256s.
            import io
            import tarfile

            tampered = base / "tampered.tar.gz"
            with tarfile.open(bundle, "r:gz") as src, tarfile.open(tampered, "w:gz") as dst:
                for member in src.getmembers():
                    handle = src.extractfile(member)
                    payload = b"" if handle is None else handle.read()
                    if member.name == "config.json":
                        payload = payload + b"TAMPER"
                        member.size = len(payload)
                    dst.addfile(member, io.BytesIO(payload))

            home_b = base / "homeB"
            with self.assertRaises(Exception):
                import_bundle(tampered, home_b, "YES-IMPORT")
            # nothing extracted from a bundle that fails verification
            self.assertFalse((home_b / "_global").exists())
            self.assertFalse((home_b / "config.json").exists())


class ImportRealHomeGuardTest(unittest.TestCase):
    def test_isolation_paths_never_touch_real_store(self) -> None:
        """Every path this suite uses stays under tempdir and outside the real store."""
        real_stores = [(Path.home() / ".silly-memory").resolve()]
        with tempfile.TemporaryDirectory(prefix="memimport_") as td:
            tmp_root = Path(tempfile.gettempdir()).resolve()
            for name in ("homeA", "homeB", "proj-old", "proj-new"):
                p = (Path(td) / name).resolve()
                self.assertTrue(
                    p.is_relative_to(tmp_root), f"{p} escaped tempdir {tmp_root}"
                )
                for real in real_stores:
                    self.assertFalse(
                        p.is_relative_to(real), f"{p} points inside the real store {real}"
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)

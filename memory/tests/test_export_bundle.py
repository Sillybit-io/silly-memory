"""RED→GREEN coverage for the ``memory export`` bundle writer.

Guards (bugs this pins):
  - A share/export bundle that silently omits a workspace store, ``_global``,
    or ``config.json`` → the recipient loses data.
  - ``project_markers`` left empty when the user does NOT pass ``--project``
    → the importer cannot relink stores to their projects (the whole point of
    T6 auto-population from each store's ``.meta.json:workspace_root``).
  - Transient SQLite journals (``*.sqlite-wal`` / ``*.sqlite-shm``) or a live
    ``.lock`` leaking into the tarball → corrupt/locked state on import.
  - A crafted store symlink escaping ``memory_home`` pulling an off-disk
    secret into the bundle (must never follow symlinks out of home).
  - A crashed write leaving a ``<out>.partial`` behind or a half-written
    tarball at the final path (writes must be atomic).

Isolation: every test roots ``SILLY_MEMORY_HOME`` in a throwaway tempdir and
``create_bundle`` is handed that home explicitly, so the real
``~/.silly-memory`` store is never read or written. ``TestRealHomeGuard``
asserts the isolation itself so a fixture regression is caught here, not by a
confused user days later.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_WS1 = "a1b2c3d4e5f60718"
_WS2 = "0011223344556677"
_ROOT1 = "/Users/test/projA"
_ROOT2 = "/Users/test/projB"


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


class ExportBundleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="export_bundle_")
        root = Path(self._tmp.name)
        self.home = root / "memory-home"
        self.home.mkdir(parents=True)
        # Keep the bundle output OUTSIDE memory_home so the walk never tries to
        # archive a partially-written tarball into itself.
        self.out_dir = root / "out"
        self.out_dir.mkdir(parents=True)
        self._saved_env = {k: os.environ.pop(k, None) for k in ("SILLY_MEMORY_HOME")}
        os.environ["SILLY_MEMORY_HOME"] = str(self.home)

    def tearDown(self) -> None:
        os.environ.pop("SILLY_MEMORY_HOME", None)
        os.environ.update({k: v for k, v in self._saved_env.items() if v is not None})
        self._tmp.cleanup()

    # --- seeding helpers ---
    def _seed_config(self) -> None:
        (self.home / "config.json").write_text(
            json.dumps({"memory_home": "~/.silly-memory", "context_pack_token_cap": 16000}, indent=2),
            encoding="utf-8",
        )

    def _seed_name_normalization(self) -> None:
        (self.home / "name-normalization.md").write_text(
            "# Name normalization\n\n- ACME -> Acme Corp\n", encoding="utf-8"
        )

    def _seed_global(self) -> None:
        gstore = self.home / "_global"
        (gstore / "memory-bank").mkdir(parents=True)
        (gstore / "memory-bank" / "learned-memories.md").write_text(
            "# Learned\n\n- global fact\n", encoding="utf-8"
        )
        (gstore / "events.jsonl").write_text('{"e":"x"}\n', encoding="utf-8")
        # Transient files that MUST be skipped by the bundler.
        (gstore / "memory.sqlite-wal").write_text("WALJUNK", encoding="utf-8")
        (gstore / "memory.sqlite-shm").write_text("SHMJUNK", encoding="utf-8")
        (gstore / ".lock").write_text("", encoding="utf-8")

    def _seed_workspace(self, ws_id: str, workspace_root: str) -> Path:
        store = self.home / ws_id
        (store / "memory-bank").mkdir(parents=True)
        (store / ".meta.json").write_text(
            json.dumps({"workspace_id": ws_id, "workspace_root": workspace_root}, indent=2),
            encoding="utf-8",
        )
        (store / "observations.md").write_text(
            f"# Observations {ws_id}\n\n- obs one\n", encoding="utf-8"
        )
        (store / "memory-bank" / "domainContext.md").write_text(
            "# Domain\n\n- [2026-07-09] #decision: ship it.\n", encoding="utf-8"
        )
        # Transient files that MUST be skipped.
        (store / ".lock").write_text("", encoding="utf-8")
        (store / "memory.sqlite-wal").write_text("WAL", encoding="utf-8")
        return store

    def _extract_members(self, bundle: Path) -> dict[str, bytes]:
        members: dict[str, bytes] = {}
        with tarfile.open(bundle, "r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    handle = tf.extractfile(m)
                    self.assertIsNotNone(handle, f"could not read {m.name}")
                    assert handle is not None
                    members[m.name] = handle.read()
        return members


class TestCreateBundle(ExportBundleTestBase):
    def test_bundle_lists_all_stores_manifest_and_matching_sha256(self) -> None:
        """Full happy path: both stores + _global + config are archived; the
        manifest lists them, version == repo VERSION, and every sha256 matches bytes."""
        from memory_system.lifecycle.export_bundle import create_bundle

        self._seed_config()
        self._seed_name_normalization()
        self._seed_global()
        self._seed_workspace(_WS1, _ROOT1)
        self._seed_workspace(_WS2, _ROOT2)

        out = self.out_dir / "bundle.tar.gz"
        result = create_bundle(self.home, out)
        self.assertEqual(result, out)
        self.assertTrue(out.is_file())
        self.assertFalse(Path(str(out) + ".partial").exists(), "leftover .partial")

        members = self._extract_members(out)
        self.assertIn("manifest.json", members)
        self.assertIn("config.json", members)
        self.assertIn("name-normalization.md", members)

        manifest = json.loads(members["manifest.json"])

        # Workspaces: both 16-hex ids listed.
        self.assertIn(_WS1, manifest["workspaces"])
        self.assertIn(_WS2, manifest["workspaces"])

        # _global present (explicit field AND file prefix).
        self.assertEqual(manifest["global"], "_global")
        self.assertTrue(any(f.startswith("_global/") for f in manifest["files"]))

        # config.json + name-normalization.md listed as files.
        self.assertIn("config.json", manifest["files"])
        self.assertIn("name-normalization.md", manifest["files"])

        # Version comes from the root VERSION file.
        repo_version = (MEM_LIB.parents[1] / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(manifest["version"], repo_version)

        # Metadata fields present.
        for key in ("schema", "created_at", "source_host", "project_markers", "sha256s"):
            self.assertIn(key, manifest)

        # project_markers non-empty and maps BOTH workspace_root -> store_id
        # even though no --project / project_markers argument was passed.
        self.assertEqual(manifest["project_markers"].get(_ROOT1), _WS1)
        self.assertEqual(manifest["project_markers"].get(_ROOT2), _WS2)

        # Every sha256 matches the archived bytes...
        self.assertTrue(manifest["sha256s"])
        for name, digest in manifest["sha256s"].items():
            self.assertIn(name, members, f"{name} in sha256s but absent from tar")
            self.assertEqual(_sha256_bytes(members[name]), digest, f"sha mismatch: {name}")

        # ...and every archived data file (not manifest) has a sha entry.
        for name in members:
            if name == "manifest.json":
                continue
            self.assertIn(name, manifest["sha256s"], f"{name} archived but not in sha256s")

        # No transient files leaked into the archive.
        for name in members:
            self.assertFalse(name.endswith((".sqlite-wal", ".sqlite-shm")), name)
            self.assertNotEqual(Path(name).name, ".lock", name)

    def test_skips_transient_wal_shm_lock(self) -> None:
        """*.sqlite-wal / *.sqlite-shm / *.lock never enter the tarball."""
        from memory_system.lifecycle.export_bundle import create_bundle

        self._seed_config()
        self._seed_global()
        self._seed_workspace(_WS1, _ROOT1)

        out = self.out_dir / "b.tgz"
        create_bundle(self.home, out)
        members = self._extract_members(out)
        self.assertTrue(members, "bundle unexpectedly empty")
        for name in members:
            self.assertFalse(name.endswith(".sqlite-wal"), name)
            self.assertFalse(name.endswith(".sqlite-shm"), name)
            self.assertFalse(name.endswith(".lock"), name)
            self.assertNotEqual(Path(name).name, ".lock", name)

    def test_project_markers_auto_populated_without_argument(self) -> None:
        """The whole T6 point: markers are derived from .meta.json, not the CLI."""
        from memory_system.lifecycle.export_bundle import create_bundle

        self._seed_config()
        self._seed_workspace(_WS1, _ROOT1)
        self._seed_workspace(_WS2, _ROOT2)

        out = self.out_dir / "b.tgz"
        create_bundle(self.home, out)  # NO project_markers argument
        manifest = json.loads(self._extract_members(out)["manifest.json"])
        self.assertEqual(
            manifest["project_markers"],
            {_ROOT1: _WS1, _ROOT2: _WS2},
        )

    def test_explicit_project_markers_merge_over_auto(self) -> None:
        """Caller-supplied markers augment/override the auto-derived map."""
        from memory_system.lifecycle.export_bundle import create_bundle

        self._seed_config()
        self._seed_workspace(_WS1, _ROOT1)

        out = self.out_dir / "b.tgz"
        create_bundle(self.home, out, project_markers={"/Users/test/extra": _WS1})
        manifest = json.loads(self._extract_members(out)["manifest.json"])
        self.assertEqual(manifest["project_markers"].get(_ROOT1), _WS1)
        self.assertEqual(manifest["project_markers"].get("/Users/test/extra"), _WS1)

    def test_does_not_follow_symlink_escaping_home(self) -> None:
        """A store symlink to an off-home secret must not pull it into the tar."""
        from memory_system.lifecycle.export_bundle import create_bundle

        self._seed_config()
        self._seed_workspace(_WS1, _ROOT1)

        secret_dir = Path(self._tmp.name) / "outside"
        secret_dir.mkdir()
        secret = secret_dir / "secret.txt"
        secret.write_text("TOPSECRET-OUTSIDE-HOME", encoding="utf-8")
        os.symlink(secret, self.home / _WS1 / "leak.txt")

        out = self.out_dir / "b.tgz"
        create_bundle(self.home, out)
        members = self._extract_members(out)
        for name, data in members.items():
            self.assertNotIn(b"TOPSECRET-OUTSIDE-HOME", data, f"secret leaked via {name}")
        self.assertNotIn(f"{_WS1}/leak.txt", members)

    def test_atomic_no_partial_left_on_success(self) -> None:
        """A successful export leaves the final path and NO .partial sibling."""
        from memory_system.lifecycle.export_bundle import create_bundle

        self._seed_config()
        self._seed_workspace(_WS1, _ROOT1)

        out = self.out_dir / "b.tgz"
        create_bundle(self.home, out)
        self.assertTrue(out.is_file())
        self.assertFalse(Path(str(out) + ".partial").exists())

    def test_nothing_to_export_raises(self) -> None:
        """An empty home yields NothingToExportError and writes no file."""
        from memory_system.lifecycle.export_bundle import NothingToExportError, create_bundle

        out = self.out_dir / "b.tgz"
        with self.assertRaises(NothingToExportError):
            create_bundle(self.home, out)
        self.assertFalse(out.exists())
        self.assertFalse(Path(str(out) + ".partial").exists())


class TestRealHomeGuard(ExportBundleTestBase):
    def test_env_points_into_tempdir_not_real_home(self) -> None:
        """Fixture guard: SILLY_MEMORY_HOME must live in a tempdir, never the
        real store."""
        env_home = os.environ.get("SILLY_MEMORY_HOME", "")
        self.assertTrue(env_home, "SILLY_MEMORY_HOME must be set by setUp")
        tmp_root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(
            Path(env_home).resolve().is_relative_to(tmp_root),
            f"SILLY_MEMORY_HOME ({env_home}) escaped the tempdir root ({tmp_root})",
        )
        for real_home in ((Path.home() / ".silly-memory").resolve(),):
            self.assertFalse(
                Path(env_home).resolve().is_relative_to(real_home),
                f"SILLY_MEMORY_HOME ({env_home}) points inside the real store ({real_home})",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

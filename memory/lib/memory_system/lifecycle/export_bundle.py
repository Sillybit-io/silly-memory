"""Lifecycle export bundle — a portable, self-contained ``.tar.gz`` of a store.

Read this first if you are new to the codebase:
  - ``create_bundle(memory_home, out_path, project_markers=None)`` writes a
    gzip tarball of DATA ONLY: ``config.json``, the ``_global/`` store, every
    16-hex ``<workspace-id>/`` store, an optional root
    ``name-normalization.md``, plus a generated top-level ``manifest.json``.
    Engine code (``bin/`` ``lib/`` ``tests/`` ``hooks/``) and the reinstallable
    ``_embeddings/`` weight cache are never bundled — the recipient reinstalls
    those from the packaged installer.
  - ``manifest.json`` records ``project_markers`` = ``{workspace_root:
    store_id}``, auto-derived from each store's ``.meta.json`` (``paths.py``
    writes ``workspace_root`` there when a store is first created). This lets a
    future importer relink each store to its project without the user
    re-declaring paths, so ``memory export`` needs no ``--project`` flag.
  - Transient SQLite journals (``*.sqlite-wal`` / ``*.sqlite-shm``) and
    ``*.lock`` files are skipped — the same exclusion intent as
    ``lifecycle.backup._IGNORED_PATTERNS`` — so a snapshot never carries a
    half-written journal or a live lock into someone else's install.
  - Symlinks are never followed or archived. A crafted store that links to a
    secret elsewhere on disk cannot pull that content into the bundle
    (defense: never escape ``memory_home``).
  - Writes are atomic: the tarball is built at ``<out>.partial``, fsynced,
    then ``os.replace``-renamed into place; a crash leaves only the partial,
    which the next run overwrites.

``import_bundle(bundle, memory_home, confirm_token, *, relink=False,
    map_workspace=None)`` is the inverse: it verifies every member against the
    manifest's sha256s BEFORE touching disk, snapshots any existing store via
    ``lifecycle.backup.snapshot_store`` (so an import never silently overwrites
    live data), extracts ``config.json`` / ``_global/`` / every workspace store
    into ``memory_home``, then optionally relinks each project's
    ``<root>/.silly-memory/memory-id`` marker. It mirrors
    ``backup.restore_snapshot``'s ``YES-RESTORE`` guard: a wrong
    ``confirm_token`` raises before any write.

Public interface (imported elsewhere): ``create_bundle``,
    ``NothingToExportError``, ``import_bundle``, ``BundleError``,
    ``BundleVerificationError``.
Depends on: stdlib only (tarfile, hashlib, json, platform, datetime, os, io,
    re, shutil, pathlib) plus lazy ``lifecycle.backup`` and ``paths`` imports
    inside ``import_bundle``. Imports no network module — enforced by
    ``memory/tests/test_no_forbidden_imports.py``.
Used by: the ``memory export`` / ``memory import`` CLI handlers
    (``bin/memory:cmd_export`` / ``cmd_import``), which lazy-import this module
    inside the handler to keep cold start cheap.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import io
import json
import os
import platform
import re
import shutil
import tarfile
from pathlib import Path
from typing import IO

GLOBAL_ID = "_global"
SCHEMA = "silly-memory-export/v1"
_WORKSPACE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_ROOT_EXTRA_FILES = ("name-normalization.md",)
_SKIP_SUFFIXES = (".sqlite-wal", ".sqlite-shm", ".lock")


class NothingToExportError(RuntimeError):
    """Raised when ``memory_home`` holds no config, ``_global``, or workspace data."""


def _read_version(memory_home: Path) -> str:
    """Resolve the store version string.

    Prefers the installed ``<memory_home>/VERSION`` marker (production truth),
    then walks the packaged tree for the root ``VERSION`` file so a repo
    checkout resolves to the same value. Falls back to a sentinel — never
    raises — because an export must still succeed on a store missing the
    marker.
    """
    candidates = [memory_home / "VERSION"]
    candidates.extend(parent / "VERSION" for parent in Path(__file__).resolve().parents)
    for candidate in candidates:
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return "0.0.0+unknown"


def _source_host() -> str:
    # platform.node() reads the nodename via os.uname() on POSIX — no socket
    # import, so the zero-network guardrail (test_no_forbidden_imports) holds.
    try:
        host = platform.node()
    except Exception:
        host = ""
    return host or "unknown"


def _created_at() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_transient(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _SKIP_SUFFIXES)


def _within(path: Path, root: Path) -> bool:
    """True iff ``path``'s real target stays under ``root`` (symlink escape guard)."""
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        return False
    return real == root or real.is_relative_to(root)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_store_files(store: Path, home_real: Path) -> list[Path]:
    """Regular files under ``store`` (recursive, deterministic order).

    Skips transient journals/locks and any symlink — dirs or files — so nothing
    is followed out of ``memory_home``.
    """
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(store, followlinks=False):
        # Prune symlinked subdirectories; keep the walk deterministic.
        dirnames[:] = sorted(
            d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
        )
        for fname in sorted(filenames):
            full = Path(dirpath) / fname
            if os.path.islink(full):
                continue
            if _is_transient(fname):
                continue
            if not full.is_file():
                continue
            if not _within(full, home_real):
                continue
            collected.append(full)
    return collected


def _workspace_ids(memory_home: Path) -> list[str]:
    """Sorted 16-hex workspace store directory names under ``memory_home``."""
    ids: list[str] = []
    for child in sorted(memory_home.iterdir()):
        if child.is_dir() and not os.path.islink(child) and _WORKSPACE_ID_RE.match(child.name):
            ids.append(child.name)
    return ids


def _collect_members(memory_home: Path, home_real: Path, ws_ids: list[str]) -> list[tuple[str, Path]]:
    """Return ``(arcname, source_path)`` pairs for every data file to archive."""
    members: list[tuple[str, Path]] = []

    config = memory_home / "config.json"
    if config.is_file() and not os.path.islink(config):
        members.append(("config.json", config))

    for extra in _ROOT_EXTRA_FILES:
        candidate = memory_home / extra
        if candidate.is_file() and not os.path.islink(candidate):
            members.append((extra, candidate))

    gstore = memory_home / GLOBAL_ID
    if gstore.is_dir() and not os.path.islink(gstore):
        for f in _iter_store_files(gstore, home_real):
            members.append((f.relative_to(memory_home).as_posix(), f))

    for ws_id in ws_ids:
        for f in _iter_store_files(memory_home / ws_id, home_real):
            members.append((f.relative_to(memory_home).as_posix(), f))

    return members


def _auto_project_markers(memory_home: Path, ws_ids: list[str]) -> dict[str, str]:
    """Map ``workspace_root -> store_id`` from each store's ``.meta.json``.

    ``paths.workspace_store`` writes ``{"workspace_id", "workspace_root"}`` into
    ``<store>/.meta.json`` on first use, so this recovers the project path for
    every store with zero user input. Stores with a missing/unreadable meta or
    no ``workspace_root`` are simply omitted from the mapping.
    """
    markers: dict[str, str] = {}
    for ws_id in ws_ids:
        meta = memory_home / ws_id / ".meta.json"
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            root = data.get("workspace_root")
            if isinstance(root, str) and root:
                markers[root] = ws_id
    return markers


def _build_manifest(
    memory_home: Path,
    ws_ids: list[str],
    members: list[tuple[str, Path]],
    markers: dict[str, str],
) -> bytes:
    files: list[str] = []
    sha256s: dict[str, str] = {}
    for arcname, source in members:
        files.append(arcname)
        sha256s[arcname] = _sha256_file(source)

    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "version": _read_version(memory_home),
        "created_at": _created_at(),
        "source_host": _source_host(),
        "workspaces": ws_ids,
        "global": GLOBAL_ID if (memory_home / GLOBAL_ID).is_dir() else None,
        "files": files,
        "sha256s": sha256s,
        "project_markers": markers,
    }
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def create_bundle(
    memory_home: Path,
    out_path: Path,
    project_markers: dict[str, str] | None = None,
) -> Path:
    """Write a ``.tar.gz`` data bundle of ``memory_home`` to ``out_path``.

    The archive holds ``config.json``, ``_global/``, every 16-hex workspace
    store, an optional root ``name-normalization.md``, and a top-level
    ``manifest.json``. ``project_markers`` is auto-populated from every store's
    ``.meta.json:workspace_root``; a caller-supplied mapping is merged on top
    (it augments/overrides the auto-derived entries).

    Raises ``NothingToExportError`` when there is no data to bundle. On success
    returns the final ``out_path`` (the ``<out>.partial`` staging file is gone).
    """
    memory_home = Path(memory_home)
    out_path = Path(out_path)

    if not memory_home.exists() or not memory_home.is_dir():
        raise NothingToExportError(f"no memory store at {memory_home}")

    home_real = Path(os.path.realpath(memory_home))
    ws_ids = _workspace_ids(memory_home)
    members = _collect_members(memory_home, home_real, ws_ids)
    if not members:
        raise NothingToExportError(
            f"nothing to export: {memory_home} has no config.json, {GLOBAL_ID}/, or workspace store"
        )

    markers = _auto_project_markers(memory_home, ws_ids)
    if project_markers:
        markers.update({str(k): str(v) for k, v in project_markers.items()})

    manifest_bytes = _build_manifest(memory_home, ws_ids, members, markers)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(f"{out_path}.partial")
    if partial.exists():
        partial.unlink()

    try:
        with tarfile.open(partial, "w:gz") as tar:
            for arcname, source in members:
                tar.add(str(source), arcname=arcname, recursive=False)
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            info.mtime = int(_datetime.datetime.now(_datetime.timezone.utc).timestamp())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(manifest_bytes))
        fd = os.open(partial, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(partial, out_path)
    except BaseException:
        if partial.exists():
            partial.unlink()
        raise

    return out_path


IMPORT_CONFIRM_TOKEN = "YES-IMPORT"
_META_FILENAME = ".meta.json"


class BundleError(RuntimeError):
    """Raised when a bundle is missing its manifest or is structurally invalid."""


class BundleVerificationError(RuntimeError):
    """Raised when a bundle member's sha256 does not match its manifest entry."""


def _safe_arcname(arcname: str) -> bool:
    """Reject absolute paths and parent-dir escapes before extraction."""
    if not arcname or arcname.startswith("/"):
        return False
    parts = Path(arcname).parts
    if not parts or ".." in parts:
        return False
    return not any(part.startswith("/") for part in parts)


def _hash_stream(handle: IO[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: handle.read(65536), b""):
        h.update(chunk)
    return h.hexdigest()


def _load_manifest(tar: tarfile.TarFile) -> dict[str, object]:
    try:
        member = tar.getmember("manifest.json")
    except KeyError:
        raise BundleError("bundle is missing manifest.json")
    handle = tar.extractfile(member)
    if handle is None:
        raise BundleError("bundle manifest.json is not a regular file")
    try:
        data = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"bundle manifest.json is unreadable: {exc}")
    if not isinstance(data, dict):
        raise BundleError("bundle manifest.json is not a JSON object")
    return data


def _verify_members(tar: tarfile.TarFile, manifest: dict[str, object]) -> list[str]:
    """Hash every declared member and compare against the manifest — no writes.

    Raises before the caller extracts anything, so a corrupt or tampered bundle
    never lands a single byte in the store. Returns the validated arcname list.
    """
    files = manifest.get("files")
    sha256s = manifest.get("sha256s")
    if not isinstance(files, list) or not isinstance(sha256s, dict):
        raise BundleError("bundle manifest.json missing files/sha256s")
    validated: list[str] = []
    for arcname in files:
        if not isinstance(arcname, str) or not _safe_arcname(arcname):
            raise BundleError(f"bundle contains an unsafe member path: {arcname!r}")
        expected = sha256s.get(arcname)
        if not isinstance(expected, str) or not expected:
            raise BundleVerificationError(f"manifest records no sha256 for {arcname}")
        try:
            member = tar.getmember(arcname)
        except KeyError:
            raise BundleVerificationError(f"bundle is missing declared member {arcname}")
        handle = tar.extractfile(member)
        if handle is None:
            raise BundleVerificationError(f"bundle member {arcname} is not a regular file")
        actual = _hash_stream(handle)
        if actual != expected:
            raise BundleVerificationError(
                f"sha256 mismatch for {arcname}: manifest {expected} != bundle {actual}"
            )
        validated.append(arcname)
    return validated


def _has_store_content(memory_home: Path) -> bool:
    """True iff ``memory_home`` holds anything worth snapshotting (ignoring backups)."""
    if not memory_home.is_dir():
        return False
    for child in memory_home.iterdir():
        if child.name.startswith(".backup-"):
            continue
        return True
    return False


def _extract_members(
    tar: tarfile.TarFile, files: list[str], memory_home: Path
) -> list[str]:
    """Write each verified member into ``memory_home`` (containment-guarded)."""
    written: list[str] = []
    home_real = Path(os.path.realpath(memory_home))
    for arcname in files:
        target = memory_home / arcname
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        parent_real = Path(os.path.realpath(parent))
        if not (parent_real == home_real or parent_real.is_relative_to(home_real)):
            raise BundleVerificationError(
                f"refusing to extract {arcname!r} outside memory_home"
            )
        member = tar.getmember(arcname)
        handle = tar.extractfile(member)
        if handle is None:
            continue
        with open(target, "wb") as out:
            shutil.copyfileobj(handle, out)
        written.append(arcname)
    return written


def _plant_marker(workspace_root: Path, store_id: str) -> Path:
    """Write ``<workspace_root>/.silly-memory/memory-id`` = ``store_id`` (overwrites).

    It overwrites any stale id that was auto-planted there, so a moved project
    resolves to the imported store.
    """
    from memory_system.paths import write_workspace_marker

    return write_workspace_marker(workspace_root, store_id)


def _rewrite_meta(store: Path, store_id: str, new_root: Path) -> None:
    """Rewrite ``<store>/.meta.json:workspace_root`` to ``new_root``.

    ``paths.workspace_store`` only writes ``.meta.json`` when it is ABSENT
    (paths.py:45-50), so it never updates a moved project — this does, keeping
    ``memws`` (which reads ``.meta.json:workspace_root``) pointed at the new path.
    """
    resolved = str(new_root.resolve())
    meta = store / _META_FILENAME
    data: dict[str, object] = {"workspace_id": store_id, "workspace_root": resolved}
    if meta.is_file():
        try:
            existing = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            existing["workspace_root"] = resolved
            existing.setdefault("workspace_id", store_id)
            data = existing
    _ = store.mkdir(parents=True, exist_ok=True)
    _ = meta.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _resolve_mapping(
    map_workspace: dict[str, str] | None, markers: dict[str, str]
) -> dict[str, str]:
    """Match ``--map-workspace OLD=NEW`` keys against manifest roots.

    OLD is a path recorded on the SOURCE machine, so it is matched literally and
    via ``resolve()`` normalization (handles e.g. macOS ``/var`` vs
    ``/private/var``); it is never resolved against this machine's filesystem in
    a way that could silently miss.
    """
    if not map_workspace:
        return {}
    normalized: dict[str, str] = {}
    for old, new in map_workspace.items():
        normalized[str(old)] = str(new)
        try:
            normalized[str(Path(old).expanduser().resolve())] = str(new)
        except OSError:
            pass
    out: dict[str, str] = {}
    for root in markers:
        if root in normalized:
            out[root] = normalized[root]
            continue
        try:
            root_resolved = str(Path(root).resolve())
        except OSError:
            root_resolved = root
        if root_resolved in normalized:
            out[root] = normalized[root_resolved]
    return out


def _apply_relink(
    memory_home: Path,
    markers: dict[str, str],
    relink: bool,
    map_workspace: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Plant project markers for the imported stores.

    Returns ``(relinked, mapped, unresolved)`` where:
      - ``mapped[new_root] = store_id`` — a ``--map-workspace`` remap (marker
        planted at NEW and the store's ``.meta.json`` rewritten to NEW);
      - ``relinked[root] = store_id`` — a same-path project present on this
        machine, relinked in place (only when ``relink`` is set);
      - ``unresolved[root] = store_id`` — neither present nor mapped; the caller
        prints a manual remedy.
    """
    mapping = _resolve_mapping(map_workspace, markers)
    relinked: dict[str, str] = {}
    mapped: dict[str, str] = {}
    unresolved: dict[str, str] = {}
    for root, store_id in markers.items():
        if root in mapping:
            new_path = Path(mapping[root]).expanduser()
            _ = _plant_marker(new_path, store_id)
            _rewrite_meta(memory_home / store_id, store_id, new_path)
            mapped[str(new_path.resolve())] = store_id
        elif relink and Path(root).is_dir():
            _ = _plant_marker(Path(root), store_id)
            relinked[root] = store_id
        else:
            unresolved[root] = store_id
    return relinked, mapped, unresolved


def import_bundle(
    bundle: Path,
    memory_home: Path,
    confirm_token: str,
    *,
    relink: bool = False,
    map_workspace: dict[str, str] | None = None,
) -> dict[str, object]:
    """Import a ``create_bundle`` ``.tar.gz`` into ``memory_home``.

    Order of operations (each precondition guards the next write):
      1. ``confirm_token`` must equal ``YES-IMPORT`` — else raise, no writes
         (mirrors ``backup.restore_snapshot``'s ``YES-RESTORE`` guard).
      2. Verify EVERY declared member against ``manifest.json``'s sha256s — a
         mismatch raises before the store is touched.
      3. Snapshot an existing non-empty store via ``backup.snapshot_store`` so
         the import never silently overwrites live data.
      4. Extract ``config.json`` / ``_global/`` / every ``<workspace-id>/`` into
         ``memory_home``.
      5. Relink project markers (``relink`` / ``map_workspace``).

    Returns a summary including the manifest ``project_markers`` and the
    relinked / mapped / unresolved project classifications.
    """
    if confirm_token != IMPORT_CONFIRM_TOKEN:
        raise ValueError(
            f"import confirmation token required (expected {IMPORT_CONFIRM_TOKEN!r})"
        )

    bundle = Path(bundle)
    memory_home = Path(memory_home)
    if not bundle.is_file():
        raise BundleError(f"no bundle at {bundle}")

    with tarfile.open(bundle, "r:*") as tar:
        manifest = _load_manifest(tar)
        files = _verify_members(tar, manifest)

        snapshot: Path | None = None
        if _has_store_content(memory_home):
            from memory_system.lifecycle import backup

            snapshot = backup.snapshot_store(memory_home, "pre-import")

        _ = memory_home.mkdir(parents=True, exist_ok=True)
        written = _extract_members(tar, files, memory_home)

    markers_raw = manifest.get("project_markers")
    markers: dict[str, str] = (
        {str(k): str(v) for k, v in markers_raw.items()}
        if isinstance(markers_raw, dict)
        else {}
    )
    relinked, mapped, unresolved = _apply_relink(
        memory_home, markers, relink, map_workspace
    )

    return {
        "memory_home": str(memory_home),
        "bundle": str(bundle),
        "snapshot": str(snapshot) if snapshot is not None else None,
        "verified": True,
        "schema": manifest.get("schema"),
        "version": manifest.get("version"),
        "workspaces": manifest.get("workspaces", []),
        "global": manifest.get("global"),
        "files": written,
        "project_markers": markers,
        "relinked": relinked,
        "mapped": mapped,
        "unresolved": unresolved,
    }

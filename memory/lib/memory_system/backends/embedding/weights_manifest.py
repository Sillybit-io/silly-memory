"""SHA-pinned weight verification for embedding backends.

The manifest is checked before instantiating any ML loader so that:
  * tampered/corrupted weights are caught early,
  * an offline run (``MEMORY_ALLOW_NETWORK=0``) refuses to silently fall back
    to a network download,
  * an online run (``MEMORY_ALLOW_NETWORK=1``) downgrades a verify failure to
    a warning and proceeds.

This module is stdlib-only; importing it must not pull in torch, sentence
transformers, fastembed, or any other ML dependency.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import TypedDict

__all__ = [
    "WEIGHTS_MANIFEST",
    "verify_weights",
    "generate_manifest",
    "network_allowed",
]


class _FileSpec(TypedDict, total=False):
    sha256: str
    min_size_bytes: int
    size_bytes_expected: int
    optional: bool


class _ModelSpec(TypedDict, total=False):
    files: dict[str, _FileSpec]
    preseed_dir: str


_REPO_ROOT_HINT = "memory/weights"


def _repo_root() -> Path:
    """Locate the repository root by walking up from this file."""
    # weights_manifest.py lives at
    #   <repo>/memory/lib/memory_system/backends/embedding/weights_manifest.py
    here = Path(__file__).resolve()
    # Go up: embedding -> backends -> memory_system -> lib -> memory -> <repo>
    return here.parents[5]


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
#
# Each entry pins the integrity of files that ship with a model. SHA-256 is
# the source of truth; ``min_size_bytes`` defends against a zero-byte / stub
# replacement attack where the hash field might also be tampered with.
#
# To populate hashes when a real preseed exists, run::
#
#   python3 -c "from memory.lib.memory_system.backends.embedding.weights_manifest \
#       import generate_manifest; \
#       generate_manifest('sentence-transformers/all-MiniLM-L6-v2', \
#                         'memory/weights/all-MiniLM-L6-v2')"
#
WEIGHTS_MANIFEST: dict[str, _ModelSpec] = {
    # ------------------------------------------------------------------
    # sentence-transformers/all-MiniLM-L6-v2
    #
    # Real preseed: 6 files (~91MB) shipped in
    # ``memory/weights/all-MiniLM-L6-v2/``. SHA-256 pins below verify byte
    # integrity on every backend load. Install copies these into the
    # user's HF cache; offline use (``MEMORY_ALLOW_NETWORK=0``) is the
    # default and requires no network. To refresh from upstream, run
    # ``./upgrade.sh`` with ``MEMORY_ALLOW_NETWORK=1`` after replacing
    # files here and rerunning generate_manifest.
    # ------------------------------------------------------------------
    "sentence-transformers/all-MiniLM-L6-v2": {
        "files": {
            "config.json": {
                "sha256": "953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41",
                "min_size_bytes": 600,
            },
            "tokenizer.json": {
                "sha256": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
                "min_size_bytes": 460000,
            },
            "tokenizer_config.json": {
                "sha256": "acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b",
                "min_size_bytes": 300,
            },
            "vocab.txt": {
                "sha256": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
                "min_size_bytes": 230000,
            },
            "special_tokens_map.json": {
                "sha256": "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3",
                "min_size_bytes": 100,
            },
            "model.safetensors": {
                "sha256": "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db",
                "min_size_bytes": 90_000_000,
                "size_bytes_expected": 90_868_376,
            },
        },
        "preseed_dir": "memory/weights/all-MiniLM-L6-v2/",
    },
    "BAAI/bge-small-en-v1.5": {
        # fastembed packages ONNX weights differently per release; pin only a
        # structural floor here. Real SHA pinning requires generate_manifest()
        # against a preseeded copy.
        "files": {
            "config.json": {
                "sha256": "",
                "min_size_bytes": 128,
                "optional": True,
            },
            "tokenizer.json": {
                "sha256": "",
                "min_size_bytes": 1024,
                "optional": True,
            },
            "model_optimized.onnx": {
                "sha256": "",
                "min_size_bytes": 1_000_000,
                "optional": True,
            },
        },
        "preseed_dir": "memory/weights/bge-small-en-v1.5/",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def network_allowed() -> bool:
    """Honor the project-wide offline default."""
    return os.environ.get("MEMORY_ALLOW_NETWORK", "0") == "1"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _candidate_dirs(model_name: str, cache_dir: str | Path | None) -> list[Path]:
    """Return search order: preseed_dir → cache_dir."""
    spec = WEIGHTS_MANIFEST.get(model_name)
    out: list[Path] = []
    if spec is not None:
        preseed = spec.get("preseed_dir")
        if preseed:
            p = Path(preseed)
            if not p.is_absolute():
                p = _repo_root() / preseed
            out.append(p.resolve())
    if cache_dir is not None:
        out.append(Path(cache_dir).resolve())
    return out


def _find_file(filename: str, dirs: list[Path]) -> Path | None:
    """Find ``filename`` anywhere under one of the candidate dirs."""
    for d in dirs:
        if not d.exists() or not d.is_dir():
            continue
        # Direct hit
        direct = d / filename
        if direct.is_file():
            return direct
        # Recursive search (HF cache nests files under snapshots/<sha>/...)
        try:
            for match in d.rglob(filename):
                if match.is_file():
                    return match
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_weights(model_name: str, cache_dir: str | Path | None) -> None:
    """Verify SHA-256 + size of each manifest-pinned file.

    Search order: ``preseed_dir`` → ``cache_dir``.

    Raises ``RuntimeError("weight integrity check failed: …")`` on:
      * unknown model name,
      * missing required file,
      * size below ``min_size_bytes``,
      * SHA-256 mismatch.

    The error string mentions ``preseed`` and ``MEMORY_ALLOW_NETWORK=1`` so
    backends can re-raise it verbatim when offline.
    """
    spec = WEIGHTS_MANIFEST.get(model_name)
    if spec is None:
        raise RuntimeError(
            "weight integrity check failed: unknown model "
            f"{model_name!r} (not in WEIGHTS_MANIFEST). "
            "Add an entry or set MEMORY_ALLOW_NETWORK=1 to bypass."
        )

    dirs = _candidate_dirs(model_name, cache_dir)
    if not dirs:
        raise RuntimeError(
            "weight integrity check failed: no preseed_dir or cache_dir "
            f"available for {model_name!r}. "
            "Preseed the weights or set MEMORY_ALLOW_NETWORK=1."
        )

    files = spec.get("files", {})
    failures: list[str] = []

    for filename, fspec in files.items():
        path = _find_file(filename, dirs)
        if path is None:
            if fspec.get("optional"):
                continue
            failures.append(
                f"missing {filename} (searched {[str(d) for d in dirs]})"
            )
            continue
        try:
            actual_size = path.stat().st_size
        except OSError as e:
            failures.append(f"{filename}: stat failed ({e})")
            continue

        min_size = int(fspec.get("min_size_bytes", 0))
        if actual_size < min_size:
            failures.append(
                f"{filename}: size {actual_size} < min_size_bytes {min_size}"
            )
            continue

        expected_sha = fspec.get("sha256", "") or ""
        if expected_sha:
            try:
                actual_sha = _sha256_of(path)
            except OSError as e:
                failures.append(f"{filename}: sha read failed ({e})")
                continue
            if actual_sha != expected_sha:
                failures.append(
                    f"{filename}: sha256 mismatch (expected {expected_sha[:12]}…, "
                    f"got {actual_sha[:12]}…)"
                )

    if failures:
        detail = "; ".join(failures)
        raise RuntimeError(
            f"weight integrity check failed: {detail}. "
            "Preseed the model into "
            f"{spec.get('preseed_dir', _REPO_ROOT_HINT)} "
            "or set MEMORY_ALLOW_NETWORK=1 to allow re-download."
        )


def verify_or_warn(
    model_name: str,
    cache_dir: str | Path | None,
    logger: logging.Logger | None = None,
) -> None:
    """Verify weights; on failure raise (offline) or warn-and-continue (online).

    This is the single entry point backends should call.
    """
    log = logger or logging.getLogger("memory.weights")
    try:
        verify_weights(model_name, cache_dir)
    except RuntimeError as e:
        if network_allowed():
            log.warning(
                "weight verification failed but MEMORY_ALLOW_NETWORK=1; "
                "continuing to load/download: %s",
                e,
            )
            return
        raise


def generate_manifest(model_name: str, dir_path: str | Path) -> None:
    """Compute SHA-256 + sizes for every file under ``dir_path`` and print
    a Python dict ready to paste into ``WEIGHTS_MANIFEST``.

    One-time bootstrap utility — never invoked in the hot path.
    """
    base = Path(dir_path).resolve()
    if not base.exists() or not base.is_dir():
        raise SystemExit(f"generate_manifest: {base} does not exist")

    files: dict[str, dict[str, int | str]] = {}
    for entry in sorted(base.rglob("*")):
        if not entry.is_file():
            continue
        rel = entry.relative_to(base).as_posix()
        files[rel] = {
            "sha256": _sha256_of(entry),
            "min_size_bytes": entry.stat().st_size,
        }

    print(f"# Paste into WEIGHTS_MANIFEST under {model_name!r}:")
    print("{")
    print('    "files": {')
    for name, spec in files.items():
        print(f'        "{name}": {{')
        print(f'            "sha256": "{spec["sha256"]}",')
        print(f'            "min_size_bytes": {spec["min_size_bytes"]},')
        print("        },")
    print("    },")
    print(f'    "preseed_dir": "{Path(dir_path).as_posix().rstrip("/")}/",')
    print("}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: python3 -m memory.lib.memory_system.backends.embedding."
            "weights_manifest <model_name> <dir>"
        )
    generate_manifest(sys.argv[1], sys.argv[2])

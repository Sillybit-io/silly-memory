from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from ...system.config import memory_home

DEFAULT_MODEL_ID = "BAAI/bge-small-en-v1.5"
DEFAULT_MODEL_DIRNAME = "fastembed-bge-small-en-v1.5"
_EMBEDDINGS_SUBDIR = "_embeddings"


def default_model_dir() -> Path:
    return memory_home() / _EMBEDDINGS_SUBDIR / DEFAULT_MODEL_DIRNAME


def _try_import() -> ModuleType | None:
    try:
        import fastembed  # pyright: ignore[reportMissingImports]
    except ImportError:
        return None
    return fastembed


class FastembedBackend:
    name: str = "fastembed"
    dim: int = 384

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        model_dir: Path | None = None,
    ) -> None:
        self.model_id: str = model_id
        self.model_dir: Path = Path(model_dir) if model_dir is not None else default_model_dir()
        self._model: Any | None = None
        _ = _try_import()

    def is_available(self) -> bool:
        if _try_import() is None:
            return False
        if not self.model_dir.exists() or not self.model_dir.is_dir():
            return False
        try:
            has_files = any(self.model_dir.iterdir())
        except OSError:
            return False
        return has_files

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        fastembed_mod = _try_import()
        if fastembed_mod is None:
            raise RuntimeError(
                "fastembed not installed; install with `pip install fastembed`"
            )
        _ = self.model_dir.parent.mkdir(parents=True, exist_ok=True)
        from .weights_manifest import verify_or_warn
        verify_or_warn(self.model_id, self.model_dir)
        loader: Any = fastembed_mod.TextEmbedding  # pyright: ignore[reportAny]
        self._model = loader(
            model_name=self.model_id,
            cache_dir=str(self.model_dir),
        )
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        embeddings_iter: Any = model.embed(texts)  # pyright: ignore[reportAny]
        result: list[list[float]] = []
        for row in embeddings_iter:  # pyright: ignore[reportAny]
            result.append([float(value) for value in row.tolist()])  # pyright: ignore[reportAny]
        return result

    def download_model(self) -> Path:
        fastembed_mod = _try_import()
        if fastembed_mod is None:
            raise RuntimeError(
                "fastembed not installed; cannot download model"
            )
        _ = self.model_dir.parent.mkdir(parents=True, exist_ok=True)
        _ = self.model_dir.mkdir(parents=True, exist_ok=True)
        loader: Any = fastembed_mod.TextEmbedding  # pyright: ignore[reportAny]
        model: Any = loader(
            model_name=self.model_id,
            cache_dir=str(self.model_dir),
        )
        # Force model materialization by running a single embed call.
        _ = list(model.embed(["warmup"]))  # pyright: ignore[reportAny]
        return self.model_dir

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ...system.config import memory_home

_EMBEDDINGS_SUBDIR = "_embeddings"
_MODEL_DIRNAME = "sentence-transformers-all-MiniLM-L6-v2"
_DEVICE = "cpu"


def _default_model_path() -> Path:
    return memory_home() / _EMBEDDINGS_SUBDIR / _MODEL_DIRNAME


class SentenceTransformersBackend:
    name: ClassVar[str] = "sentence-transformers"
    model_name: ClassVar[str] = "sentence-transformers/all-MiniLM-L6-v2"
    dim: ClassVar[int] = 384

    def __init__(self, model_path: Path | None = None) -> None:
        self._torch: Any | None = None
        self._st: Any | None = None
        try:
            import torch as _torch  # pyright: ignore[reportMissingImports]
            import sentence_transformers as _st  # pyright: ignore[reportMissingImports]
        except ImportError:
            pass
        else:
            self._torch = _torch
            self._st = _st

        self.model_path: Path = Path(model_path) if model_path is not None else _default_model_path()
        self._model: Any | None = None

    def is_available(self) -> bool:
        if self._torch is None or self._st is None:
            return False
        if not self.model_path.exists() or not self.model_path.is_dir():
            return False
        try:
            return any(self.model_path.iterdir())
        except OSError:
            return False

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._st is None or self._torch is None:
            raise RuntimeError(
                "sentence_transformers/torch not installed; "
                "this backend requires both packages on CPU"
            )
        if not self.model_path.exists():
            raise RuntimeError(
                f"Model not present at {self.model_path}; call download_model() first"
            )
        from .weights_manifest import verify_or_warn
        verify_or_warn(self.model_name, self.model_path)
        loader: Any = self._st.SentenceTransformer  # pyright: ignore[reportAny]
        self._model = loader(str(self.model_path), device=_DEVICE)
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        embeddings = model.encode(  # pyright: ignore[reportAny]
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        result: list[list[float]] = []
        for row in embeddings:  # pyright: ignore[reportAny]
            result.append([float(value) for value in row.tolist()])  # pyright: ignore[reportAny]
        return result

    def download_model(self) -> Path:
        if self._st is None or self._torch is None:
            raise RuntimeError(
                "sentence_transformers/torch not installed; cannot download model"
            )
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        loader: Any = self._st.SentenceTransformer  # pyright: ignore[reportAny]
        model: Any = loader(self.model_name, device=_DEVICE)
        model.save(str(self.model_path))  # pyright: ignore[reportAny]
        return self.model_path

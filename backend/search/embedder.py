"""
backend/search/embedder.py

Embedder — loads the all-MiniLM-L6-v2 sentence-transformer model once at
startup and generates embedding vectors for dbt models and user questions.

The Embedder is responsible for:
- Loading the sentence-transformer model at application startup (``load()``).
- Encoding every model in the SchemaIndex into a (384,) float32 vector
  (``embed_models()``).
- Encoding a user question into a (384,) float32 vector (``embed_question()``).

Text representation format per model (Requirement 4.1):
    "{model_name}: {description}. Columns: {col1} ({type1}) {desc1}, {col2} ..."

Requirements: 4.1, 4.2, 4.3, 4.7, 13.4
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer as _SentenceTransformer

from backend.models import ModelMeta

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"


class Embedder:
    """Sentence-transformer wrapper for Prism semantic search.

    Parameters
    ----------
    None — the model is loaded lazily via :meth:`load`.

    Usage::

        embedder = Embedder()
        embedder.load()                            # once at startup

        embeddings = embedder.embed_models(models) # shape (N, 384)
        question_vec = embedder.embed_question(q)  # shape (384,)
    """

    def __init__(self) -> None:
        self._model: "_SentenceTransformer | None" = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the all-MiniLM-L6-v2 model into memory.

        Must be called once at application startup before any embed call.
        Subsequent calls are idempotent — the model is only loaded once.

        Raises
        ------
        RuntimeError
            If the sentence-transformers package is not installed or the
            model download fails.
        """
        if self._model is not None:
            return

        logger.info("Embedder: loading sentence-transformer model '%s'", _MODEL_NAME)
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import]

            self._model = SentenceTransformer(_MODEL_NAME)
            logger.info(
                "Embedder: model '%s' loaded successfully (embedding dim=384)",
                _MODEL_NAME,
            )
        except Exception as exc:
            logger.error("Embedder: failed to load model '%s' — %s", _MODEL_NAME, exc)
            raise RuntimeError(
                f"Failed to load sentence-transformer model '{_MODEL_NAME}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Embedding API
    # ------------------------------------------------------------------

    def embed_models(self, models: list[ModelMeta]) -> np.ndarray:
        """Embed all *models* into a (N, 384) float32 numpy array.

        Rows are in the same order as *models*, so ``embeddings[i]``
        corresponds to ``models[i]`` (Requirement 4.2).

        Parameters
        ----------
        models:
            List of dbt models to embed.  An empty list returns
            ``np.empty((0, 384), dtype=np.float32)``.

        Returns
        -------
        np.ndarray
            Shape ``(len(models), 384)``, dtype ``float32``.
            Vectors are L2-normalised so dot-product == cosine similarity.

        Raises
        ------
        RuntimeError
            If :meth:`load` has not been called.
        """
        self._assert_loaded()

        if not models:
            return np.empty((0, 384), dtype=np.float32)

        texts = [self._model_to_text(m) for m in models]
        logger.debug("Embedder: embedding %d model(s)", len(texts))

        embeddings = self._model.encode(  # type: ignore[union-attr]
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.astype(np.float32)

    def embed_question(self, question: str) -> np.ndarray:
        """Embed a single user *question* into a (384,) float32 vector.

        Parameters
        ----------
        question:
            The plain-English question string.

        Returns
        -------
        np.ndarray
            Shape ``(384,)``, dtype ``float32``, L2-normalised.

        Raises
        ------
        RuntimeError
            If :meth:`load` has not been called.
        """
        self._assert_loaded()

        logger.debug("Embedder: embedding question (%d chars)", len(question))
        vec = self._model.encode(  # type: ignore[union-attr]
            [question],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vec[0].astype(np.float32)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _model_to_text(self, model: ModelMeta) -> str:
        """Convert *model* to a text string suitable for embedding.

        Format (Requirement 4.1):
            "{model_name}: {description}. Columns: {col1} ({type1}) {desc1}, ..."

        Every column name is included so that retrieval can match on column-
        level keywords (e.g. "revenue" or "customer_id").
        """
        col_parts: list[str] = []
        for col in model.columns:
            part = f"{col.name} ({col.data_type})"
            if col.description:
                part += f" {col.description}"
            col_parts.append(part)

        cols_str = ", ".join(col_parts) if col_parts else ""
        desc = model.description or ""
        return f"{model.name}: {desc}. Columns: {cols_str}"

    def _assert_loaded(self) -> None:
        if self._model is None:
            raise RuntimeError(
                "Embedder.load() must be called before any embed operation."
            )

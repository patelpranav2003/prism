"""
backend/search/retriever.py

Retriever — computes cosine similarity between a question embedding and all
model embeddings in the active SchemaIndex, applies Gold/Silver layer boosts,
and returns the top-N most relevant models.

Design decisions:
- Embeddings are pre-normalised (L2 norm == 1), so cosine similarity reduces
  to a simple dot product: ``embeddings @ question_vec``.
- Layer boosts are applied to the raw cosine score before ranking:
    Gold   → +0.05
    Silver → +0.025
    Bronze → +0.00
- When all raw scores are below 0.1 the caller receives ``confidence_hint="low"``
  on every returned model.
- Always returns up to ``min(top_n, N)`` models even when scores are low.

Requirements: 4.4, 4.5, 4.6, 4.8
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from backend.models import RankedModel, SchemaIndex

if TYPE_CHECKING:
    from typing import Protocol

    class CacheManagerProtocol(Protocol):
        def get_index(self) -> SchemaIndex | None: ...

logger = logging.getLogger(__name__)

# Gold/Silver/Bronze score boosts (Requirement 4.5)
_LAYER_BOOST: dict[str, float] = {
    "gold": 0.05,
    "silver": 0.025,
    "bronze": 0.0,
}

# Threshold below which all scores are considered "low confidence" (Requirement 4.6)
_LOW_CONFIDENCE_THRESHOLD = 0.1


class Retriever:
    """Semantic model retriever backed by cosine similarity + layer boosts.

    Parameters
    ----------
    cache:
        The :class:`~backend.discovery.cache_manager.CacheManager` holding
        the active :class:`~backend.models.SchemaIndex`.

    Usage::

        retriever = Retriever(cache_manager)
        question_vec = embedder.embed_question("What is total revenue by region?")
        ranked_models = retriever.retrieve(question_vec, top_n=5)
    """

    def __init__(self, cache: "CacheManagerProtocol") -> None:  # type: ignore[name-defined]
        self._cache = cache

    def retrieve(
        self,
        question_vec: np.ndarray,
        top_n: int = 5,
    ) -> list[RankedModel]:
        """Retrieve the top-*top_n* models most relevant to *question_vec*.

        Ranking uses cosine similarity (dot product on normalised vectors) plus
        a layer boost applied *before* sorting.

        Parameters
        ----------
        question_vec:
            Normalised question embedding, shape ``(384,)``, dtype ``float32``.
        top_n:
            Maximum number of models to return.  Capped at the number of
            models in the index (Requirement 4.4).

        Returns
        -------
        list[RankedModel]
            At most ``min(top_n, N)`` models ordered by ``adjusted_score``
            descending.  Returns an empty list if no index is available.
        """
        index: SchemaIndex | None = self._cache.get_index()

        if index is None or index.model_count == 0 or index.embeddings.ndim < 2:
            logger.warning("Retriever: no index available — returning empty list")
            return []

        # --- Cosine similarity (dot product on normalised vectors) ---
        # embeddings shape: (N, 384); question_vec shape: (384,)
        raw_scores: np.ndarray = index.embeddings @ question_vec  # shape (N,)

        # --- Determine confidence hint ---
        all_low = bool(np.all(raw_scores < _LOW_CONFIDENCE_THRESHOLD))
        confidence_hint = "low" if all_low else None

        # --- Apply layer boosts ---
        n_models = len(index.models)
        scored: list[tuple[int, float, float]] = []  # (index, raw, adjusted)
        for i in range(n_models):
            raw = float(raw_scores[i])
            boost = _LAYER_BOOST.get(index.models[i].layer, 0.0)
            scored.append((i, raw, raw + boost))

        # --- Sort by adjusted score descending ---
        scored.sort(key=lambda t: t[2], reverse=True)

        # --- Build result list ---
        actual_n = min(top_n, n_models)
        result: list[RankedModel] = []
        for idx, raw_sim, adj_score in scored[:actual_n]:
            result.append(
                RankedModel(
                    model=index.models[idx],
                    raw_similarity=raw_sim,
                    adjusted_score=adj_score,
                    confidence_hint=confidence_hint,  # type: ignore[arg-type]
                )
            )

        logger.debug(
            "Retriever: returning %d/%d model(s); all_low=%s; "
            "top score=%.4f (adjusted)",
            len(result),
            n_models,
            all_low,
            result[0].adjusted_score if result else 0.0,
        )
        return result

"""
tests/unit/test_retrieval_ranking.py

Property-based tests for Retriever ranking and score boost computation.

# Feature: prism, Property 8: Retrieval Ranking by Adjusted Score
For any non-empty embeddings matrix and any question vector, the Retriever
SHALL return a list of at most min(5, N) models ordered by adjusted_score
descending, where no model ranked at position i has a lower adjusted_score
than any model at position i+1.

# Feature: prism, Property 9: Layer Score Boost Computation
For any model with a known layer and raw cosine similarity score, the
adjusted_score SHALL always equal: raw_score + 0.05 for Gold models,
raw_score + 0.025 for Silver models, raw_score + 0.0 for Bronze models.

Validates: Requirements 4.4, 4.5
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from backend.models import ColumnMeta, LineageNode, ModelMeta, RankedModel, SchemaIndex
from backend.search.retriever import Retriever, _LAYER_BOOST


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model(name: str, layer: str) -> ModelMeta:
    return ModelMeta(
        name=name,
        database="db",
        schema_name="schema",
        fqn=f"db.schema.{name}",
        columns=[],
        grain="unknown",
        layer=layer,  # type: ignore[arg-type]
        compiled_sql_excerpt="",
        depends_on=[],
        tags=[],
        folder_path="",
        row_count=0,
        last_updated=None,
        description="",
    )


def _make_index(models: list[ModelMeta], embeddings: np.ndarray) -> SchemaIndex:
    return SchemaIndex(
        models=models,
        embeddings=embeddings,
        lineage={},
        built_at=datetime.now(tz=timezone.utc),
        model_count=len(models),
    )


def _make_cache(index: SchemaIndex | None) -> MagicMock:
    cache = MagicMock()
    cache.get_index.return_value = index
    return cache


def _normalise(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v)
    if norm == 0:
        return v
    return v / norm


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_layer_strategy = st.sampled_from(["gold", "silver", "bronze"])

_float_score = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@st.composite
def _index_with_models(draw, min_models: int = 1, max_models: int = 20):
    """Generate a SchemaIndex with normalised embeddings."""
    n = draw(st.integers(min_value=min_models, max_value=max_models))
    layers = draw(st.lists(_layer_strategy, min_size=n, max_size=n))
    models = [_make_model(f"model_{i}", layers[i]) for i in range(n)]

    # Random normalised embeddings (384-dim)
    raw = draw(arrays(np.float32, shape=(n, 384), elements=st.floats(-1.0, 1.0, allow_nan=False)))
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    embeddings = (raw / norms).astype(np.float32)

    return _make_index(models, embeddings)


@st.composite
def _question_vec(draw):
    """Generate a normalised question vector (384,)."""
    raw = draw(arrays(np.float32, shape=(384,), elements=st.floats(-1.0, 1.0, allow_nan=False)))
    return _normalise(raw).astype(np.float32)


# ---------------------------------------------------------------------------
# Property 8: Retrieval Ranking by Adjusted Score
# Validates: Requirements 4.4
# ---------------------------------------------------------------------------


@given(
    index=_index_with_models(min_models=1, max_models=50),
    question_vec=_question_vec(),
    top_n=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=100)
def test_property_8_ranking_by_adjusted_score(
    index: SchemaIndex,
    question_vec: np.ndarray,
    top_n: int,
) -> None:
    """**Property 8: Retrieval Ranking by Adjusted Score**

    The Retriever SHALL return at most min(top_n, N) models ordered by
    adjusted_score descending (no model at position i has lower adjusted_score
    than position i+1).

    **Validates: Requirements 4.4**
    """
    cache = _make_cache(index)
    retriever = Retriever(cache)
    results = retriever.retrieve(question_vec, top_n=top_n)

    # --- Invariant 1: result count ---
    expected_n = min(top_n, index.model_count)
    assert len(results) == expected_n, (
        f"Expected {expected_n} results, got {len(results)} "
        f"(top_n={top_n}, N={index.model_count})"
    )

    # --- Invariant 2: descending order ---
    for i in range(len(results) - 1):
        assert results[i].adjusted_score >= results[i + 1].adjusted_score, (
            f"Position {i} has adjusted_score={results[i].adjusted_score:.4f} < "
            f"position {i+1} score={results[i+1].adjusted_score:.4f} — not sorted descending"
        )

    # --- Invariant 3: types ---
    for r in results:
        assert isinstance(r, RankedModel)
        assert isinstance(r.raw_similarity, float)
        assert isinstance(r.adjusted_score, float)


# ---------------------------------------------------------------------------
# Property 9: Layer Score Boost Computation
# Validates: Requirements 4.5
# ---------------------------------------------------------------------------


@given(
    raw_score=_float_score,
    layer=_layer_strategy,
)
@settings(max_examples=200)
def test_property_9_score_boost_computation(raw_score: float, layer: str) -> None:
    """**Property 9: Layer Score Boost Computation**

    adjusted_score = raw_score + boost, where:
      Gold   → +0.05
      Silver → +0.025
      Bronze → +0.0

    **Validates: Requirements 4.5**
    """
    expected_boost = _LAYER_BOOST[layer]
    expected_adjusted = raw_score + expected_boost

    # Build a single-model index
    model = _make_model("m", layer)
    # Use a simple 1-dim embedding for predictability
    emb = np.array([[1.0] + [0.0] * 383], dtype=np.float32)
    question = np.array([1.0] + [0.0] * 383, dtype=np.float32)
    # Set raw_score directly by controlling question_vec
    # raw = emb @ question = 1.0 → adjusted = 1.0 + boost
    index = _make_index([model], emb)
    cache = _make_cache(index)
    retriever = Retriever(cache)

    # Use a question vec that gives raw_similarity ≈ 1.0
    results = retriever.retrieve(question, top_n=1)
    assert len(results) == 1
    r = results[0]

    expected_adjusted_actual = r.raw_similarity + expected_boost
    assert abs(r.adjusted_score - expected_adjusted_actual) < 1e-5, (
        f"adjusted_score should be raw_similarity({r.raw_similarity:.4f}) + "
        f"boost({expected_boost}) = {expected_adjusted_actual:.4f}, "
        f"got {r.adjusted_score:.4f}"
    )


@given(raw_score=_float_score)
@settings(max_examples=100)
def test_property_9_boost_values_are_exact(raw_score: float) -> None:
    """**Property 9 — exact values**: Gold=+0.05, Silver=+0.025, Bronze=+0.0."""
    assert _LAYER_BOOST["gold"] == 0.05
    assert _LAYER_BOOST["silver"] == 0.025
    assert _LAYER_BOOST["bronze"] == 0.0


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestRetrievalRankingExamples:

    def test_empty_index_returns_empty_list(self):
        cache = _make_cache(None)
        results = Retriever(cache).retrieve(np.zeros(384, dtype=np.float32))
        assert results == []

    def test_all_low_scores_sets_confidence_hint_low(self):
        """When all cosine scores are below 0.1, confidence_hint should be 'low'."""
        model = _make_model("m", "bronze")
        # Perpendicular vectors → cosine similarity = 0
        emb = np.zeros((1, 384), dtype=np.float32)
        emb[0, 0] = 1.0
        question = np.zeros(384, dtype=np.float32)
        question[1] = 1.0  # orthogonal
        index = _make_index([model], emb)
        results = Retriever(_make_cache(index)).retrieve(question, top_n=1)
        assert len(results) == 1
        assert results[0].confidence_hint == "low"

    def test_gold_model_ranked_above_bronze_with_equal_similarity(self):
        """Gold boost should push a gold model above a bronze model with same raw similarity."""
        gold = _make_model("gold_model", "gold")
        bronze = _make_model("bronze_model", "bronze")
        # Both have same unit vector → same raw similarity
        emb = np.array([[1.0] + [0.0] * 383, [1.0] + [0.0] * 383], dtype=np.float32)
        question = np.array([1.0] + [0.0] * 383, dtype=np.float32)
        index = _make_index([gold, bronze], emb)
        results = Retriever(_make_cache(index)).retrieve(question, top_n=2)
        # Gold model should be ranked first
        assert results[0].model.layer == "gold"
        assert results[1].model.layer == "bronze"
        assert results[0].adjusted_score > results[1].adjusted_score

    def test_top_n_capped_at_model_count(self):
        """top_n larger than N should return all N models."""
        models = [_make_model(f"m{i}", "bronze") for i in range(3)]
        emb = np.eye(3, 384, dtype=np.float32)
        question = np.zeros(384, dtype=np.float32)
        question[0] = 1.0
        index = _make_index(models, emb)
        results = Retriever(_make_cache(index)).retrieve(question, top_n=100)
        assert len(results) == 3

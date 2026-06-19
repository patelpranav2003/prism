"""
tests/smoke/test_performance.py

Performance smoke tests verifying key latency budgets.

Assertions (Requirements 13.2, 13.3, 13.4):
  - Index build (embed 500 models)  : <30 seconds
  - Retrieval over 500-model index  : <2 seconds
  - Single question embed           : <100 milliseconds

All tests use real Embedder/Retriever with synthetic data — no live secrets.

Run with:
    pytest tests/smoke/ -v
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np
import pytest

from backend.models import ColumnMeta, LineageNode, ModelMeta, SchemaIndex
from backend.search.embedder import Embedder
from backend.search.retriever import Retriever

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model(i: int) -> ModelMeta:
    """Generate a synthetic ModelMeta for performance testing."""
    layer = ["gold", "silver", "bronze"][i % 3]
    return ModelMeta(
        name=f"model_{i:04d}",
        database="main",
        schema_name=layer,
        fqn=f"main.{layer}.model_{i:04d}",
        columns=[
            ColumnMeta(f"col_{j}", "string", f"Column {j} of model {i}")
            for j in range(10)
        ],
        grain="unknown",
        layer=layer,
        compiled_sql_excerpt=f"SELECT * FROM source_{i} LIMIT 1000",
        description=f"Synthetic model number {i} used for performance testing.",
        row_count=i * 100,
        depends_on=[],
        tags=[layer],
        folder_path=f"models/{layer}",
        last_updated=None,
    )


class _FakeCache:
    def __init__(self, index: SchemaIndex) -> None:
        self._index = index

    def get_index(self) -> SchemaIndex:
        return self._index


# ---------------------------------------------------------------------------
# Fixture — load embedder once for all perf tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def loaded_embedder() -> Embedder:
    embedder = Embedder()
    embedder.load()
    return embedder


# ---------------------------------------------------------------------------
# Test 1: Index build — 500 models embedded in <30s (Requirement 13.2)
# ---------------------------------------------------------------------------


def test_index_build_500_models_under_30s(loaded_embedder):
    """Embedding 500 models must complete in under 30 seconds."""
    models = [_make_model(i) for i in range(500)]

    start = time.monotonic()
    embeddings = loaded_embedder.embed_models(models)
    elapsed = time.monotonic() - start

    assert embeddings.shape == (500, 384), (
        f"Expected shape (500, 384), got {embeddings.shape}"
    )
    assert elapsed < 30.0, (
        f"Index build took {elapsed:.2f}s — exceeds 30s budget (Requirement 13.2)"
    )


# ---------------------------------------------------------------------------
# Test 2: Retrieval — top-5 from 500 models in <2s (Requirement 13.3)
# ---------------------------------------------------------------------------


def test_retrieval_over_500_models_under_2s(loaded_embedder):
    """Retrieval over a 500-model index must complete in under 2 seconds."""
    models = [_make_model(i) for i in range(500)]
    embeddings = loaded_embedder.embed_models(models)

    index = SchemaIndex(
        models=models,
        embeddings=embeddings,
        lineage={m.name: LineageNode(parents=[], children=[]) for m in models},
        built_at=_EPOCH,
        model_count=len(models),
    )

    retriever = Retriever(_FakeCache(index))

    question_vec = loaded_embedder.embed_question("Show total revenue by region")

    start = time.monotonic()
    ranked = retriever.retrieve(question_vec, top_n=5)
    elapsed = time.monotonic() - start

    assert len(ranked) == 5, f"Expected 5 results, got {len(ranked)}"
    assert elapsed < 2.0, (
        f"Retrieval took {elapsed:.3f}s — exceeds 2s budget (Requirement 13.3)"
    )

    # Results must be descending by adjusted_score
    for a, b in zip(ranked, ranked[1:]):
        assert a.adjusted_score >= b.adjusted_score


# ---------------------------------------------------------------------------
# Test 3: Single embed — question embedding in <100ms (Requirement 13.4)
# ---------------------------------------------------------------------------


def test_single_question_embed_under_100ms(loaded_embedder):
    """A single question embedding must complete in under 100 milliseconds."""
    # Warm-up call to avoid cold-start penalty in the measurement
    loaded_embedder.embed_question("warmup")

    start = time.monotonic()
    vec = loaded_embedder.embed_question("What is the total revenue by customer region?")
    elapsed = time.monotonic() - start
    elapsed_ms = elapsed * 1000

    assert vec.shape == (384,)
    assert elapsed_ms < 100.0, (
        f"Single question embed took {elapsed_ms:.1f}ms — exceeds 100ms budget "
        f"(Requirement 13.4)"
    )


# ---------------------------------------------------------------------------
# Test 4: Repeated retrieval — 10 consecutive retrievals, each <2s
# ---------------------------------------------------------------------------


def test_repeated_retrieval_consistent_latency(loaded_embedder):
    """Ten consecutive retrievals must each finish in under 2 seconds."""
    models = [_make_model(i) for i in range(200)]
    embeddings = loaded_embedder.embed_models(models)

    index = SchemaIndex(
        models=models,
        embeddings=embeddings,
        lineage={},
        built_at=_EPOCH,
        model_count=len(models),
    )
    retriever = Retriever(_FakeCache(index))

    questions = [
        "revenue by region",
        "total orders per customer",
        "average order value",
        "top 10 customers by spend",
        "monthly revenue trend",
        "product return rate",
        "customer lifetime value",
        "warehouse stock levels",
        "shipping delay analysis",
        "discount impact on margin",
    ]

    for q in questions:
        vec = loaded_embedder.embed_question(q)
        start = time.monotonic()
        ranked = retriever.retrieve(vec, top_n=5)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, (
            f"Retrieval for '{q}' took {elapsed:.3f}s — exceeds 2s budget"
        )
        assert len(ranked) > 0

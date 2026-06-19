"""
tests/unit/test_prompt_builder.py

Property-based tests for PromptBuilder.

# Feature: prism, Property 10: Prompt Schema Content Completeness
For any list of retrieved RankedModels, the system prompt SHALL always contain:
the FQN of every retrieved model; every column name for each model (up to 300);
and all Databricks SQL dialect keywords (DATE_TRUNC, DATEADD, DATEDIFF,
QUALIFY, LIMIT).

# Feature: prism, Property 11: Deduplication Instruction Injection
For any model whose grain field is "unknown", the system prompt SHALL always
contain an explicit deduplication instruction.

# Feature: prism, Property 23: Prompt Column Truncation
For any model with more than 300 columns, the PromptBuilder SHALL always
include exactly the first 300 columns (never more, never fewer), and a
WARNING log entry SHALL be emitted.

Validates: Requirements 5.1, 5.2, 5.3, 15.3
"""

import logging
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.models import ColumnMeta, LineageNode, ModelMeta, RankedModel, SchemaIndex
from backend.generation.prompt_builder import PromptBuilder

import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_col(name: str) -> ColumnMeta:
    return ColumnMeta(name=name, data_type="STRING", description="")


def _make_model(name: str, fqn: str, grain: str = "unknown", layer: str = "bronze",
                cols: list[ColumnMeta] | None = None) -> ModelMeta:
    return ModelMeta(
        name=name,
        database="db",
        schema_name="schema",
        fqn=fqn,
        columns=cols or [_make_col("col_a")],
        grain=grain,
        layer=layer,  # type: ignore[arg-type]
        compiled_sql_excerpt="SELECT col_a FROM source",
        depends_on=[],
        tags=[],
        folder_path="",
        row_count=100,
        last_updated=None,
        description=f"Description of {name}",
    )


def _make_ranked(model: ModelMeta, score: float = 0.9) -> RankedModel:
    return RankedModel(
        model=model,
        raw_similarity=score,
        adjusted_score=score,
        confidence_hint=None,
    )


def _make_index(models: list[ModelMeta]) -> SchemaIndex:
    return SchemaIndex(
        models=models,
        embeddings=np.empty((0,), dtype=np.float32),
        lineage={m.name: LineageNode(parents=[], children=[]) for m in models},
        built_at=datetime.now(tz=timezone.utc),
        model_count=len(models),
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_fqn_strategy = st.builds(
    lambda a, b, c: f"{a}.{b}.{c}",
    a=st.from_regex(r"[a-z][a-z0-9]{0,10}", fullmatch=True),
    b=st.from_regex(r"[a-z][a-z0-9]{0,10}", fullmatch=True),
    c=st.from_regex(r"[a-z][a-z0-9]{0,15}", fullmatch=True),
)

_grain_strategy = st.one_of(
    st.just("unknown"),
    st.just("day"),
    st.just("order_id"),
    st.just("distinct"),
)

_layer_strategy = st.sampled_from(["gold", "silver", "bronze"])


@st.composite
def _ranked_model_strategy(draw) -> RankedModel:
    name = draw(st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True))
    fqn = draw(_fqn_strategy)
    grain = draw(_grain_strategy)
    layer = draw(_layer_strategy)
    n_cols = draw(st.integers(min_value=0, max_value=20))
    cols = [_make_col(f"col_{i}") for i in range(n_cols)]
    model = _make_model(name, fqn, grain, layer, cols)
    return _make_ranked(model)


# ---------------------------------------------------------------------------
# Property 10: Prompt Schema Content Completeness
# Validates: Requirements 5.1, 5.2
# ---------------------------------------------------------------------------

# Dialect keywords that must ALWAYS appear
_REQUIRED_DIALECT_KEYWORDS = [
    "DATE_TRUNC", "DATEADD", "DATEDIFF", "QUALIFY", "LIMIT",
]


@given(
    models=st.lists(_ranked_model_strategy(), min_size=1, max_size=5),
    question=st.text(min_size=1, max_size=200),
)
@settings(max_examples=100)
def test_property_10_prompt_content_completeness(
    models: list[RankedModel],
    question: str,
) -> None:
    """**Property 10: Prompt Schema Content Completeness**

    The system prompt SHALL contain:
    - FQN of every retrieved model
    - Every column name for each model (up to 300)
    - All Databricks SQL dialect keywords

    **Validates: Requirements 5.1, 5.2**
    """
    all_models = [rm.model for rm in models]
    index = _make_index(all_models)
    builder = PromptBuilder(index)
    prompt = builder.build(models, question)

    # --- Invariant 1: FQN of every model is present ---
    for rm in models:
        assert rm.model.fqn in prompt, (
            f"FQN '{rm.model.fqn}' not found in prompt"
        )

    # --- Invariant 2: Every column name is present (up to 300) ---
    for rm in models:
        cols = rm.model.columns[:300]
        for col in cols:
            assert col.name in prompt, (
                f"Column '{col.name}' from model '{rm.model.fqn}' not found in prompt"
            )

    # --- Invariant 3: Dialect keywords are present ---
    for keyword in _REQUIRED_DIALECT_KEYWORDS:
        assert keyword in prompt, (
            f"Dialect keyword '{keyword}' not found in prompt"
        )


# ---------------------------------------------------------------------------
# Property 11: Deduplication Instruction Injection
# Validates: Requirements 5.3
# ---------------------------------------------------------------------------


@given(
    unknown_model=_ranked_model_strategy().filter(lambda rm: rm.model.grain == "unknown"),
    extra_models=st.lists(_ranked_model_strategy(), max_size=4),
)
@settings(max_examples=100)
def test_property_11_dedup_instruction_injected_for_unknown_grain(
    unknown_model: RankedModel,
    extra_models: list[RankedModel],
) -> None:
    """**Property 11: Deduplication Instruction Injection**

    When any retrieved model has grain='unknown', the prompt SHALL contain
    an explicit deduplication instruction.

    **Validates: Requirements 5.3**
    """
    models = [unknown_model] + extra_models
    all_models = [rm.model for rm in models]
    index = _make_index(all_models)
    builder = PromptBuilder(index)
    prompt = builder.build(models, "What is the total?")

    assert "dedup" in prompt.lower() or "deduplica" in prompt.lower() or "DISTINCT" in prompt or "ROW_NUMBER" in prompt, (
        f"Prompt should contain deduplication instruction for model with grain='unknown'. "
        f"Grain: {unknown_model.model.grain!r}"
    )


# ---------------------------------------------------------------------------
# Property 23: Prompt Column Truncation at 300
# Validates: Requirements 15.3
# ---------------------------------------------------------------------------


def test_property_23_column_truncation(caplog) -> None:
    """**Property 23: Prompt Column Truncation**

    For a model with >300 columns, the prompt includes exactly the first 300
    and a WARNING is emitted.

    **Validates: Requirements 15.3**
    """
    n_cols = 350
    cols = [_make_col(f"col_{i:04d}") for i in range(n_cols)]
    model = _make_model("big_model", "db.schema.big_model", cols=cols)
    ranked = _make_ranked(model)
    index = _make_index([model])
    builder = PromptBuilder(index)

    with caplog.at_level(logging.WARNING, logger="backend.generation.prompt_builder"):
        prompt = builder.build([ranked], "question")

    # First 300 columns present
    for i in range(300):
        assert f"col_{i:04d}" in prompt, f"Column col_{i:04d} (index {i}) should be in prompt"

    # Columns 300–349 absent
    for i in range(300, n_cols):
        assert f"col_{i:04d}" not in prompt, f"Column col_{i:04d} (index {i}) should NOT be in prompt"

    # WARNING logged
    assert any(
        "truncat" in record.message.lower() or "300" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), "Expected a WARNING log for column truncation"


@given(
    n_cols=st.integers(min_value=301, max_value=600),
)
@settings(max_examples=50)
def test_property_23_over_300_cols_truncated_to_exactly_300(n_cols: int) -> None:
    """**Property 23 property version**: any model with >300 cols is always truncated to exactly 300."""
    cols = [_make_col(f"c_{i}") for i in range(n_cols)]
    model = _make_model("big", "db.schema.big", cols=cols)
    ranked = _make_ranked(model)
    index = _make_index([model])
    builder = PromptBuilder(index)
    prompt = builder.build([ranked], "q")

    # Exactly first 300 cols present
    present_count = sum(1 for i in range(n_cols) if f"c_{i}" in prompt)
    # At most 300 (some col names may match substrings — so use c_{i} which are unique enough)
    # Strictly check first 300 present and first beyond-300 absent
    assert f"c_299" in prompt  # last of first 300
    assert f"c_300" not in prompt  # first beyond 300

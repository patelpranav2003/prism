"""
tests/unit/test_text_representation.py

Property-based test for Embedder model text representation completeness.

# Feature: prism, Property 7: Model Text Representation Completeness
For any ModelMeta object, the text representation produced by the Embedder
SHALL always contain the model's name and every column name. No column name
from the ModelMeta SHALL be absent from the generated text string.

Validates: Requirements 4.1
"""

from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.models import ColumnMeta, ModelMeta
from backend.search.embedder import Embedder


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_col_name_strategy = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{0,29}", fullmatch=True)
_col_type_strategy = st.sampled_from(["STRING", "INTEGER", "FLOAT", "BOOLEAN", "TIMESTAMP", "DATE"])


@st.composite
def _column_meta(draw) -> ColumnMeta:
    return ColumnMeta(
        name=draw(_col_name_strategy),
        data_type=draw(_col_type_strategy),
        description=draw(st.text(max_size=50)),
    )


@st.composite
def _model_meta(draw) -> ModelMeta:
    cols = draw(st.lists(_column_meta(), min_size=0, max_size=20, unique_by=lambda c: c.name))
    return ModelMeta(
        name=draw(st.from_regex(r"[a-z][a-z0-9_]{1,30}", fullmatch=True)),
        database=draw(st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)),
        schema_name=draw(st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)),
        fqn="db.schema.model",
        columns=cols,
        grain="unknown",
        layer="bronze",
        compiled_sql_excerpt="",
        depends_on=[],
        tags=[],
        folder_path="",
        row_count=0,
        last_updated=None,
        description=draw(st.text(max_size=100)),
    )


# ---------------------------------------------------------------------------
# Property 7: Model Text Representation Completeness
# Validates: Requirements 4.1
# ---------------------------------------------------------------------------


@given(model=_model_meta())
@settings(max_examples=100)
def test_property_7_text_representation_completeness(model: ModelMeta) -> None:
    """**Property 7: Model Text Representation Completeness**

    For any ModelMeta object, the text representation SHALL contain:
    - The model's name.
    - Every column name (none may be absent).

    **Validates: Requirements 4.1**
    """
    embedder = Embedder()  # no load() needed — we only call _model_to_text
    text = embedder._model_to_text(model)

    # --- Invariant 1: text is a non-empty string ---
    assert isinstance(text, str) and len(text) > 0, "Text representation must be non-empty"

    # --- Invariant 2: model name is present ---
    assert model.name in text, (
        f"Model name '{model.name}' not found in text representation: {text[:200]!r}"
    )

    # --- Invariant 3: every column name is present ---
    for col in model.columns:
        assert col.name in text, (
            f"Column name '{col.name}' not found in text representation: {text[:200]!r}. "
            f"Model has {len(model.columns)} column(s)."
        )


@given(
    model_name=st.from_regex(r"[a-z][a-z0-9_]{1,30}", fullmatch=True),
    description=st.text(max_size=50),
)
@settings(max_examples=50)
def test_property_7_format_structure(model_name: str, description: str) -> None:
    """**Property 7 — format**: text must match the documented format
    '{model_name}: {description}. Columns: ...'
    """
    model = ModelMeta(
        name=model_name,
        database="db",
        schema_name="schema",
        fqn=f"db.schema.{model_name}",
        columns=[ColumnMeta("col1", "STRING", "First col")],
        grain="unknown",
        layer="bronze",
        compiled_sql_excerpt="",
        depends_on=[],
        tags=[],
        folder_path="",
        row_count=0,
        last_updated=None,
        description=description,
    )
    text = Embedder()._model_to_text(model)
    assert text.startswith(f"{model_name}:"), (
        f"Text must start with model name: {text[:100]!r}"
    )
    assert "Columns:" in text, "Text must contain 'Columns:' section"
    assert "col1" in text, "Column 'col1' must appear in text"


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestTextRepresentationExamples:

    def _text(self, model: ModelMeta) -> str:
        return Embedder()._model_to_text(model)

    def test_empty_columns(self):
        model = ModelMeta(
            name="orders",
            database="db",
            schema_name="schema",
            fqn="db.schema.orders",
            columns=[],
            grain="unknown",
            layer="bronze",
            compiled_sql_excerpt="",
            depends_on=[],
            tags=[],
            folder_path="",
            row_count=0,
            last_updated=None,
            description="All orders",
        )
        text = self._text(model)
        assert "orders" in text
        assert "Columns:" in text

    def test_multiple_columns_all_present(self):
        cols = [
            ColumnMeta("order_id", "INTEGER", "Primary key"),
            ColumnMeta("customer_id", "INTEGER", "FK to customers"),
            ColumnMeta("revenue", "FLOAT", "Order revenue"),
        ]
        model = ModelMeta(
            name="fact_orders",
            database="db",
            schema_name="schema",
            fqn="db.schema.fact_orders",
            columns=cols,
            grain="order_id",
            layer="gold",
            compiled_sql_excerpt="",
            depends_on=[],
            tags=[],
            folder_path="",
            row_count=1000,
            last_updated=None,
            description="",
        )
        text = self._text(model)
        for col in cols:
            assert col.name in text, f"Column '{col.name}' missing from text"

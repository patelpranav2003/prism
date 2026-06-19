"""
tests/unit/test_layer_inference.py

Property-based test for IndexBuilder layer inference priority order.

# Feature: prism, Property 3: Layer Inference Priority Order
For any model entry with any combination of tags and folder path, the inferred
layer SHALL always follow the priority order: tag match → folder path match →
default "bronze". Specifically, if a gold/silver/bronze tag is present, it
always wins over folder path, regardless of what the folder path contains;
and if neither tag nor folder path matches, the result is always "bronze".

Validates: Requirements 3.3
"""

from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.discovery.index_builder import _infer_layer
from backend.models import ColumnMeta, ModelMeta


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_model(tags: list[str], folder_path: str) -> ModelMeta:
    return ModelMeta(
        name="test_model",
        database="db",
        schema_name="schema",
        fqn="db.schema.test_model",
        columns=[],
        grain="unknown",
        layer="bronze",  # will be overwritten
        compiled_sql_excerpt="",
        depends_on=[],
        tags=tags,
        folder_path=folder_path,
        row_count=0,
        last_updated=None,
        description="",
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_layer_keyword = st.sampled_from(["gold", "silver", "bronze"])
_non_layer_tag = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{1,10}", fullmatch=True).filter(
    lambda s: s.lower() not in ("gold", "silver", "bronze")
)

# Tags that contain a layer keyword
_layer_tag = st.one_of(
    _layer_keyword,  # exact match
    st.builds(lambda kw, prefix: f"{prefix}_{kw}", kw=_layer_keyword, prefix=_non_layer_tag),
    st.builds(lambda kw, suffix: f"{kw}_{suffix}", kw=_layer_keyword, suffix=_non_layer_tag),
)

# Folder paths that contain / don't contain a layer keyword
_layer_folder = st.one_of(
    _layer_keyword.map(lambda kw: f"models/{kw}"),
    _layer_keyword.map(lambda kw: f"dbt/{kw}/orders"),
)
_non_layer_folder = st.from_regex(r"[a-zA-Z][a-zA-Z0-9/_\-]{0,30}", fullmatch=True).filter(
    lambda s: not any(kw in s.lower() for kw in ("gold", "silver", "bronze"))
)


# ---------------------------------------------------------------------------
# Property 3: Layer Inference Priority Order
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------


@given(
    layer_tag=_layer_tag,
    other_tags=st.lists(_non_layer_tag, max_size=5),
    folder_path=st.one_of(_layer_folder, _non_layer_folder, st.just("")),
)
@settings(max_examples=100)
def test_property_3a_tag_always_beats_folder(
    layer_tag: str,
    other_tags: list[str],
    folder_path: str,
) -> None:
    """**Property 3a**: When a tag contains a layer keyword, the tag always wins
    over the folder path regardless of what the folder path says.

    **Validates: Requirements 3.3 (priority 1 > priority 2)**
    """
    tags = [layer_tag] + other_tags
    model = _make_model(tags, folder_path)
    result = _infer_layer(model)

    # Identify the expected layer from the tag
    expected = None
    for kw in ("gold", "silver", "bronze"):
        if any(kw in t.lower() for t in tags):
            expected = kw
            break

    assert result == expected, (
        f"Tag '{layer_tag}' should have forced layer='{expected}', "
        f"but got '{result}' (folder_path={folder_path!r})"
    )


@given(
    folder_path=_layer_folder,
    tags=st.lists(_non_layer_tag, max_size=5),
)
@settings(max_examples=100)
def test_property_3b_folder_wins_when_no_layer_tag(
    folder_path: str,
    tags: list[str],
) -> None:
    """**Property 3b**: When no tag has a layer keyword but the folder path does,
    the folder path determines the layer.

    **Validates: Requirements 3.3 (priority 2 > default)**
    """
    model = _make_model(tags, folder_path)
    result = _infer_layer(model)

    # Identify expected layer from folder
    expected = None
    for kw in ("gold", "silver", "bronze"):
        if kw in folder_path.lower():
            expected = kw
            break

    assert result == expected, (
        f"Folder path '{folder_path}' should have given layer='{expected}', "
        f"but got '{result}' (tags={tags!r})"
    )


@given(
    tags=st.lists(_non_layer_tag, max_size=5),
    folder_path=_non_layer_folder,
)
@settings(max_examples=100)
def test_property_3c_default_is_bronze(
    tags: list[str],
    folder_path: str,
) -> None:
    """**Property 3c**: When neither tags nor folder path contain a layer keyword,
    the result is always "bronze" (the default).

    **Validates: Requirements 3.3 (priority 3: default)**
    """
    model = _make_model(tags, folder_path)
    result = _infer_layer(model)
    assert result == "bronze", (
        f"With no layer keyword in tags={tags!r} or folder={folder_path!r}, "
        f"expected 'bronze', got '{result}'"
    )


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestLayerInferenceExamples:

    def test_gold_tag_exact(self):
        model = _make_model(["gold"], "models/silver")
        assert _infer_layer(model) == "gold"

    def test_silver_tag_beats_gold_folder(self):
        model = _make_model(["silver_certified"], "models/gold")
        assert _infer_layer(model) == "silver"

    def test_bronze_tag_beats_gold_folder(self):
        model = _make_model(["bronze"], "models/gold")
        assert _infer_layer(model) == "bronze"

    def test_folder_gold_no_tags(self):
        model = _make_model([], "models/gold/sales")
        assert _infer_layer(model) == "gold"

    def test_folder_silver_no_tags(self):
        model = _make_model([], "dbt/silver")
        assert _infer_layer(model) == "silver"

    def test_no_tag_no_folder_defaults_bronze(self):
        model = _make_model([], "models/staging/orders")
        assert _infer_layer(model) == "bronze"

    def test_empty_everything_defaults_bronze(self):
        model = _make_model([], "")
        assert _infer_layer(model) == "bronze"

    def test_tag_priority_gold_over_silver_tag(self):
        """gold appears first in _LAYER_KEYWORDS, so it wins."""
        model = _make_model(["gold_layer", "silver_certified"], "")
        assert _infer_layer(model) == "gold"

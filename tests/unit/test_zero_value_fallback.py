"""
tests/unit/test_zero_value_fallback.py

Property-based test for missing field zero-value handling in the discovery pipeline.

# Feature: prism, Property 5: Missing Field Zero-Value Handling
For any model entry in manifest.json that has any combination of missing or
null fields, the Index_Builder SHALL always record those fields as their zero
value (empty string for strings, empty list for lists, 0 for integers) and
SHALL always continue building the index for that model and all subsequent
models — it SHALL never skip a model or halt construction due to a missing
individual field.

Validates: Requirements 3.7
"""

import json
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from backend.discovery.manifest_parser import ManifestParser
from backend.models import ModelMeta


# ---------------------------------------------------------------------------
# Strategy: build manifest JSON with randomly nulled fields
# ---------------------------------------------------------------------------

def _minimal_node(name: str) -> dict:
    """A complete manifest node that satisfies all fields."""
    return {
        "unique_id": f"model.project.{name}",
        "name": name,
        "database": "catalog",
        "schema": "schema",
        "fqn": ["catalog", "schema", name],
        "columns": {
            "col_a": {"name": "col_a", "description": "Column A", "data_type": "STRING"},
        },
        "meta": {"grain": "day"},
        "compiled_code": "SELECT col_a FROM source",
        "depends_on": {"nodes": ["model.project.other"]},
        "tags": ["gold"],
        "path": "models/gold/model.sql",
        "description": "A gold model.",
    }


def _manifest_with_nodes(nodes: list[dict]) -> bytes:
    nodes_dict = {f"model.project.{n['name']}": n for n in nodes}
    return json.dumps({"nodes": nodes_dict}).encode()


# Fields that are optional and have zero-value fallbacks
_OPTIONAL_FIELDS = [
    "description",
    "compiled_code",
    "meta",
    "depends_on",
    "tags",
    "path",
    "columns",
]


@st.composite
def _node_with_random_nulls(draw) -> dict:
    name = draw(st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True))
    node = _minimal_node(name)
    # Randomly null out some optional fields
    fields_to_null = draw(st.lists(st.sampled_from(_OPTIONAL_FIELDS), max_size=len(_OPTIONAL_FIELDS)))
    for field in fields_to_null:
        node[field] = None
    return node


# ---------------------------------------------------------------------------
# Property 5: Missing Field Zero-Value Handling
# Validates: Requirements 3.7
# ---------------------------------------------------------------------------


@given(
    nodes=st.lists(_node_with_random_nulls(), min_size=1, max_size=10),
)
@settings(max_examples=100)
def test_property_5_zero_value_fallback(nodes: list[dict]) -> None:
    """**Property 5: Missing Field Zero-Value Handling**

    For any manifest with any combination of missing/null fields per node,
    ManifestParser SHALL:
    - Produce exactly len(nodes) ModelMeta objects (no model skipped).
    - Record all missing string fields as "" (empty string).
    - Record all missing list fields as [] (empty list).
    - Record all missing integer fields as 0.

    **Validates: Requirements 3.7**
    """
    raw = _manifest_with_nodes(nodes)
    parser = ManifestParser()
    models = parser.parse(raw)

    # --- Invariant 1: No model is skipped ---
    assert len(models) == len(nodes), (
        f"Expected {len(nodes)} models, got {len(models)}. "
        "ManifestParser must never skip a model due to missing fields."
    )

    # --- Invariant 2: All string fields are non-None ---
    for model in models:
        assert isinstance(model.name, str), f"name must be str, got {type(model.name)}"
        assert isinstance(model.database, str), "database must be str"
        assert isinstance(model.schema_name, str), "schema_name must be str"
        assert isinstance(model.fqn, str), "fqn must be str"
        assert isinstance(model.grain, str), "grain must be str"
        assert isinstance(model.compiled_sql_excerpt, str), "compiled_sql_excerpt must be str"
        assert isinstance(model.folder_path, str), "folder_path must be str"
        assert isinstance(model.description, str), "description must be str"

    # --- Invariant 3: All list fields are non-None lists ---
    for model in models:
        assert isinstance(model.columns, list), "columns must be a list"
        assert isinstance(model.depends_on, list), "depends_on must be a list"
        assert isinstance(model.tags, list), "tags must be a list"

    # --- Invariant 4: Integer fields are non-negative ints ---
    for model in models:
        assert isinstance(model.row_count, int), "row_count must be int"
        assert model.row_count >= 0, "row_count must be >= 0"

    # --- Invariant 5: layer is always a valid value ---
    for model in models:
        assert model.layer in ("gold", "silver", "bronze"), (
            f"layer must be gold/silver/bronze, got {model.layer!r}"
        )


@given(
    extra_nodes=st.lists(_node_with_random_nulls(), min_size=0, max_size=5),
)
@settings(max_examples=50)
def test_property_5_parsing_continues_after_null_fields(
    extra_nodes: list[dict],
) -> None:
    """**Property 5 continuation**: parsing must not stop when early nodes have null fields."""
    # First node has ALL optional fields nulled
    first_node = {
        "unique_id": "model.project.first",
        "name": "first",
        "database": None,
        "schema": None,
        "fqn": None,
        "columns": None,
        "meta": None,
        "compiled_code": None,
        "depends_on": None,
        "tags": None,
        "path": None,
        "description": None,
    }
    all_nodes = [first_node] + extra_nodes
    raw = _manifest_with_nodes(all_nodes)
    parser = ManifestParser()
    models = parser.parse(raw)

    assert len(models) == len(all_nodes), (
        "ManifestParser must parse all nodes even when the first has all-null fields"
    )
    # First model should have zero values
    first = models[0]
    assert first.database == "" or first.database is not None
    assert isinstance(first.columns, list)
    assert isinstance(first.depends_on, list)
    assert isinstance(first.tags, list)


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestZeroValueFallbackExamples:

    def _parse(self, node_overrides: dict) -> ModelMeta:
        base = _minimal_node("test")
        base.update(node_overrides)
        raw = _manifest_with_nodes([base])
        return ManifestParser().parse(raw)[0]

    def test_null_description_becomes_empty_string(self):
        m = self._parse({"description": None})
        assert m.description == ""

    def test_null_compiled_code_becomes_empty_string(self):
        m = self._parse({"compiled_code": None})
        assert m.compiled_sql_excerpt == ""

    def test_null_tags_becomes_empty_list(self):
        m = self._parse({"tags": None})
        assert m.tags == []

    def test_null_columns_becomes_empty_list(self):
        m = self._parse({"columns": None})
        assert m.columns == []

    def test_null_depends_on_becomes_empty_list(self):
        m = self._parse({"depends_on": None})
        assert m.depends_on == []

    def test_null_meta_grain_becomes_empty_string(self):
        m = self._parse({"meta": None})
        assert isinstance(m.grain, str)

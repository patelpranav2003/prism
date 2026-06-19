"""
tests/unit/test_column_preservation.py

Property-based test for column name round-trip fidelity.

# Feature: prism, Property 6: Column Name Preservation (Round-Trip Fidelity)
For any manifest.json and catalog.json content, every column name that appears
in those source files SHALL appear in the SchemaIndex with the exact same
string — no case transformation, whitespace normalisation, or renaming applied.
The set of column names in the index SHALL be identical to the set in the
sources.

Validates: Requirements 3.2, 15.1
"""

import json
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from backend.discovery.catalog_parser import CatalogParser
from backend.discovery.manifest_parser import ManifestParser


# ---------------------------------------------------------------------------
# Strategies — column names with potentially tricky characters
# ---------------------------------------------------------------------------

_column_name_strategy = st.one_of(
    # Simple snake_case names
    st.from_regex(r"[a-z][a-z0-9_]{0,29}", fullmatch=True),
    # Mixed-case names (should NOT be normalised)
    st.from_regex(r"[A-Za-z][A-Za-z0-9_]{0,29}", fullmatch=True),
    # Names with spaces (rare but valid in some warehouses)
    st.from_regex(r"[a-zA-Z][a-zA-Z0-9 _]{0,20}", fullmatch=True),
)


def _make_manifest_with_columns(model_name: str, col_names: list[str]) -> bytes:
    columns = {
        name: {"name": name, "description": f"Desc of {name}", "data_type": "TEXT"}
        for name in col_names
    }
    node = {
        "unique_id": f"model.project.{model_name}",
        "name": model_name,
        "database": "db",
        "schema": "schema",
        "fqn": ["db", "schema", model_name],
        "columns": columns,
        "meta": {},
        "compiled_code": "",
        "depends_on": {"nodes": []},
        "tags": [],
        "path": "models/staging/model.sql",
        "description": "Test model",
    }
    return json.dumps({"nodes": {f"model.project.{model_name}": node}}).encode()


def _make_catalog_with_columns(model_name: str, col_names: list[str]) -> bytes:
    columns = {
        name: {"type": "STRING", "metadata": {}}
        for name in col_names
    }
    stats = {
        "row_count": {"value": 100, "include": True, "id": "row_count", "label": "Row Count"},
        "last_modified": {"value": None, "include": False, "id": "last_modified", "label": "Last Modified"},
    }
    node = {
        "unique_id": f"model.project.{model_name}",
        "metadata": {"type": "table", "name": model_name, "schema": "schema", "database": "db"},
        "columns": columns,
        "stats": stats,
    }
    return json.dumps({"nodes": {f"model.project.{model_name}": node}}).encode()


# ---------------------------------------------------------------------------
# Property 6: Column Name Preservation
# Validates: Requirements 3.2, 15.1
# ---------------------------------------------------------------------------


@given(
    model_name=st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True),
    col_names=st.lists(
        _column_name_strategy,
        min_size=1,
        max_size=20,
        unique=True,
    ),
)
@settings(max_examples=100)
def test_property_6a_manifest_column_names_preserved(
    model_name: str,
    col_names: list[str],
) -> None:
    """**Property 6a**: Column names from manifest.json appear verbatim in the
    parsed ModelMeta — no case transformation or normalisation.

    **Validates: Requirements 3.2, 15.1**
    """
    raw = _make_manifest_with_columns(model_name, col_names)
    models = ManifestParser().parse(raw)
    assert len(models) == 1

    parsed_names = [c.name for c in models[0].columns]

    # Every source column name must appear exactly in the parsed output
    for name in col_names:
        assert name in parsed_names, (
            f"Column name {name!r} from manifest was not found in parsed columns. "
            f"Parsed names: {parsed_names!r}"
        )

    # No extra column names introduced
    for name in parsed_names:
        assert name in col_names, (
            f"Extra column name {name!r} in parsed output — not in source: {col_names!r}"
        )


@given(
    model_name=st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True),
    manifest_col_names=st.lists(
        st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True),
        min_size=1,
        max_size=15,
        unique=True,
    ),
)
@settings(max_examples=100)
def test_property_6b_catalog_merge_preserves_column_names(
    model_name: str,
    manifest_col_names: list[str],
) -> None:
    """**Property 6b**: After CatalogParser.merge(), all column names from the
    manifest are preserved exactly — not normalised, even though catalog lookup
    is case-insensitive.

    **Validates: Requirements 3.2, 15.1**
    """
    manifest_raw = _make_manifest_with_columns(model_name, manifest_col_names)
    catalog_raw = _make_catalog_with_columns(model_name, manifest_col_names)

    models = ManifestParser().parse(manifest_raw)
    merged = CatalogParser().merge(models, catalog_raw)

    assert len(merged) == 1
    result_names = [c.name for c in merged[0].columns]

    # Every manifest column name must survive the merge unchanged
    for name in manifest_col_names:
        assert name in result_names, (
            f"Column name {name!r} was lost or transformed during catalog merge. "
            f"Merged names: {result_names!r}"
        )

    # The merge must not add new columns from the catalog
    for name in result_names:
        assert name in manifest_col_names, (
            f"Column {name!r} appeared after merge but was not in manifest: "
            f"{manifest_col_names!r}"
        )


@given(
    model_name=st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True),
    col_names=st.lists(
        st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True),
        min_size=1,
        max_size=10,
        unique=True,
    ),
)
@settings(max_examples=100)
def test_property_6c_no_case_transformation(
    model_name: str,
    col_names: list[str],
) -> None:
    """**Property 6c**: Column names must never be uppercased, lowercased, or
    otherwise case-transformed anywhere in the pipeline.

    **Validates: Requirements 15.1**
    """
    # Use mixed-case names to catch any normalisation
    mixed_names = [n[:1].upper() + n[1:] for n in col_names]  # capitalise first letter
    raw = _make_manifest_with_columns(model_name, mixed_names)
    models = ManifestParser().parse(raw)

    parsed_names = [c.name for c in models[0].columns]
    for original, parsed in zip(mixed_names, parsed_names):
        assert original == parsed, (
            f"Column name was transformed: {original!r} → {parsed!r}"
        )

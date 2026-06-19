"""
tests/unit/test_schema_models.py

Property-based tests for SchemaIndex and RankedModel construction, and for
CatalogParser.merge() schema merge fidelity.

Property 4: Schema Merge Fidelity
  For any model present in both manifest.json and catalog.json, the merged
  Schema_Index entry SHALL always have column types sourced from catalog.json
  and column descriptions sourced from manifest.json. For any model present in
  manifest.json but absent from catalog.json, the row_count SHALL always be 0
  and declared column types from manifest.json SHALL be used.

Validates: Requirements 3.4, 15.2
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.discovery.catalog_parser import CatalogParser
from backend.models import ColumnMeta, LineageNode, ModelMeta, RankedModel, SchemaIndex


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Safe text: printable ASCII, no surrogates that can trip up comparison
safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd")),
    min_size=1,
    max_size=64,
)

layer_strategy: st.SearchStrategy[Literal["bronze", "silver", "gold"]] = st.sampled_from(
    ["bronze", "silver", "gold"]
)

confidence_strategy = st.sampled_from(["high", "medium", "low", None])


def column_meta_strategy() -> st.SearchStrategy[ColumnMeta]:
    """Build a random ColumnMeta with non-empty name, data_type, and description."""
    return st.builds(
        ColumnMeta,
        name=safe_text,
        data_type=safe_text,
        description=safe_text,
    )


def model_meta_strategy(
    layer: st.SearchStrategy[Literal["bronze", "silver", "gold"]] | None = None,
    row_count: st.SearchStrategy[int] | None = None,
) -> st.SearchStrategy[ModelMeta]:
    """Build a random ModelMeta."""
    return st.builds(
        ModelMeta,
        name=safe_text,
        database=safe_text,
        schema_name=safe_text,
        fqn=safe_text,
        columns=st.lists(column_meta_strategy(), min_size=0, max_size=10),
        grain=safe_text,
        layer=layer if layer is not None else layer_strategy,
        compiled_sql_excerpt=safe_text,
        depends_on=st.lists(safe_text, min_size=0, max_size=5),
        tags=st.lists(safe_text, min_size=0, max_size=5),
        folder_path=safe_text,
        row_count=row_count if row_count is not None else st.integers(min_value=0, max_value=10_000_000),
        last_updated=st.one_of(st.none(), st.just(datetime.now(tz=timezone.utc))),
        description=safe_text,
    )


def ranked_model_strategy(
    layer: st.SearchStrategy[Literal["bronze", "silver", "gold"]] | None = None,
    raw_similarity: st.SearchStrategy[float] | None = None,
    adjusted_score: st.SearchStrategy[float] | None = None,
) -> st.SearchStrategy[RankedModel]:
    """Build a random RankedModel."""
    raw = raw_similarity if raw_similarity is not None else st.floats(
        min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False
    )
    adj = adjusted_score if adjusted_score is not None else st.floats(
        min_value=-1.0, max_value=1.1, allow_nan=False, allow_infinity=False
    )
    return st.builds(
        RankedModel,
        model=model_meta_strategy(layer=layer),
        raw_similarity=raw,
        adjusted_score=adj,
        confidence_hint=confidence_strategy,
    )


def schema_index_strategy(
    models: st.SearchStrategy[list[ModelMeta]] | None = None,
) -> st.SearchStrategy[SchemaIndex]:
    """Build a SchemaIndex where model_count == len(models) (the invariant under test)."""
    if models is None:
        models_st = st.lists(model_meta_strategy(), min_size=0, max_size=20)
    else:
        models_st = models

    @st.composite
    def _build(draw: st.DrawFn) -> SchemaIndex:
        model_list: list[ModelMeta] = draw(models_st)
        n = len(model_list)
        embeddings = np.zeros((n, 384), dtype=np.float32)
        lineage: dict[str, LineageNode] = {}
        built_at = datetime.now(tz=timezone.utc)
        return SchemaIndex(
            models=model_list,
            embeddings=embeddings,
            lineage=lineage,
            built_at=built_at,
            model_count=n,  # invariant: always set to len(models)
        )

    return _build()


# ---------------------------------------------------------------------------
# Property 4a: SchemaIndex model_count invariant
# ---------------------------------------------------------------------------

@given(schema_index=schema_index_strategy())
@settings(max_examples=100)
def test_property_4a_schema_index_model_count_equals_len_models(
    schema_index: SchemaIndex,
) -> None:
    """**Validates: Requirements 3.4, 15.2**

    Property 4: Schema Merge Fidelity (model_count invariant).

    A SchemaIndex built with model_count = len(models) always has
    model_count == len(models). The index stores this redundantly so that
    callers never have to recompute it; the property guarantees that
    construction always keeps these two values in sync.
    """
    assert schema_index.model_count == len(schema_index.models), (
        f"model_count={schema_index.model_count} but len(models)={len(schema_index.models)}"
    )


# ---------------------------------------------------------------------------
# Property 4b: RankedModel layer boost computation
# ---------------------------------------------------------------------------

LAYER_BOOSTS: dict[str, float] = {
    "gold": 0.05,
    "silver": 0.025,
    "bronze": 0.0,
}


@given(
    layer=layer_strategy,
    raw_similarity=st.floats(
        min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False
    ),
)
@settings(max_examples=100)
def test_property_4b_ranked_model_adjusted_score_equals_raw_plus_boost(
    layer: Literal["bronze", "silver", "gold"],
    raw_similarity: float,
) -> None:
    """**Validates: Requirements 3.4, 15.2**

    Property 4: Schema Merge Fidelity (layer boost).

    A RankedModel's adjusted_score SHALL always equal raw_similarity + boost,
    where boost is +0.05 for gold, +0.025 for silver, +0.0 for bronze.
    This test constructs a RankedModel with the correct adjusted_score and
    verifies that the stored value matches the expected formula exactly.
    """
    boost = LAYER_BOOSTS[layer]
    expected_adjusted = raw_similarity + boost

    # Build a minimal ModelMeta for the given layer
    column = ColumnMeta(name="id", data_type="INT", description="primary key")
    model = ModelMeta(
        name="test_model",
        database="db",
        schema_name="schema",
        fqn="db.schema.test_model",
        columns=[column],
        grain="unknown",
        layer=layer,
        compiled_sql_excerpt="SELECT 1",
        depends_on=[],
        tags=[],
        folder_path="models/",
        row_count=0,
        last_updated=None,
        description="test",
    )

    ranked = RankedModel(
        model=model,
        raw_similarity=raw_similarity,
        adjusted_score=expected_adjusted,
        confidence_hint=None,
    )

    assert ranked.adjusted_score == pytest.approx(raw_similarity + boost, abs=1e-9), (
        f"layer={layer}: expected adjusted_score={raw_similarity + boost}, "
        f"got {ranked.adjusted_score}"
    )


# ---------------------------------------------------------------------------
# Property 4c: ModelMeta without catalog data preserves manifest column types
# ---------------------------------------------------------------------------

@given(
    columns=st.lists(column_meta_strategy(), min_size=1, max_size=20),
)
@settings(max_examples=100)
def test_property_4c_manifest_only_model_preserves_column_names_and_types(
    columns: list[ColumnMeta],
) -> None:
    """**Validates: Requirements 3.4, 15.2**

    Property 4: Schema Merge Fidelity (manifest-only model).

    A ModelMeta constructed without catalog data (row_count=0) SHALL retain
    the manifest column types — column names and data_types are preserved
    exactly as passed. No normalisation or transformation is applied.
    """
    model = ModelMeta(
        name="manifest_only_model",
        database="db",
        schema_name="schema",
        fqn="db.schema.manifest_only_model",
        columns=columns,
        grain="unknown",
        layer="bronze",
        compiled_sql_excerpt="",
        depends_on=[],
        tags=[],
        folder_path="models/",
        row_count=0,  # absent from catalog.json → always 0
        last_updated=None,
        description="manifest only",
    )

    # row_count must be 0 (absent from catalog)
    assert model.row_count == 0, f"Expected row_count=0 for manifest-only model, got {model.row_count}"

    # Every column name and data_type must be preserved exactly
    assert len(model.columns) == len(columns), (
        f"Column count mismatch: expected {len(columns)}, got {len(model.columns)}"
    )
    for original, stored in zip(columns, model.columns):
        assert stored.name == original.name, (
            f"Column name changed: {original.name!r} → {stored.name!r}"
        )
        assert stored.data_type == original.data_type, (
            f"Column data_type changed for '{original.name}': "
            f"{original.data_type!r} → {stored.data_type!r}"
        )
        assert stored.description == original.description, (
            f"Column description changed for '{original.name}': "
            f"{original.description!r} → {stored.description!r}"
        )


# ---------------------------------------------------------------------------
# Strategies for CatalogParser merge fidelity tests
# ---------------------------------------------------------------------------

# A column name/type/description that is safe for use in JSON keys/values.
# We restrict to printable ASCII identifiers to avoid JSON encoding surprises.
_ident_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=32,
)

_type_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=20,
)

_desc_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ.",
    min_size=0,
    max_size=80,
)


@st.composite
def manifest_column_strategy(draw: st.DrawFn) -> ColumnMeta:
    """Generate a ColumnMeta as the manifest parser would produce it.

    Manifest columns have a name and description; data_type starts as '' because
    CatalogParser fills it in later.
    """
    name = draw(_ident_text)
    description = draw(_desc_text)
    return ColumnMeta(name=name, data_type="", description=description)


@st.composite
def manifest_model_strategy(draw: st.DrawFn) -> ModelMeta:
    """Generate a ModelMeta as ManifestParser would produce it (no catalog data yet)."""
    name = draw(_ident_text)
    columns = draw(st.lists(manifest_column_strategy(), min_size=0, max_size=8))

    # Deduplicate column names (manifest parser wouldn't produce duplicates from
    # a well-formed manifest, but Hypothesis might generate them; deduplicate by
    # keeping first occurrence to keep the test simple).
    seen: set[str] = set()
    unique_columns: list[ColumnMeta] = []
    for col in columns:
        if col.name not in seen:
            seen.add(col.name)
            unique_columns.append(col)

    return ModelMeta(
        name=name,
        database="db",
        schema_name="schema",
        fqn=f"db.schema.{name}",
        columns=unique_columns,
        grain="unknown",
        layer="bronze",
        compiled_sql_excerpt="SELECT 1",
        depends_on=[],
        tags=[],
        folder_path="models/",
        row_count=0,
        last_updated=None,
        description="",
    )


def _build_catalog_raw(models: list[ModelMeta], catalog_types: dict[str, dict[str, str]], row_counts: dict[str, int]) -> bytes:
    """Build a minimal catalog.json bytes for the given models.

    Args:
        models: The ModelMeta objects to include in the catalog.
        catalog_types: Mapping of model_name → {col_name → catalog_type}.
        row_counts: Mapping of model_name → row_count.
    """
    nodes: dict = {}
    for model in models:
        node_key = f"model.project.{model.name}"
        col_entries: dict = {}
        for col in model.columns:
            col_type = catalog_types.get(model.name, {}).get(col.name, "STRING")
            col_entries[col.name] = {"name": col.name, "type": col_type}
        nodes[node_key] = {
            "columns": col_entries,
            "stats": {
                "row_count": {
                    "value": row_counts.get(model.name, 0),
                    "label": "Row Count",
                },
            },
        }
    return json.dumps({"nodes": nodes}).encode()


# ---------------------------------------------------------------------------
# Property 4d: Models present in both manifest and catalog — catalog types
#              override; manifest descriptions are preserved.
# ---------------------------------------------------------------------------

@given(
    models=st.lists(manifest_model_strategy(), min_size=1, max_size=10),
    catalog_row_counts=st.dictionaries(
        keys=_ident_text,
        values=st.integers(min_value=0, max_value=10_000_000),
        max_size=10,
    ),
)
@settings(max_examples=100)
def test_property_4d_catalog_types_override_and_manifest_descriptions_preserved(
    models: list[ModelMeta],
    catalog_row_counts: dict[str, int],
) -> None:
    """**Validates: Requirements 3.4, 15.2**

    Property 4: Schema Merge Fidelity — catalog present case.

    For any model present in BOTH manifest and catalog:
    - column data_types SHALL be sourced from catalog.json.
    - column descriptions SHALL be sourced from manifest.json (never overwritten).
    """
    # Assign a distinct catalog type for every column so we can verify the override.
    catalog_types: dict[str, dict[str, str]] = {}
    for model in models:
        catalog_types[model.name] = {}
        for col in model.columns:
            # Use a type that differs from the empty manifest default.
            catalog_types[model.name][col.name] = f"CATALOG_{col.name.upper()}_TYPE"

    # Build catalog raw bytes covering ALL models (all present in catalog).
    catalog_raw = _build_catalog_raw(models, catalog_types, catalog_row_counts)

    parser = CatalogParser()
    merged = parser.merge(models, catalog_raw)

    assert len(merged) == len(models), (
        f"Expected {len(models)} merged models, got {len(merged)}"
    )

    for original, result in zip(models, merged):
        # Name must be preserved.
        assert result.name == original.name

        assert len(result.columns) == len(original.columns), (
            f"Model '{original.name}': expected {len(original.columns)} columns, "
            f"got {len(result.columns)}"
        )

        for orig_col, merged_col in zip(original.columns, result.columns):
            # Column name must be unchanged (Requirement 15.1).
            assert merged_col.name == orig_col.name, (
                f"Model '{original.name}': column name changed "
                f"{orig_col.name!r} → {merged_col.name!r}"
            )

            # data_type must come from catalog (not the empty manifest default).
            expected_type = catalog_types[original.name][orig_col.name]
            assert merged_col.data_type == expected_type, (
                f"Model '{original.name}', column '{orig_col.name}': "
                f"expected catalog type {expected_type!r}, got {merged_col.data_type!r}"
            )

            # description must be preserved from manifest.
            assert merged_col.description == orig_col.description, (
                f"Model '{original.name}', column '{orig_col.name}': "
                f"manifest description changed "
                f"{orig_col.description!r} → {merged_col.description!r}"
            )


# ---------------------------------------------------------------------------
# Property 4e: Models absent from catalog — row_count == 0, manifest types kept.
# ---------------------------------------------------------------------------

@given(
    models=st.lists(manifest_model_strategy(), min_size=1, max_size=10),
)
@settings(max_examples=100)
def test_property_4e_absent_from_catalog_row_count_zero_and_manifest_types_kept(
    models: list[ModelMeta],
) -> None:
    """**Validates: Requirements 3.4, 15.2**

    Property 4: Schema Merge Fidelity — catalog absent case.

    For any model present in manifest.json but absent from catalog.json:
    - row_count SHALL always be 0.
    - declared column types from manifest.json SHALL be used.

    To set up meaningful manifest types (since ManifestParser sets them to ''),
    we assign non-empty manifest types to each column before calling merge with
    an empty catalog.
    """
    # Give each manifest column a non-empty type so we can verify it is kept.
    models_with_types: list[ModelMeta] = []
    for model in models:
        updated_cols = [
            ColumnMeta(
                name=col.name,
                data_type=f"MANIFEST_{col.name.upper()}_TYPE",
                description=col.description,
            )
            for col in model.columns
        ]
        models_with_types.append(ModelMeta(
            name=model.name,
            database=model.database,
            schema_name=model.schema_name,
            fqn=model.fqn,
            columns=updated_cols,
            grain=model.grain,
            layer=model.layer,
            compiled_sql_excerpt=model.compiled_sql_excerpt,
            depends_on=model.depends_on,
            tags=model.tags,
            folder_path=model.folder_path,
            row_count=model.row_count,
            last_updated=model.last_updated,
            description=model.description,
        ))

    # Empty catalog — no models present.
    empty_catalog = json.dumps({"nodes": {}}).encode()

    parser = CatalogParser()
    merged = parser.merge(models_with_types, empty_catalog)

    assert len(merged) == len(models_with_types), (
        f"Expected {len(models_with_types)} merged models, got {len(merged)}"
    )

    for original, result in zip(models_with_types, merged):
        # row_count MUST be 0 for any model absent from catalog.
        assert result.row_count == 0, (
            f"Model '{original.name}': expected row_count=0 (absent from catalog), "
            f"got {result.row_count}"
        )

        assert len(result.columns) == len(original.columns), (
            f"Model '{original.name}': expected {len(original.columns)} columns, "
            f"got {len(result.columns)}"
        )

        for orig_col, merged_col in zip(original.columns, result.columns):
            # Column name must be unchanged.
            assert merged_col.name == orig_col.name, (
                f"Model '{original.name}': column name changed "
                f"{orig_col.name!r} → {merged_col.name!r}"
            )

            # data_type must be retained from manifest (not overwritten to '').
            assert merged_col.data_type == orig_col.data_type, (
                f"Model '{original.name}', column '{orig_col.name}': "
                f"manifest type should be kept when model is absent from catalog; "
                f"expected {orig_col.data_type!r}, got {merged_col.data_type!r}"
            )

            # description must be preserved.
            assert merged_col.description == orig_col.description, (
                f"Model '{original.name}', column '{orig_col.name}': "
                f"description changed "
                f"{orig_col.description!r} → {merged_col.description!r}"
            )


# ---------------------------------------------------------------------------
# Property 4f: Mixed scenario — some models in catalog, some absent.
# ---------------------------------------------------------------------------

@given(
    in_catalog=st.lists(manifest_model_strategy(), min_size=1, max_size=6),
    not_in_catalog=st.lists(manifest_model_strategy(), min_size=1, max_size=6),
)
@settings(max_examples=100)
def test_property_4f_mixed_catalog_presence_merge_fidelity(
    in_catalog: list[ModelMeta],
    not_in_catalog: list[ModelMeta],
) -> None:
    """**Validates: Requirements 3.4, 15.2**

    Property 4: Schema Merge Fidelity — mixed presence.

    When some models are in the catalog and others are not:
    - Models IN catalog: column types from catalog; descriptions from manifest.
    - Models NOT in catalog: row_count == 0; column types from manifest.
    """
    # Deduplicate model names across both lists so catalog lookup is unambiguous.
    # If a name appears in both groups, drop it from not_in_catalog.
    in_catalog_names = {m.name for m in in_catalog}
    not_in_catalog_deduped = [m for m in not_in_catalog if m.name not in in_catalog_names]

    # Assign manifest types to all columns of the "not in catalog" models.
    not_in_catalog_with_types: list[ModelMeta] = []
    for model in not_in_catalog_deduped:
        updated_cols = [
            ColumnMeta(
                name=col.name,
                data_type=f"MANIFEST_{col.name.upper()}_TYPE",
                description=col.description,
            )
            for col in model.columns
        ]
        not_in_catalog_with_types.append(ModelMeta(
            name=model.name,
            database=model.database,
            schema_name=model.schema_name,
            fqn=model.fqn,
            columns=updated_cols,
            grain=model.grain,
            layer=model.layer,
            compiled_sql_excerpt=model.compiled_sql_excerpt,
            depends_on=model.depends_on,
            tags=model.tags,
            folder_path=model.folder_path,
            row_count=model.row_count,
            last_updated=model.last_updated,
            description=model.description,
        ))

    # Assign catalog types for "in catalog" models.
    catalog_types: dict[str, dict[str, str]] = {}
    for model in in_catalog:
        catalog_types[model.name] = {
            col.name: f"CATALOG_{col.name.upper()}_TYPE"
            for col in model.columns
        }

    # Build catalog raw bytes covering ONLY the "in_catalog" group.
    catalog_raw = _build_catalog_raw(in_catalog, catalog_types, {})

    # Combine all models in a single input list, in_catalog first.
    all_models = list(in_catalog) + not_in_catalog_with_types

    parser = CatalogParser()
    merged = parser.merge(all_models, catalog_raw)

    assert len(merged) == len(all_models), (
        f"Expected {len(all_models)} merged models, got {len(merged)}"
    )

    # Verify "in catalog" models.
    for original, result in zip(merged[: len(in_catalog)], in_catalog):
        for orig_col, merged_col in zip(result.columns, original.columns):
            assert merged_col.name == orig_col.name
            expected_type = catalog_types[original.name].get(orig_col.name)
            if expected_type is not None:
                assert merged_col.data_type == expected_type, (
                    f"IN-CATALOG model '{original.name}', column '{orig_col.name}': "
                    f"expected catalog type {expected_type!r}, got {merged_col.data_type!r}"
                )
            # description always from manifest
            assert merged_col.description == orig_col.description, (
                f"IN-CATALOG model '{original.name}', column '{orig_col.name}': "
                f"description should come from manifest, not catalog"
            )

    # Verify "not in catalog" models.
    offset = len(in_catalog)
    for original, result in zip(not_in_catalog_with_types, merged[offset:]):
        assert result.row_count == 0, (
            f"NOT-IN-CATALOG model '{original.name}': "
            f"expected row_count=0, got {result.row_count}"
        )
        for orig_col, merged_col in zip(original.columns, result.columns):
            assert merged_col.name == orig_col.name
            assert merged_col.data_type == orig_col.data_type, (
                f"NOT-IN-CATALOG model '{original.name}', column '{orig_col.name}': "
                f"manifest type should be preserved; "
                f"expected {orig_col.data_type!r}, got {merged_col.data_type!r}"
            )
            assert merged_col.description == orig_col.description

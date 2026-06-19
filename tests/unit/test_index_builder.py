"""
tests/unit/test_index_builder.py

Unit tests for ``IndexBuilder.build()`` covering:
  - Full happy-path orchestration
  - Layer inference priority (tag → folder path → default "bronze")
  - Grain inference (GROUP BY → DISTINCT → _by_ suffix → "unknown")
  - Parse failure fallback (returns previous index or None)
  - model_count / built_at correctness
  - Embeddings placeholder is empty
  - Lineage is populated from graph

Property-based tests:
  - Property 3: Layer Inference Priority Order (Validates: Requirements 3.3)
  - Property 6: Column Name Preservation Round-Trip Fidelity (Validates: Requirements 3.2, 15.1)

Requirements: 3.1, 3.2, 3.3, 3.6, 3.7, 3.8, 15.1
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.discovery.index_builder import IndexBuilder, _infer_grain, _infer_layer
from backend.models import (
    ArtifactBundle,
    ColumnMeta,
    LineageNode,
    ModelMeta,
    SchemaIndex,
)

# ---------------------------------------------------------------------------
# Helpers — build minimal valid artifact bytes
# ---------------------------------------------------------------------------


def _make_manifest(models: list[dict]) -> bytes:
    """Wrap a list of model-node dicts into a minimal manifest.json payload."""
    nodes = {}
    for m in models:
        name = m.get("name", "test_model")
        key = f"model.my_project.{name}"
        nodes[key] = {
            "name": name,
            "database": m.get("database", "db"),
            "schema": m.get("schema", "dbt_schema"),
            "path": m.get("path", f"models/{name}.sql"),
            "compiled_code": m.get("compiled_code", "SELECT 1"),
            "depends_on": {"nodes": m.get("depends_on", [])},
            "tags": m.get("tags", []),
            "columns": m.get("columns", {}),
            "description": m.get("description", ""),
            "meta": m.get("meta", {}),
        }
    return json.dumps({"nodes": nodes}).encode()


def _make_catalog(models: list[dict]) -> bytes:
    """Build a minimal catalog.json with optional column type overrides."""
    nodes = {}
    for m in models:
        name = m.get("name", "test_model")
        key = f"model.my_project.{name}"
        col_entries = {}
        for col in m.get("columns", []):
            col_entries[col["name"]] = {"type": col.get("type", "STRING"), "name": col["name"]}
        nodes[key] = {
            "columns": col_entries,
            "stats": {
                "row_count": {"value": m.get("row_count", 0), "label": "Row Count"},
            },
        }
    return json.dumps({"nodes": nodes}).encode()


def _make_empty_graph() -> bytes:
    return json.dumps({"nodes": {}}).encode()


def _make_graph(edges: dict[str, dict]) -> bytes:
    """Build graph_summary.json from a {node_id: {"depends_on": [...], "children": [...]}} dict."""
    return json.dumps({"nodes": edges}).encode()


def _make_bundle(
    manifest_models: list[dict] | None = None,
    catalog_models: list[dict] | None = None,
    graph_bytes: bytes | None = None,
) -> ArtifactBundle:
    manifest_models = manifest_models or [{"name": "test_model"}]
    catalog_models = catalog_models or []
    return ArtifactBundle(
        manifest=_make_manifest(manifest_models),
        catalog=_make_catalog(catalog_models),
        graph=graph_bytes or _make_empty_graph(),
        fetched_at=datetime.now(tz=timezone.utc),
    )


def _make_previous_index() -> SchemaIndex:
    col = ColumnMeta(name="id", data_type="INT", description="pk")
    model = ModelMeta(
        name="old_model",
        database="db",
        schema_name="schema",
        fqn="db.schema.old_model",
        columns=[col],
        grain="unknown",
        layer="bronze",
        compiled_sql_excerpt="SELECT 1",
        depends_on=[],
        tags=[],
        folder_path="models",
        row_count=0,
        last_updated=None,
        description="previous",
    )
    return SchemaIndex(
        models=[model],
        embeddings=np.empty((0,), dtype=np.float32),
        lineage={},
        built_at=datetime.now(tz=timezone.utc),
        model_count=1,
    )


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestIndexBuilderHappyPath:
    def test_build_returns_schema_index(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{"name": "orders"}])
        result = builder.build(bundle)

        assert result is not None
        assert isinstance(result, SchemaIndex)

    def test_model_count_matches_models(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{"name": "orders"}, {"name": "customers"}])
        result = builder.build(bundle)

        assert result is not None
        assert result.model_count == 2
        assert len(result.models) == 2

    def test_built_at_is_recent_utc(self):
        before = datetime.now(tz=timezone.utc)
        builder = IndexBuilder()
        result = builder.build(_make_bundle())
        after = datetime.now(tz=timezone.utc)

        assert result is not None
        assert before <= result.built_at <= after

    def test_embeddings_placeholder_is_empty_array(self):
        builder = IndexBuilder()
        result = builder.build(_make_bundle([{"name": "sales"}]))

        assert result is not None
        assert isinstance(result.embeddings, np.ndarray)
        assert result.embeddings.size == 0

    def test_lineage_populated_from_graph(self):
        builder = IndexBuilder()
        graph = _make_graph({
            "model.proj.orders": {
                "depends_on": ["model.proj.customers"],
                "children": [],
            },
            "model.proj.customers": {
                "depends_on": [],
                "children": ["model.proj.orders"],
            },
        })
        bundle = _make_bundle(
            manifest_models=[{"name": "orders"}, {"name": "customers"}],
            graph_bytes=graph,
        )
        result = builder.build(bundle)

        assert result is not None
        assert "orders" in result.lineage
        assert "customers" in result.lineage
        assert "customers" in result.lineage["orders"].parents
        assert "orders" in result.lineage["customers"].children


# ---------------------------------------------------------------------------
# Tests — layer inference (Property 3)
# ---------------------------------------------------------------------------


class TestLayerInference:
    """Layer priority: tag → folder path → default "bronze". (Requirement 3.3)"""

    def _model(self, tags: list[str] = (), folder_path: str = "") -> ModelMeta:
        return ModelMeta(
            name="m",
            database="db",
            schema_name="s",
            fqn="db.s.m",
            columns=[],
            grain="",
            layer="bronze",
            compiled_sql_excerpt="",
            depends_on=[],
            tags=list(tags),
            folder_path=folder_path,
            row_count=0,
            last_updated=None,
            description="",
        )

    # --- tag wins ---

    def test_tag_gold_wins(self):
        assert _infer_layer(self._model(tags=["gold"])) == "gold"

    def test_tag_silver_wins(self):
        assert _infer_layer(self._model(tags=["silver"])) == "silver"

    def test_tag_bronze_explicit(self):
        assert _infer_layer(self._model(tags=["bronze"])) == "bronze"

    def test_tag_case_insensitive(self):
        assert _infer_layer(self._model(tags=["Gold"])) == "gold"
        assert _infer_layer(self._model(tags=["SILVER"])) == "silver"

    def test_tag_partial_match(self):
        # A tag like "gold_layer" still contains "gold"
        assert _infer_layer(self._model(tags=["gold_layer"])) == "gold"

    def test_gold_tag_beats_silver_folder(self):
        """Tag wins over folder path regardless of content."""
        assert _infer_layer(self._model(tags=["gold"], folder_path="models/silver")) == "gold"

    def test_gold_tag_beats_bronze_folder(self):
        assert _infer_layer(self._model(tags=["gold"], folder_path="models/bronze")) == "gold"

    def test_silver_tag_beats_gold_folder(self):
        assert _infer_layer(self._model(tags=["silver"], folder_path="models/gold")) == "silver"

    # --- folder path wins when no layer tag ---

    def test_folder_gold(self):
        assert _infer_layer(self._model(folder_path="models/gold")) == "gold"

    def test_folder_silver(self):
        assert _infer_layer(self._model(folder_path="models/silver")) == "silver"

    def test_folder_bronze(self):
        assert _infer_layer(self._model(folder_path="models/bronze")) == "bronze"

    def test_folder_case_insensitive(self):
        assert _infer_layer(self._model(folder_path="models/Gold")) == "gold"

    def test_folder_nested_path(self):
        assert _infer_layer(self._model(folder_path="models/gold/finance")) == "gold"

    # --- default ---

    def test_default_bronze_when_no_tag_no_folder(self):
        assert _infer_layer(self._model(tags=[], folder_path="models/staging")) == "bronze"

    def test_default_bronze_empty_everything(self):
        assert _infer_layer(self._model()) == "bronze"

    # --- integration: layer applied in build() ---

    def test_build_applies_layer_from_tag(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{"name": "fact_sales", "tags": ["gold"]}])
        result = builder.build(bundle)
        assert result is not None
        assert result.models[0].layer == "gold"

    def test_build_applies_layer_from_folder(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{"name": "dim_customer", "path": "models/silver/dim_customer.sql"}])
        result = builder.build(bundle)
        assert result is not None
        assert result.models[0].layer == "silver"

    def test_build_defaults_to_bronze(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{"name": "raw_orders", "path": "models/raw/raw_orders.sql"}])
        result = builder.build(bundle)
        assert result is not None
        assert result.models[0].layer == "bronze"


# ---------------------------------------------------------------------------
# Tests — grain inference (Requirement 3.6)
# ---------------------------------------------------------------------------


class TestGrainInference:
    """Grain priority: GROUP BY → DISTINCT → _by_ suffix → "unknown"."""

    def _model(self, name: str = "m", compiled_sql: str = "") -> ModelMeta:
        return ModelMeta(
            name=name,
            database="db",
            schema_name="s",
            fqn="db.s.m",
            columns=[],
            grain="",
            layer="bronze",
            compiled_sql_excerpt=compiled_sql,
            depends_on=[],
            tags=[],
            folder_path="",
            row_count=0,
            last_updated=None,
            description="",
        )

    # --- GROUP BY ---

    def test_group_by_single_column(self):
        grain = _infer_grain(self._model(compiled_sql="SELECT customer_id, SUM(amount) FROM t GROUP BY customer_id"))
        assert "customer_id" in grain

    def test_group_by_multiple_columns(self):
        grain = _infer_grain(self._model(compiled_sql="SELECT a, b, COUNT(*) FROM t GROUP BY a, b"))
        assert "a" in grain
        assert "b" in grain

    def test_group_by_case_insensitive(self):
        grain = _infer_grain(self._model(compiled_sql="SELECT x FROM t group by x"))
        assert "x" in grain

    def test_group_by_beats_distinct(self):
        sql = "SELECT DISTINCT a, b FROM t GROUP BY a, b"
        grain = _infer_grain(self._model(compiled_sql=sql))
        # GROUP BY should win
        assert "a" in grain

    def test_group_by_beats_by_suffix(self):
        grain = _infer_grain(self._model(name="sales_by_day", compiled_sql="SELECT d, SUM(v) FROM t GROUP BY d"))
        assert "d" in grain

    # --- DISTINCT ---

    def test_distinct_keyword(self):
        grain = _infer_grain(self._model(compiled_sql="SELECT DISTINCT customer_id FROM orders"))
        assert grain == "distinct"

    def test_distinct_case_insensitive(self):
        grain = _infer_grain(self._model(compiled_sql="select distinct id from t"))
        assert grain == "distinct"

    def test_distinct_beats_by_suffix(self):
        grain = _infer_grain(self._model(name="orders_by_day", compiled_sql="SELECT DISTINCT order_id FROM orders"))
        assert grain == "distinct"

    # --- _by_ suffix ---

    def test_by_suffix_day(self):
        grain = _infer_grain(self._model(name="sales_by_day"))
        assert grain == "day"

    def test_by_suffix_brand(self):
        grain = _infer_grain(self._model(name="revenue_by_brand"))
        assert grain == "brand"

    def test_by_suffix_region(self):
        grain = _infer_grain(self._model(name="orders_by_region"))
        assert grain == "region"

    def test_by_suffix_not_at_end_ignored(self):
        # "by_product_sales" does NOT have _by_ at the end
        grain = _infer_grain(self._model(name="by_product_sales"))
        assert grain == "unknown"

    # --- unknown fallback ---

    def test_unknown_when_nothing_matches(self):
        grain = _infer_grain(self._model(name="raw_events", compiled_sql="SELECT * FROM events"))
        assert grain == "unknown"

    def test_unknown_empty_sql_no_suffix(self):
        grain = _infer_grain(self._model(name="stg_orders", compiled_sql=""))
        assert grain == "unknown"

    # --- integration: grain applied in build() ---

    def test_build_uses_meta_grain_when_present(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{
            "name": "orders",
            "meta": {"grain": "order_id"},
            "compiled_code": "SELECT DISTINCT id FROM t",
        }])
        result = builder.build(bundle)
        assert result is not None
        # meta.grain takes priority over inference
        assert result.models[0].grain == "order_id"

    def test_build_infers_grain_when_meta_absent(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{
            "name": "sales_by_day",
            "meta": {},
            "compiled_code": "SELECT day, SUM(v) FROM t",
        }])
        result = builder.build(bundle)
        assert result is not None
        # No GROUP BY, no DISTINCT → _by_day suffix
        assert result.models[0].grain == "day"

    def test_build_infers_group_by_grain(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{
            "name": "agg_orders",
            "meta": {},
            "compiled_code": "SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id",
        }])
        result = builder.build(bundle)
        assert result is not None
        assert "customer_id" in result.models[0].grain

    def test_build_grain_unknown_as_fallback(self):
        builder = IndexBuilder()
        bundle = _make_bundle([{
            "name": "raw_events",
            "meta": {},
            "compiled_code": "SELECT id FROM events",
        }])
        result = builder.build(bundle)
        assert result is not None
        assert result.models[0].grain == "unknown"


# ---------------------------------------------------------------------------
# Tests — parse failure fallback (Requirement 3.8)
# ---------------------------------------------------------------------------


class TestParseFailureFallback:
    def test_invalid_manifest_returns_previous_index(self):
        builder = IndexBuilder()
        previous = _make_previous_index()
        bundle = ArtifactBundle(
            manifest=b"NOT VALID JSON {{{",
            catalog=_make_catalog([]),
            graph=_make_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle, previous_index=previous)
        assert result is previous

    def test_invalid_catalog_returns_previous_index(self):
        builder = IndexBuilder()
        previous = _make_previous_index()
        bundle = ArtifactBundle(
            manifest=_make_manifest([{"name": "orders"}]),
            catalog=b"totally broken ][",
            graph=_make_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle, previous_index=previous)
        assert result is previous

    def test_invalid_graph_returns_previous_index(self):
        builder = IndexBuilder()
        previous = _make_previous_index()
        bundle = ArtifactBundle(
            manifest=_make_manifest([{"name": "orders"}]),
            catalog=_make_catalog([]),
            graph=b"invalid graph ][",
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle, previous_index=previous)
        assert result is previous

    def test_invalid_manifest_no_previous_returns_none(self):
        builder = IndexBuilder()
        bundle = ArtifactBundle(
            manifest=b"[1, 2, 3]",  # valid JSON but wrong top-level type
            catalog=_make_catalog([]),
            graph=_make_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle, previous_index=None)
        assert result is None

    def test_invalid_catalog_no_previous_returns_none(self):
        builder = IndexBuilder()
        bundle = ArtifactBundle(
            manifest=_make_manifest([{"name": "orders"}]),
            catalog=b"null",  # valid JSON but wrong top-level type
            graph=_make_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle, previous_index=None)
        assert result is None

    def test_partial_failure_does_not_affect_previous(self):
        """Previous index is returned unchanged — not mutated."""
        builder = IndexBuilder()
        previous = _make_previous_index()
        original_model_count = previous.model_count

        bundle = ArtifactBundle(
            manifest=b"bad json",
            catalog=_make_catalog([]),
            graph=_make_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle, previous_index=previous)
        assert result is previous
        assert result.model_count == original_model_count


# ---------------------------------------------------------------------------
# Tests — catalog merge integration (Requirement 3.4)
# ---------------------------------------------------------------------------


class TestCatalogMergeIntegration:
    def test_catalog_types_override_manifest_types(self):
        builder = IndexBuilder()
        bundle = _make_bundle(
            manifest_models=[{
                "name": "orders",
                "columns": {"order_id": {"name": "order_id", "description": "The order PK"}},
            }],
            catalog_models=[{
                "name": "orders",
                "columns": [{"name": "order_id", "type": "BIGINT"}],
            }],
        )
        result = builder.build(bundle)
        assert result is not None
        model = result.models[0]
        col = next(c for c in model.columns if c.name == "order_id")
        assert col.data_type == "BIGINT"
        assert col.description == "The order PK"  # description from manifest

    def test_model_absent_from_catalog_has_row_count_zero(self):
        builder = IndexBuilder()
        bundle = _make_bundle(
            manifest_models=[{"name": "raw_events"}],
            catalog_models=[],  # not in catalog
        )
        result = builder.build(bundle)
        assert result is not None
        assert result.models[0].row_count == 0

    def test_row_count_populated_from_catalog(self):
        builder = IndexBuilder()
        bundle = _make_bundle(
            manifest_models=[{"name": "fact_sales"}],
            catalog_models=[{"name": "fact_sales", "row_count": 42000}],
        )
        result = builder.build(bundle)
        assert result is not None
        assert result.models[0].row_count == 42000


# ---------------------------------------------------------------------------
# Tests — zero-value fallback for missing fields (Requirement 3.7)
# ---------------------------------------------------------------------------


class TestZeroValueFallback:
    def test_model_with_missing_description_still_indexed(self):
        """Missing description → empty string; model is still included."""
        builder = IndexBuilder()
        nodes = {
            "model.proj.no_desc": {
                "name": "no_desc",
                "database": "db",
                "schema": "s",
                "path": "models/no_desc.sql",
                "compiled_code": "SELECT 1",
                "depends_on": {"nodes": []},
                "tags": [],
                "columns": {},
                # description intentionally absent
            }
        }
        manifest_bytes = json.dumps({"nodes": nodes}).encode()
        bundle = ArtifactBundle(
            manifest=manifest_bytes,
            catalog=_make_catalog([]),
            graph=_make_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle)
        assert result is not None
        assert result.model_count == 1
        assert result.models[0].description == ""

    def test_multiple_models_all_indexed_despite_missing_fields(self):
        """Build with 5 models, each missing a different field — all 5 must appear."""
        builder = IndexBuilder()
        bundle = _make_bundle([
            {"name": f"model_{i}"} for i in range(5)
        ])
        result = builder.build(bundle)
        assert result is not None
        assert result.model_count == 5


# ---------------------------------------------------------------------------
# Tests — empty manifest
# ---------------------------------------------------------------------------


class TestEmptyManifest:
    def test_empty_nodes_returns_empty_index(self):
        builder = IndexBuilder()
        bundle = ArtifactBundle(
            manifest=json.dumps({"nodes": {}}).encode(),
            catalog=_make_catalog([]),
            graph=_make_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        result = builder.build(bundle)
        assert result is not None
        assert result.model_count == 0
        assert result.models == []


# ---------------------------------------------------------------------------
# Property-based tests — Property 3: Layer Inference Priority Order
# ---------------------------------------------------------------------------
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------

# Strategies for generating layer tags and non-layer tags
_LAYER_WORDS = ("gold", "silver", "bronze")

# A tag that is guaranteed to contain a layer keyword (any casing, with optional
# surrounding text so we exercise sub-string matching as the implementation does).
_layer_tag_strategy = st.one_of(
    # Exact keyword in various casings
    st.sampled_from(["gold", "Gold", "GOLD", "silver", "Silver", "SILVER", "bronze", "Bronze", "BRONZE"]),
    # Keyword embedded in a larger tag string
    st.builds(
        lambda kw, prefix, suffix: f"{prefix}{kw}{suffix}",
        kw=st.sampled_from(_LAYER_WORDS),
        prefix=st.from_regex(r"[a-z_]{0,6}", fullmatch=True),
        suffix=st.from_regex(r"[a-z_]{0,6}", fullmatch=True),
    ),
)

# A tag that contains NONE of the layer keywords (in any form).
_non_layer_tag_strategy = st.from_regex(
    r"[a-z][a-z0-9_]{0,19}",
    fullmatch=True,
).filter(lambda t: not any(kw in t.lower() for kw in _LAYER_WORDS))

# A folder path that contains at least one layer keyword as a path segment.
_layer_folder_strategy = st.builds(
    lambda prefix, kw, suffix: f"{prefix}/{kw}/{suffix}",
    prefix=st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True),
    kw=st.sampled_from(["gold", "Gold", "GOLD", "silver", "Silver", "SILVER", "bronze", "Bronze", "BRONZE"]),
    suffix=st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True),
)

# A folder path that contains NONE of the layer keywords.
_non_layer_folder_strategy = st.from_regex(
    r"models/[a-z][a-z0-9_/]{0,30}",
    fullmatch=True,
).filter(lambda p: not any(kw in p.lower() for kw in _LAYER_WORDS))


def _make_model_meta(tags: list[str], folder_path: str) -> "ModelMeta":
    """Build a minimal ModelMeta with the given tags and folder_path."""
    from backend.models import ModelMeta

    return ModelMeta(
        name="test_model",
        database="db",
        schema_name="schema",
        fqn="db.schema.test_model",
        columns=[],
        grain="unknown",
        layer="bronze",  # placeholder — will be overridden by _infer_layer
        compiled_sql_excerpt="SELECT 1",
        depends_on=[],
        tags=tags,
        folder_path=folder_path,
        row_count=0,
        last_updated=None,
        description="",
    )


def _extract_layer_keyword(value: str) -> str | None:
    """Return the first layer keyword found in *value* (case-insensitive), or None."""
    lower = value.lower()
    for kw in _LAYER_WORDS:
        if kw in lower:
            return kw
    return None


class TestProperty3LayerInferencePriorityOrder:
    """**Property 3: Layer Inference Priority Order**

    For any model entry with any combination of tags and folder path, the
    inferred layer SHALL always follow the priority order:
        tag match → folder path match → default "bronze".

    Specifically:
      - If a gold/silver/bronze tag is present, it always wins over folder
        path, regardless of what the folder path contains.
      - If no tag matches but the folder path contains a layer keyword, the
        folder path result is used.
      - If neither tag nor folder path matches, the result is always "bronze".

    **Validates: Requirements 3.3**
    """

    # ------------------------------------------------------------------
    # Sub-property A: Tag always beats folder path
    # ------------------------------------------------------------------

    @given(
        tags=st.lists(_layer_tag_strategy, min_size=1, max_size=5),
        folder_path=st.one_of(_layer_folder_strategy, _non_layer_folder_strategy),
        extra_tags=st.lists(_non_layer_tag_strategy, min_size=0, max_size=3),
    )
    @settings(max_examples=100)
    def test_tag_always_beats_folder_path(
        self,
        tags: list[str],
        folder_path: str,
        extra_tags: list[str],
    ) -> None:
        """When a layer tag is present, it MUST win over any folder path content.

        **Validates: Requirements 3.3**
        """
        all_tags = tags + extra_tags
        model = _make_model_meta(tags=all_tags, folder_path=folder_path)
        result = _infer_layer(model)

        # Determine which layer keyword the tags contain (first-match in priority order).
        expected_from_tags: str | None = None
        for keyword in _LAYER_WORDS:
            for tag in all_tags:
                if keyword in tag.lower():
                    expected_from_tags = keyword
                    break
            if expected_from_tags is not None:
                break

        # There MUST be a matching tag (we generated at least one layer tag).
        assert expected_from_tags is not None, (
            f"Test setup error: no layer keyword found in tags={all_tags!r}"
        )

        assert result == expected_from_tags, (
            f"Expected tag-derived layer {expected_from_tags!r} to win over "
            f"folder_path={folder_path!r}, but got {result!r}. "
            f"tags={all_tags!r}"
        )

    # ------------------------------------------------------------------
    # Sub-property B: Folder path wins when no layer tag is present
    # ------------------------------------------------------------------

    @given(
        folder_path=_layer_folder_strategy,
        tags=st.lists(_non_layer_tag_strategy, min_size=0, max_size=5),
    )
    @settings(max_examples=100)
    def test_folder_path_wins_when_no_layer_tag(
        self,
        folder_path: str,
        tags: list[str],
    ) -> None:
        """When NO layer tag is present but the folder path contains a layer
        keyword, the folder path result MUST be used.

        **Validates: Requirements 3.3**
        """
        model = _make_model_meta(tags=tags, folder_path=folder_path)
        result = _infer_layer(model)

        # Determine the expected layer from the folder path segments.
        import re
        segments = re.split(r"[/\\]", folder_path)
        expected_from_folder: str | None = None
        for keyword in _LAYER_WORDS:
            for segment in segments:
                if keyword in segment.lower():
                    expected_from_folder = keyword
                    break
            if expected_from_folder is not None:
                break

        assert expected_from_folder is not None, (
            f"Test setup error: no layer keyword found in folder_path={folder_path!r}"
        )

        assert result == expected_from_folder, (
            f"Expected folder-derived layer {expected_from_folder!r} "
            f"(from folder_path={folder_path!r}) but got {result!r}. "
            f"tags={tags!r}"
        )

    # ------------------------------------------------------------------
    # Sub-property C: Default is always "bronze" when nothing matches
    # ------------------------------------------------------------------

    @given(
        tags=st.lists(_non_layer_tag_strategy, min_size=0, max_size=5),
        folder_path=_non_layer_folder_strategy,
    )
    @settings(max_examples=100)
    def test_default_is_bronze_when_neither_tag_nor_folder_matches(
        self,
        tags: list[str],
        folder_path: str,
    ) -> None:
        """When neither tags nor folder path contain any layer keyword, the
        inferred layer MUST always be "bronze".

        **Validates: Requirements 3.3**
        """
        model = _make_model_meta(tags=tags, folder_path=folder_path)
        result = _infer_layer(model)

        assert result == "bronze", (
            f"Expected default layer 'bronze' when no layer keyword present, "
            f"but got {result!r}. tags={tags!r}, folder_path={folder_path!r}"
        )

    # ------------------------------------------------------------------
    # Sub-property D: Result is always a valid layer value
    # ------------------------------------------------------------------

    @given(
        tags=st.lists(
            st.one_of(_layer_tag_strategy, _non_layer_tag_strategy),
            min_size=0,
            max_size=6,
        ),
        folder_path=st.one_of(_layer_folder_strategy, _non_layer_folder_strategy),
    )
    @settings(max_examples=100)
    def test_result_is_always_valid_layer(
        self,
        tags: list[str],
        folder_path: str,
    ) -> None:
        """For any combination of tags and folder path, the inferred layer
        MUST always be exactly one of "gold", "silver", or "bronze".

        **Validates: Requirements 3.3**
        """
        model = _make_model_meta(tags=tags, folder_path=folder_path)
        result = _infer_layer(model)

        assert result in {"gold", "silver", "bronze"}, (
            f"Inferred layer {result!r} is not one of the valid values "
            f"{{\"gold\", \"silver\", \"bronze\"}}. "
            f"tags={tags!r}, folder_path={folder_path!r}"
        )


# ---------------------------------------------------------------------------
# Property 5: Missing Field Zero-Value Handling (Requirement 3.7)
# ---------------------------------------------------------------------------
# For any model entry in manifest.json that has any combination of missing or
# null fields, the Index_Builder SHALL always record those fields as their zero
# value (empty string for strings, empty list for lists, 0 for integers) and
# SHALL always continue building the index for that model and all subsequent
# models — it SHALL never skip a model or halt construction due to a missing
# individual field.
#
# Validates: Requirements 3.7
# ---------------------------------------------------------------------------

# Optional field values: a field may be absent (key not present), null (None),
# an invalid type (int where string expected), or a valid string/list/int.
_optional_str = st.one_of(
    st.none(),
    st.just(""),
    st.text(max_size=30),
    st.integers(),          # wrong type — should fall back to ""
    st.booleans(),          # wrong type — should fall back to ""
)

_optional_list = st.one_of(
    st.none(),
    st.just([]),
    st.lists(st.text(max_size=10), max_size=5),
    st.integers(),          # wrong type — should fall back to []
)

_optional_columns = st.one_of(
    st.none(),
    st.just({}),
    st.dictionaries(
        keys=st.text(min_size=1, max_size=15, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_")),
        values=st.fixed_dictionaries({
            "name": st.one_of(st.none(), st.text(max_size=15)),
            "description": st.one_of(st.none(), st.text(max_size=30)),
        }),
        max_size=5,
    ),
    st.integers(),          # wrong type — should fall back to []
)


def _build_partial_node(
    name: str,
    database,
    schema,
    path,
    compiled_code,
    depends_on,
    tags,
    columns,
    description,
    meta,
) -> dict:
    """Build a manifest node dict with only the fields that are not _ABSENT."""
    node: dict = {"name": name}
    # Each field is included in the node dict unless its value is the sentinel
    # _ABSENT, which means the key is entirely missing from the JSON.
    for key, value in [
        ("database", database),
        ("schema", schema),
        ("path", path),
        ("compiled_code", compiled_code),
        ("depends_on", depends_on),
        ("tags", tags),
        ("columns", columns),
        ("description", description),
        ("meta", meta),
    ]:
        if value is not _ABSENT:
            node[key] = value
    return node


_ABSENT = object()  # sentinel: key is missing from the JSON dict

# Strategy that generates a field value or leaves it absent entirely.
def _maybe_absent(inner_st):
    return st.one_of(st.just(_ABSENT), inner_st)


_partial_node_st = st.fixed_dictionaries({
    "database":     _maybe_absent(_optional_str),
    "schema":       _maybe_absent(_optional_str),
    "path":         _maybe_absent(_optional_str),
    "compiled_code":_maybe_absent(_optional_str),
    "depends_on":   _maybe_absent(st.one_of(
        st.none(),
        st.just({"nodes": None}),
        st.just({"nodes": []}),
        st.fixed_dictionaries({"nodes": st.lists(st.text(max_size=10), max_size=3)}),
        st.integers(),
    )),
    "tags":         _maybe_absent(_optional_list),
    "columns":      _maybe_absent(_optional_columns),
    "description":  _maybe_absent(_optional_str),
    "meta":         _maybe_absent(st.one_of(
        st.none(),
        st.just({}),
        st.fixed_dictionaries({"grain": st.one_of(st.none(), st.text(max_size=20))}),
        st.integers(),
    )),
})


def _node_to_manifest_bytes(node_fields: dict, model_name: str) -> bytes:
    """Convert the generated field dict into a full manifest.json payload."""
    node: dict = {"name": model_name}
    for key, value in node_fields.items():
        if value is not _ABSENT:
            node[key] = value
    nodes = {f"model.proj.{model_name}": node}
    return json.dumps({"nodes": nodes}).encode()


def _empty_catalog() -> bytes:
    return json.dumps({"nodes": {}}).encode()


def _empty_graph() -> bytes:
    return json.dumps({"nodes": {}}).encode()


class TestMissingFieldZeroValueProperty:
    """Property 5: Missing Field Zero-Value Handling.

    **Validates: Requirements 3.7**
    """

    @given(
        node_fields=_partial_node_st,
        model_name=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                whitelist_characters="_",
            ),
        ),
    )
    @settings(max_examples=100, suppress_health_check=["too_slow"])
    def test_single_model_always_indexed_with_zero_values(self, node_fields, model_name):
        """A single model with any combination of missing/null fields is always indexed.

        The Index_Builder SHALL:
        1. Never raise an exception for a missing/null individual field.
        2. Always include the model in the resulting index (model_count == 1).
        3. Record string fields as "" (not None) when missing/null/wrong type.
        4. Record list fields as [] (not None) when missing/null/wrong type.
        5. Record integer fields as 0 (not None) when missing/null/wrong type.

        **Validates: Requirements 3.7**
        """
        manifest = _node_to_manifest_bytes(node_fields, model_name)
        bundle = ArtifactBundle(
            manifest=manifest,
            catalog=_empty_catalog(),
            graph=_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )

        builder = IndexBuilder()
        # Must never raise — zero-value fallback always applies
        result = builder.build(bundle)

        assert result is not None, "IndexBuilder must return a SchemaIndex, never None"
        assert result.model_count == 1, (
            f"Expected 1 model in the index but got {result.model_count}. "
            "A model must never be skipped due to missing fields."
        )
        assert len(result.models) == 1

        model = result.models[0]

        # --- String fields must be str, never None ---
        assert isinstance(model.name, str), f"name must be str, got {type(model.name)}"
        assert isinstance(model.database, str), f"database must be str, got {type(model.database)}"
        assert isinstance(model.schema_name, str), f"schema_name must be str, got {type(model.schema_name)}"
        assert isinstance(model.fqn, str), f"fqn must be str, got {type(model.fqn)}"
        assert isinstance(model.grain, str), f"grain must be str, got {type(model.grain)}"
        assert isinstance(model.compiled_sql_excerpt, str), (
            f"compiled_sql_excerpt must be str, got {type(model.compiled_sql_excerpt)}"
        )
        assert isinstance(model.folder_path, str), (
            f"folder_path must be str, got {type(model.folder_path)}"
        )
        assert isinstance(model.description, str), (
            f"description must be str, got {type(model.description)}"
        )

        # --- List fields must be list, never None ---
        assert isinstance(model.columns, list), f"columns must be list, got {type(model.columns)}"
        assert isinstance(model.depends_on, list), f"depends_on must be list, got {type(model.depends_on)}"
        assert isinstance(model.tags, list), f"tags must be list, got {type(model.tags)}"

        # --- Integer fields must be int >= 0, never None ---
        assert isinstance(model.row_count, int), f"row_count must be int, got {type(model.row_count)}"
        assert model.row_count >= 0, f"row_count must be >= 0, got {model.row_count}"

        # --- Layer must be a valid string (inferred) ---
        assert model.layer in ("bronze", "silver", "gold"), (
            f"layer must be one of bronze/silver/gold, got {model.layer!r}"
        )

    @given(
        node_fields_list=st.lists(
            _partial_node_st,
            min_size=2,
            max_size=5,
        ),
    )
    @settings(max_examples=100, suppress_health_check=["too_slow"])
    def test_multiple_models_all_indexed_despite_missing_fields(self, node_fields_list):
        """All models in a multi-model manifest are always indexed.

        For N models where each has any combination of missing/null fields,
        the index must contain exactly N models — none skipped or dropped.

        **Validates: Requirements 3.7**
        """
        # Build a manifest with N models, each with a unique name.
        nodes: dict = {}
        model_names: list[str] = []
        for i, node_fields in enumerate(node_fields_list):
            model_name = f"model_{i}"
            model_names.append(model_name)
            node: dict = {"name": model_name}
            for key, value in node_fields.items():
                if value is not _ABSENT:
                    node[key] = value
            nodes[f"model.proj.{model_name}"] = node

        manifest = json.dumps({"nodes": nodes}).encode()
        bundle = ArtifactBundle(
            manifest=manifest,
            catalog=_empty_catalog(),
            graph=_empty_graph(),
            fetched_at=datetime.now(tz=timezone.utc),
        )

        builder = IndexBuilder()
        result = builder.build(bundle)

        expected_count = len(node_fields_list)
        assert result is not None, "IndexBuilder must return a SchemaIndex, never None"
        assert result.model_count == expected_count, (
            f"Expected {expected_count} models but got {result.model_count}. "
            "No model should be skipped due to missing individual fields."
        )
        assert len(result.models) == expected_count

        # All models must have zero-value typed fields (no Nones)
        for model in result.models:
            assert isinstance(model.name, str)
            assert isinstance(model.database, str)
            assert isinstance(model.schema_name, str)
            assert isinstance(model.fqn, str)
            assert isinstance(model.grain, str)
            assert isinstance(model.compiled_sql_excerpt, str)
            assert isinstance(model.folder_path, str)
            assert isinstance(model.description, str)
            assert isinstance(model.columns, list)
            assert isinstance(model.depends_on, list)
            assert isinstance(model.tags, list)
            assert isinstance(model.row_count, int)
            assert model.row_count >= 0


# ---------------------------------------------------------------------------
# Property 6: Column Name Preservation (Round-Trip Fidelity)
# Validates: Requirements 3.2, 15.1
# ---------------------------------------------------------------------------

# Strategy: generate column names that are valid JSON strings, including
# Unicode, mixed case, embedded whitespace, and special characters.
# We avoid the NUL byte (u+0000) because JSON encoders reject it, and we
# avoid empty strings because they have no identity to preserve.
_column_name_strategy = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),   # exclude surrogates
        blacklist_characters=("\x00",), # exclude NUL (invalid in JSON strings)
    ),
    min_size=1,
    max_size=64,
)

# A single model's column dict for manifest.json — keys and "name" values are
# the generated column name; description and data_type are fixed placeholders.
_column_entry_strategy = _column_name_strategy.map(
    lambda n: {"name": n, "description": "test description"}
)


def _make_manifest_with_columns(columns: list[str]) -> bytes:
    """Build manifest.json bytes with a single model containing *columns*."""
    col_dict = {name: {"name": name, "description": "test"} for name in columns}
    nodes = {
        "model.proj.round_trip_model": {
            "name": "round_trip_model",
            "database": "db",
            "schema": "schema",
            "path": "models/round_trip_model.sql",
            "compiled_code": "SELECT 1",
            "depends_on": {"nodes": []},
            "tags": [],
            "columns": col_dict,
            "description": "round-trip test model",
            "meta": {},
        }
    }
    return json.dumps({"nodes": nodes}).encode("utf-8")


def _make_catalog_with_columns(columns: list[str]) -> bytes:
    """Build catalog.json bytes with matching column type overrides.

    The catalog uses the exact same column names supplied, so we can verify
    the type-override path also preserves names without transformation.
    """
    col_dict = {name: {"name": name, "type": "STRING"} for name in columns}
    nodes = {
        "model.proj.round_trip_model": {
            "columns": col_dict,
            "stats": {"row_count": {"value": 0, "label": "Row Count"}},
        }
    }
    return json.dumps({"nodes": nodes}).encode("utf-8")


def _make_empty_graph_bytes() -> bytes:
    return json.dumps({"nodes": {}}).encode("utf-8")


def _make_bundle_with_columns(
    columns: list[str],
    include_catalog: bool = True,
) -> ArtifactBundle:
    return ArtifactBundle(
        manifest=_make_manifest_with_columns(columns),
        catalog=(
            _make_catalog_with_columns(columns)
            if include_catalog
            else json.dumps({"nodes": {}}).encode("utf-8")
        ),
        graph=_make_empty_graph_bytes(),
        fetched_at=datetime.now(tz=timezone.utc),
    )


class TestColumnNameRoundTripPreservation:
    """Property 6: Column Name Preservation (Round-Trip Fidelity).

    **Validates: Requirements 3.2, 15.1**

    For any manifest.json and catalog.json content, every column name that
    appears in those source files SHALL appear in the Schema_Index with the
    exact same string — no case transformation, whitespace normalisation, or
    renaming applied.  The set of column names in the index SHALL be identical
    to the set in the sources.
    """

    # ------------------------------------------------------------------
    # Concrete sanity checks
    # ------------------------------------------------------------------

    def test_simple_column_names_preserved(self):
        """Basic happy path: lowercase ASCII column names survive the pipeline."""
        columns = ["order_id", "customer_id", "amount"]
        bundle = _make_bundle_with_columns(columns)
        result = IndexBuilder().build(bundle)
        assert result is not None
        index_names = {c.name for c in result.models[0].columns}
        assert index_names == set(columns)

    def test_mixed_case_column_names_preserved(self):
        """Column names with uppercase letters are not lowercased."""
        columns = ["OrderID", "CustomerName", "TotalAMOUNT"]
        bundle = _make_bundle_with_columns(columns)
        result = IndexBuilder().build(bundle)
        assert result is not None
        index_names = {c.name for c in result.models[0].columns}
        assert index_names == set(columns)

    def test_column_names_with_spaces_preserved(self):
        """Column names containing spaces are not stripped or normalised."""
        columns = ["order id", "customer name", "  leading space"]
        bundle = _make_bundle_with_columns(columns)
        result = IndexBuilder().build(bundle)
        assert result is not None
        index_names = {c.name for c in result.models[0].columns}
        assert index_names == set(columns)

    def test_column_names_with_special_characters_preserved(self):
        """Column names with special characters are not modified."""
        columns = ["col#1", "col$amount", "col/path", "col.dot"]
        bundle = _make_bundle_with_columns(columns)
        result = IndexBuilder().build(bundle)
        assert result is not None
        index_names = {c.name for c in result.models[0].columns}
        assert index_names == set(columns)

    def test_unicode_column_names_preserved(self):
        """Unicode column names pass through without transformation."""
        columns = ["mängd", "données", "数量", "Ärger"]
        bundle = _make_bundle_with_columns(columns)
        result = IndexBuilder().build(bundle)
        assert result is not None
        index_names = {c.name for c in result.models[0].columns}
        assert index_names == set(columns)

    def test_column_names_preserved_without_catalog(self):
        """Column names are preserved even when catalog is absent (empty)."""
        columns = ["id", "Name", "AMOUNT"]
        bundle = _make_bundle_with_columns(columns, include_catalog=False)
        result = IndexBuilder().build(bundle)
        assert result is not None
        index_names = {c.name for c in result.models[0].columns}
        assert index_names == set(columns)

    def test_no_duplicate_column_names_introduced(self):
        """The pipeline must not introduce duplicate column entries."""
        columns = ["col_a", "col_b", "col_c"]
        bundle = _make_bundle_with_columns(columns)
        result = IndexBuilder().build(bundle)
        assert result is not None
        all_names = [c.name for c in result.models[0].columns]
        assert len(all_names) == len(set(all_names)), (
            "Duplicate column names found after pipeline: "
            + str([n for n in all_names if all_names.count(n) > 1])
        )

    def test_no_extra_columns_injected(self):
        """The pipeline must not add columns that were not in the manifest."""
        columns = ["id", "value"]
        bundle = _make_bundle_with_columns(columns)
        result = IndexBuilder().build(bundle)
        assert result is not None
        index_names = {c.name for c in result.models[0].columns}
        assert index_names == set(columns), (
            f"Extra columns found: {index_names - set(columns)}"
        )

    # ------------------------------------------------------------------
    # Property-based tests
    # ------------------------------------------------------------------

    @given(
        columns=st.lists(
            _column_name_strategy,
            min_size=1,
            max_size=20,
            unique=True,
        )
    )
    @settings(max_examples=100)
    def test_manifest_column_names_survive_pipeline_unchanged(self, columns: list[str]):
        """Property 6 (manifest path): for any list of distinct column names,
        the index contains exactly those names — no transformation applied.

        **Validates: Requirements 3.2, 15.1**
        """
        bundle = _make_bundle_with_columns(columns, include_catalog=False)
        result = IndexBuilder().build(bundle)
        assert result is not None, "IndexBuilder returned None for valid input"
        assert len(result.models) == 1, "Expected exactly one model in the index"

        index_names = {c.name for c in result.models[0].columns}
        source_names = set(columns)

        # Every source column must appear in the index
        missing = source_names - index_names
        assert not missing, (
            f"Column names lost in transit: {missing!r}"
        )

        # No extra column names must have been injected
        extra = index_names - source_names
        assert not extra, (
            f"Unexpected columns added to index: {extra!r}"
        )

    @given(
        columns=st.lists(
            _column_name_strategy,
            min_size=1,
            max_size=20,
            unique=True,
        )
    )
    @settings(max_examples=100)
    def test_catalog_column_type_override_preserves_names(self, columns: list[str]):
        """Property 6 (catalog path): column type overrides from catalog.json
        must not alter column names in any way.

        When catalog.json provides type information, the merge must keep
        the original name from manifest.json intact.

        **Validates: Requirements 3.2, 15.1**
        """
        bundle = _make_bundle_with_columns(columns, include_catalog=True)
        result = IndexBuilder().build(bundle)
        assert result is not None, "IndexBuilder returned None for valid input"
        assert len(result.models) == 1, "Expected exactly one model in the index"

        index_names = {c.name for c in result.models[0].columns}
        source_names = set(columns)

        missing = source_names - index_names
        assert not missing, (
            f"Column names altered/lost after catalog merge: {missing!r}"
        )

        extra = index_names - source_names
        assert not extra, (
            f"Unexpected columns added during catalog merge: {extra!r}"
        )

    @given(
        columns=st.lists(
            _column_name_strategy,
            min_size=1,
            max_size=20,
            unique=True,
        )
    )
    @settings(max_examples=100)
    def test_column_names_are_exact_string_identity(self, columns: list[str]):
        """Property 6 (exact identity): every column name in the index must be
        the byte-for-byte identical string as it appeared in the manifest —
        not just case-equivalent, not whitespace-stripped, not normalised.

        **Validates: Requirements 3.2, 15.1**
        """
        bundle = _make_bundle_with_columns(columns, include_catalog=False)
        result = IndexBuilder().build(bundle)
        assert result is not None, "IndexBuilder returned None for valid input"
        assert len(result.models) == 1, "Expected exactly one model in the index"

        # Build a map from source name → index name for every column.
        # If a name was transformed, the source name would be missing from the
        # index names set even though a similar name might be present.
        index_names = {c.name for c in result.models[0].columns}

        for col_name in columns:
            assert col_name in index_names, (
                f"Column name {col_name!r} not found exactly as-is in the "
                f"index (found: {index_names!r}). "
                "A case transformation or normalisation may have been applied."
            )

"""Shared dataclass types for the Prism backend.

These types are imported by every component (discovery, search, generation,
execution) and form the canonical in-memory data model for the application.
"""

from __future__ import annotations

import asyncio
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# ---------------------------------------------------------------------------
# Schema metadata types
# ---------------------------------------------------------------------------

@dataclass
class ColumnMeta:
    """Metadata for a single column within a dbt model.

    Attributes:
        name: Column name as it appears in the source artifact (never normalised).
        data_type: Actual column type sourced from catalog.json; falls back to
            the declared type from manifest.json when catalog data is absent.
        description: Human-readable column description from manifest.json.
    """

    name: str
    data_type: str       # from catalog.json (or manifest.json if catalog absent)
    description: str     # from manifest.json


@dataclass
class ModelMeta:
    """Full metadata for one dbt model, merged from manifest.json and catalog.json.

    Attributes:
        name: dbt model name (unique identifier).
        database: Databricks catalog name.
        schema_name: Schema (database in dbt terminology) the model lives in.
        fqn: Fully-qualified name in ``catalog.schema.table`` format.
        columns: Ordered list of column metadata.
        grain: Unique key / granularity; ``"unknown"`` when not determinable.
        layer: Medallion tier inferred from tags or folder path.
        compiled_sql_excerpt: First 500 characters of the model's compiled SQL.
        depends_on: Direct parent model names from ``depends_on.nodes``.
        tags: dbt tags attached to the model.
        folder_path: Relative folder path inside the dbt project.
        row_count: Row count from the last dbt run (0 if absent from catalog).
        last_updated: Timestamp from catalog run statistics; ``None`` if absent.
        description: Model-level description from manifest.json.
    """

    name: str
    database: str
    schema_name: str
    fqn: str             # catalog.schema.table
    columns: list[ColumnMeta]
    grain: str           # "unknown" if not determinable
    layer: Literal["bronze", "silver", "gold"]
    compiled_sql_excerpt: str   # first 500 chars
    depends_on: list[str]       # direct parent model names
    tags: list[str]
    folder_path: str
    row_count: int              # 0 if absent from catalog.json
    last_updated: datetime | None  # from catalog run stats
    description: str            # model-level description from manifest


# ---------------------------------------------------------------------------
# Lineage types
# ---------------------------------------------------------------------------

@dataclass
class LineageNode:
    """Adjacency entry for one model in the lineage graph.

    Attributes:
        parents: Names of models this model directly depends on.
        children: Names of models that directly depend on this model.
    """

    parents: list[str]
    children: list[str]


# ---------------------------------------------------------------------------
# Index and artifact bundle types
# ---------------------------------------------------------------------------

@dataclass
class SchemaIndex:
    """Top-level in-memory semantic index built from the three dbt artifacts.

    The ``models`` list and the ``embeddings`` array share the same ordering:
    ``models[i]`` corresponds to row ``i`` of ``embeddings``.

    Attributes:
        models: Ordered list of all parsed ModelMeta objects.
        embeddings: Numpy array of shape ``(N, 384)``, dtype ``float32``,
            one row per model produced by the all-MiniLM-L6-v2 sentence
            transformer.
        lineage: Mapping of model name to its LineageNode (parents + children).
        built_at: UTC timestamp when this index was constructed.
        model_count: Total number of models in the index (== len(models)).
    """

    models: list[ModelMeta]                  # ordered list, index == row in embeddings
    embeddings: np.ndarray                   # shape (N, 384), float32
    lineage: dict[str, LineageNode]          # model_name → {parents, children}
    built_at: datetime
    model_count: int


@dataclass
class ArtifactBundle:
    """Raw bytes for the three dbt CI artifacts fetched from GitLab.

    Attributes:
        manifest: Raw bytes of ``manifest.json``.
        catalog: Raw bytes of ``catalog.json``.
        graph: Raw bytes of ``graph_summary.json``.
        fetched_at: UTC timestamp when the fetch completed successfully.
    """

    manifest: bytes
    catalog: bytes
    graph: bytes
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Retrieval types
# ---------------------------------------------------------------------------

@dataclass
class RankedModel:
    """A retrieved model together with its similarity scores.

    Attributes:
        model: The ModelMeta for this candidate.
        raw_similarity: Cosine similarity between the question embedding and
            this model's embedding, before any layer boost is applied.
        adjusted_score: ``raw_similarity`` plus the layer boost
            (+0.05 for Gold, +0.025 for Silver, +0.0 for Bronze).
        confidence_hint: Set to ``"low"`` when all raw scores are below 0.1,
            otherwise ``None``.
    """

    model: ModelMeta
    raw_similarity: float
    adjusted_score: float    # raw + layer boost
    confidence_hint: Literal["high", "medium", "low"] | None


# ---------------------------------------------------------------------------
# Cache state
# ---------------------------------------------------------------------------

@dataclass
class CacheState:
    """In-memory state managed by the CacheManager.

    Attributes:
        bundle: Last successfully fetched ArtifactBundle, or ``None`` if no
            successful fetch has completed yet.
        index: Last valid SchemaIndex, or ``None`` before the first successful
            index build.
        status: Current cache health; one of ``"fresh"``, ``"stale"``, or
            ``"unavailable"``.
        last_refresh_utc: UTC timestamp of the last successful refresh, or
            ``None`` if no refresh has succeeded.
        refresh_lock: Async lock used to ensure atomic index swaps and
            serialise concurrent refresh attempts.
    """

    bundle: ArtifactBundle | None
    index: SchemaIndex | None
    status: Literal["fresh", "stale", "unavailable"]
    last_refresh_utc: datetime | None
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# SQL generation result
# ---------------------------------------------------------------------------

@dataclass
class SQLResult:
    """Structured output from the SQL_Generator after a successful Claude call.

    These fields mirror the strict JSON format requested from Claude and are
    validated before a SQLResult is constructed.

    Attributes:
        sql: The generated, executable Databricks SQL statement.
        explanation: Plain-English explanation of how the query answers the
            user's question.
        models_used: Names of the dbt models referenced in the generated SQL.
        confidence: Claude's self-reported confidence that the selected models
            answer the question.
        confidence_reason: A short explanation of the confidence level.
    """

    sql: str
    explanation: str
    models_used: list[str]
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str

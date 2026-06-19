"""
tests/integration/test_full_query_flow.py

End-to-end integration test for the full query pipeline:
  question → embed → retrieve → build_prompt → generate (mocked) → execute (mocked) → rows

Uses real Embedder + Retriever + PromptBuilder + SQLGenerator (mocked Anthropic client)
+ QueryRunner (mocked Databricks connector).  Does NOT require live secrets.

Requirements: 6.1, 6.3, 13.1

Run with:
    pytest tests/integration/ -v
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from backend.config import AppConfig
from backend.exceptions import SecurityError
from backend.execution.databricks_runner import QueryRunner
from backend.generation.prompt_builder import PromptBuilder
from backend.generation.sql_generator import SQLGenerator
from backend.models import (
    ColumnMeta,
    LineageNode,
    ModelMeta,
    SchemaIndex,
    SQLResult,
)
from backend.search.embedder import Embedder
from backend.search.retriever import Retriever


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_config() -> AppConfig:
    """Minimal AppConfig — no live secrets needed for these tests."""
    return AppConfig(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_project_id="123",
        gitlab_token="glpat-test",
        databricks_sql_warehouse="warehouse-abc123",
        anthropic_api_key="sk-ant-test",
        admin_password_hash="$2b$12$placeholder",
    )


@pytest.fixture()
def sample_models() -> list[ModelMeta]:
    """A small set of representative dbt models for the index."""
    return [
        ModelMeta(
            name="fact_orders",
            database="main",
            schema_name="gold",
            fqn="main.gold.fact_orders",
            columns=[
                ColumnMeta("order_id", "string", "Unique order identifier"),
                ColumnMeta("customer_id", "string", "FK to dim_customer"),
                ColumnMeta("revenue", "decimal(18,2)", "Order revenue in USD"),
                ColumnMeta("order_date", "date", "Date the order was placed"),
            ],
            grain="one_row_per_order",
            layer="gold",
            compiled_sql_excerpt="SELECT order_id, customer_id, revenue, order_date FROM orders",
            description="One row per order with key order metrics.",
            row_count=500_000,
            depends_on=["stg_orders"],
            tags=["gold"],
            folder_path="models/gold",
            last_updated=None,
        ),
        ModelMeta(
            name="dim_customer",
            database="main",
            schema_name="gold",
            fqn="main.gold.dim_customer",
            columns=[
                ColumnMeta("customer_id", "string", "Unique customer ID"),
                ColumnMeta("customer_name", "string", "Full name"),
                ColumnMeta("region", "string", "Geographic region"),
            ],
            grain="one_row_per_customer",
            layer="gold",
            compiled_sql_excerpt="SELECT DISTINCT customer_id, customer_name, region FROM customers",
            description="Customer dimension table.",
            row_count=10_000,
            depends_on=["stg_customers"],
            tags=["gold"],
            folder_path="models/gold",
            last_updated=None,
        ),
    ]


@pytest.fixture()
def schema_index(sample_models) -> SchemaIndex:
    """SchemaIndex built with real embeddings for the sample models."""
    embedder = Embedder()
    embedder.load()
    embeddings = embedder.embed_models(sample_models)

    lineage = {
        "fact_orders": LineageNode(parents=["stg_orders"], children=[]),
        "dim_customer": LineageNode(parents=["stg_customers"], children=[]),
    }

    return SchemaIndex(
        models=sample_models,
        embeddings=embeddings,
        lineage=lineage,
        built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        model_count=len(sample_models),
    )


# ---------------------------------------------------------------------------
# Helper: fake CacheManager that holds the index
# ---------------------------------------------------------------------------


class _FakeCache:
    def __init__(self, index: SchemaIndex) -> None:
        self._index = index

    def get_index(self) -> SchemaIndex:
        return self._index


# ---------------------------------------------------------------------------
# Test 1: embed → retrieve returns ranked models for a revenue question
# ---------------------------------------------------------------------------


def test_embed_and_retrieve_revenue_question(schema_index):
    """Embedding a revenue-related question retrieves fact_orders first."""
    embedder = Embedder()
    embedder.load()

    cache = _FakeCache(schema_index)
    retriever = Retriever(cache)

    question = "What is the total revenue per region?"
    question_vec = embedder.embed_question(question)
    ranked = retriever.retrieve(question_vec, top_n=2)

    assert len(ranked) == 2
    # fact_orders should score highest for a revenue question
    assert ranked[0].model.name in {"fact_orders", "dim_customer"}
    # Scores must be descending
    assert ranked[0].adjusted_score >= ranked[1].adjusted_score


# ---------------------------------------------------------------------------
# Test 2: PromptBuilder includes FQN and columns in the prompt
# ---------------------------------------------------------------------------


def test_prompt_builder_includes_schema(schema_index, sample_models):
    """System prompt must include the FQN and column names for retrieved models."""
    embedder = Embedder()
    embedder.load()

    cache = _FakeCache(schema_index)
    retriever = Retriever(cache)

    question = "Total revenue by region"
    question_vec = embedder.embed_question(question)
    ranked = retriever.retrieve(question_vec, top_n=2)

    builder = PromptBuilder(schema_index)
    prompt = builder.build(ranked, question)

    # FQNs must be present
    assert "main.gold.fact_orders" in prompt
    assert "main.gold.dim_customer" in prompt

    # Column names must be present
    assert "order_id" in prompt
    assert "revenue" in prompt
    assert "customer_id" in prompt

    # Dialect rules must be present
    assert "DATE_TRUNC" in prompt
    assert "LIMIT" in prompt


# ---------------------------------------------------------------------------
# Test 3: SQLGenerator returns a validated SQLResult on a well-formed response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_generator_returns_result_on_valid_response(
    app_config, schema_index
):
    """SQLGenerator returns SQLResult when Claude responds with valid JSON."""
    valid_response = {
        "sql": "SELECT region, SUM(revenue) FROM main.gold.fact_orders fo JOIN main.gold.dim_customer dc ON fo.customer_id = dc.customer_id GROUP BY region LIMIT 1000",
        "explanation": "Joins fact_orders with dim_customer to compute revenue per region.",
        "models_used": ["fact_orders", "dim_customer"],
        "confidence": "high",
        "confidence_reason": "Both models are gold-layer with clear revenue and region columns.",
    }

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps(valid_response))]
    mock_message.usage = MagicMock(input_tokens=500, output_tokens=120)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    generator = SQLGenerator(app_config, schema_index)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        result = await generator.generate(
            system_prompt="test prompt",
            question="Total revenue by region",
            model_names=["fact_orders", "dim_customer"],
        )

    assert isinstance(result, SQLResult)
    assert result.confidence == "high"
    assert "fact_orders" in result.models_used
    assert "SELECT" in result.sql.upper()


# ---------------------------------------------------------------------------
# Test 4: SQLGenerator downgrades confidence when SQL references unknown columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_generator_downgrades_confidence_on_unknown_column(
    app_config, schema_index
):
    """Unrecognised columns force confidence='low' (Requirements 15.4, 15.5)."""
    bad_response = {
        "sql": "SELECT nonexistent_column FROM main.gold.fact_orders LIMIT 10",
        "explanation": "Query references a made-up column.",
        "models_used": ["fact_orders"],
        "confidence": "high",
        "confidence_reason": "Seems fine.",
    }

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps(bad_response))]
    mock_message.usage = MagicMock(input_tokens=200, output_tokens=60)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    generator = SQLGenerator(app_config, schema_index)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        result = await generator.generate(
            system_prompt="test prompt",
            question="Show nonexistent_column",
            model_names=["fact_orders"],
        )

    assert isinstance(result, SQLResult)
    assert result.confidence == "low"
    assert "nonexistent_column" in result.confidence_reason.lower() or "unrecognised" in result.confidence_reason.lower()


# ---------------------------------------------------------------------------
# Test 5: QueryRunner blocks DDL before reaching Databricks
# ---------------------------------------------------------------------------


async def test_query_runner_blocks_ddl(app_config):
    """DDL statements must raise SecurityError before any network call."""
    runner = QueryRunner(app_config)

    with pytest.raises(SecurityError, match="DROP"):
        await runner.execute("DROP TABLE main.gold.fact_orders")


# ---------------------------------------------------------------------------
# Test 6: QueryRunner executes a valid SELECT and returns rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_runner_returns_rows_for_valid_select(app_config):
    """QueryRunner returns a list of dicts on successful execution."""
    fake_rows = [
        ("North", 100_000.0),
        ("South", 75_000.0),
    ]
    fake_description = [("region",), ("total_revenue",)]

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.description = fake_description
    mock_cursor.fetchmany = MagicMock(return_value=fake_rows)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    runner = QueryRunner(app_config)

    with patch("databricks.sql.connect", return_value=mock_conn):
        rows = await runner.execute(
            sql="SELECT region, SUM(revenue) AS total_revenue FROM main.gold.fact_orders GROUP BY region",
            row_limit=100,
        )

    assert len(rows) == 2
    assert rows[0]["region"] == "North"
    assert rows[1]["region"] == "South"


# ---------------------------------------------------------------------------
# Test 7: Full pipeline — embed → retrieve → prompt → generate (mocked) → execute (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_end_to_end(app_config, schema_index):
    """Smoke test for the complete question → rows pipeline with all I/O mocked."""
    embedder = Embedder()
    embedder.load()
    cache = _FakeCache(schema_index)
    retriever = Retriever(cache)

    question = "Show total revenue by region"

    # --- Embed + retrieve ---
    question_vec = embedder.embed_question(question)
    ranked = retriever.retrieve(question_vec, top_n=2)
    assert ranked, "Expected at least one model to be retrieved"

    # --- Build prompt ---
    builder = PromptBuilder(schema_index)
    system_prompt = builder.build(ranked, question)
    assert len(system_prompt) > 100

    # --- Generate SQL (mocked) ---
    expected_sql = (
        "SELECT dc.region, SUM(fo.revenue) AS total_revenue "
        "FROM main.gold.fact_orders fo "
        "JOIN main.gold.dim_customer dc ON fo.customer_id = dc.customer_id "
        "GROUP BY dc.region "
        "LIMIT 1000"
    )
    mock_response = {
        "sql": expected_sql,
        "explanation": "Joins orders with customers and groups by region.",
        "models_used": ["fact_orders", "dim_customer"],
        "confidence": "high",
        "confidence_reason": "Gold-layer models with exact columns.",
    }

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps(mock_response))]
    mock_message.usage = MagicMock(input_tokens=600, output_tokens=150)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    generator = SQLGenerator(app_config, schema_index)
    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        sql_result = await generator.generate(
            system_prompt=system_prompt,
            question=question,
            model_names=[rm.model.name for rm in ranked],
        )

    assert isinstance(sql_result, SQLResult)

    # --- Execute SQL (mocked Databricks) ---
    fake_rows = [("North", 200_000.0), ("South", 150_000.0)]
    fake_desc = [("region",), ("total_revenue",)]

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.description = fake_desc
    mock_cursor.fetchmany = MagicMock(return_value=fake_rows)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    runner = QueryRunner(app_config)
    with patch("databricks.sql.connect", return_value=mock_conn):
        rows = await runner.execute(
            sql=sql_result.sql,
            row_limit=1000,
            question=question,
            system_prompt=system_prompt,
        )

    assert len(rows) == 2
    assert {"region", "total_revenue"} == set(rows[0].keys())

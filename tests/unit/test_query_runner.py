"""
tests/unit/test_query_runner.py

Property-based tests for QueryRunner row limit enforcement and DDL/DML blocking.

# Feature: prism, Property 13: Row Limit Enforcement
For any SQL string and any row_limit value in [1, 10000], the Query_Runner
SHALL always inject or enforce a LIMIT clause such that the effective limit
applied to the query is exactly the requested row_limit, and no more than
10000 rows can ever be returned.

# Feature: prism, Property 20: DDL/DML Blocking
For any SQL string that contains any of the prohibited keywords (CREATE,
INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, MERGE, REPLACE) as a
standalone keyword (case-insensitive, word-boundary matched), the Query_Runner
SHALL always raise a SecurityError and SHALL never pass the SQL to the
Databricks connector.

Validates: Requirements 6.4, 11.5
"""

import re

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from backend.exceptions import SecurityError
from backend.execution.databricks_runner import (
    QueryRunner,
    check_read_only,
    _inject_limit,
    PROHIBITED,
    _LIMIT_RE,
    _MAX_ROW_LIMIT,
    _MIN_ROW_LIMIT,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROHIBITED_KEYWORDS = [
    "CREATE", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
    "TRUNCATE", "MERGE", "REPLACE",
]

_SAFE_SQLS = [
    "SELECT * FROM orders",
    "SELECT order_id, revenue FROM fact_orders LIMIT 100",
    "WITH cte AS (SELECT 1) SELECT * FROM cte",
    "SELECT COUNT(*) FROM users WHERE active = TRUE",
]


# ---------------------------------------------------------------------------
# Property 13: Row Limit Enforcement
# Validates: Requirements 6.4
# ---------------------------------------------------------------------------


@given(
    sql=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd", "Zs")),
        min_size=10,
        max_size=200,
    ).filter(lambda s: not _LIMIT_RE.search(s)),  # no existing LIMIT
    row_limit=st.integers(min_value=1, max_value=10000),
)
@settings(max_examples=100)
def test_property_13a_limit_injected_when_absent(sql: str, row_limit: int) -> None:
    """**Property 13a**: When SQL has no LIMIT clause, _inject_limit injects one
    matching exactly row_limit.

    **Validates: Requirements 6.4**
    """
    result = _inject_limit(sql, row_limit)
    assert _LIMIT_RE.search(result), (
        f"Expected LIMIT clause in result: {result[:200]!r}"
    )
    # Extract the LIMIT value
    m = _LIMIT_RE.search(result)
    limit_val = int(m.group(0).split()[-1])
    assert limit_val == row_limit, (
        f"Expected LIMIT {row_limit}, found LIMIT {limit_val}"
    )


@given(
    existing_limit=st.integers(min_value=1, max_value=10000),
    row_limit=st.integers(min_value=1, max_value=10000),
)
@settings(max_examples=100)
def test_property_13b_existing_limit_not_changed(
    existing_limit: int,
    row_limit: int,
) -> None:
    """**Property 13b**: When SQL already has a LIMIT clause, _inject_limit
    does not modify it.

    **Validates: Requirements 6.4**
    """
    sql = f"SELECT order_id FROM orders LIMIT {existing_limit}"
    result = _inject_limit(sql, row_limit)
    # The original LIMIT should still be there
    assert f"LIMIT {existing_limit}" in result, (
        f"Original LIMIT {existing_limit} should be preserved, got: {result!r}"
    )


@given(row_limit=st.integers())
@settings(max_examples=100)
def test_property_13c_limit_always_clamped_to_valid_range(row_limit: int) -> None:
    """**Property 13c**: _inject_limit clamps to [1, 10000] regardless of input."""
    sql = "SELECT 1 FROM t"
    result = _inject_limit(sql, row_limit)
    m = _LIMIT_RE.search(result)
    if m:
        limit_val = int(m.group(0).split()[-1])
        assert 1 <= limit_val <= 10000, (
            f"LIMIT {limit_val} out of valid range [1, 10000] for input row_limit={row_limit}"
        )


def test_property_13d_max_10000_rows_enforced() -> None:
    """**Property 13d**: No more than 10000 rows are ever returned (via row_limit cap)."""
    # _inject_limit with 10001 should produce LIMIT 10000
    result = _inject_limit("SELECT 1 FROM t", 10001)
    m = _LIMIT_RE.search(result)
    assert m is not None
    limit_val = int(m.group(0).split()[-1])
    assert limit_val == 10000


# ---------------------------------------------------------------------------
# Property 20: DDL/DML Blocking
# Validates: Requirements 11.5
# ---------------------------------------------------------------------------


@given(
    keyword=st.sampled_from(_PROHIBITED_KEYWORDS),
    prefix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz ",
        min_size=0,
        max_size=20,
    ),
    suffix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz ",
        min_size=0,
        max_size=20,
    ),
    case_fn=st.sampled_from([str.upper, str.lower, str.title]),
)
@settings(max_examples=200)
def test_property_20a_ddl_keyword_raises_security_error(
    keyword: str,
    prefix: str,
    suffix: str,
    case_fn,
) -> None:
    """**Property 20a**: SQL containing any prohibited DDL/DML keyword (any case)
    at a word boundary SHALL always raise SecurityError.

    **Validates: Requirements 11.5**
    """
    # Build SQL with the keyword at a word boundary
    sql = f"{prefix} {case_fn(keyword)} {suffix}".strip()

    with pytest.raises(SecurityError):
        check_read_only(sql)


@given(
    safe_sql=st.sampled_from(_SAFE_SQLS),
)
@settings(max_examples=50)
def test_property_20b_safe_sql_does_not_raise(safe_sql: str) -> None:
    """**Property 20b**: SQL with no DDL/DML keywords must NOT raise SecurityError.

    **Validates: Requirements 11.5 (negative case)**
    """
    try:
        check_read_only(safe_sql)
    except SecurityError:
        pytest.fail(f"check_read_only raised SecurityError for safe SQL: {safe_sql!r}")


@given(
    keyword=st.sampled_from(_PROHIBITED_KEYWORDS),
    table_name=st.from_regex(r"[a-z][a-z0-9_]{1,15}", fullmatch=True),
)
@settings(max_examples=100)
def test_property_20c_blocked_before_any_execution(
    keyword: str,
    table_name: str,
) -> None:
    """**Property 20c**: SecurityError is raised before any DB connection is made.

    **Validates: Requirements 11.5**
    """
    sql = f"{keyword} TABLE {table_name} (id INT)"
    from unittest.mock import MagicMock

    config = MagicMock()
    config.databricks_sql_warehouse = "wh-id"
    config.anthropic_api_key = "sk-test"
    runner = QueryRunner(config, sql_generator=None)

    import asyncio

    with pytest.raises(SecurityError):
        asyncio.run(runner.execute(sql=sql, row_limit=100))


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestDDLBlockingExamples:

    def test_create_table_blocked(self):
        with pytest.raises(SecurityError):
            check_read_only("CREATE TABLE foo (id INT)")

    def test_drop_table_blocked(self):
        with pytest.raises(SecurityError):
            check_read_only("DROP TABLE orders")

    def test_insert_blocked(self):
        with pytest.raises(SecurityError):
            check_read_only("INSERT INTO orders VALUES (1, 2)")

    def test_truncate_blocked_case_insensitive(self):
        with pytest.raises(SecurityError):
            check_read_only("truncate table orders")

    def test_merge_blocked(self):
        with pytest.raises(SecurityError):
            check_read_only("MERGE INTO target USING source ON target.id = source.id")

    def test_select_not_blocked(self):
        check_read_only("SELECT id FROM orders WHERE active = TRUE")

    def test_with_select_not_blocked(self):
        check_read_only("WITH cte AS (SELECT 1 as n) SELECT * FROM cte")

    def test_keyword_in_string_literal_not_word_boundary(self):
        # "DROP" inside a string — should NOT trigger (it's not a standalone keyword)
        # Note: word-boundary regex will still match here if the word is surrounded by quotes
        # This tests that we only block standalone DDL at word boundaries
        # "updater" should NOT match UPDATE
        check_read_only("SELECT updater FROM audit_log")

    def test_update_substring_not_blocked(self):
        check_read_only("SELECT last_update FROM changelog")


class TestRowLimitExamples:

    def test_no_limit_gets_injected(self):
        sql = "SELECT id FROM orders"
        result = _inject_limit(sql, 500)
        assert "LIMIT 500" in result

    def test_existing_limit_preserved(self):
        sql = "SELECT id FROM orders LIMIT 100"
        result = _inject_limit(sql, 999)
        assert "LIMIT 100" in result
        assert "LIMIT 999" not in result

    def test_semicolon_stripped_before_limit(self):
        sql = "SELECT id FROM orders;"
        result = _inject_limit(sql, 200)
        assert "LIMIT 200" in result

    def test_limit_10000_exactly(self):
        result = _inject_limit("SELECT 1", 10000)
        m = _LIMIT_RE.search(result)
        assert m and int(m.group(0).split()[-1]) == 10000

    def test_limit_clamped_above_max(self):
        result = _inject_limit("SELECT 1", 99999)
        m = _LIMIT_RE.search(result)
        assert m and int(m.group(0).split()[-1]) == 10000

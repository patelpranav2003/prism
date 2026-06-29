"""
backend/execution/databricks_runner.py

QueryRunner — executes generated SQL against a Databricks SQL warehouse,
enforces the row limit, guards against DDL/DML statements, and auto-retries
up to twice on warehouse error by asking the SQLGenerator for a corrected query.

Design decisions:
- Uses ``databricks-sql-connector`` with workspace OAuth.
- DDL/DML guard runs BEFORE any network call (Requirement 11.5).
- Row limit is enforced by injecting ``LIMIT {row_limit}`` when no LIMIT
  clause is detected in the SQL (Requirement 6.4).
- Auto-retry (up to 2 attempts):
    Attempt 1 — classifies the Databricks error type (column not found,
    type mismatch, ambiguous column, syntax error, etc.) and injects a
    targeted fix instruction into the retry prompt so the model knows
    exactly what kind of mistake to fix.
    Attempt 2 — if attempt 1 also fails, sends both error messages and
    both failed SQL strings so the model has the full failure context.
    A double failure surfaces a safe non-technical error (Req 6.6–6.8).
- Fallback to workspace default warehouse if the configured warehouse ID is
  invalid (Requirement 14.4).

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 11.2, 11.5, 14.4
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from backend.config import AppConfig
from backend.exceptions import SecurityError, GenerationError
from backend.models import SQLResult

logger = logging.getLogger(__name__)

# DDL/DML keywords that must never reach the warehouse
PROHIBITED = re.compile(
    r"\b(CREATE|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|MERGE|REPLACE)\b",
    re.IGNORECASE,
)

# Detect existing LIMIT clause in SQL
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE)

_MAX_ROW_LIMIT = 10_000
_MIN_ROW_LIMIT = 1

_SAFE_ERROR_MESSAGE = (
    "Unable to execute the generated query. Please try rephrasing your question "
    "or contact your data team if the issue persists."
)

# Type alias for a result row
ResultRow = dict[str, object]


# ---------------------------------------------------------------------------
# Error classification for targeted retry prompts
# ---------------------------------------------------------------------------

def _classify_error(error_msg: str) -> str:
    """Return a short error-type key from a Databricks SQL error message."""
    msg = error_msg.lower()
    if any(k in msg for k in ("cannot resolve", "column_not_found", "unresolved column", "no such struct field")):
        return "column_not_found"
    if any(k in msg for k in ("table_or_view_not_found", "table or view not found", "nosuchnamespaceexception")):
        return "table_not_found"
    if any(k in msg for k in ("datatype_mismatch", "cannot cast", "type mismatch", "type error", "incompatible types")):
        return "type_mismatch"
    if any(k in msg for k in ("ambiguous", "ambiguous_column_or_field", "reference is ambiguous")):
        return "ambiguous_column"
    if any(k in msg for k in ("parse_syntax_error", "syntax error", "mismatched input", "extraneous input", "no viable alternative")):
        return "syntax_error"
    if any(k in msg for k in ("division by zero", "divide by zero", "division_by_zero")):
        return "division_by_zero"
    return "generic"


_RETRY_INSTRUCTIONS: dict[str, str] = {
    "column_not_found": (
        "ERROR TYPE: COLUMN NOT FOUND.\n"
        "Fix: check each column name against the schema — it is likely misspelled, belongs to "
        "the wrong table, or requires a JOIN that is missing. Use ONLY column names listed in "
        "the schema blocks. Do not change the query logic — fix only the column references."
    ),
    "table_not_found": (
        "ERROR TYPE: TABLE OR VIEW NOT FOUND.\n"
        "Fix: use the EXACT fully qualified name `catalog.schema.table` shown in the schema. "
        "Use the dbt alias (the table name as it appears in the schema block header), not the "
        "dbt model name. Do not invent table names — only use tables listed in the schema."
    ),
    "type_mismatch": (
        "ERROR TYPE: TYPE MISMATCH.\n"
        "Fix: a column or literal is being compared to or cast into an incompatible type. "
        "Use explicit CAST(column AS type) where needed. Do not compare a string column to "
        "an integer literal — quote it. For dates use TO_DATE() or CAST(col AS DATE)."
    ),
    "ambiguous_column": (
        "ERROR TYPE: AMBIGUOUS COLUMN.\n"
        "Fix: a column name exists in more than one joined table. Qualify EVERY column reference "
        "with its table alias (e.g. `t1.brand_id`, not just `brand_id`). Apply this to all "
        "columns in SELECT, WHERE, JOIN ON, GROUP BY, and ORDER BY."
    ),
    "syntax_error": (
        "ERROR TYPE: SQL SYNTAX ERROR.\n"
        "Fix: check that (1) all parentheses are balanced, (2) there are no trailing commas "
        "before FROM or GROUP BY, (3) every subquery has an alias, (4) CTEs use "
        "`WITH name AS (...)` syntax, (5) QUALIFY is used instead of a subquery WHERE on a "
        "window function."
    ),
    "division_by_zero": (
        "ERROR TYPE: DIVISION BY ZERO.\n"
        "Fix: wrap every denominator in NULLIF to avoid this: "
        "`numerator / NULLIF(denominator, 0)` instead of `numerator / denominator`."
    ),
    "generic": (
        "Carefully re-read the error message and the schema. Fix only the part of the SQL "
        "that caused the error. Do not restructure the entire query."
    ),
}


def check_read_only(sql: str) -> None:
    """Raise :class:`~backend.exceptions.SecurityError` if *sql* contains any prohibited keyword.

    Word-boundary matched, case-insensitive.  Must be called before any SQL
    is forwarded to the Databricks connector (Requirement 11.5).

    Parameters
    ----------
    sql:
        The SQL string to check.

    Raises
    ------
    SecurityError
        If a prohibited keyword is detected.
    """
    match = PROHIBITED.search(sql)
    if match:
        logger.warning(
            "QueryRunner: DDL/DML keyword '%s' detected in generated SQL "
            "(first 500 chars): %r — blocking execution",
            match.group(),
            sql[:500],
        )
        raise SecurityError(
            f"Prohibited keyword '{match.group()}' detected in generated SQL"
        )


def _inject_limit(sql: str, row_limit: int) -> str:
    """Return *sql* with a LIMIT clause injected if one is not already present.

    Clamps *row_limit* to [1, 10000] (Requirement 6.4).
    """
    clamped = max(_MIN_ROW_LIMIT, min(_MAX_ROW_LIMIT, row_limit))
    if _LIMIT_RE.search(sql):
        return sql
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {clamped}"


class QueryRunner:
    """Executes SQL against a Databricks SQL warehouse.

    Parameters
    ----------
    config:
        Application configuration (warehouse ID, credentials).
    sql_generator:
        Optional :class:`~backend.generation.sql_generator.SQLGenerator`
        for auto-retry.  If ``None``, auto-retry is skipped.

    Usage::

        runner = QueryRunner(config, sql_generator)
        async for row in runner.execute(sql, row_limit=1000):
            process(row)
    """

    def __init__(
        self,
        config: AppConfig,
        sql_generator: object | None = None,
    ) -> None:
        self._config = config
        self._sql_generator = sql_generator

    async def execute(
        self,
        sql: str,
        row_limit: int = 1000,
        question: str = "",
        system_prompt: str = "",
        model_names: list[str] | None = None,
    ) -> list[ResultRow]:
        """Execute *sql* and return up to *row_limit* rows.

        Parameters
        ----------
        sql:
            The Databricks SQL query to execute.
        row_limit:
            Maximum rows to return.  Clamped to [1, 10000].
        question:
            Original user question — used for auto-retry prompt context.
        system_prompt:
            System prompt — used for auto-retry.
        model_names:
            Model names selected — used for auto-retry logging.

        Returns
        -------
        list[ResultRow]
            Each row is a ``dict[str, object]``.

        Raises
        ------
        SecurityError
            If the SQL contains DDL/DML keywords.
        RuntimeError
            If both the initial execution and the auto-retry fail.
        """
        # --- DDL/DML guard (Requirement 11.5) ---
        check_read_only(sql)

        # --- Row limit enforcement (Requirement 6.4) ---
        clamped_limit = max(_MIN_ROW_LIMIT, min(_MAX_ROW_LIMIT, row_limit))
        effective_sql = _inject_limit(sql, clamped_limit)

        # --- Execute (with optional auto-retry) ---
        start_ms = int(time.monotonic() * 1000)

        try:
            rows = await self._run_query(effective_sql, clamped_limit)
            elapsed_ms = int(time.monotonic() * 1000) - start_ms
            logger.info(
                "QueryRunner: executed successfully — rows=%d, elapsed_ms=%d, "
                "warehouse=%s",
                len(rows),
                elapsed_ms,
                self._config.databricks_sql_warehouse,
            )
            return rows

        except Exception as first_exc:
            logger.error(
                "QueryRunner: first execution failed — %s: %s; sql[:2000]=%r",
                type(first_exc).__name__,
                str(first_exc)[:500],
                effective_sql[:2000],
            )

            # --- Auto-retry (Requirements 6.6–6.8) ---
            if self._sql_generator is not None and question:
                logger.info("QueryRunner: attempting auto-retry via SQLGenerator")
                retry_result = await self._auto_retry(
                    question=question,
                    failed_sql=effective_sql,
                    error_msg=str(first_exc),
                    system_prompt=system_prompt,
                    model_names=model_names,
                    row_limit=clamped_limit,
                )
                if retry_result is not None:
                    elapsed_ms = int(time.monotonic() * 1000) - start_ms
                    logger.info(
                        "QueryRunner: auto-retry succeeded — rows=%d, elapsed_ms=%d",
                        len(retry_result),
                        elapsed_ms,
                    )
                    return retry_result

            # Both attempts failed (or no retry configured)
            raise RuntimeError(_SAFE_ERROR_MESSAGE) from first_exc

    async def _auto_retry(
        self,
        question: str,
        failed_sql: str,
        error_msg: str,
        system_prompt: str,
        model_names: list[str] | None,
        row_limit: int,
    ) -> list[ResultRow] | None:
        """Ask the SQLGenerator for a corrected SQL and retry up to twice.

        Attempt 1: classifies the error type and injects targeted fix instructions
        so the model knows exactly what kind of mistake to correct.

        Attempt 2: if attempt 1 produces SQL that also fails, sends both error
        messages and both failed SQL strings for full failure context.

        Returns ``None`` if both retries fail or SQLGenerator is unavailable.
        """
        if self._sql_generator is None:
            return None

        # --- Attempt 1: targeted fix based on error classification ---
        error_type = _classify_error(error_msg)
        fix_hint = _RETRY_INSTRUCTIONS[error_type]
        logger.info("QueryRunner: retry 1 — error classified as '%s'", error_type)

        attempt1_question = (
            f"{question}\n\n"
            f"[RETRY 1 — FIX REQUIRED]\n"
            f"{fix_hint}\n\n"
            f"Databricks error:\n{error_msg}\n\n"
            f"Failed SQL:\n{failed_sql}\n\n"
            f"Generate corrected SQL that avoids this specific error. "
            f"Return ONLY the JSON response — no explanation outside the JSON."
        )

        attempt1_sql = await self._generate_and_validate(
            system_prompt, attempt1_question, model_names, row_limit, attempt=1
        )
        if attempt1_sql is None:
            return None

        try:
            rows = await self._run_query(attempt1_sql, row_limit)
            logger.info("QueryRunner: retry 1 succeeded — rows=%d", len(rows))
            return rows
        except Exception as exc1:
            logger.error(
                "QueryRunner: retry 1 execution failed — %s: %s; sql[:2000]=%r",
                type(exc1).__name__,
                str(exc1)[:500],
                attempt1_sql[:2000],
            )

            # --- Attempt 2: full failure context from both errors ---
            error_type2 = _classify_error(str(exc1))
            fix_hint2 = _RETRY_INSTRUCTIONS[error_type2]
            logger.info(
                "QueryRunner: retry 2 — second error classified as '%s'", error_type2
            )

            attempt2_question = (
                f"{question}\n\n"
                f"[RETRY 2 — TWO CONSECUTIVE FAILURES]\n"
                f"The original SQL and a first correction have both failed. "
                f"Read both errors carefully and produce a correct query from scratch.\n\n"
                f"FAILURE 1\n"
                f"Error: {error_msg}\n"
                f"SQL:\n{failed_sql}\n\n"
                f"FAILURE 2\n"
                f"{fix_hint2}\n"
                f"Error: {str(exc1)}\n"
                f"SQL:\n{attempt1_sql}\n\n"
                f"Generate a completely correct SQL query. "
                f"Return ONLY the JSON response — no explanation outside the JSON."
            )

            attempt2_sql = await self._generate_and_validate(
                system_prompt, attempt2_question, model_names, row_limit, attempt=2
            )
            if attempt2_sql is None:
                return None

            try:
                rows = await self._run_query(attempt2_sql, row_limit)
                logger.info("QueryRunner: retry 2 succeeded — rows=%d", len(rows))
                return rows
            except Exception as exc2:
                logger.error(
                    "QueryRunner: retry 2 execution also failed — %s: %s; sql[:2000]=%r",
                    type(exc2).__name__,
                    str(exc2)[:500],
                    attempt2_sql[:2000],
                )
                return None

    async def _generate_and_validate(
        self,
        system_prompt: str,
        question: str,
        model_names: list[str] | None,
        row_limit: int,
        attempt: int,
    ) -> str | None:
        """Call SQLGenerator, validate, DDL-guard, and inject limit.

        Returns the ready-to-execute SQL string, or None on any failure.
        """
        try:
            result = await self._sql_generator.generate(  # type: ignore[union-attr]
                system_prompt=system_prompt,
                question=question,
                model_names=model_names,
            )
        except Exception as exc:
            logger.error(
                "QueryRunner: SQLGenerator unavailable during retry %d — %s: %s",
                attempt,
                type(exc).__name__,
                str(exc),
            )
            return None

        if isinstance(result, GenerationError):
            logger.error(
                "QueryRunner: SQLGenerator returned error on retry %d — %s",
                attempt,
                result,
            )
            return None

        try:
            check_read_only(result.sql)
        except SecurityError:
            logger.error(
                "QueryRunner: retry %d SQL blocked by DDL/DML guard", attempt
            )
            return None

        return _inject_limit(result.sql, row_limit)

    async def _run_query(self, sql: str, row_limit: int) -> list[ResultRow]:
        """Run *sql* via the Databricks SQL connector and return rows.

        This is a blocking call wrapped in ``asyncio.to_thread`` to avoid
        blocking the event loop.
        """
        return await asyncio.to_thread(
            self._run_query_sync, sql, row_limit
        )

    def _run_query_sync(self, sql: str, row_limit: int) -> list[ResultRow]:
        """Synchronous Databricks SQL execution — runs in a thread pool."""
        import os as _os
        from databricks import sql as dbsql  # type: ignore[import]

        warehouse_id = self._config.databricks_sql_warehouse
        server_hostname = self._config.databricks_server_hostname or None

        logger.debug(
            "QueryRunner: connecting to warehouse %s",
            "***MASKED***",  # Never log the warehouse ID plainly
        )

        # Fall back to DATABRICKS_HOST when no explicit hostname is configured.
        # databricks-sql-connector requires server_hostname — it does not infer it.
        if not server_hostname:
            host = _os.environ.get("DATABRICKS_HOST", "")
            if host:
                server_hostname = host.removeprefix("https://").removeprefix("http://").rstrip("/")

        if not server_hostname:
            raise RuntimeError(
                "Databricks server hostname is not configured. "
                "Set DATABRICKS_SERVER_HOSTNAME or DATABRICKS_HOST in your .env file."
            )

        # Accept either a full path (/sql/1.0/warehouses/<id>) or just the ID
        http_path = warehouse_id if warehouse_id.startswith("/") else f"/sql/1.0/warehouses/{warehouse_id}"

        connection_kwargs: dict[str, object] = {
            "server_hostname": server_hostname,
            "http_path": http_path,
        }

        # Local dev: use PAT from DATABRICKS_TOKEN if set.
        # credentials_provider must return a HeaderFactory (a callable returning headers),
        # not the headers dict directly.
        # Production (Databricks Apps): omit credentials_provider; workspace OAuth handles auth.
        _token = _os.environ.get("DATABRICKS_TOKEN", "")
        if _token:
            connection_kwargs["credentials_provider"] = lambda: (lambda: {"Authorization": f"Bearer {_token}"})

        try:
            with dbsql.connect(**connection_kwargs) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    columns = [desc[0] for desc in (cursor.description or [])]
                    rows: list[ResultRow] = []
                    fetched = 0
                    for raw_row in cursor.fetchmany(row_limit):
                        rows.append(dict(zip(columns, raw_row)))
                        fetched += 1
                        if fetched >= row_limit:
                            break
                    return rows
        except Exception:
            # Let _run_query handle the exception — don't swallow it
            raise

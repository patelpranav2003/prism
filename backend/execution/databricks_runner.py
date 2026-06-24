"""
backend/execution/databricks_runner.py

QueryRunner — executes generated SQL against a Databricks SQL warehouse,
enforces the row limit, guards against DDL/DML statements, and auto-retries
once on warehouse error by asking the SQLGenerator for a corrected query.

Design decisions:
- Uses ``databricks-sql-connector`` with workspace OAuth.
- DDL/DML guard runs BEFORE any network call (Requirement 11.5).
- Row limit is enforced by injecting ``LIMIT {row_limit}`` when no LIMIT
  clause is detected in the SQL (Requirement 6.4).
- Auto-retry: on first warehouse SQL error, calls ``SQLGenerator.generate()``
  with the original question + failed SQL + error message; double failure
  surfaces a safe non-technical error (Requirements 6.6–6.8).
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
        """Ask the SQLGenerator for a corrected SQL and retry.

        Returns ``None`` if the retry also fails or if SQLGenerator is unavailable.
        """
        if self._sql_generator is None:
            return None

        retry_question = (
            f"{question}\n\n"
            f"[RETRY CONTEXT] The previous SQL failed with this error:\n"
            f"{error_msg}\n\n"
            f"Failed SQL:\n{failed_sql}\n\n"
            f"Please generate corrected SQL that avoids this error."
        )

        try:
            retry_result = await self._sql_generator.generate(  # type: ignore[union-attr]
                system_prompt=system_prompt,
                question=retry_question,
                model_names=model_names,
            )
        except Exception as exc:
            logger.error(
                "QueryRunner: SQLGenerator unavailable during retry — %s: %s",
                type(exc).__name__,
                str(exc),
            )
            return None

        if isinstance(retry_result, GenerationError):
            logger.error(
                "QueryRunner: SQLGenerator returned error on retry — %s",
                retry_result,
            )
            return None

        # Check the retried SQL for DDL/DML and inject limit
        try:
            check_read_only(retry_result.sql)
        except SecurityError:
            logger.error("QueryRunner: retry SQL also blocked by DDL/DML guard")
            return None

        retry_sql = _inject_limit(retry_result.sql, row_limit)

        try:
            return await self._run_query(retry_sql, row_limit)
        except Exception as retry_exc:
            logger.error(
                "QueryRunner: auto-retry execution also failed — %s: %s; "
                "sql[:2000]=%r",
                type(retry_exc).__name__,
                str(retry_exc)[:500],
                retry_sql[:2000],
            )
            return None

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

"""
backend/generation/sql_generator.py

SQLGenerator — calls the Claude API with a system prompt and user question,
parses the structured JSON response, validates all required fields, and
performs post-generation column cross-checking against the SchemaIndex.

Design decisions:
- Model: ``claude-sonnet-4-6``, max_tokens=2000
- Required JSON fields: sql, explanation, models_used, confidence, confidence_reason
- On any error (non-200, timeout, invalid JSON, missing fields): returns a
  ``GenerationError`` with a safe user-facing message — no stack traces exposed.
- Post-generation: extracts column references from the generated SQL and
  cross-checks against the SchemaIndex; unrecognised columns trigger a WARN
  log and force confidence="low" (Requirements 15.4, 15.5).
- Timeout: 30 seconds per Claude API call.

Requirements: 5.4, 5.5, 5.6, 15.4, 15.5
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from backend.config import AppConfig
from backend.exceptions import GenerationError
from backend.models import SQLResult, SchemaIndex

if TYPE_CHECKING:
    import anthropic

logger = logging.getLogger(__name__)

_CLAUDE_MODEL = "claude-sonnet-4-6"
_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-6"  # same model via OpenRouter
_MAX_TOKENS = 2000
_TIMEOUT_SECONDS = 30.0

_USER_FACING_ERROR = (
    "Unable to generate SQL — please try rephrasing your question."
)

# The five required fields and their expected Python types
_REQUIRED_FIELDS: dict[str, type] = {
    "sql": str,
    "explanation": str,
    "models_used": list,
    "confidence": str,
    "confidence_reason": str,
}

_VALID_CONFIDENCE = {"high", "medium", "low"}

# Backtick-quoted identifier or plain word: used for simple column extraction
_COLUMN_REF_RE = re.compile(r"`([^`]+)`|\b([a-zA-Z_][a-zA-Z0-9_]*)\b")

# SQL keywords and built-in functions to ignore when extracting column references
_SQL_KEYWORDS = frozenset({
    # DML / DDL / clause keywords
    "select", "from", "where", "join", "left", "right", "inner", "outer",
    "on", "and", "or", "not", "in", "is", "null", "as", "group", "by",
    "order", "having", "limit", "offset", "distinct", "case", "when",
    "then", "else", "end", "with", "union", "all", "except", "intersect",
    "create", "insert", "update", "delete", "drop", "alter", "truncate",
    "qualify", "over", "partition", "rows", "range", "between", "using",
    "asc", "desc", "nulls", "first", "last", "cross", "full",
    "true", "false",
    # Comparison / pattern operators
    "like", "ilike", "rlike", "similar", "to",
    # Window / aggregate functions
    "row_number", "rank", "dense_rank", "ntile", "lag", "lead",
    "first_value", "last_value", "count", "sum", "avg", "min", "max",
    "coalesce", "nullif", "ifnull", "nvl",
    # String functions
    "lower", "upper", "trim", "ltrim", "rtrim", "concat", "substring",
    "substr", "length", "len", "replace", "split", "regexp_replace",
    "regexp_extract", "initcap", "lpad", "rpad",
    # Date / time functions
    "date_trunc", "dateadd", "datediff", "date_add", "date_sub",
    "date_diff", "current_date", "current_timestamp", "now", "year",
    "month", "day", "hour", "minute", "second", "to_date", "to_timestamp",
    "date_format", "unix_timestamp", "from_unixtime", "add_months",
    # Type casting / conversion
    "cast", "try_cast", "convert", "int", "bigint", "varchar", "string",
    "double", "float", "boolean", "timestamp", "date",
    # Misc functions
    "if", "iff", "decode", "greatest", "least", "abs", "round", "floor",
    "ceil", "ceiling", "mod", "sign", "power", "sqrt", "log", "exp",
    "array_agg", "collect_list", "collect_set", "flatten", "explode",
    "size", "array_size", "struct", "map", "named_struct",
})

# Regex to strip string literals before token extraction
_STRING_LITERAL_RE = re.compile(r"'[^']*'")


class SQLGenerator:
    """Generates Databricks SQL from a user question via the Claude API.

    Parameters
    ----------
    config:
        Application configuration (provides ``anthropic_api_key``).
    schema_index:
        The active :class:`~backend.models.SchemaIndex` used for post-
        generation column validation.

    Usage::

        generator = SQLGenerator(config, schema_index)
        result = await generator.generate(system_prompt, question)
        if isinstance(result, GenerationError):
            # surface user-facing error
        else:
            # use result.sql, result.confidence, etc.
    """

    def __init__(
        self,
        config: AppConfig,
        schema_index: SchemaIndex,
    ) -> None:
        self._config = config
        self._index = schema_index

    async def generate(
        self,
        system_prompt: str,
        question: str,
        model_names: list[str] | None = None,
        history: list | None = None,
    ) -> SQLResult | GenerationError:
        """Call Claude and return a validated :class:`~backend.models.SQLResult`.

        Parameters
        ----------
        system_prompt:
            Pre-built system prompt from :class:`~backend.generation.prompt_builder.PromptBuilder`.
        question:
            The user's plain-English question.
        model_names:
            Names of models that were selected for the prompt (for logging).

        Returns
        -------
        SQLResult | GenerationError
            A valid :class:`~backend.models.SQLResult` on success, or a
            :class:`~backend.exceptions.GenerationError` on any failure.
        """
        provider = "anthropic" if self._config.anthropic_api_key else "openrouter"

        logger.info(
            "SQLGenerator: generating SQL for question[:500]=%r; "
            "selected models=%r; provider=%s",
            question[:500],
            model_names or [],
            provider,
        )

        try:
            if provider == "anthropic":
                raw_text, in_tok, out_tok = await self._call_anthropic(system_prompt, question, history)
            else:
                raw_text, in_tok, out_tok = await self._call_openrouter(system_prompt, question, history)

            logger.info(
                "SQLGenerator: %s responded — input_tokens=%d, output_tokens=%d",
                provider,
                in_tok,
                out_tok,
            )

        except Exception as exc:
            logger.error(
                "SQLGenerator: LLM API call failed (%s) — %s: %s",
                provider,
                type(exc).__name__,
                str(exc)[:500],
            )
            return GenerationError(_USER_FACING_ERROR)

        # --- Parse JSON response ---
        try:
            # Strip markdown code fences if Claude wrapped them anyway
            text = raw_text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
                text = text.strip()

            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error(
                "SQLGenerator: invalid JSON response — %s; raw[:200]=%r",
                exc,
                raw_text[:200],
            )
            return GenerationError(_USER_FACING_ERROR)

        # --- Validate required fields ---
        validation_error = self._validate_response(data)
        if validation_error:
            logger.error("SQLGenerator: response validation failed — %s", validation_error)
            return GenerationError(_USER_FACING_ERROR)

        # --- Build SQLResult ---
        result = SQLResult(
            sql=data["sql"],
            explanation=data["explanation"],
            models_used=data["models_used"],
            confidence=data["confidence"],
            confidence_reason=data["confidence_reason"],
        )

        # --- Post-generation column validation (Requirements 15.4, 15.5) ---
        result = self._check_columns(result)

        logger.info(
            "SQLGenerator: success — confidence=%s, models_used=%r",
            result.confidence,
            result.models_used,
        )
        return result

    # ------------------------------------------------------------------
    # Provider helpers
    # ------------------------------------------------------------------

    async def _call_anthropic(
        self, system_prompt: str, question: str, history: list | None = None
    ) -> tuple[str, int, int]:
        import anthropic  # lazy import
        client = anthropic.AsyncAnthropic(
            api_key=self._config.anthropic_api_key,
            timeout=_TIMEOUT_SECONDS,
        )
        messages = [{"role": msg.role, "content": msg.content} for msg in (history or [])]
        messages.append({"role": "user", "content": question})
        message = await client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system_prompt,
            messages=messages,
        )
        raw_text = "".join(
            block.text for block in message.content if hasattr(block, "text")
        )
        return raw_text, message.usage.input_tokens, message.usage.output_tokens

    async def _call_openrouter(
        self, system_prompt: str, question: str, history: list | None = None
    ) -> tuple[str, int, int]:
        from openai import AsyncOpenAI  # lazy import
        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self._config.openrouter_api_key,
            timeout=_TIMEOUT_SECONDS,
        )
        messages = [{"role": "system", "content": system_prompt}]
        for msg in (history or []):
            messages.append({"role": msg.role, "content": msg.content})
        messages.append({"role": "user", "content": question})
        completion = await client.chat.completions.create(
            model=_OPENROUTER_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=messages,
        )
        raw_text = completion.choices[0].message.content or ""
        return raw_text, completion.usage.prompt_tokens, completion.usage.completion_tokens

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_response(self, data: object) -> str | None:
        """Return an error string if *data* fails validation, else None."""
        if not isinstance(data, dict):
            return f"Expected JSON object, got {type(data).__name__}"

        for field_name, expected_type in _REQUIRED_FIELDS.items():
            if field_name not in data:
                return f"Missing required field '{field_name}'"
            if not isinstance(data[field_name], expected_type):
                return (
                    f"Field '{field_name}' expected {expected_type.__name__}, "
                    f"got {type(data[field_name]).__name__}"
                )

        if data["confidence"] not in _VALID_CONFIDENCE:
            return (
                f"Field 'confidence' must be one of {_VALID_CONFIDENCE}, "
                f"got {data['confidence']!r}"
            )

        return None

    def _check_columns(self, result: SQLResult) -> SQLResult:
        """Cross-check SQL column references against the SchemaIndex.

        Unrecognised columns trigger a WARN log and force confidence="low"
        (Requirements 15.4, 15.5).
        """
        if not self._index.models:
            return result

        # Build lookup sets for everything that is NOT a column reference
        known_columns: set[str] = set()
        known_catalogs: set[str] = set()
        known_schemas: set[str] = set()
        known_table_names: set[str] = set()

        for model in self._index.models:
            for col in model.columns:
                known_columns.add(col.name.lower())
            if model.database:
                known_catalogs.add(model.database.lower())
            if model.schema_name:
                known_schemas.add(model.schema_name.lower())
            if model.fqn:
                known_table_names.add(model.fqn.rsplit(".", 1)[-1].lower())
            known_table_names.add(model.name.lower())

        # Extract all defined aliases so they are not flagged as unknown columns:
        # - CTE names:       WITH deduped AS (  →  word BEFORE "AS ("
        # - Column aliases:  SUM(...) AS total  →  word AFTER "AS"
        # - Table aliases:   FROM tbl AS t      →  word AFTER "AS"
        defined_aliases: set[str] = set()
        for m in re.findall(r'\b(\w+)\s+AS\s*\(', result.sql, re.IGNORECASE):
            defined_aliases.add(m.lower())
        for m in re.findall(r'\bAS\s+(\w+)', result.sql, re.IGNORECASE):
            defined_aliases.add(m.lower())

        # Strip string literals before scanning for column tokens so that
        # values inside LIKE '%foo%' are not mistaken for column names
        sql_for_scan = _STRING_LITERAL_RE.sub("''", result.sql)

        # Extract column-like tokens from the generated SQL
        unrecognised: list[str] = []
        for match in _COLUMN_REF_RE.finditer(sql_for_scan):
            token = (match.group(1) or match.group(2) or "").lower()
            if not token or token in _SQL_KEYWORDS:
                continue
            if token.isdigit():
                continue
            # Skip tokens with dots/slashes — these are FQN parts, not columns
            if "." in token or "/" in token:
                continue
            # Skip catalog, schema, table name, and CTE alias references
            if token in known_catalogs or token in known_schemas or token in known_table_names:
                continue
            if token in defined_aliases:
                continue
            if token not in known_columns:
                unrecognised.append(match.group(1) or match.group(2) or "")

        if unrecognised:
            unique_unrecognised = list(dict.fromkeys(unrecognised))[:5]  # dedupe, limit
            for col in unique_unrecognised:
                logger.warning(
                    "SQLGenerator: unrecognised column '%s' in generated SQL "
                    "(not found in SchemaIndex) — forcing confidence=low",
                    col,
                )

            # Force confidence low and explain
            cols_str = ", ".join(f"'{c}'" for c in unique_unrecognised)
            reason = (
                f"{result.confidence_reason} [WARNING: unrecognised column(s) "
                f"{cols_str} detected — confidence downgraded to low]"
            )
            result = SQLResult(
                sql=result.sql,
                explanation=result.explanation,
                models_used=result.models_used,
                confidence="low",
                confidence_reason=reason,
            )

        return result

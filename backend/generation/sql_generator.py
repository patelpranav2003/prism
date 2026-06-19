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

# SQL keywords to ignore when extracting column references
_SQL_KEYWORDS = frozenset({
    "select", "from", "where", "join", "left", "right", "inner", "outer",
    "on", "and", "or", "not", "in", "is", "null", "as", "group", "by",
    "order", "having", "limit", "offset", "distinct", "case", "when",
    "then", "else", "end", "with", "union", "all", "except", "intersect",
    "create", "insert", "update", "delete", "drop", "alter", "truncate",
    "qualify", "over", "partition", "rows", "range", "between", "row_number",
    "rank", "dense_rank", "date_trunc", "dateadd", "datediff", "true",
    "false", "asc", "desc", "nulls", "first", "last", "using",
})


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
                raw_text, in_tok, out_tok = await self._call_anthropic(system_prompt, question)
            else:
                raw_text, in_tok, out_tok = await self._call_openrouter(system_prompt, question)

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
        self, system_prompt: str, question: str
    ) -> tuple[str, int, int]:
        import anthropic  # lazy import
        client = anthropic.AsyncAnthropic(
            api_key=self._config.anthropic_api_key,
            timeout=_TIMEOUT_SECONDS,
        )
        message = await client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        raw_text = "".join(
            block.text for block in message.content if hasattr(block, "text")
        )
        return raw_text, message.usage.input_tokens, message.usage.output_tokens

    async def _call_openrouter(
        self, system_prompt: str, question: str
    ) -> tuple[str, int, int]:
        from openai import AsyncOpenAI  # lazy import
        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self._config.openrouter_api_key,
            timeout=_TIMEOUT_SECONDS,
        )
        completion = await client.chat.completions.create(
            model=_OPENROUTER_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
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

        # Build a set of all known column names (case-insensitive)
        known_columns: set[str] = set()
        for model in self._index.models:
            for col in model.columns:
                known_columns.add(col.name.lower())

        # Extract column-like tokens from the generated SQL
        sql_lower = result.sql.lower()
        unrecognised: list[str] = []
        for match in _COLUMN_REF_RE.finditer(result.sql):
            token = (match.group(1) or match.group(2) or "").lower()
            if not token or token in _SQL_KEYWORDS:
                continue
            # Skip numeric-only tokens and tokens that look like table references
            if token.isdigit():
                continue
            # Only flag tokens that look like column names (no dots/slashes)
            if "." in token or "/" in token:
                continue
            # Skip tokens that match a known model name (table reference)
            if any(token == m.name.lower() for m in self._index.models):
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

"""
backend/generation/prompt_builder.py

PromptBuilder — assembles the Claude system prompt from retrieved model
schemas, lineage information, Databricks SQL dialect rules, and (when
needed) a deduplication instruction.

System prompt structure (Requirement 5.1, 5.2):
  1. Schema block per model: FQN, columns (≤300) with types + descriptions,
     grain, layer, compiled SQL excerpt.
  2. Lineage block: direct parent/child relationships.
  3. Databricks SQL dialect rules:
       - Fully qualified ``catalog.schema.table`` names
       - Backtick-quoted columns with special chars
       - DATE_TRUNC / DATEADD / DATEDIFF / QUALIFY for window filtering
       - LIMIT 1000 default
       - No SELECT *
  4. Deduplication instruction injected when grain == "unknown" or no
     GROUP BY / DISTINCT / _by_ pattern found (Requirement 5.3).

Column truncation (Requirement 15.3):
  If a model has >300 columns, only the first 300 are included in the prompt
  and a WARNING is logged with the model name and total column count.

Requirements: 5.1, 5.2, 5.3, 15.3
"""

from __future__ import annotations

import logging
import re

from backend.models import RankedModel, SchemaIndex

logger = logging.getLogger(__name__)

_MAX_COLUMNS = 300

# Grain patterns indicating no deduplication instruction is needed
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_DISTINCT_RE = re.compile(r"\bDISTINCT\b", re.IGNORECASE)
_BY_SUFFIX_RE = re.compile(r"_by_[a-zA-Z0-9_]+$")

# Databricks SQL dialect rules block — always appended.
_DIALECT_RULES = """
## Databricks SQL Dialect Rules

- Always use fully qualified names: `catalog.schema.table`
- Backtick-quote column names containing spaces, hyphens, or special characters
- Date/time functions: DATE_TRUNC, DATEADD, DATEDIFF (not TRUNC or DATE_ADD)
- Window filtering: use QUALIFY instead of a subquery WHERE on a window function
- Always include a LIMIT clause; default LIMIT is 1000 when not specified
- Never write SELECT * — always list specific column names
- Databricks SQL does not support FULL OUTER JOIN ON TRUE; use CROSS JOIN instead
- IMPORTANT: When filtering by brand, product, or other named entity: (1) If a dimension/lookup table is available (e.g. portfolio, brand, dim_brands), always JOIN the fact table to it on the shared ID key (e.g. portfolio_id, brand_id) and filter on the dimension table's name column — this is the authoritative source and avoids NULL gaps in denormalized columns. (2) Only filter directly on a denormalized name column (e.g. `brand` on a fact table) when no dimension table is available. (3) Always use case-insensitive partial match: `LOWER(column) LIKE '%keyword%'` — never exact equality since stored values may include extra words (e.g. 'MONDAY Haircare' not 'monday').
- IMPORTANT: Every SQL query you generate — whether exploratory, a data preview, or a final answer — must apply ALL conditions the user specified (filters, time ranges, metric thresholds, marketplace, etc.). Never omit a condition from an intermediate query because it feels like a structural check; a query that drops the user's filters returns misleading data.
- IMPORTANT: Match temporal granularity — the grain of the table you choose MUST match the granularity the question asks for. If the question asks about "days" or "daily", use a daily-grain model (e.g. a `__day` or `_daily` table), NOT a weekly or monthly model. A gold weekly model must never be used to count "days" — the rows represent week-end snapshots, so counting them gives the wrong number. Always check the model grain shown in the schema blocks before writing the query. When a question asks for day-level analysis and only a weekly gold model and a daily silver model are available, use the silver daily model.
- IMPORTANT: When querying a model that unions multiple distributor views or record types (e.g. SOURCING vs MANUFACTURING), always filter to a single view unless the question explicitly asks to combine them. Unfiltered queries on such tables double-count values. Check the model description and SQL excerpt for clues about what `distributor_view`, `record_type`, or similar partitioning columns exist.
""".strip()


def _needs_dedup(model: RankedModel) -> bool:
    """Return True when a deduplication instruction is needed (Requirement 5.3).

    Condition: grain == "unknown" OR no GROUP BY / DISTINCT / _by_ pattern found.
    """
    grain = model.model.grain
    sql = model.model.compiled_sql_excerpt

    if grain == "unknown":
        return True

    # Even with an explicit grain, check for structural dedup patterns.
    # If none are found, Claude still needs the warning.
    has_dedup_pattern = (
        bool(_GROUP_BY_RE.search(sql))
        or bool(_DISTINCT_RE.search(sql))
        or bool(_BY_SUFFIX_RE.search(model.model.name))
    )
    return not has_dedup_pattern


class PromptBuilder:
    """Assembles the Claude system prompt from ranked model schemas.

    Parameters
    ----------
    schema_index:
        The active :class:`~backend.models.SchemaIndex` used to look up
        lineage information for each model.

    Usage::

        builder = PromptBuilder(schema_index)
        system_prompt = builder.build(ranked_models, question)
    """

    def __init__(self, schema_index: SchemaIndex) -> None:
        self._index = schema_index

    def build(self, models: list[RankedModel], question: str) -> str:
        """Build the Claude system prompt.

        Parameters
        ----------
        models:
            Top-N ranked models retrieved for *question*.
        question:
            The user's plain-English question (used for the dedup heuristic).

        Returns
        -------
        str
            Complete system prompt ready to pass to the Claude API.
        """
        sections: list[str] = []

        # --- Preamble ---
        sections.append(
            "You are a data exploration assistant with full knowledge of the dbt models, "
            "schemas, and Databricks warehouse available to this organisation. "
            "You help users query data, explore schemas, understand models, and analyse results.\n\n"
            "Always respond with ONLY a JSON object — no markdown fences, no text outside it:\n"
            '{\n'
            '  "sql": "<Databricks SQL query, or empty string if no query is needed>",\n'
            '  "explanation": "<your full response>",\n'
            '  "models_used": ["<fqn1>", "<fqn2>"],\n'
            '  "confidence": "high" | "medium" | "low",\n'
            '  "confidence_reason": "<brief reason>"\n'
            '}\n\n'
            "Use your judgement to decide whether the message needs SQL or not. "
            "When writing SQL, use ONLY the tables and columns defined in the schemas below — "
            "never invent tables or columns that are not listed.\n\n"
            "Match the scope of your query to the specificity of the user's question. "
            "For precise questions (a count, a total, a specific metric), return exactly that — "
            "do not add extra JOINs, columns, or GROUP BY breakdowns that were not requested. "
            "For open or exploratory questions ('show me', 'list', 'what products…'), include "
            "enough context columns (e.g. date, identifier, name) to make the result interpretable, "
            "but still avoid adding dimensions or tables beyond what is needed to answer the question."
        )

        # --- Schema blocks ---
        sections.append("## Available dbt Models\n")
        for ranked in models:
            sections.append(self._schema_block(ranked))

        # --- Lineage block ---
        lineage_block = self._lineage_block(models)
        if lineage_block:
            sections.append(lineage_block)

        # --- Deduplication instructions ---
        dedup_models = [rm for rm in models if _needs_dedup(rm)]
        if dedup_models:
            dedup_section = self._dedup_block(dedup_models)
            sections.append(dedup_section)

        # --- Dialect rules ---
        sections.append(_DIALECT_RULES)

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _schema_block(self, ranked: RankedModel) -> str:
        """Return the schema block string for one model."""
        model = ranked.model

        # Column truncation (Requirement 15.3)
        columns = model.columns
        if len(columns) > _MAX_COLUMNS:
            logger.warning(
                "PromptBuilder: model '%s' has %d columns — truncating to first %d "
                "in prompt (Requirement 15.3)",
                model.name,
                len(columns),
                _MAX_COLUMNS,
            )
            columns = columns[:_MAX_COLUMNS]

        col_lines: list[str] = []
        for col in columns:
            line = f"  - {col.name} ({col.data_type})"
            if col.description:
                line += f": {col.description}"
            col_lines.append(line)

        cols_str = "\n".join(col_lines) if col_lines else "  (no columns)"

        lines = [
            f"### Model: `{model.fqn}`",
            f"- **Layer**: {model.layer}",
            f"- **Grain**: {model.grain}",
        ]

        if model.description:
            lines.append(f"- **Description**: {model.description}")
        if model.row_count:
            lines.append(f"- **Row count** (last run): {model.row_count:,}")

        lines.append(f"- **Columns** ({len(columns)} shown):\n{cols_str}")

        if model.compiled_sql_excerpt:
            sql_in_prompt = model.compiled_sql_excerpt[:1500]
            truncated = len(model.compiled_sql_excerpt) > 1500
            label = "SQL excerpt (first 1500 chars)" if truncated else "SQL"
            lines.append(
                f"- **{label}**:\n"
                f"  ```sql\n  {sql_in_prompt}\n  ```"
            )

        return "\n".join(lines)

    def _lineage_block(self, models: list[RankedModel]) -> str:
        """Return a lineage relationships section, or empty string if none.

        Prefers graph-based lineage from graph_summary.json; falls back to
        the manifest's depends_on list when the graph is unavailable.
        """
        lines: list[str] = ["## Model Lineage"]
        has_content = False

        for ranked in models:
            model = ranked.model
            name = model.name
            node = self._index.lineage.get(name)

            parents: list[str] = []
            children: list[str] = []

            if node is not None:
                parents = node.parents
                children = node.children
            elif model.depends_on:
                # Fall back to manifest depends_on when graph lineage is absent.
                # Strip the "model.<project>." prefix to get bare model names.
                parents = [
                    p.rsplit(".", 1)[-1] if "." in p else p
                    for p in model.depends_on
                ]

            if parents or children:
                has_content = True
                lines.append(f"\n**{name}**")
                if parents:
                    lines.append(f"  - Depends on (join candidates): {', '.join(parents)}")
                if children:
                    lines.append(f"  - Used by: {', '.join(children)}")

        if not has_content:
            return ""

        return "\n".join(lines)

    def _dedup_block(self, dedup_models: list[RankedModel]) -> str:
        """Return a deduplication instruction section."""
        model_names = ", ".join(f"`{rm.model.fqn}`" for rm in dedup_models)
        return (
            "## Deduplication Warning\n\n"
            f"The following model(s) have an **unknown grain**: {model_names}.\n\n"
            "This means the table may contain duplicate rows for the same entity. "
            "Apply deduplication ONLY when fetching row-level data (non-aggregate SELECT):\n"
            "- `SELECT DISTINCT ...` if all selected columns define uniqueness\n"
            "- `ROW_NUMBER() OVER (PARTITION BY <exact_key> ORDER BY <tiebreak>) = 1` "
            "  to keep one row per entity — only when you are certain of the exact key\n\n"
            "CRITICAL: For aggregate queries (SUM, COUNT, AVG, etc.) do NOT add "
            "ROW_NUMBER() deduplication. Partitioning incorrectly WILL silently remove "
            "legitimate rows and produce wrong totals. Instead, write the aggregate "
            "directly: `SELECT SUM(col) FROM table WHERE ...`"
        )

"""
backend/generation/chart_advisor.py

Heuristic chart type detector — no extra LLM call required.
Inspects column names, data distributions, and the original question
to pick the best chart type for a result set.
"""
from __future__ import annotations

import logging

from backend.api.models import ChartSuggestion

logger = logging.getLogger(__name__)

_DATE_PATTERNS = (
    "date", "day", "week", "month", "year", "period", "quarter",
    "created_at", "updated_at", "timestamp", "dt", "ymd",
)
_PIE_PATTERNS = (
    "share", "breakdown", "distribution", "proportion", "percent",
    "percentage", "split", "composition", "mix", "portion",
)


def _is_date_col(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in _DATE_PATTERNS)


def _is_numeric(val: object) -> bool:
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return True
    if val is None:
        return False
    try:
        float(str(val).replace(",", ""))
        return True
    except ValueError:
        return False


def _numeric_cols(rows: list[dict], columns: list[str]) -> list[str]:
    result = []
    for col in columns:
        vals = [r[col] for r in rows if r.get(col) is not None]
        if vals and sum(_is_numeric(v) for v in vals) / len(vals) > 0.8:
            result.append(col)
    return result


def suggest_chart(rows: list[dict], question: str) -> ChartSuggestion:
    """Return the best chart type for these rows and question. Never raises."""
    none = ChartSuggestion(type="none")
    try:
        if len(rows) <= 1:
            return none

        columns = list(rows[0].keys())
        if not columns:
            return none

        date_cols = [c for c in columns if _is_date_col(c)]
        num_cols = _numeric_cols(rows, columns)

        # Categorical: not numeric, not date, reasonable cardinality
        cat_cols = []
        for col in columns:
            if col in num_cols or _is_date_col(col):
                continue
            distinct = len({str(r.get(col, "")) for r in rows})
            if 1 < distinct <= 30:
                cat_cols.append(col)

        q = question.lower()

        # Time series: date column + numeric columns → line chart
        # Skip when a categorical column is also present (3-D pivot data can't be
        # rendered as a flat line chart without pivoting — show table instead).
        if date_cols and num_cols and not cat_cols:
            y = [c for c in num_cols if c not in date_cols][:3]
            if y:
                result = ChartSuggestion(type="line", x_column=date_cols[0], y_columns=y)
                logger.info(
                    "chart_advisor: line — x=%s y=%s rows=%d", result.x_column, y, len(rows)
                )
                return result

        # Pie: categorical + 1 numeric + distribution keywords + small result set
        if (
            cat_cols
            and len(num_cols) == 1
            and any(p in q for p in _PIE_PATTERNS)
            and len(rows) <= 15
        ):
            result = ChartSuggestion(type="pie", x_column=cat_cols[0], y_columns=[num_cols[0]])
            logger.info(
                "chart_advisor: pie — x=%s y=%s rows=%d", result.x_column, result.y_columns, len(rows)
            )
            return result

        # Bar: categorical + numeric
        if cat_cols and num_cols:
            result = ChartSuggestion(type="bar", x_column=cat_cols[0], y_columns=num_cols[:3])
            logger.info(
                "chart_advisor: bar — x=%s y=%s rows=%d", result.x_column, result.y_columns, len(rows)
            )
            return result

        # Scatter: exactly 2 numerics, no categorical
        if len(num_cols) == 2 and not cat_cols and not date_cols:
            result = ChartSuggestion(
                type="scatter", x_column=num_cols[0], y_columns=[num_cols[1]]
            )
            logger.info(
                "chart_advisor: scatter — x=%s y=%s rows=%d",
                result.x_column, result.y_columns, len(rows),
            )
            return result

        logger.info(
            "chart_advisor: none — date_cols=%s num_cols=%s cat_cols=%s rows=%d",
            date_cols, num_cols, cat_cols, len(rows),
        )
        return none
    except Exception as exc:
        logger.warning("chart_advisor: exception — %s", exc)
        return none

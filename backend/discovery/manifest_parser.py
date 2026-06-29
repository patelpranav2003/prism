"""ManifestParser — extracts per-model metadata from manifest.json.

Parses the raw bytes of a dbt manifest.json artifact and returns a list of
ModelMeta objects.  Only nodes whose key starts with "model." are processed;
sources, tests, and seeds are skipped.

Missing or null fields produce a zero-value (empty string / empty list / 0)
and emit a WARN log entry that includes the model name and field name.

A top-level JSON decode failure raises ParseError so the caller (IndexBuilder)
can preserve the previous SchemaIndex and set cache status appropriately.

Requirement references:
  - 3.2  Extract defined fields for each dbt model
  - 3.7  Missing/null fields → zero value + WARN log; never skip a model
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

_REF_RE = re.compile(r"ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

from backend.exceptions import ParseError
from backend.models import ColumnMeta, JoinHint, ModelMeta

logger = logging.getLogger(__name__)

# Sentinel used to distinguish "field was missing" from "field was None".
_MISSING = object()


class ManifestParser:
    """Parse manifest.json bytes into a list of ModelMeta objects."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, raw: bytes) -> list[ModelMeta]:
        """Parse *raw* manifest.json bytes and return one ModelMeta per model.

        Args:
            raw: Raw bytes of the manifest.json artifact.

        Returns:
            A list of ModelMeta objects, one per ``model.*`` node found in the
            manifest.  The list may be empty if the manifest contains no model
            nodes.

        Raises:
            ParseError: If *raw* cannot be decoded as JSON or the top-level
                structure is not a dict (i.e. no ``"nodes"`` mapping exists).
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(f"Failed to parse manifest.json: {exc}") from exc

        if not isinstance(data, dict):
            raise ParseError(
                f"manifest.json has unexpected top-level type: {type(data).__name__}"
            )

        nodes = data.get("nodes")
        if nodes is None:
            # No nodes key — treat as an empty manifest; log and return nothing.
            logger.warning("manifest.json missing 'nodes' key; returning empty model list")
            return []

        if not isinstance(nodes, dict):
            raise ParseError(
                f"manifest.json 'nodes' value has unexpected type: {type(nodes).__name__}"
            )

        models: list[ModelMeta] = []
        for node_key, node in nodes.items():
            if not node_key.startswith("model."):
                continue
            model = self._parse_node(node_key, node)
            if model is not None:
                models.append(model)

        logger.info("ManifestParser: parsed %d model(s) from manifest.json", len(models))
        return models

    def parse_join_hints(self, raw: bytes) -> list[JoinHint]:
        """Extract FK→PK join hints from dbt ``relationships`` test nodes.

        Scans the same manifest.json for test nodes where
        ``test_metadata.name == "relationships"`` and extracts:
          - from_model / from_col  (the model+column holding the FK)
          - to_model   / to_col    (the referenced model+column holding the PK)

        Returns an empty list (never raises) if the manifest cannot be parsed
        or contains no relationship tests.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []

        if not isinstance(data, dict):
            return []

        nodes = data.get("nodes", {})
        if not isinstance(nodes, dict):
            return []

        hints: list[JoinHint] = []
        seen: set[tuple[str, str, str, str]] = set()

        for node_key, node in nodes.items():
            if not node_key.startswith("test.") or not isinstance(node, dict):
                continue

            test_meta = node.get("test_metadata")
            if not isinstance(test_meta, dict) or test_meta.get("name") != "relationships":
                continue

            kwargs = test_meta.get("kwargs")
            if not isinstance(kwargs, dict):
                continue

            from_model = self._ref_name(str(kwargs.get("model", "")))
            from_col = str(kwargs.get("column_name", "")).strip()
            to_model = self._ref_name(str(kwargs.get("to", "")))
            to_col = str(kwargs.get("field", "")).strip()

            if not (from_model and from_col and to_model and to_col):
                continue

            key = (from_model, from_col, to_model, to_col)
            if key in seen:
                continue
            seen.add(key)
            hints.append(JoinHint(from_model=from_model, from_col=from_col,
                                   to_model=to_model, to_col=to_col))

        logger.info("ManifestParser: extracted %d join hint(s) from relationship tests", len(hints))
        return hints

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ref_name(ref_str: str) -> str:
        """Extract the model name from a dbt ref() expression.

        Handles both ``ref('name')`` and ``{{ ref('name') }}`` forms.
        Returns '' when no match is found.
        """
        m = _REF_RE.search(ref_str)
        return m.group(1) if m else ""

    def _parse_node(self, node_key: str, node: Any) -> ModelMeta | None:
        """Convert a single manifest node dict into a ModelMeta.

        Returns None only when *node* is not a dict (unexpected structure).
        For any missing/null *field*, records the zero value and emits a WARN.
        """
        if not isinstance(node, dict):
            logger.warning(
                "Skipping node '%s': expected dict, got %s", node_key, type(node).__name__
            )
            return None

        # Derive a display name for log messages as early as possible.
        raw_name = node.get("name")
        model_name: str = raw_name if isinstance(raw_name, str) and raw_name else node_key

        name = self._get_str(node, "name", model_name)
        database = self._get_str(node, "database", model_name)
        schema_name = self._get_str(node, "schema", model_name)

        # dbt alias = the actual table name materialized in the warehouse.
        # When absent or empty, the model name is used as the table name.
        alias_raw = node.get("alias")
        table_name = alias_raw if isinstance(alias_raw, str) and alias_raw else name

        # FQN: database.schema.table_name (uses alias so the SQL references the
        # real table, not the dbt model name which may differ).
        fqn = (
            f"{database}.{schema_name}.{table_name}"
            if database and schema_name and table_name
            else ""
        )

        # Columns: dict keyed by col name, each value has name + description.
        columns = self._parse_columns(node, model_name)

        # meta.grain
        meta_raw = node.get("meta")
        grain: str = ""
        if isinstance(meta_raw, dict):
            grain_raw = meta_raw.get("grain")
            if isinstance(grain_raw, str) and grain_raw:
                grain = grain_raw
            elif grain_raw is not None and not isinstance(grain_raw, str):
                logger.warning(
                    "Model '%s': field 'meta.grain' has unexpected type %s; using ''",
                    model_name, type(grain_raw).__name__,
                )
        elif meta_raw is not None:
            logger.warning(
                "Model '%s': field 'meta' has unexpected type %s; skipping grain",
                model_name, type(meta_raw).__name__,
            )

        # compiled_sql: prefer compiled_code, fall back to compiled_sql key.
        compiled_sql_excerpt = self._parse_compiled_sql(node, model_name)

        # depends_on.nodes → list of strings
        depends_on = self._parse_depends_on(node, model_name)

        # tags → list of strings
        tags = self._get_list_of_str(node, "tags", model_name)

        # folder_path: from the 'path' field
        folder_path_raw = node.get("path")
        folder_path: str = ""
        if isinstance(folder_path_raw, str) and folder_path_raw:
            folder_path = os.path.dirname(folder_path_raw)
        elif folder_path_raw is None or folder_path_raw == "":
            logger.warning("Model '%s': field 'path' is missing or empty; using ''", model_name)
        else:
            logger.warning(
                "Model '%s': field 'path' has unexpected type %s; using ''",
                model_name, type(folder_path_raw).__name__,
            )

        # description
        description = self._get_str(node, "description", model_name)

        return ModelMeta(
            name=name,
            database=database,
            schema_name=schema_name,
            fqn=fqn,
            columns=columns,
            grain=grain,
            layer="bronze",      # IndexBuilder infers the real layer later
            compiled_sql_excerpt=compiled_sql_excerpt,
            depends_on=depends_on,
            tags=tags,
            folder_path=folder_path,
            row_count=0,         # populated by CatalogParser
            last_updated=None,   # populated by CatalogParser
            description=description,
        )

    # ------------------------------------------------------------------
    # Field extractors
    # ------------------------------------------------------------------

    def _get_str(self, node: dict, field: str, model_name: str) -> str:
        """Return node[field] as str, or '' with a WARN log on missing/null."""
        value = node.get(field, _MISSING)
        if value is _MISSING or value is None:
            logger.warning("Model '%s': field '%s' is missing or null; using ''", model_name, field)
            return ""
        if not isinstance(value, str):
            logger.warning(
                "Model '%s': field '%s' has unexpected type %s; using ''",
                model_name, field, type(value).__name__,
            )
            return ""
        return value

    def _get_list_of_str(self, node: dict, field: str, model_name: str) -> list[str]:
        """Return node[field] as list[str], or [] with a WARN log on missing/null."""
        value = node.get(field, _MISSING)
        if value is _MISSING or value is None:
            logger.warning("Model '%s': field '%s' is missing or null; using []", model_name, field)
            return []
        if not isinstance(value, list):
            logger.warning(
                "Model '%s': field '%s' has unexpected type %s; using []",
                model_name, field, type(value).__name__,
            )
            return []
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            else:
                logger.warning(
                    "Model '%s': non-string item in '%s' (%r); skipping item",
                    model_name, field, item,
                )
        return result

    def _parse_columns(self, node: dict, model_name: str) -> list[ColumnMeta]:
        """Extract columns from the 'columns' dict in a manifest node."""
        columns_raw = node.get("columns", _MISSING)
        if columns_raw is _MISSING or columns_raw is None:
            logger.warning(
                "Model '%s': field 'columns' is missing or null; using []", model_name
            )
            return []
        if not isinstance(columns_raw, dict):
            logger.warning(
                "Model '%s': field 'columns' has unexpected type %s; using []",
                model_name, type(columns_raw).__name__,
            )
            return []

        result: list[ColumnMeta] = []
        for col_key, col_data in columns_raw.items():
            if not isinstance(col_data, dict):
                logger.warning(
                    "Model '%s': column entry '%s' is not a dict; skipping",
                    model_name, col_key,
                )
                continue

            col_name_raw = col_data.get("name")
            col_name: str = (
                col_name_raw
                if isinstance(col_name_raw, str) and col_name_raw
                else col_key
            )

            col_desc_raw = col_data.get("description")
            col_desc: str = (
                col_desc_raw
                if isinstance(col_desc_raw, str)
                else ""
            )
            if not isinstance(col_desc_raw, str) and col_desc_raw is not None:
                logger.warning(
                    "Model '%s': column '%s' description has unexpected type %s; using ''",
                    model_name, col_name, type(col_desc_raw).__name__,
                )

            result.append(
                ColumnMeta(
                    name=col_name,
                    data_type="",    # CatalogParser fills this in later
                    description=col_desc,
                )
            )
        return result

    def _parse_compiled_sql(self, node: dict, model_name: str) -> str:
        """Return the full compiled SQL for this model.

        Prefers the ``compiled_code`` key (dbt ≥1.3) and falls back to
        ``compiled_sql`` (dbt <1.3).  Returns '' on missing/null.
        """
        for key in ("compiled_code", "compiled_sql"):
            value = node.get(key, _MISSING)
            if value is _MISSING or value is None:
                continue
            if isinstance(value, str):
                return value
            logger.warning(
                "Model '%s': field '%s' has unexpected type %s; using ''",
                model_name, key, type(value).__name__,
            )
            return ""

        logger.warning(
            "Model '%s': neither 'compiled_code' nor 'compiled_sql' present; using ''",
            model_name,
        )
        return ""

    def _parse_depends_on(self, node: dict, model_name: str) -> list[str]:
        """Extract depends_on.nodes as a list of strings."""
        depends_on_raw = node.get("depends_on", _MISSING)
        if depends_on_raw is _MISSING or depends_on_raw is None:
            logger.warning(
                "Model '%s': field 'depends_on' is missing or null; using []", model_name
            )
            return []
        if not isinstance(depends_on_raw, dict):
            logger.warning(
                "Model '%s': field 'depends_on' has unexpected type %s; using []",
                model_name, type(depends_on_raw).__name__,
            )
            return []

        nodes_raw = depends_on_raw.get("nodes", _MISSING)
        if nodes_raw is _MISSING or nodes_raw is None:
            logger.warning(
                "Model '%s': field 'depends_on.nodes' is missing or null; using []", model_name
            )
            return []
        if not isinstance(nodes_raw, list):
            logger.warning(
                "Model '%s': field 'depends_on.nodes' has unexpected type %s; using []",
                model_name, type(nodes_raw).__name__,
            )
            return []

        result: list[str] = []
        for item in nodes_raw:
            if isinstance(item, str):
                result.append(item)
            else:
                logger.warning(
                    "Model '%s': non-string item in 'depends_on.nodes' (%r); skipping",
                    model_name, item,
                )
        return result

"""CatalogParser — merges catalog.json data into manifest-derived ModelMeta objects.

Responsibilities:
- Override column ``data_type`` with actual types from the last dbt run (catalog.json).
- Set ``row_count`` from catalog statistics (``stats.row_count.value``).
- Set ``last_updated`` from catalog run statistics (``stats.last_modified.value``) when available.
- Column names are NEVER normalised or case-transformed (Requirement 15.1).
- Models absent from catalog retain manifest-declared types and receive ``row_count = 0``.
- Raises ``ParseError`` on a top-level JSON failure of catalog.json.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone

from backend.exceptions import ParseError
from backend.models import ColumnMeta, ModelMeta

logger = logging.getLogger(__name__)


class CatalogParser:
    """Merges catalog.json data into a list of ``ModelMeta`` objects."""

    def merge(self, models: list[ModelMeta], raw: bytes) -> list[ModelMeta]:
        """Merge catalog data into *models* and return the updated list.

        Parameters
        ----------
        models:
            ``ModelMeta`` objects produced by ``ManifestParser.parse()``.
        raw:
            Raw bytes content of ``catalog.json``.

        Returns
        -------
        list[ModelMeta]
            A new list of ``ModelMeta`` objects with catalog data merged in.
            Objects for models absent from the catalog are returned with their
            manifest-declared types intact and ``row_count`` set to ``0``.

        Raises
        ------
        ParseError
            If ``raw`` cannot be decoded as JSON or the top-level structure is
            not a mapping containing a ``"nodes"`` key.
        """
        # --- 1. Parse catalog JSON ---
        try:
            catalog = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(f"catalog.json: JSON parse error — {exc}") from exc

        if not isinstance(catalog, dict):
            raise ParseError(
                f"catalog.json: expected a JSON object at the top level, "
                f"got {type(catalog).__name__}"
            )

        nodes: dict = catalog.get("nodes", {})
        if not isinstance(nodes, dict):
            raise ParseError(
                f"catalog.json: 'nodes' key must be a JSON object, "
                f"got {type(nodes).__name__}"
            )

        # --- 2. Build a lookup: model_name (last segment of node key) → node data ---
        # Node keys follow the pattern "model.project.model_name".
        # We keep only the last segment for matching (case-sensitive, as-is).
        catalog_by_name: dict[str, dict] = {}
        for node_key, node_data in nodes.items():
            if not isinstance(node_data, dict):
                logger.warning(
                    "catalog.json: node '%s' is not a JSON object — skipping",
                    node_key,
                )
                continue
            # The model name is the last dot-separated segment of the node key.
            model_name = node_key.rsplit(".", 1)[-1]
            catalog_by_name[model_name] = node_data

        # --- 3. Merge catalog data into each ModelMeta ---
        result: list[ModelMeta] = []
        for model in models:
            node = catalog_by_name.get(model.name)

            if node is None:
                # Model absent from catalog — retain manifest types, row_count = 0.
                # Make a shallow copy so we don't mutate the input list in place.
                merged = copy.copy(model)
                merged.row_count = 0
                result.append(merged)
                logger.debug(
                    "catalog.json: model '%s' not found — retaining manifest types, row_count=0",
                    model.name,
                )
                continue

            # Build a case-insensitive lookup for catalog columns so that we
            # can match manifest column names regardless of case differences,
            # while preserving the ORIGINAL column name casing from the manifest
            # (Requirement 15.1 — column names are NEVER normalised).
            catalog_columns: dict = node.get("columns", {})
            if not isinstance(catalog_columns, dict):
                logger.warning(
                    "catalog.json: model '%s' — 'columns' is not a JSON object; "
                    "retaining manifest types",
                    model.name,
                )
                catalog_columns = {}

            # Map lowercase column name → catalog column data for case-insensitive lookup.
            catalog_col_lower: dict[str, dict] = {}
            for col_key, col_data in catalog_columns.items():
                if isinstance(col_data, dict):
                    catalog_col_lower[col_key.lower()] = col_data
                else:
                    logger.warning(
                        "catalog.json: model '%s', column '%s' is not a JSON object — skipping",
                        model.name,
                        col_key,
                    )

            # Override column types using catalog data.
            # Column names retain their original casing from the manifest.
            updated_columns: list[ColumnMeta] = []
            for col in model.columns:
                catalog_col = catalog_col_lower.get(col.name.lower())
                if catalog_col is not None:
                    new_type = catalog_col.get("type")
                    if isinstance(new_type, str) and new_type:
                        updated_col = ColumnMeta(
                            name=col.name,  # preserve original casing — Req 15.1
                            data_type=new_type,
                            description=col.description,  # always from manifest
                        )
                    else:
                        # Catalog column exists but type is missing/null — keep manifest type.
                        logger.warning(
                            "catalog.json: model '%s', column '%s' — "
                            "catalog type is absent or empty; retaining manifest type '%s'",
                            model.name,
                            col.name,
                            col.data_type,
                        )
                        updated_col = copy.copy(col)
                    updated_columns.append(updated_col)
                else:
                    # Column not found in catalog — keep manifest declaration as-is.
                    updated_columns.append(copy.copy(col))

            # Extract row_count from catalog stats.
            row_count: int = 0
            stats: dict = node.get("stats", {})
            if isinstance(stats, dict):
                row_count_stat = stats.get("row_count", {})
                if isinstance(row_count_stat, dict):
                    raw_value = row_count_stat.get("value")
                    if isinstance(raw_value, (int, float)) and raw_value is not None:
                        row_count = int(raw_value)
                    elif raw_value is not None:
                        logger.warning(
                            "catalog.json: model '%s' — row_count stat value '%s' "
                            "is not numeric; defaulting to 0",
                            model.name,
                            raw_value,
                        )

            # Extract last_updated from catalog stats (last_modified).
            last_updated: datetime | None = None
            if isinstance(stats, dict):
                last_mod_stat = stats.get("last_modified", {})
                if isinstance(last_mod_stat, dict):
                    raw_ts = last_mod_stat.get("value")
                    if isinstance(raw_ts, str) and raw_ts:
                        try:
                            last_updated = datetime.fromisoformat(
                                raw_ts.replace("Z", "+00:00")
                            )
                        except ValueError:
                            logger.warning(
                                "catalog.json: model '%s' — last_modified value '%s' "
                                "is not a valid ISO-8601 timestamp; last_updated will be None",
                                model.name,
                                raw_ts,
                            )

            # Assemble the merged ModelMeta — all other fields come from the manifest.
            merged = ModelMeta(
                name=model.name,
                database=model.database,
                schema_name=model.schema_name,
                fqn=model.fqn,
                columns=updated_columns,
                grain=model.grain,
                layer=model.layer,
                compiled_sql_excerpt=model.compiled_sql_excerpt,
                depends_on=model.depends_on,
                tags=model.tags,
                folder_path=model.folder_path,
                row_count=row_count,
                last_updated=last_updated,
                description=model.description,
            )
            result.append(merged)

        return result

"""GraphParser — parses graph_summary.json into a lineage adjacency dict.

Requirement 3.5: The Index_Builder SHALL build a lineage adjacency list from
graph_summary.json, mapping each model to its direct parent and child models.
"""

from __future__ import annotations

import json
import logging

from backend.exceptions import ParseError
from backend.models import LineageNode

logger = logging.getLogger(__name__)

# dbt fully-qualified node IDs use the form  "<resource_type>.<project>.<name>"
# e.g.  "model.my_project.orders"  →  "orders"
_DBT_PREFIX_PARTS = 3  # resource_type + project + model_name


def _strip_prefix(node_id: str) -> str:
    """Return just the model name from a dbt node ID.

    Examples
    --------
    >>> _strip_prefix("model.my_project.orders")
    'orders'
    >>> _strip_prefix("orders")          # already stripped
    'orders'
    """
    parts = node_id.split(".")
    if len(parts) >= _DBT_PREFIX_PARTS:
        return parts[-1]
    return node_id


class GraphParser:
    """Parses ``graph_summary.json`` raw bytes into a lineage adjacency dict.

    The returned dict maps every model name that appears in the JSON (either as
    a top-level node key or as a referenced parent/child) to a
    :class:`~backend.models.LineageNode`.  Nodes that are *referenced* but not
    declared as top-level entries are created with empty parents and children
    (zero-value fallback — Requirement 3.7).

    Raises
    ------
    ParseError
        When ``raw`` is not valid JSON or the top-level value is not a JSON
        object (dict).
    """

    def parse(self, raw: bytes) -> dict[str, LineageNode]:
        """Parse raw ``graph_summary.json`` bytes.

        Parameters
        ----------
        raw:
            Raw bytes content of ``graph_summary.json``.

        Returns
        -------
        dict[str, LineageNode]
            Mapping of ``model_name → LineageNode(parents=[...], children=[...])``.
            Always returns a dict (possibly empty) on success; never ``None``.

        Raises
        ------
        ParseError
            On JSON parse failure or unexpected top-level structure.
        """
        # ------------------------------------------------------------------ #
        # 1. Decode JSON — raise ParseError on any failure                    #
        # ------------------------------------------------------------------ #
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(
                f"graph_summary.json: JSON parse error — {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ParseError(
                f"graph_summary.json: expected a JSON object at the top level, "
                f"got {type(data).__name__}"
            )

        # ------------------------------------------------------------------ #
        # 2. Extract the nodes mapping                                        #
        # ------------------------------------------------------------------ #
        nodes_raw = data.get("nodes")

        if nodes_raw is None:
            # Tolerate missing "nodes" key — return empty lineage with a warning
            logger.warning(
                "graph_summary.json: 'nodes' key absent; lineage will be empty"
            )
            return {}

        if not isinstance(nodes_raw, dict):
            logger.warning(
                "graph_summary.json: 'nodes' is not a JSON object (got %s); "
                "lineage will be empty",
                type(nodes_raw).__name__,
            )
            return {}

        # ------------------------------------------------------------------ #
        # 3. Build adjacency dict with zero-value fallbacks                   #
        # ------------------------------------------------------------------ #
        lineage: dict[str, LineageNode] = {}

        def _ensure(name: str) -> LineageNode:
            """Return existing node or create a zero-value one."""
            if name not in lineage:
                lineage[name] = LineageNode(parents=[], children=[])
            return lineage[name]

        for node_id, node_data in nodes_raw.items():
            model_name = _strip_prefix(node_id)
            node = _ensure(model_name)

            # node_data may be missing or not a dict — use zero-value fallback
            if not isinstance(node_data, dict):
                logger.warning(
                    "graph_summary.json: node '%s' has unexpected type %s; "
                    "using empty parents/children",
                    node_id,
                    type(node_data).__name__,
                )
                continue

            # ---- parents (depends_on) ------------------------------------ #
            depends_on_raw = node_data.get("depends_on", [])
            if not isinstance(depends_on_raw, list):
                logger.warning(
                    "graph_summary.json: node '%s' — 'depends_on' is not a list "
                    "(got %s); treating as empty",
                    node_id,
                    type(depends_on_raw).__name__,
                )
                depends_on_raw = []

            for parent_id in depends_on_raw:
                if not isinstance(parent_id, str):
                    logger.warning(
                        "graph_summary.json: node '%s' — non-string entry in "
                        "'depends_on': %r; skipping",
                        node_id,
                        parent_id,
                    )
                    continue
                parent_name = _strip_prefix(parent_id)
                # Add to this node's parents
                if parent_name not in node.parents:
                    node.parents.append(parent_name)
                # Add this node as a child of the parent (zero-value if new)
                parent_node = _ensure(parent_name)
                if model_name not in parent_node.children:
                    parent_node.children.append(model_name)

            # ---- children ------------------------------------------------ #
            children_raw = node_data.get("children", [])
            if not isinstance(children_raw, list):
                logger.warning(
                    "graph_summary.json: node '%s' — 'children' is not a list "
                    "(got %s); treating as empty",
                    node_id,
                    type(children_raw).__name__,
                )
                children_raw = []

            for child_id in children_raw:
                if not isinstance(child_id, str):
                    logger.warning(
                        "graph_summary.json: node '%s' — non-string entry in "
                        "'children': %r; skipping",
                        node_id,
                        child_id,
                    )
                    continue
                child_name = _strip_prefix(child_id)
                # Add to this node's children
                if child_name not in node.children:
                    node.children.append(child_name)
                # Add this node as a parent of the child (zero-value if new)
                child_node = _ensure(child_name)
                if model_name not in child_node.parents:
                    child_node.parents.append(model_name)

        return lineage

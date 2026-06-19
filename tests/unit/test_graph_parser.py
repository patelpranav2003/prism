"""
tests/unit/test_graph_parser.py

Unit tests for GraphParser.parse() — backend/discovery/graph_parser.py.

Validates Requirement 3.5: The Index_Builder SHALL build a lineage adjacency
list from graph_summary.json, mapping each model to its direct parent and
child models.

Also validates Requirement 3.7 (zero-value fallback for missing/referenced
nodes) and Requirement 3.8 (ParseError raised on top-level JSON failure).
"""

from __future__ import annotations

import json

import pytest

from backend.discovery.graph_parser import GraphParser, _strip_prefix
from backend.exceptions import ParseError
from backend.models import LineageNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw(data: object) -> bytes:
    """Serialise an object to JSON bytes."""
    return json.dumps(data).encode()


# ---------------------------------------------------------------------------
# _strip_prefix unit tests
# ---------------------------------------------------------------------------

class TestStripPrefix:
    def test_strips_dbt_node_id(self) -> None:
        assert _strip_prefix("model.my_project.orders") == "orders"

    def test_strips_source_node_id(self) -> None:
        assert _strip_prefix("source.my_project.raw_orders") == "raw_orders"

    def test_already_stripped(self) -> None:
        assert _strip_prefix("orders") == "orders"

    def test_two_part_id_returns_unchanged(self) -> None:
        # Only IDs with 3+ parts are stripped; 2-part IDs are returned as-is
        assert _strip_prefix("model.orders") == "model.orders"


# ---------------------------------------------------------------------------
# ParseError on invalid JSON / wrong top-level type
# ---------------------------------------------------------------------------

class TestParseError:
    def test_raises_on_invalid_json(self) -> None:
        parser = GraphParser()
        with pytest.raises(ParseError, match="JSON parse error"):
            parser.parse(b"not json at all {{{")

    def test_raises_on_invalid_utf8(self) -> None:
        parser = GraphParser()
        with pytest.raises(ParseError):
            parser.parse(b"\xff\xfe invalid bytes")

    def test_raises_on_json_array_top_level(self) -> None:
        parser = GraphParser()
        with pytest.raises(ParseError, match="expected a JSON object"):
            parser.parse(b"[1, 2, 3]")

    def test_raises_on_json_string_top_level(self) -> None:
        parser = GraphParser()
        with pytest.raises(ParseError, match="expected a JSON object"):
            parser.parse(b'"a string"')

    def test_raises_on_json_null_top_level(self) -> None:
        parser = GraphParser()
        with pytest.raises(ParseError, match="expected a JSON object"):
            parser.parse(b"null")


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------

class TestEmptyInputs:
    def test_empty_nodes_returns_empty_dict(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {}})
        result = parser.parse(raw)
        assert result == {}

    def test_missing_nodes_key_returns_empty_dict(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"other_key": "value"})
        result = parser.parse(raw)
        assert result == {}

    def test_nodes_not_a_dict_returns_empty_dict(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": ["a", "b"]})
        result = parser.parse(raw)
        assert result == {}


# ---------------------------------------------------------------------------
# Basic single-node parsing
# ---------------------------------------------------------------------------

class TestSingleNode:
    def test_node_with_no_depends_or_children(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {}
        }})
        result = parser.parse(raw)
        assert "orders" in result
        assert result["orders"].parents == []
        assert result["orders"].children == []

    def test_node_with_empty_depends_and_children(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {"depends_on": [], "children": []}
        }})
        result = parser.parse(raw)
        assert result["orders"].parents == []
        assert result["orders"].children == []

    def test_already_stripped_node_key(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "orders": {"depends_on": [], "children": []}
        }})
        result = parser.parse(raw)
        assert "orders" in result


# ---------------------------------------------------------------------------
# Parent/child relationship building
# ---------------------------------------------------------------------------

class TestLineageRelationships:
    def test_depends_on_sets_parents_and_child_of_parent(self) -> None:
        """orders depends_on raw_orders → orders.parents=[raw_orders], raw_orders.children=[orders]"""
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {
                "depends_on": ["model.proj.raw_orders"],
                "children": []
            }
        }})
        result = parser.parse(raw)

        assert "orders" in result
        assert "raw_orders" in result
        assert result["orders"].parents == ["raw_orders"]
        assert result["raw_orders"].children == ["orders"]
        assert result["raw_orders"].parents == []

    def test_children_sets_children_and_parent_of_child(self) -> None:
        """raw_orders has children=[orders] → raw_orders.children=[orders], orders.parents=[raw_orders]"""
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.raw_orders": {
                "depends_on": [],
                "children": ["model.proj.orders"]
            }
        }})
        result = parser.parse(raw)

        assert "raw_orders" in result
        assert "orders" in result
        assert result["raw_orders"].children == ["orders"]
        assert result["orders"].parents == ["raw_orders"]
        assert result["orders"].children == []

    def test_bidirectional_declaration_no_duplicates(self) -> None:
        """When both depends_on and children declare the same edge, no duplicates appear."""
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {
                "depends_on": ["model.proj.raw_orders"],
                "children": []
            },
            "model.proj.raw_orders": {
                "depends_on": [],
                "children": ["model.proj.orders"]
            }
        }})
        result = parser.parse(raw)

        # No duplicates in parent or child lists
        assert result["orders"].parents.count("raw_orders") == 1
        assert result["raw_orders"].children.count("orders") == 1

    def test_multiple_parents(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.summary": {
                "depends_on": ["model.proj.a", "model.proj.b"],
                "children": []
            }
        }})
        result = parser.parse(raw)

        assert set(result["summary"].parents) == {"a", "b"}
        assert "summary" in result["a"].children
        assert "summary" in result["b"].children

    def test_multiple_children(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.base": {
                "depends_on": [],
                "children": ["model.proj.x", "model.proj.y"]
            }
        }})
        result = parser.parse(raw)

        assert set(result["base"].children) == {"x", "y"}
        assert "base" in result["x"].parents
        assert "base" in result["y"].parents


# ---------------------------------------------------------------------------
# Zero-value fallback for referenced-but-not-declared nodes (Requirement 3.7)
# ---------------------------------------------------------------------------

class TestZeroValueFallback:
    def test_referenced_parent_gets_empty_node(self) -> None:
        """A node referenced in depends_on but not declared as a top-level key
        still appears in the output with empty parents and children."""
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {
                "depends_on": ["model.proj.ghost_model"],
                "children": []
            }
        }})
        result = parser.parse(raw)

        assert "ghost_model" in result
        node = result["ghost_model"]
        assert isinstance(node, LineageNode)
        assert node.parents == []
        # ghost_model IS a parent of orders, so orders should be in its children
        assert "orders" in node.children

    def test_referenced_child_gets_empty_node(self) -> None:
        """A node referenced in children but not declared gets an empty LineageNode."""
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.source": {
                "depends_on": [],
                "children": ["model.proj.ghost_child"]
            }
        }})
        result = parser.parse(raw)

        assert "ghost_child" in result
        node = result["ghost_child"]
        assert node.children == []
        assert "source" in node.parents

    def test_all_nodes_are_lineage_node_instances(self) -> None:
        """Every value in the returned dict is a LineageNode."""
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.a": {"depends_on": ["model.proj.b"], "children": ["model.proj.c"]}
        }})
        result = parser.parse(raw)

        for name, node in result.items():
            assert isinstance(node, LineageNode), f"Node '{name}' is not a LineageNode"

    def test_node_data_not_a_dict_gets_empty_node(self) -> None:
        """If a node's value is not a dict (e.g. null), we still get an empty node."""
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.bad_node": None
        }})
        result = parser.parse(raw)

        assert "bad_node" in result
        assert result["bad_node"].parents == []
        assert result["bad_node"].children == []


# ---------------------------------------------------------------------------
# Robustness: malformed depends_on / children values
# ---------------------------------------------------------------------------

class TestMalformedFields:
    def test_depends_on_not_a_list_is_treated_as_empty(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {"depends_on": "not_a_list", "children": []}
        }})
        result = parser.parse(raw)
        assert result["orders"].parents == []

    def test_children_not_a_list_is_treated_as_empty(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {"depends_on": [], "children": 42}
        }})
        result = parser.parse(raw)
        assert result["orders"].children == []

    def test_non_string_entry_in_depends_on_is_skipped(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {"depends_on": [123, None, "model.proj.valid_parent"], "children": []}
        }})
        result = parser.parse(raw)
        # Only the valid string entry should be recorded
        assert result["orders"].parents == ["valid_parent"]

    def test_non_string_entry_in_children_is_skipped(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.orders": {"depends_on": [], "children": [True, {}, "model.proj.valid_child"]}
        }})
        result = parser.parse(raw)
        assert result["orders"].children == ["valid_child"]


# ---------------------------------------------------------------------------
# Return type guarantees
# ---------------------------------------------------------------------------

class TestReturnType:
    def test_returns_dict(self) -> None:
        parser = GraphParser()
        result = parser.parse(_make_raw({"nodes": {}}))
        assert isinstance(result, dict)

    def test_never_returns_none(self) -> None:
        parser = GraphParser()
        result = parser.parse(_make_raw({"nodes": {}}))
        assert result is not None

    def test_values_are_lineage_nodes(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.a": {"depends_on": [], "children": []}
        }})
        result = parser.parse(raw)
        for v in result.values():
            assert isinstance(v, LineageNode)

    def test_lineage_node_fields_are_lists(self) -> None:
        parser = GraphParser()
        raw = _make_raw({"nodes": {
            "model.proj.a": {"depends_on": [], "children": []}
        }})
        result = parser.parse(raw)
        node = result["a"]
        assert isinstance(node.parents, list)
        assert isinstance(node.children, list)


# ---------------------------------------------------------------------------
# Realistic multi-node graph
# ---------------------------------------------------------------------------

class TestRealisticGraph:
    """A small but realistic dbt-style dependency graph:

        raw_orders ──► stg_orders ──► orders ──► order_items
                                              └──► revenue_summary
    """

    def _make_realistic_raw(self) -> bytes:
        return _make_raw({"nodes": {
            "model.proj.raw_orders": {
                "depends_on": [],
                "children": ["model.proj.stg_orders"]
            },
            "model.proj.stg_orders": {
                "depends_on": ["model.proj.raw_orders"],
                "children": ["model.proj.orders"]
            },
            "model.proj.orders": {
                "depends_on": ["model.proj.stg_orders"],
                "children": ["model.proj.order_items", "model.proj.revenue_summary"]
            },
            "model.proj.order_items": {
                "depends_on": ["model.proj.orders"],
                "children": []
            },
            "model.proj.revenue_summary": {
                "depends_on": ["model.proj.orders"],
                "children": []
            },
        }})

    def test_all_five_nodes_present(self) -> None:
        parser = GraphParser()
        result = parser.parse(self._make_realistic_raw())
        assert set(result.keys()) == {
            "raw_orders", "stg_orders", "orders", "order_items", "revenue_summary"
        }

    def test_root_has_no_parents(self) -> None:
        parser = GraphParser()
        result = parser.parse(self._make_realistic_raw())
        assert result["raw_orders"].parents == []

    def test_leaf_nodes_have_no_children(self) -> None:
        parser = GraphParser()
        result = parser.parse(self._make_realistic_raw())
        assert result["order_items"].children == []
        assert result["revenue_summary"].children == []

    def test_intermediate_node_relationships(self) -> None:
        parser = GraphParser()
        result = parser.parse(self._make_realistic_raw())
        assert result["stg_orders"].parents == ["raw_orders"]
        assert result["stg_orders"].children == ["orders"]

    def test_diamond_fan_out_children(self) -> None:
        parser = GraphParser()
        result = parser.parse(self._make_realistic_raw())
        assert set(result["orders"].children) == {"order_items", "revenue_summary"}
        assert result["orders"].parents == ["stg_orders"]

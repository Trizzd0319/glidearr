"""Tests for the stable component-dependency topological sort."""
import pytest

from scripts.managers.factories.mixins.ordered_components import topo_order


def test_already_valid_order_is_returned_unchanged():
    # A dict whose insertion order already honours every dependency must come
    # back byte-identical — this is what makes wiring it in behaviour-preserving.
    deps = {
        "instance_manager": ["manager"],   # "manager" is not a key → ignored
        "storage": ["instance_manager"],
        "series": ["instance_manager"],
        "episodes": ["series", "instance_manager"],
        "orchestration": ["series", "episodes", "storage"],
    }
    assert topo_order(deps) == list(deps)


def test_unknown_dependencies_are_ignored():
    # Deps pointing outside the map (built eagerly elsewhere) are pre-satisfied.
    deps = {"x": ["manager", "cache"], "y": ["x"]}
    assert topo_order(deps) == ["x", "y"]


def test_invalid_order_is_self_corrected():
    # A dependency declared before its prerequisite is reordered, not run early.
    deps = {"b": ["a"], "a": [], "c": ["b"]}
    assert topo_order(deps) == ["a", "b", "c"]


def test_independent_nodes_keep_their_declared_order():
    # Stable sort: nodes with no ordering constraint between them stay put.
    deps = {"a": [], "b": [], "c": ["a"]}
    assert topo_order(deps) == ["a", "b", "c"]


def test_cycle_raises():
    with pytest.raises(ValueError):
        topo_order({"a": ["b"], "b": ["a"]})


def test_self_dependency_is_a_cycle():
    with pytest.raises(ValueError):
        topo_order({"a": ["a"]})


def test_empty_map():
    assert topo_order({}) == []

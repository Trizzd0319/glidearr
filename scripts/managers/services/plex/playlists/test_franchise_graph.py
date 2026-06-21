"""Pure franchise-graph core — connected-component franchises from spin-off edges + node metadata."""
from __future__ import annotations

from scripts.managers.services.plex.playlists.franchise_graph import (
    build_franchises, connected_components, normalize_key,
)


def test_connected_components_undirected_and_transitive():
    # Breaking Bad→Better Call Saul; Buffy→Angel; Grey's→Private Practice, Grey's→Station 19
    comps = connected_components([(81189, 273181), (70327, 71035),
                                  (73762, 80542), (73762, 341852)])
    as_sets = sorted([frozenset(c) for c in comps], key=len)
    assert frozenset({81189, 273181}) in as_sets
    assert frozenset({70327, 71035}) in as_sets
    assert frozenset({73762, 80542, 341852}) in as_sets        # transitive: one component, not two


def test_connected_components_ignores_self_and_none_edges():
    assert connected_components([(5, 5), (1, None), (None, 2)]) == []


NODES = {
    73762: {"title": "Grey's Anatomy", "date": "2005-03-27T00:00:00Z"},
    80542: {"title": "Private Practice", "date": "2007-09-26T00:00:00Z"},
    341852: {"title": "Station 19", "date": "2018-03-22T00:00:00Z"},
    70327: {"title": "Buffy the Vampire Slayer", "date": "1997-03-10T00:00:00Z"},
    71035: {"title": "Angel", "date": "1999-10-05T00:00:00Z"},
}


def test_build_franchises_keys_on_earliest_and_orders_by_debut():
    cat = build_franchises([(73762, 341852), (80542, 73762), (70327, 71035)], NODES)
    assert cat["greysanatomy"]["shows"] == [73762, 80542, 341852]       # debut order, not edge order
    assert cat["greysanatomy"]["titles"][0] == "Grey's Anatomy"          # key = earliest member
    assert cat["buffythevampireslayer"]["shows"] == [70327, 71035]


def test_build_franchises_drops_singletons_and_unknown_nodes():
    # 999 has an edge but no node metadata → the pair has only one KNOWN member → dropped
    assert build_franchises([(73762, 999)], NODES) == {}


def test_build_franchises_undated_members_sort_last():
    nodes = {1: {"title": "A Show", "date": None}, 2: {"title": "B Show", "date": "2000-01-01"}}
    assert build_franchises([(1, 2)], nodes)["bshow"]["shows"] == [2, 1]   # dated first


def test_build_franchises_key_collision_disambiguated():
    nodes = {1: {"title": "Titans", "date": "1990"}, 2: {"title": "Spin A", "date": "1991"},
             3: {"title": "Titans", "date": "2000"}, 4: {"title": "Spin B", "date": "2001"}}
    cat = build_franchises([(1, 2), (3, 4)], nodes)
    assert set(cat) == {"titans", "titans2"}                              # second 'Titans' franchise disambiguated


def test_build_franchises_deny_suppresses():
    assert build_franchises([(70327, 71035)], NODES, deny={"buffythevampireslayer"}) == {}


def test_normalize_key():
    assert normalize_key("Grey's Anatomy") == "greysanatomy"
    assert normalize_key("Star Trek: The Next Generation") == "startrekthenextgeneration"

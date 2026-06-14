"""Pure people-matrix builder tests — the searchable co-occurrence graph."""
from __future__ import annotations

from scripts.managers.machine_learning.people_matrix.build import (
    build_index, co_occurring, films_with_all, route_people,
)

# tmdb person ids
SCARJO, RDJ, EVANS, KEANU = 1245, 3223, 16828, 6384


def _cast(*ids):
    return {"cast": [{"name": f"p{p}", "id": p, "order": i} for i, p in enumerate(ids)],
            "crew": []}


# Avengers (both ScarJo+RDJ+Evans), Cap1 (Evans), Civil War (RDJ+Evans), Matrix (Keanu)
LIBRARY = {
    ("movie", 24428):  _cast(SCARJO, RDJ, EVANS),
    ("movie", 1771):   _cast(EVANS),
    ("movie", 271110): _cast(RDJ, EVANS),
    ("movie", 603):    _cast(KEANU),
}


def test_inverted_index_and_forward_map():
    pidx, fwd = build_index(LIBRARY)
    assert pidx[SCARJO] == {("movie", 24428)}
    assert pidx[RDJ] == {("movie", 24428), ("movie", 271110)}
    assert pidx[EVANS] == {("movie", 24428), ("movie", 1771), ("movie", 271110)}
    assert fwd[("movie", 24428)]["cast"] == [SCARJO, RDJ, EVANS]


def test_co_occurring_ranks_by_match_count():
    pidx, _ = build_index(LIBRARY)
    co = co_occurring(pidx, {SCARJO, RDJ})
    assert co[("movie", 24428)] == 2      # both appear
    assert co[("movie", 271110)] == 1     # only RDJ
    assert ("movie", 603) not in co       # neither


def test_films_with_all_is_strict_AND():
    pidx, _ = build_index(LIBRARY)
    # "films with ScarJo AND RDJ" → only Avengers has both
    assert films_with_all(pidx, {SCARJO, RDJ}) == {("movie", 24428)}
    # RDJ AND Evans → Avengers + Civil War
    assert films_with_all(pidx, {RDJ, EVANS}) == {("movie", 24428), ("movie", 271110)}
    # an absent person → empty conjunction
    assert films_with_all(pidx, {SCARJO, 99999}) == set()


def test_empty_input_is_empty():
    assert build_index({}) == ({}, {})
    assert co_occurring({}, {SCARJO}) == {}
    assert films_with_all({}, set()) == set()


def test_movie_show_id_spaces_do_not_collide():
    lib = {("movie", 100): _cast(SCARJO), ("show", 100): _cast(RDJ)}
    pidx, _ = build_index(lib)
    assert pidx[SCARJO] == {("movie", 100)}
    assert pidx[RDJ] == {("show", 100)}


def test_none_and_bool_ids_dropped():
    credits = {"cast": [{"name": "a", "id": SCARJO, "order": 0},
                        {"name": "b", "id": None, "order": 1},
                        {"name": "c", "id": True, "order": 2}],  # bool is not a valid id
               "crew": []}
    assert route_people(credits)["cast"] == [SCARJO]


def test_route_people_mirrors_flatten_crew_branches():
    credits = {"cast": [], "crew": [
        {"name": "d", "id": 1, "job": "Director", "department": "Directing"},
        {"name": "w", "id": 2, "job": "Screenplay", "department": "Writing"},
        {"name": "c", "id": 3, "job": "Original Music Composer", "department": "Sound"},
        {"name": "p", "id": 4, "job": "Producer", "department": "Production"},
        {"name": "x", "id": 5, "job": "Gaffer", "department": "Lighting"},  # unclassified → dropped
    ]}
    r = route_people(credits)
    assert r["directors"] == [1] and r["writers"] == [2]
    assert r["composers"] == [3] and r["producers"] == [4]
    assert 5 not in sum(r.values(), [])


def test_cast_limit_and_order():
    credits = {"cast": [{"name": str(i), "id": 100 + i, "order": 9 - i} for i in range(12)],
               "crew": []}
    ids = route_people(credits, cast_limit=3)["cast"]
    assert len(ids) == 3
    assert ids == [111, 110, 109]   # lowest order first (billing), capped at 3

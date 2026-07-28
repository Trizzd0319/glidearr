"""Pure people-matrix builder tests — the searchable co-occurrence graph."""
from __future__ import annotations

from scripts.managers.machine_learning.people_matrix.build import (
    ROLES, build_index, co_occurring, deserialize_names, films_with_all,
    forward_from_relations, invert_forward, merge_forward, route_people,
    route_people_names, serialize_names,
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
    pidx, fwd, _ = build_index(LIBRARY)
    assert pidx[SCARJO] == {("movie", 24428)}
    assert pidx[RDJ] == {("movie", 24428), ("movie", 271110)}
    assert pidx[EVANS] == {("movie", 24428), ("movie", 1771), ("movie", 271110)}
    assert fwd[("movie", 24428)]["cast"] == [SCARJO, RDJ, EVANS]


def test_co_occurring_ranks_by_match_count():
    pidx, _, _ = build_index(LIBRARY)
    co = co_occurring(pidx, {SCARJO, RDJ})
    assert co[("movie", 24428)] == 2      # both appear
    assert co[("movie", 271110)] == 1     # only RDJ
    assert ("movie", 603) not in co       # neither


def test_films_with_all_is_strict_AND():
    pidx, _, _ = build_index(LIBRARY)
    # "films with ScarJo AND RDJ" → only Avengers has both
    assert films_with_all(pidx, {SCARJO, RDJ}) == {("movie", 24428)}
    # RDJ AND Evans → Avengers + Civil War
    assert films_with_all(pidx, {RDJ, EVANS}) == {("movie", 24428), ("movie", 271110)}
    # an absent person → empty conjunction
    assert films_with_all(pidx, {SCARJO, 99999}) == set()


def test_empty_input_is_empty():
    assert build_index({}) == ({}, {}, {})
    assert co_occurring({}, {SCARJO}) == {}
    assert films_with_all({}, set()) == set()


def test_movie_show_id_spaces_do_not_collide():
    lib = {("movie", 100): _cast(SCARJO), ("show", 100): _cast(RDJ)}
    pidx, _, _ = build_index(lib)
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


# ── id→name infra (additive; not read by the scorers) ────────────────────────────────
def test_route_people_names_captures_cast_and_crew():
    credits = {"cast": [{"name": "ScarJo", "id": SCARJO, "order": 0},
                        {"name": "RDJ", "id": RDJ, "order": 1}],
               "crew": [{"name": "Dir", "id": 1, "job": "Director", "department": "Directing"},
                        {"name": "NoId", "id": None, "job": "Writer", "department": "Writing"}]}
    names = route_people_names(credits)
    assert names == {SCARJO: "ScarJo", RDJ: "RDJ", 1: "Dir"}     # null-id member dropped


def test_route_people_names_respects_cast_limit_and_drops_bool_ids():
    credits = {"cast": [{"name": str(i), "id": 100 + i, "order": i} for i in range(12)]
                       + [{"name": "boolid", "id": True, "order": 99}],
               "crew": []}
    names = route_people_names(credits, cast_limit=3)
    assert set(names) == {100, 101, 102}                          # capped to billing top-3, bool dropped


def test_build_index_aggregates_flat_names_union():
    lib = {("movie", 24428): _cast(SCARJO, RDJ), ("movie", 1771): _cast(EVANS)}
    _, _, names = build_index(lib)
    assert names == {SCARJO: f"p{SCARJO}", RDJ: f"p{RDJ}", EVANS: f"p{EVANS}"}


def test_serialize_names_round_trips_int_keys():
    names = {SCARJO: "ScarJo", RDJ: "RDJ"}
    assert serialize_names(names) == {str(SCARJO): "ScarJo", str(RDJ): "RDJ"}
    assert deserialize_names(serialize_names(names)) == names     # int keys preserved
    assert deserialize_names({"bad": "x", "7": "ok"}) == {7: "ok"}  # unparseable key dropped


# ── the RELATIONAL source (production movie half) ────────────────────────────────────
def _rel(tmdb, pid, role, order=None):
    return {"tmdb_id": tmdb, "person_tmdb_id": pid, "role_type": role,
            "billing_order": order}


def test_forward_from_relations_covers_every_credited_role():
    recs = [
        _rel(603, 10, "actor", 2.0), _rel(603, 11, "actor", 0.0), _rel(603, 12, "actor", 1.0),
        _rel(603, 20, "director"), _rel(603, 30, "writer"), _rel(603, 40, "producer"),
        _rel(603, 50, "composer"), _rel(603, 60, "cinematographer"), _rel(603, 70, "editor"),
    ]
    fwd = forward_from_relations(recs)
    roles = fwd[("movie", 603)]
    assert roles["cast"] == [11, 12, 10]          # sorted by billing_order, not input order
    assert roles["directors"] == [20]
    assert roles["writers"] == [30]
    assert roles["producers"] == [40]
    assert roles["composers"] == [50]
    assert roles["cinematographers"] == [60]      # not modelled by the daemon buckets
    assert roles["editors"] == [70]
    assert set(roles) == set(ROLES)               # every role key present, even when empty


def test_forward_from_relations_caps_and_dedupes_cast():
    recs = [_rel(1, 100 + i, "actor", float(i)) for i in range(15)]
    recs += [_rel(1, 100, "actor", 0.0)]           # duplicate top-billed
    fwd = forward_from_relations(recs, cast_limit=3)
    assert fwd[("movie", 1)]["cast"] == [100, 101, 102]


def test_forward_from_relations_null_billing_sorts_last():
    recs = [_rel(1, 7, "actor", None), _rel(1, 8, "actor", 0.0)]
    assert forward_from_relations(recs)[("movie", 1)]["cast"] == [8, 7]


def test_forward_from_relations_skips_unusable_rows():
    recs = [
        _rel(1, 9, "grip"),                        # unmapped role_type
        _rel(1, None, "actor", 0.0),               # no person id
        {"tmdb_id": None, "person_tmdb_id": 5, "role_type": "actor"},   # no title id
        _rel(1, 6, "actor", 0.0),                  # the only good row
    ]
    fwd = forward_from_relations(recs)
    assert fwd == {("movie", 1): {**{r: [] for r in ROLES}, "cast": [6]}}


def test_forward_from_relations_is_empty_on_no_records():
    assert forward_from_relations([]) == {}
    assert forward_from_relations(None) == {}


def test_merge_forward_prefers_the_map_that_actually_has_credits():
    empty = {("movie", 1): {r: [] for r in ROLES}}
    full = {("movie", 1): {**{r: [] for r in ROLES}, "cast": [42]}}
    assert merge_forward(empty, full)[("movie", 1)]["cast"] == [42]
    assert merge_forward(full, empty)[("movie", 1)]["cast"] == [42]


def test_relational_forward_feeds_the_same_downstream_as_build_index():
    """The two sources must be interchangeable: the inverted index built from the
    relational map has to answer the same co-occurrence queries."""
    fwd = forward_from_relations([_rel(24428, SCARJO, "actor", 0.0),
                                  _rel(24428, RDJ, "actor", 1.0),
                                  _rel(271110, RDJ, "actor", 0.0)])
    pidx = invert_forward(fwd)
    assert pidx[RDJ] == {("movie", 24428), ("movie", 271110)}
    assert films_with_all(pidx, {SCARJO, RDJ}) == {("movie", 24428)}


# ── crew routing gained two roles ────────────────────────────────────────────────────
def test_route_people_classifies_cinematographers_and_editors():
    credits = {"cast": [], "crew": [
        {"name": "DP", "id": 1, "job": "Director of Photography", "department": "Camera"},
        {"name": "Cut", "id": 2, "job": "Editor", "department": "Editing"},
    ]}
    r = route_people(credits)
    assert r["cinematographers"] == [1] and r["editors"] == [2]

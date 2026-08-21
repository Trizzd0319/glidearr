"""TraktPeopleMatrixManager — build from injected credits, from the RELATIONAL credits
table (the production source), cache + load roundtrip, and the repeat-run fingerprint.
"""
from __future__ import annotations

import json

import pandas as pd

from scripts.managers.services.trakt.people_matrix import TraktPeopleMatrixManager


class _Log:
    def __init__(self): self.lines = []
    def log_info(self, m="", *a, **k): self.lines.append(("info", m))
    def log_warning(self, m="", *a, **k): self.lines.append(("warn", m))
    def log_debug(self, m="", *a, **k): self.lines.append(("debug", m))


class _KB:
    def __init__(self, base): self.base_dir = base


class _GC:
    def __init__(self, base=None):
        self.d = {}
        if base is not None:
            self.key_builder = _KB(base)
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def _mgr(tmp_path, cache_base=None):
    m = object.__new__(TraktPeopleMatrixManager)
    m.logger, m.global_cache, m.dry_run, m.ttl = _Log(), _GC(cache_base), False, 999_999
    m.config = None
    m.matrix_path = tmp_path / "people_matrix.json.gz"
    m.affinity_path = tmp_path / "people_affinity.json.gz"
    m.names_path = tmp_path / "people_names.json.gz"
    m.shows_sidecar = tmp_path / "people_matrix.shows.json.gz"
    m.state_path = tmp_path / "people_matrix.state.json"
    # point the daemon credit buckets at an empty temp dir — never the real ~31k-file cache
    m.movie_bucket = tmp_path / "buckets" / "movies"
    m.show_bucket = tmp_path / "buckets" / "shows"
    m.movie_bucket.mkdir(parents=True, exist_ok=True)
    m.show_bucket.mkdir(parents=True, exist_ok=True)
    m._movie_cache = m._show_cache = None
    return m


def _cast(*ids):
    return {"cast": [{"name": f"p{p}", "id": p, "order": i} for i, p in enumerate(ids)], "crew": []}


# ── injected-credits path (unchanged contract) ───────────────────────────────

def test_build_caches_and_load_roundtrips(tmp_path):
    m = _mgr(tmp_path)
    media = {("movie", 24428): _cast(1245, 3223),
             ("movie", 271110): _cast(3223),
             ("show", 100): _cast(999)}
    stats = m.build(media_people=media)
    assert stats == {"titles": 3, "with_people": 3, "persons": 3,
                     "weighted_people": 0, "named_people": 3}
    assert m.matrix_path.exists() and m.names_path.exists()

    pidx, fwd = m.load_index()                       # from global_cache
    assert pidx[3223] == {("movie", 24428), ("movie", 271110)}
    assert fwd[("show", 100)]["cast"] == [999]
    assert m.load_names() == {1245: "p1245", 3223: "p3223", 999: "p999"}  # id→name infra

    m.global_cache.d.clear()                          # force the gz fallback
    pidx2, _ = m.load_index()
    assert pidx2[1245] == {("movie", 24428)}


def test_build_empty_is_safe(tmp_path):
    m = _mgr(tmp_path)
    assert m.build(media_people={}) == {"titles": 0, "with_people": 0, "persons": 0,
                                        "weighted_people": 0, "named_people": 0}
    assert m.load_index() == (None, None)


def test_household_weights_from_cached_watched(tmp_path):
    m = _mgr(tmp_path)
    # household watched movie 24428 (Trakt history) → its cast gets affinity weight
    m.global_cache.d["trakt/history/movies"] = [{"movie": {"ids": {"tmdb": 24428}}}]
    media = {("movie", 24428): _cast(1245, 3223), ("movie", 271110): _cast(3223)}
    stats = m.build(media_people=media)
    assert stats["weighted_people"] == 2          # 1245 + 3223 each appear in the watched film
    aff = m.global_cache.d["people_matrix/affinity"]
    assert aff["1245"] == 1.0                     # top-billed → full cast weight
    assert round(aff["3223"], 4) == 0.8           # second-billed → billing decay applied


def test_rewatching_outweighs_a_single_play(tmp_path):
    m = _mgr(tmp_path)
    # three plays of 24428, one of 271110 → the rewatched film's lead outranks the other
    m.global_cache.d["trakt/history/movies"] = [
        {"movie": {"ids": {"tmdb": 24428}}}] * 3 + [{"movie": {"ids": {"tmdb": 271110}}}]
    m.build(media_people={("movie", 24428): _cast(1245), ("movie", 271110): _cast(4242)})
    aff = m.global_cache.d["people_matrix/affinity"]
    assert aff["1245"] == 2.0 and aff["4242"] == 1.0


def test_load_index_missing_cache(tmp_path):
    m = _mgr(tmp_path)
    assert m.load_index() == (None, None)             # never built


# ── production path: the relational credits table ────────────────────────────

RELATIONS = [
    # Movie 603 — all seven credited roles, cast deliberately out of billing order
    dict(tmdb_id=603, person_tmdb_id=11, role_type="actor", billing_order=1.0),
    dict(tmdb_id=603, person_tmdb_id=10, role_type="actor", billing_order=0.0),
    dict(tmdb_id=603, person_tmdb_id=20, role_type="director", billing_order=None),
    dict(tmdb_id=603, person_tmdb_id=30, role_type="writer", billing_order=None),
    dict(tmdb_id=603, person_tmdb_id=40, role_type="producer", billing_order=None),
    dict(tmdb_id=603, person_tmdb_id=50, role_type="composer", billing_order=None),
    dict(tmdb_id=603, person_tmdb_id=60, role_type="cinematographer", billing_order=None),
    dict(tmdb_id=603, person_tmdb_id=70, role_type="editor", billing_order=None),
    # Movie 604 — unwatched, shares the director
    dict(tmdb_id=604, person_tmdb_id=20, role_type="director", billing_order=None),
]


def _seed_cache(base, *, watch_count=1, pct=100.0):
    rel = base / "radarr" / "standard" / "relational"
    rel.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(RELATIONS).to_parquet(rel / "movie_person_relations.parquet", index=False)
    pd.DataFrame([{"person_tmdb_id": 10, "name": "Lead"},
                  {"person_tmdb_id": 20, "name": "Director"}]
                 ).to_parquet(rel / "people.parquet", index=False)
    pd.DataFrame([{"tmdb_id": 603, "watch_count": watch_count, "percent_complete": pct},
                  {"tmdb_id": 604, "watch_count": 0, "percent_complete": 0.0}]
                 ).to_parquet(base / "radarr" / "standard" / "movie_files.parquet", index=False)


def test_builds_every_role_from_the_relational_table(tmp_path):
    base = tmp_path / "cache"
    _seed_cache(base)
    m = _mgr(tmp_path, cache_base=base)
    stats = m.build()

    fwd = m.global_cache.d["people_matrix/forward"]
    roles = fwd["movie:603"]
    assert roles["cast"] == [10, 11]              # re-ordered by billing_order
    assert roles["directors"] == [20]
    assert roles["writers"] == [30]
    assert roles["producers"] == [40]
    assert roles["composers"] == [50]
    assert roles["cinematographers"] == [60]      # roles the daemon buckets never expose
    assert roles["editors"] == [70]
    assert stats["titles"] == 2

    # engagement came from movie_files: 603 is watched, 604 is not
    aff = m.global_cache.d["people_matrix/affinity"]
    assert set(aff) == {"10", "11", "20", "30", "40", "50", "60", "70"}
    assert aff["10"] == 1.0                       # top-billed lead, full engagement
    assert round(aff["11"], 4) == 0.8             # second-billed
    assert aff["20"] == 0.7                       # director (operator ruling: cast over crew)
    assert aff["70"] == 0.2                       # editor — lowest weight, per Robert
    assert aff["10"] > aff["70"] and aff["20"] > aff["70"]


def _director_weight(tmp_path, tag, **seed):
    """Build once in an isolated dir and return the director's affinity weight.

    NOTE: BaseManager.__new__ is a singleton registry, so two "different" managers are
    the SAME object — the value has to be read out immediately after each build rather
    than by holding two manager handles."""
    root = tmp_path / tag
    root.mkdir()
    _seed_cache(root / "cache", **seed)
    m = _mgr(root, cache_base=root / "cache")
    m.build()
    return m.global_cache.d["people_matrix/affinity"]["20"]


def test_engagement_scales_the_whole_titles_credit(tmp_path):
    once = _director_weight(tmp_path, "a", watch_count=1, pct=100.0)
    lots = _director_weight(tmp_path, "b", watch_count=4, pct=100.0)
    assert lots > once


def test_abandoned_title_contributes_less_than_a_finished_one(tmp_path):
    finished = _director_weight(tmp_path, "f", watch_count=1, pct=100.0)
    abandoned = _director_weight(tmp_path, "g", watch_count=1, pct=20.0)
    assert abandoned < finished


# ── repeat-run cost ──────────────────────────────────────────────────────────

def test_second_run_reuses_the_fingerprint(tmp_path):
    base = tmp_path / "cache"
    _seed_cache(base)
    m = _mgr(tmp_path, cache_base=base)
    first = m.build()
    assert not first.get("reused")
    assert json.loads(m.state_path.read_text())["fingerprint"]

    m2 = _mgr(tmp_path, cache_base=base)
    second = m2.build()
    assert second.get("reused") is True
    # a skipped rebuild must still publish everything a consumer reads this run
    assert m2.global_cache.d["people_matrix/forward"]
    assert m2.global_cache.d["people_matrix/affinity"]


def test_force_rebuilds_despite_matching_fingerprint(tmp_path):
    base = tmp_path / "cache"
    _seed_cache(base)
    _mgr(tmp_path, cache_base=base).build()
    again = _mgr(tmp_path, cache_base=base).build(force=True)
    assert not again.get("reused") and again["titles"] == 2


def test_changed_credits_invalidate_the_fingerprint(tmp_path):
    base = tmp_path / "cache"
    _seed_cache(base)
    _mgr(tmp_path, cache_base=base).build()
    # a new credit lands → the relational parquet changes → rebuild
    rel = base / "radarr" / "standard" / "relational" / "movie_person_relations.parquet"
    pd.DataFrame(RELATIONS + [dict(tmdb_id=605, person_tmdb_id=99,
                                   role_type="actor", billing_order=0.0)]
                 ).to_parquet(rel, index=False)
    again = _mgr(tmp_path, cache_base=base).build()
    assert not again.get("reused") and again["titles"] == 3


def test_build_never_raises_on_a_corrupt_source(tmp_path):
    base = tmp_path / "cache"
    _seed_cache(base)
    (base / "radarr" / "standard" / "relational"
     / "movie_person_relations.parquet").write_bytes(b"not a parquet")
    m = _mgr(tmp_path, cache_base=base)
    stats = m.build()                     # must degrade, not explode
    assert isinstance(stats, dict)

"""Tests for tagless universe membership (quality/universe_membership.py).

Covers Robert's policy contract:
  * PRECEDENCE — explicit *arr tags (the ONLY source that can yield keep=True) >
    TMDB collection > learned franchise maps > MDBList universe lists.
  * DERIVED-NEVER-KEEP — derived membership is always bare-universe policy
    (``keep_policy='universe'``: deletable as last resort), never ``keep_universe``.
  * mode="tags" (DEFAULT) is byte-identical to classification.keep_policy on a
    synthetic mixed library; "hybrid" fills only the untagged gaps.
  * Name normalization matches the existing saga naming (placeholders dropped,
    " Collection" suffix stripped, casefold, pipe-joined sorted multi-membership).

Plus the service wiring: RadarrCacheMovieFilesManager._resolve_membership_maps
(tags mode delegates verbatim + stays silent; hybrid logs one counts line and
stashes the counts blob) and an end-to-end check that hybrid-derived rows flow
through RadarrQualityUniverseManager.evaluate_quality_actions (an untagged
library gets universe quality management).
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.classification.keep_policy import build_keep_policy_map
from scripts.managers.services.radarr.cache.movie_files import RadarrCacheMovieFilesManager
from scripts.managers.services.radarr.quality.universe import RadarrQualityUniverseManager
from scripts.managers.services.radarr.quality.universe_membership import (
    KOMETA_FRANCHISE_KEY,
    MODE_DERIVED,
    MODE_HYBRID,
    MODE_TAGS,
    UNIVERSE_SOURCE_KEY,
    build_membership_maps,
    gather_derived_maps,
    membership_counts_key,
    membership_mode,
    normalize_universe_label,
    resolve_universe_membership,
)

# ── Synthetic mixed library ──────────────────────────────────────────────────────
TAGS = {1: "keep", 2: "keep-movie", 3: "keep-universe", 4: "keep-universe-mcu",
        5: "universe", 6: "conjuring", 7: "random"}

MOVIES = [
    # keep_forever + a TMDB collection (protection tag, no universe tag)
    {"id": 10, "tags": [1], "tmdbId": 100, "collection": {"title": "Shrek Collection", "tmdbId": 2150}},
    # keep-universe-mcu AND a collection → the tag must win membership
    {"id": 11, "tags": [4], "tmdbId": 101, "collection": {"title": "Iron Man Collection"}},
    # bare keep-universe, nothing derived knows it
    {"id": 12, "tags": [3], "tmdbId": 102},
    # bare "universe" + a FRANCHISE_HINT ("conjuring") — tag-layer membership
    {"id": 13, "tags": [5, 6], "tmdbId": 103},
    # untagged (an unrelated tag), Radarr v4-style collection payload ('title', no 'name')
    {"id": 14, "tags": [7], "tmdbId": 104, "collection": {"title": "The Conjuring Collection"}},
    # untagged, known only to the mdblist universe lists
    {"id": 15, "tags": [], "tmdbId": 105},
    # untagged, known only to the learned franchise maps
    {"id": 16, "tags": [], "tmdbId": 106},
    # untagged, unknown everywhere
    {"id": 17, "tags": [], "tmdbId": 107},
    # untagged, collection normalizes to a PLACEHOLDER → must fall through to mdblist
    {"id": 18, "tags": [], "tmdbId": 108, "collection": {"title": "Universe Collection"}},
    # Radarr v3-style payload ('name' key) still resolves
    {"id": 19, "tags": [], "tmdbId": 109, "collection": {"name": "The Mummy Collection"}},
]

FRANCHISE_MAPS = {104: {"conjuring-learned"}, 106: {"onechicago"}}
MDBLIST_MAPS   = {103: {"trek"}, 104: {"mcu"}, 105: {"mcu", "avengers"}, 108: {"wizard"}}


def _maps(mode, movies=MOVIES):
    return build_membership_maps(
        movies, TAGS, mode=mode,
        franchise_maps=FRANCHISE_MAPS, mdblist_maps=MDBLIST_MAPS)


def _resolve(movie, mode):
    return resolve_universe_membership(
        movie, tags_map=TAGS, franchise_maps=FRANCHISE_MAPS,
        mdblist_maps=MDBLIST_MAPS, mode=mode)


# ── mode="tags": byte-identical to the historical tag-only brain ─────────────────
def test_mode_tags_byte_identical_on_mixed_library():
    expect_policy, expect_names = build_keep_policy_map(MOVIES, TAGS)
    policy, names, counts = _maps(MODE_TAGS)
    assert policy == expect_policy
    assert names == expect_names
    # No derived source may leak into tags mode.
    assert counts["collection"] == counts["franchise"] == counts["mdblist"] == 0


def test_mode_tags_is_the_default_and_unknown_values_fail_safe():
    assert membership_mode(None) == MODE_TAGS
    assert membership_mode({}) == MODE_TAGS
    assert membership_mode({"universe_membership": "HYBRID"}) == MODE_HYBRID   # case-folded
    assert membership_mode({"universe_membership": " derived "}) == MODE_DERIVED
    assert membership_mode({"universe_membership": "bogus"}) == MODE_TAGS      # unknown → safe
    assert membership_mode({"universe_membership": None}) == MODE_TAGS


# ── precedence ───────────────────────────────────────────────────────────────────
def test_precedence_tag_beats_collection():
    out = _resolve(MOVIES[1], MODE_HYBRID)          # keep-universe-mcu + Iron Man Collection
    assert out == {"universe_name": "mcu", "source": "tag", "keep": True,
                   "policy": "keep_universe"}


def test_precedence_collection_beats_franchise_and_mdblist():
    out = _resolve(MOVIES[4], MODE_HYBRID)          # collection + franchise map + mdblist all know 104
    assert out["universe_name"] == "the conjuring"  # collection wins
    assert out["source"] == "collection"
    assert out["keep"] is False and out["policy"] == "universe"


def test_precedence_franchise_beats_mdblist_and_mdblist_fills_last():
    fran = _resolve({"id": 1, "tags": [], "tmdbId": 555}, MODE_HYBRID)
    assert fran == {"universe_name": None, "source": None, "keep": False, "policy": None}
    out = resolve_universe_membership(
        {"id": 2, "tags": [], "tmdbId": 556}, tags_map={},
        franchise_maps={556: {"fastfam"}}, mdblist_maps={556: {"fast"}}, mode=MODE_HYBRID)
    assert (out["universe_name"], out["source"]) == ("fastfam", "franchise")
    out = resolve_universe_membership(
        {"id": 3, "tags": [], "tmdbId": 557}, tags_map={},
        mdblist_maps={557: {"fast"}}, mode=MODE_HYBRID)
    assert (out["universe_name"], out["source"]) == ("fast", "mdblist")


# ── derived-never-keep invariant ─────────────────────────────────────────────────
def test_derived_membership_never_yields_keep():
    """No derived source — however rich — may mint keep=True / keep_universe."""
    for mode in (MODE_DERIVED, MODE_HYBRID):
        for movie in MOVIES:
            out = _resolve(movie, mode)
            has_keep_tag = any(
                (TAGS.get(t, "") == "keep-universe"
                 or TAGS.get(t, "").startswith("keep-universe-")) for t in movie["tags"])
            if not has_keep_tag:
                assert out["keep"] is False, (mode, movie["id"], out)
                assert out["policy"] != "keep_universe", (mode, movie["id"], out)
        policy, _names, _c = _maps(mode)
        derived_keep = [mid for mid, pol in policy.items()
                        if pol == "keep_universe" and mid not in (11, 12)]
        assert derived_keep == [], (mode, derived_keep)


# ── hybrid: tags where present, derived fills the gaps ───────────────────────────
def test_hybrid_fills_untagged_and_leaves_tagged_rows_identical():
    tag_policy, tag_names = build_keep_policy_map(MOVIES, TAGS)
    policy, names, counts = _maps(MODE_HYBRID)

    # Every row the TAG layer resolved (universe membership OR protection policy)
    # is untouched by hybrid.
    for mid in (11, 12, 13):                        # tag-resolved universe membership
        assert policy[mid] == tag_policy[mid]
        assert names[mid] == tag_names[mid]
    assert policy[10] == "keep_forever"             # protection tag: policy unchanged…
    assert names[10] == "shrek"                     # …but grouping name enriched (collection)

    # Untagged gaps are filled with BARE-universe policy.
    assert (policy[14], names[14]) == ("universe", "the conjuring")
    assert (policy[15], names[15]) == ("universe", "avengers|mcu")   # sorted pipe-join
    assert (policy[16], names[16]) == ("universe", "onechicago")
    assert (policy[17], names[17]) == (None, None)                   # nothing knows it
    assert (policy[18], names[18]) == ("universe", "wizard")         # placeholder fell through
    assert (policy[19], names[19]) == ("universe", "the mummy")      # v3 'name' payload

    assert counts["mode"] == MODE_HYBRID
    assert counts["tag"] == 3                       # 11, 12, 13
    assert counts["collection"] == 3                # 10 (enrichment), 14, 19
    assert counts["franchise"] == 1                 # 16
    assert counts["mdblist"] == 2                   # 15, 18
    assert counts["members"] == 9
    assert counts["keep_universe"] == 2             # 11, 12 — tag-pinned only
    assert counts["bare_universe"] == 6             # 13(+hint), 14, 15, 16, 18, 19


# ── derived: tags ignored for membership, keep-universe pin honored ──────────────
def test_derived_mode_ignores_bare_tag_membership_but_honors_keep_pin():
    policy, names, _c = _maps(MODE_DERIVED)
    # keep-universe pin survives (protection), name may come from derived sources.
    assert policy[11] == "keep_universe"
    assert names[11] == "iron man"                  # derived (collection) replaces tag name
    assert policy[12] == "keep_universe"
    assert names[12] == "universe"                  # nothing derived → tag name retained
    out12 = _resolve(MOVIES[2], MODE_DERIVED)
    assert out12["keep"] is True and out12["source"] == "tag"
    # bare "universe"+hint tag: membership now comes from mdblist, not the hint.
    assert (policy[13], names[13]) == ("universe", "trek")
    # bare tag with NO derived source would be dropped entirely:
    p, n, _ = build_membership_maps(
        [{"id": 30, "tags": [5], "tmdbId": 999}], TAGS, mode=MODE_DERIVED)
    assert (p[30], n[30]) == (None, None)
    # keep/keep-movie policies unaffected; keep_forever still gains a grouping name.
    assert policy[10] == "keep_forever" and names[10] == "shrek"


# ── name normalization ───────────────────────────────────────────────────────────
def test_normalize_universe_label_matches_saga_naming():
    assert normalize_universe_label("The Conjuring Collection") == "the conjuring"
    assert normalize_universe_label("  James   Bond   Collection ") == "james bond"
    assert normalize_universe_label("MCU") == "mcu"
    assert normalize_universe_label("one chicago") == "one chicago"
    assert normalize_universe_label("The Universe Collection") == "the universe"   # ≠ bare placeholder
    # Placeholders can never become a group.
    assert normalize_universe_label("Universe Collection") is None
    assert normalize_universe_label("universe") is None
    assert normalize_universe_label("Franchise") is None
    assert normalize_universe_label("nan") is None
    assert normalize_universe_label("") is None
    assert normalize_universe_label(None) is None


# ── gather (I/O reader) ──────────────────────────────────────────────────────────
class _Cache:
    def __init__(self, d=None):
        self.d = dict(d or {})
    def get(self, k):
        return self.d.get(k)
    def set(self, k, v):
        self.d[k] = v


def test_gather_derived_maps_reads_universe_source_and_learned_movies():
    cache = _Cache({
        UNIVERSE_SOURCE_KEY: {"universes": {
            "mcu":        {"timeline": True, "movies": [101, 105], "shows": [9001]},
            "tvfran:911": {"timeline": False, "movies": [], "shows": [9002]},   # shows-only → no movie rows
        }},
        KOMETA_FRANCHISE_KEY: {
            "onechicago": {"display": "One Chicago", "shows": [9003], "titles": ["Chicago Fire"]},
            "future":     {"display": "Future", "shows": [], "movies": [106]},  # forward-compat movies list
        },
    })
    maps = gather_derived_maps(cache)
    assert maps["mdblist_maps"] == {101: {"mcu"}, 105: {"mcu"}}
    assert maps["franchise_maps"] == {106: {"future"}}


def test_gather_reads_movies_off_merged_show_and_movie_entry_and_precedence_holds():
    """The movie builder persists movie members INTO the same kometa_franchises entries
    the show pass writes (one entry carrying shows AND movies — the merged-catalog
    shape). The reader must take the ``movies`` list, and the precedence chain must
    stay tags > TMDB collection > franchise maps > mdblist with the new source live."""
    cache = _Cache({
        KOMETA_FRANCHISE_KEY: {
            "mcu": {"display": "Marvel Cinematic Universe",
                    "shows": [9001], "titles": ["Loki"],
                    "movies": [700, 701], "movie_titles": ["Black Widow", "Eternals"],
                    "source": "kometa-plex"},
        },
        UNIVERSE_SOURCE_KEY: {"universes": {"marvel": {"movies": [700], "shows": []}}},
    })
    maps = gather_derived_maps(cache)
    assert maps["franchise_maps"] == {700: {"mcu"}, 701: {"mcu"}}   # movies read, shows ignored
    assert maps["mdblist_maps"] == {700: {"marvel"}}

    movies = [
        {"id": 1, "tags": [], "tmdbId": 700},                       # franchise + mdblist → franchise wins
        {"id": 2, "tags": [], "tmdbId": 701,                        # collection beats franchise
         "collection": {"title": "Eternals Collection"}},
        {"id": 3, "tags": [4], "tmdbId": 700},                      # keep-universe-mcu tag beats all
    ]
    policy, names, counts = build_membership_maps(movies, TAGS, mode=MODE_HYBRID, **maps)
    assert (policy[1], names[1]) == ("universe", "mcu")             # bare-universe, never keep
    assert (policy[2], names[2]) == ("universe", "eternals")
    assert (policy[3], names[3]) == ("keep_universe", "mcu")
    assert counts["tag"] == 1 and counts["collection"] == 1
    assert counts["franchise"] == 1 and counts["mdblist"] == 0


def test_gather_derived_maps_degrades_to_empty_on_missing_or_broken_cache():
    assert gather_derived_maps(None) == {"franchise_maps": {}, "mdblist_maps": {}}
    assert gather_derived_maps(_Cache()) == {"franchise_maps": {}, "mdblist_maps": {}}

    class _Boom:
        def get(self, k):
            raise RuntimeError("down")
    assert gather_derived_maps(_Boom()) == {"franchise_maps": {}, "mdblist_maps": {}}


# ── service wiring: RadarrCacheMovieFilesManager._resolve_membership_maps ────────
class _Log:
    def __init__(self):
        self.infos, self.debugs = [], []
    def log_info(self, m):
        self.infos.append(m)
    def log_debug(self, m):
        self.debugs.append(m)
    def log_warning(self, m):
        pass


def _mfm(config, cache):
    m = object.__new__(RadarrCacheMovieFilesManager)   # skip __init__/registry/base
    m.config = config
    m.logger = _Log()
    m.global_cache = cache
    return m


def test_resolve_membership_maps_tags_mode_delegates_and_stays_silent():
    cache = _Cache({UNIVERSE_SOURCE_KEY: {"universes": {"mcu": {"movies": [104], "shows": []}}}})
    m = _mfm({}, cache)                                # no universe_membership key → tags
    policy, names = m._resolve_membership_maps("standard", MOVIES, TAGS)
    expect_policy, expect_names = build_keep_policy_map(MOVIES, TAGS)
    assert policy == expect_policy and names == expect_names
    assert m.logger.infos == []                        # byte-identical logs in default mode
    assert membership_counts_key("standard") not in cache.d


def test_resolve_membership_maps_hybrid_logs_once_and_stashes_counts():
    cache = _Cache({UNIVERSE_SOURCE_KEY: {"universes": {"mcu": {"movies": [105], "shows": []}}}})
    m = _mfm({"universe_membership": "hybrid"}, cache)
    policy, names = m._resolve_membership_maps("standard", MOVIES, TAGS)
    assert (policy[14], names[14]) == ("universe", "the conjuring")   # collection fill
    assert (policy[15], names[15]) == ("universe", "mcu")             # mdblist fill
    assert policy[11] == "keep_universe"                              # tag pin intact
    assert len(m.logger.infos) == 1                                   # ONE line per instance
    assert "mode=hybrid" in m.logger.infos[0]
    blob = cache.d[membership_counts_key("standard")]
    assert blob["mode"] == "hybrid" and blob["members"] >= 5
    assert blob["keep_universe"] == 2


# ── end-to-end: hybrid-derived rows activate the universe quality pass ───────────
class _FakeMfm:
    def __init__(self, df):
        self._df = df
        self.saved = None
    def load(self, instance):
        return self._df.copy()
    def save(self, instance, df):
        self.saved = df


class _FakeInstanceMgr:
    def disk_total_gb(self, instance):
        return 10_000.0


def test_untagged_library_gets_universe_quality_management_in_hybrid():
    """An all-untagged library (public-release user) derives bare-universe rows in
    hybrid mode, and evaluate_quality_actions marks them for downgrade under
    pressure — the pass is ACTIVE with zero tags."""
    untagged = [m for m in MOVIES if not any(
        TAGS.get(t, "").startswith(("keep", "universe")) or TAGS.get(t, "") == "universe"
        for t in m["tags"])]
    policy, names, _c = build_membership_maps(
        untagged, TAGS, mode=MODE_HYBRID,
        franchise_maps=FRANCHISE_MAPS, mdblist_maps=MDBLIST_MAPS)
    rows = [dict(movie_id=mv["id"], title=f"Movie {mv['id']}",
                 keep_policy=policy[mv["id"]], universe_name=names[mv["id"]],
                 quality_action=None)
            for mv in untagged if policy[mv["id"]] is not None]
    assert rows, "hybrid derivation should activate at least one untagged movie"
    assert all(r["keep_policy"] == "universe" for r in rows)          # bare-universe ONLY

    mgr = object.__new__(RadarrQualityUniverseManager)
    mgr.config = {"free_space_limit": 9000}                           # T=9000
    mgr.logger = _Log()
    mgr.instance_manager = _FakeInstanceMgr()
    fake = _FakeMfm(pd.DataFrame(rows))
    mgr._get_movie_files_manager = lambda: fake
    mgr._resolve_instance = lambda i: i
    stats = mgr.evaluate_quality_actions("standard", free_space_gb=8000.0)
    assert stats["universe_count"] == len(rows)
    assert stats["downgrade_marked"] == len(rows)                     # pressure → downgrade

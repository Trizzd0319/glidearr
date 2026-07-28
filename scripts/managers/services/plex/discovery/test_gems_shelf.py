"""Tests for the Hidden Gems shelf BUILDER: gather -> pure pipeline -> cached plan + delete
shield + recommendation ledger + the two summary tables.

Flag OFF must be a total no-op (no plan, no shield key, no parquet on disk); flag ON must
publish a taste-ranked, age-gated, diversity-capped shelf, feed the space-coordinator delete
shield, and measure its own picks across a frozen clock.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from scripts.managers.machine_learning.labels.recommendations import load_events
from scripts.managers.machine_learning.playlists.cert_gate import LITTLE_KID, TEEN
from scripts.managers.services.coordinator.space_coordinator import SpaceCoordinatorManager
from scripts.managers.services.plex.discovery.gems import (
    _PLAN_KEY,
    _PROTECTED_KEY,
    HiddenGemsShelfBuilderManager,
)
from scripts.managers.services.plex.playlists.builder import PlexPlaylistBuilderManager

_DAY = 86400
_T0 = 1_800_000_000              # frozen "now" for every test in this module

# Two owned movies with IDENTICAL taste signals; #2 additionally carries a full house of
# ENGAGEMENT points (it is a rewatched favourite) — the shelf must not care.
_TASTE = {"B1_actor_affinity": 8.0, "B4_genre_affinity": 4.0, "F1_critic_consensus": 14.0}
_ENGAGED = {**_TASTE, "A2_completion": 12.0, "A3_rewatch": 8.0, "A4_user_rating": 10.0}


def _movie(tmdb, breakdown, *, title=None, cert="PG-13", collection=None, cast=None,
           director=None, year=2001):
    return {"tmdb_id": tmdb, "title": title or f"Movie {tmdb}", "year": year,
            "has_file": True, "certification": cert, "collection_name": collection,
            "universe_name": None, "cast_names": cast, "director_names": director,
            "watchability_breakdown": json.dumps(breakdown) if breakdown else None}


_OWNED = [
    _movie(1, {**_TASTE, "F2_popularity": 2.0}, title="Gem One"),          # top taste
    _movie(2, _ENGAGED, title="Favourite"),                                # same taste, engaged
    _movie(3, {"B4_genre_affinity": 1.0}, title="Weak Match"),
    _movie(4, _TASTE, title="Adults Only", cert="R"),
]
_INVENTORY = {
    "1": {"rating_key": "rk1", "title": "Gem One", "year": 2001, "section": "1"},
    "2": {"rating_key": "rk2", "title": "Favourite", "year": 2001, "section": "1"},
    "3": {"rating_key": "rk3", "title": "Weak Match", "year": 2001, "section": "1"},
    "4": {"rating_key": "rk4", "title": "Adults Only", "year": 2001, "section": "1"},
}


class _Cache:
    def __init__(self, d=None, root=None):
        self.d = dict(d or {})
        self.cache_root = root
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _Log:
    def __init__(self):
        self.grids = []
        self.files = []
        self.lines = []
    def log_info(self, m): self.lines.append(str(m))
    def log_debug(self, *a, **k): pass
    def log_warning(self, m): self.lines.append(str(m))
    def log_error(self, m): self.lines.append(str(m))
    def log_grid(self, headers, rows, title="", cap=16, caption=""):
        self.grids.append({"headers": list(headers), "rows": [list(r) for r in rows],
                           "title": title, "caption": caption})
    def log_to_file(self, category, message): self.files.append((category, str(message)))


class _UsersMgr:
    def __init__(self, tracked, allowed):
        self.tracked_users = tracked
        self._allowed = allowed
    def allowed_sections(self, u): return self._allowed.get(u["safe_user"], set())


class _History:
    """Stands in for TautulliWatchHistoryManager — per-user Tautulli rows."""
    def __init__(self, by_user): self._by_user = by_user
    def get_all_history_cached(self, user_id): return list(self._by_user.get(user_id, []))


class _Registry:
    def __init__(self, m): self.m = m
    def get(self, category, name): return self.m.get(name)


_ROB = {"safe_user": "rob", "title": "Rob", "is_admin": True, "restriction_profile": None,
        "tautulli_user_id": 1, "tautulli_username": "rob"}
_MUM = {"safe_user": "mum", "title": "Mum", "is_admin": False, "restriction_profile": None,
        "tautulli_user_id": 2, "tautulli_username": "mum"}
_KID = {"safe_user": "kid", "title": "Kid", "is_admin": False,
        "restriction_profile": "little_kid", "tautulli_user_id": 3, "tautulli_username": "kid"}
_ON = {"plex": {"playlists": {"hidden_gems": {"enabled": True}}}}


def _play(rating_key, title, year, ts, *, pct=100):
    return {"media_type": "movie", "rating_key": rating_key, "title": title, "year": year,
            "date": ts, "percent_complete": pct}


def _mgr(tmp_path, *, config=_ON, tracked=(_ROB,), owned=None, history=None, cache_extra=None,
         allowed=None, now=_T0):
    cache = _Cache({"plex/movies/owned_inventory": dict(_INVENTORY), **(cache_extra or {})},
                   root=tmp_path)
    log = _Log()
    m = object.__new__(HiddenGemsShelfBuilderManager)
    m.logger = log
    m.global_cache = cache
    m.config = config
    m.plex_api = None
    m.dry_run = True
    m.registry = _Registry({
        "PlexUsersManager": _UsersMgr(list(tracked),
                                      allowed if allowed is not None else {u["safe_user"]: {"1"}
                                                                           for u in tracked}),
        "TautulliWatchHistoryManager": _History(history or {}),
    })
    m._load_owned_movies = lambda _o=(owned if owned is not None else _OWNED): list(_o)
    m._movie_csm_ages = lambda: {}
    # ONE frozen clock seam: the recommended_at stamp and the outcome join both derive from it.
    m._now = lambda _n=now: datetime.fromtimestamp(float(_n), tz=timezone.utc)
    return m, cache, log


# ── the default-OFF contract ──────────────────────────────────────────────────
def test_flag_off_is_a_total_noop(tmp_path):
    print("test_flag_off_is_a_total_noop:")
    m, cache, log = _mgr(tmp_path, config={"plex": {"playlists": {}}})
    assert m.run() == {"enabled": False}
    assert cache.d.keys() == {"plex/movies/owned_inventory"}      # no plan, no shield key
    assert not log.grids and not log.files
    assert not list(tmp_path.rglob("*.parquet"))                  # nothing written to disk


# ── the shelf ─────────────────────────────────────────────────────────────────
def test_shelf_is_owned_unwatched_and_taste_ranked(tmp_path):
    print("test_shelf_is_owned_unwatched_and_taste_ranked:")
    m, cache, _ = _mgr(tmp_path)
    stats = m.run()
    assert stats["built"] == 1 and stats["picks"] == 4
    items = cache.d[f"{_PLAN_KEY}/rob"]["items"]
    # Gem One leads (it carries F2 on top of the shared taste). The heavily-watched
    # "Favourite" (rk2) is NOT lifted by its 30 points of engagement — it TIES "Adults Only"
    # (rk4), which carries the identical taste half and nothing else, and loses the tie only
    # on the alphabetical tie-break. That tie IS the invariant.
    assert [i["rating_key"] for i in items] == ["rk1", "rk4", "rk2", "rk3"]
    assert items[1]["score"] == items[2]["score"]
    assert [i["ordinal"] for i in items] == [0, 1, 2, 3]
    assert items[0]["score"] > items[3]["score"]


def test_watched_by_this_profile_is_dropped_but_not_for_another(tmp_path):
    """Per-USER, not household — the whole reason this joins per-profile Tautulli history."""
    print("test_watched_by_this_profile_is_dropped_but_not_for_another:")
    hist = {1: [_play("rk1", "Gem One", 2001, _T0 - 90 * _DAY)]}       # Rob finished Gem One
    m, cache, _ = _mgr(tmp_path, tracked=(_ROB, _MUM), history=hist,
                       allowed={"rob": {"1"}, "mum": {"1"}})
    m.run()
    rob = [i["rating_key"] for i in cache.d[f"{_PLAN_KEY}/rob"]["items"]]
    mum = [i["rating_key"] for i in cache.d[f"{_PLAN_KEY}/mum"]["items"]]
    assert "rk1" not in rob                                # Rob watched it
    assert "rk1" in mum                                    # still a gem for Mum


def test_age_gate_scopes_a_restricted_profile(tmp_path):
    print("test_age_gate_scopes_a_restricted_profile:")
    m, cache, _ = _mgr(tmp_path, tracked=(_KID,), allowed={"kid": {"1"}})
    m.run()
    rks = [i["rating_key"] for i in cache.d[f"{_PLAN_KEY}/kid"]["items"]]
    assert rks == []      # every candidate is PG-13/R -> nothing a little kid may see


def test_titles_already_on_another_plan_are_not_double_surfaced(tmp_path):
    print("test_titles_already_on_another_plan_are_not_double_surfaced:")
    m, cache, _ = _mgr(tmp_path, cache_extra={
        "plex/playlists/movie_plan/rob": {"items": [{"rating_key": "rk1"}]},
        "plex/playlists/fresh_movie_plan/rob": {"items": [{"rating_key": "rk2"}]}})
    m.run()
    rks = [i["rating_key"] for i in cache.d[f"{_PLAN_KEY}/rob"]["items"]]
    assert "rk1" not in rks and "rk2" not in rks and "rk3" in rks


def test_diversity_caps_hold_end_to_end(tmp_path):
    print("test_diversity_caps_hold_end_to_end:")
    saga = [_movie(i, {"F1_critic_consensus": 20.0 - i}, title=f"Saga {i}",
                   collection="Big Saga Collection") for i in range(1, 6)]
    inv = {str(i): {"rating_key": f"rk{i}", "section": "1"} for i in range(1, 6)}
    m, cache, _ = _mgr(tmp_path, owned=saga,
                       cache_extra={"plex/movies/owned_inventory": inv})
    m.run()
    assert len(cache.d[f"{_PLAN_KEY}/rob"]["items"]) == 2       # max_per_franchise default 2

    star = [_movie(i, {"F1_critic_consensus": 20.0 - i}, title=f"Star {i}",
                   cast="Nicolas Cage|Other") for i in range(1, 6)]
    m2, cache2, _ = _mgr(tmp_path, owned=star,
                         cache_extra={"plex/movies/owned_inventory": inv})
    m2.run()
    assert len(cache2.d[f"{_PLAN_KEY}/rob"]["items"]) == 3      # max_per_person default 3


def test_size_knob_bounds_the_shelf(tmp_path):
    print("test_size_knob_bounds_the_shelf:")
    cfg = {"plex": {"playlists": {"hidden_gems": {"enabled": True, "size": 2}}}}
    m, cache, _ = _mgr(tmp_path, config=cfg)
    m.run()
    assert len(cache.d[f"{_PLAN_KEY}/rob"]["items"]) == 2


# ── the delete shield ─────────────────────────────────────────────────────────
def test_published_gems_reach_the_delete_shield(tmp_path):
    print("test_published_gems_reach_the_delete_shield:")
    m, cache, _ = _mgr(tmp_path)
    m.run()
    assert cache.d[_PROTECTED_KEY] == {"tmdbs": [1, 2, 3, 4]}

    # …and the space coordinator actually reads that key and drops those candidates.
    coord = object.__new__(SpaceCoordinatorManager)
    coord.global_cache = cache
    coord.config = {}
    pool = [{"service": "movie", "tmdb_id": 1}, {"service": "movie", "tmdb_id": 999},
            {"service": "movie", "tmdb_id": 2, "is_uhd_copy": True}]
    kept, shielded = coord._shield_protected_picks(pool)
    assert shielded == 1                                   # tmdb 1 shielded
    assert [c["tmdb_id"] for c in kept] == [999, 2]        # 4K bonus copy still reclaimable
    assert 1 in coord._protected_playlist_tmdbs()


# ── the measurement loop ──────────────────────────────────────────────────────
def test_events_persist_and_a_republish_is_idempotent(tmp_path):
    print("test_events_persist_and_a_republish_is_idempotent:")
    m, _cache, _ = _mgr(tmp_path)
    first = m.run()
    assert first["events_appended"] == 4
    df = load_events(tmp_path)
    assert sorted(df["entity_id"]) == ["1", "2", "3", "4"]
    assert set(df["profile"]) == {"rob"} and set(df["surface"]) == {"hidden_gems"}
    # Same day, same shelf -> the ledger collapses to one row per pick.
    m2, _c2, _l2 = _mgr(tmp_path)
    assert m2.run()["events_appended"] == 0
    assert len(load_events(tmp_path)) == 4


def test_a_pick_is_held_for_its_whole_window_then_republished(tmp_path):
    """A pick is offered for its whole measurement window, not for one run: the next day's
    shelf HOLDS it (same items, no new ledger event — re-publishing would reset its clock)."""
    print("test_a_pick_is_held_for_its_whole_window_then_republished:")
    m, cache, _ = _mgr(tmp_path)
    m.run()
    day0 = [i["rating_key"] for i in cache.d[f"{_PLAN_KEY}/rob"]["items"]]

    m2, cache2, _ = _mgr(tmp_path, now=_T0 + 3 * _DAY)
    assert m2.run()["events_appended"] == 0                 # nothing NEW recommended
    assert [i["rating_key"] for i in cache2.d[f"{_PLAN_KEY}/rob"]["items"]] == day0
    assert len(load_events(tmp_path)) == 4                  # the clock was not reset

    # 31 days on the windows have closed: the picks are eligible again and re-publishing them
    # is a NEW, separately-measurable recommendation.
    m3, cache3, _ = _mgr(tmp_path, now=_T0 + 31 * _DAY)
    assert m3.run()["events_appended"] == 4
    assert [i["rating_key"] for i in cache3.d[f"{_PLAN_KEY}/rob"]["items"]] == day0
    assert len(load_events(tmp_path)) == 8


def test_a_held_pick_leaves_the_shelf_once_it_is_watched(tmp_path):
    """A watched pick has done its job (it is a HIT) — it drops off the shelf and its slot is
    topped up, while its ledger row keeps being measured."""
    print("test_a_held_pick_leaves_the_shelf_once_it_is_watched:")
    cfg = {"plex": {"playlists": {"hidden_gems": {"enabled": True, "size": 2}}}}
    m, cache, _ = _mgr(tmp_path, config=cfg)
    m.run()
    assert [i["rating_key"] for i in cache.d[f"{_PLAN_KEY}/rob"]["items"]] == ["rk1", "rk4"]
    hist = {1: [_play("rk1", "Gem One", 2001, _T0 + 2 * _DAY)]}
    m2, cache2, _ = _mgr(tmp_path, now=_T0 + 3 * _DAY, history=hist, config=cfg)
    assert m2.run()["events_appended"] == 1                 # one freed slot topped up
    rks = [i["rating_key"] for i in cache2.d[f"{_PLAN_KEY}/rob"]["items"]]
    assert "rk1" not in rks and rks[0] == "rk4" and len(rks) == 2


def test_held_picks_keep_their_delete_shield(tmp_path):
    """The shield must cover a pick for its WHOLE window — the delete pool preferentially
    removes never-watched titles, which is exactly the gem pool."""
    print("test_held_picks_keep_their_delete_shield:")
    m, _c, _l = _mgr(tmp_path)
    m.run()
    m2, cache2, _ = _mgr(tmp_path, now=_T0 + 3 * _DAY)
    m2.run()
    assert cache2.d[_PROTECTED_KEY] == {"tmdbs": [1, 2, 3, 4]}


def test_outcome_join_classifies_hit_miss_and_pending(tmp_path):
    print("test_outcome_join_classifies_hit_miss_and_pending:")
    m, _c, _l = _mgr(tmp_path)
    m.run()                                                   # publish 4 picks at _T0

    # Rob plays ONE of them on day 5 (and rewatches it on day 40 — the join must take the
    # FIRST play, not the latest, or the hit would read as a miss).
    hist = {1: [_play("rk1", "Gem One", 2001, _T0 + 5 * _DAY),
                _play("rk1", "Gem One", 2001, _T0 + 40 * _DAY)]}

    # Day 10: nothing has matured yet -> all four PENDING, no hit rate claimed.
    m2, _c2, log2 = _mgr(tmp_path, now=_T0 + 10 * _DAY, history=hist)
    m2.run()
    row = _hit_row(log2)
    assert row[1:5] == ["4", "3", "1", "1"]                   # published/pending/matured/hits
    assert row[5] == "100.0%"                                 # the day-5 play already matured
    assert row[7] == "directional"                            # 1 matured pick -> be honest

    # Day 40: the window has closed on everything -> 1 hit, 3 misses.
    m3, _c3, log3 = _mgr(tmp_path, now=_T0 + 40 * _DAY, history=hist)
    m3.run()
    row = _hit_row(log3)
    assert row[1:6] == ["4", "0", "4", "1", "25.0%"]
    assert row[6] == "5.0"                                    # median days-to-watch for hits


def test_a_play_before_the_recommendation_is_never_a_hit(tmp_path):
    print("test_a_play_before_the_recommendation_is_never_a_hit:")
    m, _c, _l = _mgr(tmp_path)
    m.run()
    hist = {1: [_play("rk1", "Gem One", 2001, _T0 - 5 * _DAY)]}
    m2, _c2, log2 = _mgr(tmp_path, now=_T0 + 40 * _DAY, history=hist)
    m2.run()
    assert _hit_row(log2)[4] == "0"                           # zero hits


def test_outcome_join_survives_the_title_leaving_the_library(tmp_path):
    """A deleted movie has no ratingKey left, but the (title, year) watch identity recorded on
    the event still resolves — the label must not be lost with the file."""
    print("test_outcome_join_survives_the_title_leaving_the_library:")
    m, _c, _l = _mgr(tmp_path)
    m.run()
    hist = {1: [_play("some-new-key", "Gem One", 2001, _T0 + 2 * _DAY)]}
    m2, _c2, log2 = _mgr(tmp_path, now=_T0 + 40 * _DAY, history=hist,
                         cache_extra={"plex/movies/owned_inventory": {}}, owned=[])
    m2.run()
    assert _hit_row(log2)[4] == "1"                           # matched on (title, year)


def _hit_row(log):
    grid = next(g for g in log.grids if "outcomes" in g["title"])
    return grid["rows"][0]


# ── the run-log tables ────────────────────────────────────────────────────────
def test_summary_table_matches_the_playlist_builders_cert_evidence(tmp_path):
    """The gating evidence must read identically everywhere: cells 0-6 of the gems summary row
    are produced by the SAME cert_summary/tier_ceiling helpers the playlist builders use, so
    this compares the two rows directly rather than re-asserting the numbers."""
    print("test_summary_table_matches_the_playlist_builders_cert_evidence:")
    m, _c, log = _mgr(tmp_path, tracked=(dict(_ROB, restriction_profile="teen"),))
    m.run()
    grid = next(g for g in log.grids if "per-profile summary" in g["title"])
    gem_row = grid["rows"][0]

    class _Item:
        def __init__(self, rk): self.rating_key = rk

    class _Plan:
        items = [_Item("rk1"), _Item("rk2"), _Item("rk3")]        # the 3 teen-legal picks

    ref = object.__new__(PlexPlaylistBuilderManager)
    ref._summary_rows = []
    ref._add_summary_row(family_label="Hidden Gems", who=gem_row[1], plan=_Plan(),
                         certs={"rk1": "PG-13", "rk2": "PG-13", "rk3": "PG-13"}, level=TEEN)
    assert gem_row[:7] == ref._summary_rows[0]
    base_cols = list(PlexPlaylistBuilderManager._SUMMARY_COLS)
    assert grid["headers"][1:7] == base_cols[1:7]                  # same evidence columns
    assert grid["headers"][0] == "Shelf" and base_cols[0] == "Playlist"
    assert grid["headers"][7] == "Median taste"
    assert float(gem_row[7]) > 0                                   # median taste reported


def test_a_violation_would_be_flagged_not_hidden(tmp_path):
    print("test_a_violation_would_be_flagged_not_hidden:")
    m, _c, _log = _mgr(tmp_path)
    m._summary_rows = []
    m._add_gem_summary_row(who="K - little_kid 1",
                           picks=[{"certification": "R", "taste_score": 50.0}],
                           level=LITTLE_KID)
    assert m._summary_rows[0][6] == "VIOLATION x1"
    assert m._summary_rows[0][3] == "TV-G/G"                       # the tier's ceiling


def test_per_item_detail_goes_to_the_playlists_log_not_the_shell(tmp_path):
    print("test_per_item_detail_goes_to_the_playlists_log_not_the_shell:")
    m, _c, log = _mgr(tmp_path)
    m.run()
    mirrored = [msg for cat, msg in log.files if cat == "playlists"]
    assert any("Hidden Gems - 4 pick(s)" in msg for msg in mirrored)
    assert sum(1 for msg in mirrored if "Gem One" in msg) == 1
    # The shell gets ONE table on a first run (there is nothing measured yet to table) and
    # never a per-item line.
    assert not any("Gem One" in line for line in log.lines)
    assert [g["title"].split(" - ")[-1] for g in log.grids] == ["per-profile summary"]
    # …and the measurement table joins it once the ledger has something in it.
    m2, _c2, log2 = _mgr(tmp_path, now=_T0 + 40 * _DAY)
    m2.run()
    assert len(log2.grids) == 2

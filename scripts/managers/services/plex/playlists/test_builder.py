"""Tests for the per-user TV playlist builder core (_build_for_users)."""
from __future__ import annotations

from scripts.managers.services.plex.playlists.builder import (
    _PLAN_KEY,
    PlexPlaylistBuilderManager,
)


class _Log:
    def __init__(self):
        self.infos: list = []
        self.warns: list = []
        self.grids: list = []

    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.warns.append(m)
    def log_error(self, m): pass
    def log_grid(self, headers, rows, title="", cap=16): self.grids.append((title, rows))


class _Cache:
    def __init__(self):
        self.d: dict = {}

    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def _mgr(cache=None, logger=None, config=None):
    m = PlexPlaylistBuilderManager.__new__(PlexPlaylistBuilderManager)
    m.global_cache = cache
    m.logger = logger or _Log()
    m.config = config if config is not None else {}
    m.registry = None
    m.dry_run = False
    return m


_TRACKED = [{"safe_user": "rob", "title": "Rob", "tautulli_user_id": 1},
            {"safe_user": "kid", "title": "Kid", "tautulli_user_id": 2}]


def _owned(sid, s, e, jk, title="ep"):
    return {"series_id": sid, "season_number": s, "episode_number": e, "tvdb_join_key": jk,
            "title": title, "air_date_utc": f"2020-01-0{e}", "is_special": s == 0}


_OWNED = [_owned(1, 1, 1, "100:1:1", "Pilot"), _owned(1, 1, 2, "100:1:2", "Ep2")]
_INV = {"100:1:1": {"rating_key": "a", "series_title": "Show", "title": "Pilot"},
        "100:1:2": {"rating_key": "b", "series_title": "Show", "title": "Ep2"}}
_STATS = {"resolution_pct": 100.0, "max_pages_hit": False}


def _items(cache, safe_user):
    return [it["rating_key"] for it in cache.get(f"{_PLAN_KEY}/{safe_user}")["items"]]


# ── happy path: per-user plans built, cached, previewed ────────────────────────
def test_builds_and_caches_per_user_plans():
    cache, log = _Cache(), _Log()
    m = _mgr(cache, log)
    res = m._build_for_users(_TRACKED, _OWNED, _INV, _STATS, {1: 80.0},
                             {"rob": {"a"}, "kid": set()},      # rob already watched 'a'
                             daemon_enabled=True, daemon_running=False)
    assert res == {"users": 2, "built": 2, "can_build": True}
    assert _items(cache, "rob") == ["b"]                       # watched 'a' dropped
    assert _items(cache, "kid") == ["a", "b"]                  # nothing watched
    assert len(log.grids) == 2                                 # a preview per user
    assert log.warns == []                                     # full coverage, scored


def test_serialized_plan_shape():
    cache = _Cache()
    _mgr(cache)._build_for_users(_TRACKED[:1], _OWNED, _INV, _STATS, {1: 80.0},
                                 {"rob": set()}, daemon_enabled=True, daemon_running=False)
    plan = cache.get(f"{_PLAN_KEY}/rob")
    assert plan["family"] == "up_next" and "coverage" in plan
    it = plan["items"][0]
    assert set(it) == {"rating_key", "ordinal", "group_key", "group_kind", "score", "reason"}
    assert it["group_kind"] == "series"


# ── no inventory: cannot build, actionable warn, nothing cached ────────────────
def test_no_inventory_short_circuits():
    cache, log = _Cache(), _Log()
    res = _mgr(cache, log)._build_for_users(_TRACKED, _OWNED, {}, {}, {1: 80.0},
                                            {}, daemon_enabled=True, daemon_running=False)
    assert res["can_build"] is False and res["built"] == 0
    assert cache.d == {}                                       # nothing written
    assert any("plex.episodes.enabled" in w for w in log.warns)


# ── enrichment pending: builds, but logs the auto-resume message ───────────────
def test_enrichment_pending_message_logged_but_still_builds():
    owned = _OWNED + [_owned(2, 1, 1, "200:1:1", "S2")]
    inv = dict(_INV); inv["200:1:1"] = {"rating_key": "c", "series_title": "Two", "title": "S2"}
    cache, log = _Cache(), _Log()
    res = _mgr(cache, log)._build_for_users(
        _TRACKED[:1], owned, inv, _STATS, {},                 # NO series scored
        {"rob": set()}, daemon_enabled=True, daemon_running=True)
    assert res["built"] == 1                                   # still builds
    assert any("AUTOMATICALLY resume" in i and "running now" in i for i in log.infos)


def test_low_coverage_warns_and_still_builds():
    cache, log = _Cache(), _Log()
    res = _mgr(cache, log)._build_for_users(
        _TRACKED[:1], _OWNED, _INV, {"resolution_pct": 40.0, "max_pages_hit": False},
        {1: 80.0}, {"rob": set()}, daemon_enabled=True, daemon_running=False)
    assert res["built"] == 1 and any("match a Plex item" in w for w in log.warns)


def test_age_gating_restricts_a_kid_profile_to_age_appropriate_certs():
    owned = [_owned(1, 1, 1, "k1"), _owned(2, 1, 1, "k2")]   # series 1 kids, series 2 adult
    inv = {"k1": {"rating_key": "a"}, "k2": {"rating_key": "b"}}
    cache, log = _Cache(), _Log()
    res = _mgr(cache, log)._build_for_users(
        [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}],
        owned, inv, _STATS, {1: 50, 2: 50}, {"wyatt": set()},
        series_certs={1: "TV-Y", 2: "TV-MA"}, daemon_enabled=False, daemon_running=False)
    assert res["built"] == 1
    assert _items(cache, "wyatt") == ["a"]                   # TV-MA series excluded
    assert any("age-gated" in i for i in log.infos)


def test_csm_age_fallback_admits_uncertified_kids_series():
    # The fix: with NO Sonarr cert, the gate falls back to the Common Sense age.
    #   series 1 → CSM age 4 (little kid) kept; series 2 → CSM age 16 (adult) excluded;
    #   series 3 → no cert AND no age → fail-closed (excluded), unchanged behaviour.
    owned = [_owned(1, 1, 1, "k1"), _owned(2, 1, 1, "k2"), _owned(3, 1, 1, "k3")]
    inv = {"k1": {"rating_key": "a"}, "k2": {"rating_key": "b"}, "k3": {"rating_key": "c"}}
    cache = _Cache()
    _mgr(cache)._build_for_users(
        [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}],
        owned, inv, _STATS, {1: 50, 2: 50, 3: 50}, {"wyatt": set()},
        series_certs={},                                  # nothing certified
        series_csm_ages={1: 4, 2: 16},                    # series 3 has no CSM age
        daemon_enabled=False, daemon_running=False)
    assert _items(cache, "wyatt") == ["a"]                # only the CSM-young series survives


def test_real_cert_still_wins_over_csm_age():
    # A recognised cert decides; CSM age is consulted only when the cert is unknown.
    owned = [_owned(1, 1, 1, "k1"), _owned(2, 1, 1, "k2")]
    inv = {"k1": {"rating_key": "a"}, "k2": {"rating_key": "b"}}
    cache = _Cache()
    _mgr(cache)._build_for_users(
        [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}],
        owned, inv, _STATS, {1: 50, 2: 50}, {"wyatt": set()},
        series_certs={1: "TV-MA"},                        # explicit adult cert on series 1
        series_csm_ages={1: 4, 2: 4},                     # low age must NOT override TV-MA
        daemon_enabled=False, daemon_running=False)
    assert _items(cache, "wyatt") == ["b"]                # series 1 stays excluded (TV-MA)


def test_adult_profile_sees_all_certs():
    owned = [_owned(1, 1, 1, "k1"), _owned(2, 1, 1, "k2")]
    inv = {"k1": {"rating_key": "a"}, "k2": {"rating_key": "b"}}
    cache = _Cache()
    _mgr(cache)._build_for_users(
        [{"safe_user": "trizzd", "title": "Trizzd", "is_admin": True}],   # no restriction → adult
        owned, inv, _STATS, {1: 50, 2: 50}, {"trizzd": set()},
        series_certs={1: "TV-Y", 2: "TV-MA"}, daemon_enabled=False, daemon_running=False)
    assert set(_items(cache, "trizzd")) == {"a", "b"}        # both kept


def test_config_override_age_beats_plex_profile():
    owned = [_owned(1, 1, 1, "k1"), _owned(2, 1, 1, "k2")]
    inv = {"k1": {"rating_key": "a"}, "k2": {"rating_key": "b"}}
    cache = _Cache()
    # Plex says 'teen' but operator override pins 'little_kid' → only TV-Y survives
    m = _mgr(cache, config={"plex": {"playlists": {"profile_ages": {"Kid": "little_kid"}}}})
    m._build_for_users(
        [{"safe_user": "kid", "title": "Kid", "restriction_profile": "teen"}],
        owned, inv, _STATS, {1: 50, 2: 50}, {"kid": set()},
        series_certs={1: "TV-Y", 2: "PG-13"}, daemon_enabled=False, daemon_running=False)
    assert _items(cache, "kid") == ["a"]


def test_episode_cap_from_config():
    owned = [_owned(1, 1, e, f"k{e}") for e in range(1, 6)]
    inv = {f"k{e}": {"rating_key": f"r{e}", "series_title": "S", "title": f"e{e}"} for e in range(1, 6)}
    cache = _Cache()
    m = _mgr(cache, config={"plex": {"playlists": {"episode_cap": 2}}})
    m._build_for_users(_TRACKED[:1], owned, inv, _STATS, {1: 80.0}, {"rob": set()},
                       daemon_enabled=False, daemon_running=False)
    assert _items(cache, "rob") == ["r1", "r2"]                # capped at 2


def test_as_genre_list_parses_json_string_cell():
    """REGRESSION: the Sonarr episode cache serializes genres as a JSON-array STRING
    ('["Animation", "Family"]'). A naive comma-split leaves [ ] " on each token so they
    never match the affinity vocab and the per-user tilt silently degrades to a uniform
    floor scaling (every profile gets the same household order). Must parse JSON first."""
    f = PlexPlaylistBuilderManager._as_genre_list
    assert f('["Animation", "Children", "Comedy", "Family"]') == \
        ["Animation", "Children", "Comedy", "Family"]
    assert f("Drama, Action") == ["Drama", "Action"]          # plain CSV still works
    assert f(["Sci-Fi & Fantasy", "Drama"]) == ["Sci-Fi & Fantasy", "Drama"]  # real list
    assert f("[]") == [] and f(None) == []


# ── JIT priority (user affinity > JIT > household) ────────────────────────────
def test_jit_series_attributed_per_member():
    """jit_grabbed ∩ jit_watchers → only the member(s) actually watching a series get it."""
    cache = _Cache()
    cache.set("sonarr/sonarr/jit_grabbed", [10, 20])
    cache.set("sonarr/sonarr/jit_watchers", {"10": ["Dad"], "20": ["Kid", "Dad"]})
    m = _mgr(cache, config={"sonarr_instances": {"sonarr": {}}})
    tracked = [{"safe_user": "dad", "tautulli_username": "Dad"},
               {"safe_user": "kid", "tautulli_username": "Kid"}]
    out = m._jit_series_by_user(tracked)
    assert out["dad"] == {10, 20}
    assert out["kid"] == {20}                                  # not series 10 (Dad-only)


def test_priority_weights_enforce_precedence_at_every_tilt():
    """REGRESSION (review): the precedence affinity > JIT > household must be INTRINSIC —
    a low/edge personal_tilt (the buggy 10 default, the degenerate 50) must NOT invert it,
    and a wild jit_weight must stay bracketed."""
    for conf in [{}, {"plex": {"playlists": {"personal_tilt": 10}}},
                 {"plex": {"playlists": {"personal_tilt": 50}}},
                 {"plex": {"playlists": {"personal_tilt": 90}}},
                 {"plex": {"playlists": {"jit_weight": 5.0}}}]:          # wild jit_weight
        aff_w, hh_w, jit_w = _mgr(config=conf)._priority_weights()
        assert aff_w > jit_w > hh_w, f"precedence inverted for {conf}"


def test_default_config_is_affinity_led_not_household():
    """REGRESSION (review): with NO knobs set, a perfect-affinity off-household series must
    outrank a zero-affinity household-favourite — the old default (tilt=10) inverted this."""
    from scripts.managers.machine_learning.playlists.per_user import priority_score
    aff_w, hh_w, jit_w = _mgr(config={})._priority_weights()
    on_taste = priority_score(0.0, 1.0, is_jit=False, affinity_weight=aff_w,
                              jit_weight=jit_w, household_weight=hh_w)
    hh_fav = priority_score(1.0, 0.0, is_jit=False, affinity_weight=aff_w,
                            jit_weight=jit_w, household_weight=hh_w)
    assert on_taste > hh_fav


def test_jit_series_lifted_above_household_popular():
    """A JIT-grabbed (actively-watched) series outranks a higher-household but off-taste
    show for the member watching it — JIT beats plain household popularity."""
    cache = _Cache()
    cache.set("sonarr/sonarr/jit_grabbed", [2])
    cache.set("sonarr/sonarr/jit_watchers", {"2": ["Rob"]})
    m = _mgr(cache, config={"plex": {"playlists": {"personal_tilt": 90}},
                            "sonarr_instances": {"sonarr": {}}})
    tracked = [{"safe_user": "rob", "title": "Rob", "tautulli_username": "Rob"}]
    owned = [_owned(1, 1, 1, "100:1:1", "PopEp"), _owned(2, 1, 1, "200:1:1", "JitEp")]
    inv = {"100:1:1": {"rating_key": "pop", "series_title": "Popular", "title": "PopEp"},
           "200:1:1": {"rating_key": "jit", "series_title": "JitShow", "title": "JitEp"}}
    m._build_for_users(tracked, owned, inv, _STATS, {1: 90.0, 2: 20.0},   # 1 popular, 2 low
                       {"rob": set()}, daemon_enabled=False, daemon_running=False)
    assert _items(cache, "rob") == ["jit", "pop"]


def test_jit_series_without_household_score_still_ranked():
    """REGRESSION (re-review): a freshly JIT-acquired series the daemon hasn't scored yet
    (absent from series_scores) must still be ranked by its JIT signal — not dropped to
    last — else JIT can't surface the new show you just started."""
    cache = _Cache()
    cache.set("sonarr/sonarr/jit_grabbed", [2])
    cache.set("sonarr/sonarr/jit_watchers", {"2": ["Rob"]})
    m = _mgr(cache, config={"plex": {"playlists": {"personal_tilt": 90}},
                            "sonarr_instances": {"sonarr": {}}})
    tracked = [{"safe_user": "rob", "title": "Rob", "tautulli_username": "Rob"}]
    owned = [_owned(1, 1, 1, "100:1:1", "S1"), _owned(2, 1, 1, "200:1:1", "N1")]
    inv = {"100:1:1": {"rating_key": "scored", "series_title": "Scored", "title": "S1"},
           "200:1:1": {"rating_key": "jitnew", "series_title": "JitNew", "title": "N1"}}
    m._build_for_users(tracked, owned, inv, _STATS, {1: 50.0},   # series 2 has NO score
                       {"rob": set()}, daemon_enabled=False, daemon_running=False)
    items = _items(cache, "rob")
    assert "jitnew" in items and items[0] == "jitnew"          # ranked by JIT, leads


def test_non_finite_weights_fail_safe():
    """REGRESSION (re-review): inf/nan config weights (json.load accepts Infinity/NaN) must
    NOT propagate non-finite into the ranking and collapse order_items' sort."""
    import math
    m = _mgr(config={"plex": {"playlists": {"jit_weight": float("inf"),
                                            "affinity_weight": float("nan")}}})
    aff_w, hh_w, jit_w = m._priority_weights()
    assert all(math.isfinite(w) for w in (aff_w, hh_w, jit_w))
    assert aff_w > jit_w > hh_w
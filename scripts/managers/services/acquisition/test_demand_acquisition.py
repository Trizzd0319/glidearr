"""Tests for demand-aware acquisition ORDERING — the _demand_rank reorder, per-instance tightness, the
per-user affinity enumeration, and the popularity prior. Exercises the helpers in isolation (the full
run() has heavy deps); the default-OFF run path keeps the existing score-desc behavior."""
from __future__ import annotations

from scripts.managers.services.acquisition import AcquisitionManager


class _IM:
    def __init__(self, free, total): self._free = free; self._total = total
    def disk_free_gb(self, inst): return self._free
    def disk_total_gb(self, inst): return self._total


class _Gw:
    def __init__(self, service, free, total=1000.0): self.service = service; self.im = _IM(free, total)


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _Users:
    def __init__(self, tracked): self.tracked_users = tracked


class _Reg:
    def __init__(self, m): self.m = m
    def get(self, category, name): return self.m.get(name)


class _Log:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


_CFG = {"free_space_limit": 100,
        "acquisition": {"demand": {"enabled": True, "band": 0.30, "threshold": 0.15}}}
_TRACKED = [{"safe_user": "rob"}, {"safe_user": "sam"}]
_AFF = {"tautulli/users/rob/affinity": {"genres": {"action": 1.0}},
        "tautulli/users/sam/affinity": {"genres": {"action": 1.0}}}     # both love Action


def _mgr(*, tracked=_TRACKED, cache=_AFF, config=_CFG):
    m = object.__new__(AcquisitionManager)
    m.config = config; m.logger = _Log(); m.global_cache = _Cache(cache)
    m.registry = _Reg({"PlexUsersManager": _Users(tracked)})
    return m


def _cands():
    return [
        {"type": "movie", "title": "Action 70", "genres": ["Action"], "score": 70, "votes": 1000, "instance": "r"},
        {"type": "movie", "title": "Comedy 90", "genres": ["Comedy"], "score": 90, "votes": 1000, "instance": "r"},
    ]


def test_demand_rank_promotes_broad_appeal_when_tight():
    # floor 100, free 110 → tight (band top 130). Both users love Action → the lower-scored Action title
    # outranks the higher-scored Comedy nobody's taste matches.
    m = _mgr()
    gateways = {"radarr": _Gw("radarr", 110), "sonarr": _Gw("sonarr", 110)}
    cands = _cands()
    m._demand_rank(cands, gateways, {}, _CFG["acquisition"])
    assert [c["title"] for c in cands] == ["Action 70", "Comedy 90"]


def test_demand_rank_is_byte_identical_when_roomy():
    # free 500 >> band top → t=0 → demand-neutral → pure score-desc.
    m = _mgr()
    gateways = {"radarr": _Gw("radarr", 500), "sonarr": _Gw("sonarr", 500)}
    cands = _cands()
    m._demand_rank(cands, gateways, {}, _CFG["acquisition"])
    assert [c["title"] for c in cands] == ["Comedy 90", "Action 70"]


def test_per_user_affinities_reads_each_tracked_user_cache():
    assert _mgr()._per_user_affinities() == [{"action": 1.0}, {"action": 1.0}]
    # a user with no cached affinity yields {} (→ demand uses the popularity prior for them)
    m = _mgr(tracked=[{"safe_user": "rob"}, {"safe_user": "newbie"}])
    assert m._per_user_affinities() == [{"action": 1.0}, {}]


def test_votes_to_unit_matches_scorer_scale():
    m = _mgr()
    assert m._votes_to_unit(0) == 0.0 and m._votes_to_unit(None) == 0.0
    assert round(m._votes_to_unit(50000), 2) == 1.0
    assert 0.0 < m._votes_to_unit(100) < 1.0


def test_instance_tightness_floor_band_and_fail_open():
    m = _mgr()
    assert 0.0 < m._instance_tightness(_Gw("radarr", 110), "r", 0.30, {}) < 1.0   # in the band
    assert m._instance_tightness(_Gw("radarr", 200), "r", 0.30, {}) == 0.0        # roomy
    assert m._instance_tightness(_Gw("radarr", 90), "r", 0.30, {}) == 1.0         # at/below floor
    assert m._instance_tightness(None, "r", 0.30, {}) == 0.0                       # no gateway → 0


def test_instance_tightness_hysteresis_holds_across_runs():
    # Engaged last run (persisted t>0); free recovers to 135 GB — inside the hysteresis zone (band top
    # 130, release 140) — so it stays tight rather than snapping to 0 and re-grabbing into a near-full disk.
    m = _mgr(cache={"acquisition/demand/tightness/radarr/r": 0.5})
    assert m._instance_tightness(_Gw("radarr", 135), "r", 0.30, {}) > 0.0
    # A fresh run (no persisted state) at the same 135 GB is already released.
    m2 = _mgr(cache={})
    assert m2._instance_tightness(_Gw("radarr", 135), "r", 0.30, {}) == 0.0
    # The new t is persisted for the next run.
    assert "acquisition/demand/tightness/radarr/r" in m.global_cache.d


def test_cold_users_fall_back_to_popularity_prior():
    # Roster present but neither user matched to Tautulli (no affinity) → each contributes the
    # popularity prior, so under tightness the more-popular title leads despite a lower score.
    m = _mgr(tracked=[{"safe_user": "a"}, {"safe_user": "b"}], cache={})
    gateways = {"radarr": _Gw("radarr", 110), "sonarr": _Gw("sonarr", 110)}
    cands = [
        {"type": "movie", "title": "Obscure 90", "genres": ["Drama"], "score": 90, "votes": 10, "instance": "r"},
        {"type": "movie", "title": "Popular 70", "genres": ["Drama"], "score": 70, "votes": 40000, "instance": "r"},
    ]
    m._demand_rank(cands, gateways, {}, _CFG["acquisition"])
    assert cands[0]["title"] == "Popular 70"     # popularity prior breaks the tie toward broad reach


_FAIR_CFG = {"free_space_limit": 100,
             "acquisition": {"max_adds_per_run": 2,
                             "demand": {"enabled": True, "band": 0.30, "threshold": 0.15}}}


def test_fairness_reserves_each_users_top_pick_under_scarcity():
    # Rob→Action, Sam→Comedy, cap 2. Demand is ~1 each, so order is score-desc: A(80), B(75), C(70) —
    # the top-2 would drop Sam's only pick (C). The fairness floor guarantees C a slot, keeping the top
    # broad-appeal pick (A) and displacing the redundant second Action title (B).
    m = _mgr(tracked=[{"safe_user": "rob"}, {"safe_user": "sam"}],
             cache={"tautulli/users/rob/affinity": {"genres": {"action": 1.0}},
                    "tautulli/users/sam/affinity": {"genres": {"comedy": 1.0}}},
             config=_FAIR_CFG)
    gateways = {"radarr": _Gw("radarr", 110), "sonarr": _Gw("sonarr", 110)}
    cands = [
        {"type": "movie", "title": "A", "genres": ["Action"], "score": 80, "votes": 0, "instance": "r"},
        {"type": "movie", "title": "B", "genres": ["Action"], "score": 75, "votes": 0, "instance": "r"},
        {"type": "movie", "title": "C", "genres": ["Comedy"], "score": 70, "votes": 0, "instance": "r"},
    ]
    m._demand_rank(cands, gateways, {}, _FAIR_CFG["acquisition"])
    top2 = {c["title"] for c in cands[:2]}
    assert "C" in top2 and "A" in top2 and "B" not in top2


def test_fairness_inactive_when_roomy_keeps_score_order():
    # Same setup but roomy → no scarcity → no fairness reshuffle → pure score-desc.
    m = _mgr(tracked=[{"safe_user": "rob"}, {"safe_user": "sam"}],
             cache={"tautulli/users/rob/affinity": {"genres": {"action": 1.0}},
                    "tautulli/users/sam/affinity": {"genres": {"comedy": 1.0}}},
             config=_FAIR_CFG)
    gateways = {"radarr": _Gw("radarr", 500), "sonarr": _Gw("sonarr", 500)}
    cands = [
        {"type": "movie", "title": "A", "genres": ["Action"], "score": 80, "votes": 0, "instance": "r"},
        {"type": "movie", "title": "B", "genres": ["Action"], "score": 75, "votes": 0, "instance": "r"},
        {"type": "movie", "title": "C", "genres": ["Comedy"], "score": 70, "votes": 0, "instance": "r"},
    ]
    m._demand_rank(cands, gateways, {}, _FAIR_CFG["acquisition"])
    assert [c["title"] for c in cands] == ["A", "B", "C"]


# ── Phase 5: degenerate-state stress (sane behavior, no collapse) ─────────────
def test_stress_disk_full_orders_by_breadth_then_watchability():
    # free below the floor → t=1 everywhere → priority == watchability × demand; broad titles lead.
    m = _mgr()
    gateways = {"radarr": _Gw("radarr", 50), "sonarr": _Gw("sonarr", 50)}   # below floor 100
    cands = _cands()
    m._demand_rank(cands, gateways, {}, _CFG["acquisition"])
    assert [c["title"] for c in cands] == ["Action 70", "Comedy 90"]        # 70×2 > 90×0


def test_stress_unreadable_disk_fails_open_to_score_desc():
    # disk_free_gb raising → free=inf → t=0 → demand-neutral → score order (never blocks on a read error).
    class _Boom:
        service = "radarr"
        class im:
            @staticmethod
            def disk_free_gb(inst): raise OSError("unreadable")
            @staticmethod
            def disk_total_gb(inst): return None
    m = _mgr()
    cands = _cands()
    m._demand_rank(cands, {"radarr": _Boom(), "sonarr": _Boom()}, {}, _CFG["acquisition"])
    assert [c["title"] for c in cands] == ["Comedy 90", "Action 70"]


def test_no_roster_degrades_to_score_desc():
    # No tracked users at all → no breadth signal → graceful fallback to watchability order (not a
    # collapse to 0 under tightness).
    m = _mgr(tracked=[], cache={})
    gateways = {"radarr": _Gw("radarr", 110), "sonarr": _Gw("sonarr", 110)}
    cands = [
        {"type": "movie", "title": "Lo 70", "genres": ["Drama"], "score": 70, "votes": 10, "instance": "r"},
        {"type": "movie", "title": "Hi 90", "genres": ["Drama"], "score": 90, "votes": 10, "instance": "r"},
    ]
    m._demand_rank(cands, gateways, {}, _CFG["acquisition"])
    assert [c["title"] for c in cands] == ["Hi 90", "Lo 70"]

"""AcquisitionManager._space_budget_context — the per-run byte-budget snapshot and its
fail-direction inversion: 'off' is byte-identical legacy, 'budget' carries a live context
plus the TTL-surviving ledger, and ANY information gap (unreadable free space, corrupt or
raising ledger) is 'fallback' — the bounded count cap, never unlimited."""
from __future__ import annotations

import time

from scripts.managers.machine_learning.acquisition import space_budget
from scripts.managers.services.acquisition import AcquisitionManager


class _Log:
    def __init__(self):
        self.warns = []
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_warning(self, m, *a, **k): self.warns.append(str(m))


class _GC:
    def __init__(self, data=None, raise_on_get=False):
        self.d = dict(data or {})
        self.raise_on_get = raise_on_get
    def get(self, k):
        if self.raise_on_get:
            raise RuntimeError("cache down")
        return self.d.get(k)
    def set(self, k, v):
        self.d[k] = v


class _GW:
    available = True


def _mgr(free_by, ledger=None, gc=None, sb_cfg=None):
    m = object.__new__(AcquisitionManager)
    m.logger = _Log()
    m.global_cache = gc if gc is not None else _GC(
        {space_budget.LEDGER_KEY: ledger} if ledger is not None else {})
    # Same memoised signature as the real band reader; U pinned at 3300 (the
    # free_space_limit 3000 + 10% headroom this deployment runs).
    m._space_band = lambda gw, inst, cache, _f=free_by: (_f[inst], 3300.0)
    return m, {"space_budget": dict({"enabled": True}, **(sb_cfg or {}))}


_GWS = {"radarr": _GW(), "sonarr": _GW()}
_ELIG = [{"type": "movie", "instance": "standard"},
         {"type": "show", "instance": "standard"}]


def test_budget_mode_nets_inflight_from_min_headroom():
    m, acq = _mgr({"standard": 3800.0},
                  ledger=[{"gb": 100.0, "committed_at": time.time(),
                           "service": "radarr", "instance": "standard"}])
    mode, ctx, kept = m._space_budget_context(_ELIG, acq, _GWS, {})
    assert mode == "budget" and len(kept) == 1
    # headroom 3800-3300=500, minus 100 in flight
    assert abs(ctx.budgets[space_budget.SHARED_POOL] - 400.0) < 1e-9


def test_unreadable_free_space_falls_back_to_the_count_cap():
    m, acq = _mgr({"standard": float("inf")})
    mode, ctx, kept = m._space_budget_context(_ELIG, acq, _GWS, {})
    assert mode == "fallback" and ctx is None and kept is None
    assert m.logger.warns                      # loud, never silent


def test_corrupt_ledger_falls_back():
    m, acq = _mgr({"standard": 3800.0}, gc=_GC({space_budget.LEDGER_KEY: {"not": "a list"}}))
    assert m._space_budget_context(_ELIG, acq, _GWS, {})[0] == "fallback"


def test_raising_ledger_falls_back():
    m, acq = _mgr({"standard": 3800.0}, gc=_GC(raise_on_get=True))
    assert m._space_budget_context(_ELIG, acq, _GWS, {})[0] == "fallback"


def test_absent_ledger_is_first_run_empty_not_fallback():
    """The safe conflation, stated: before the feature ever ran nothing was ever
    committed, so an ABSENT key genuinely means empty. This cache's missing-key
    sentinel is {} (GlobalCacheManager.get's compat wrapper documents it), so
    BOTH None and {} must read as first-run empty -- the first live run proved
    it, when a strict `is not None` check read the {} sentinel as corrupt, fell
    back, and (fallback never persists) could never arm the budget at all. Only
    truthy non-list values are 'unknown'."""
    for missing in (None, {}):
        gc = _GC({space_budget.LEDGER_KEY: missing} if missing is not None else {})
        m, acq = _mgr({"standard": 3800.0}, gc=gc)
        mode, ctx, kept = m._space_budget_context(_ELIG, acq, _GWS, {})
        assert mode == "budget" and kept == []
        assert abs(ctx.budgets[space_budget.SHARED_POOL] - 500.0) < 1e-9


def test_ttl_expiry_releases_budget_at_snapshot_time():
    old = time.time() - 73 * 3600              # past the 72h default
    m, acq = _mgr({"standard": 3800.0},
                  ledger=[{"gb": 400.0, "committed_at": old,
                           "service": "radarr", "instance": "standard"}])
    mode, ctx, kept = m._space_budget_context(_ELIG, acq, _GWS, {})
    assert mode == "budget" and kept == []
    assert abs(ctx.budgets[space_budget.SHARED_POOL] - 500.0) < 1e-9


def test_disabled_is_off_and_touches_nothing():
    m, acq = _mgr({"standard": 3800.0}, sb_cfg={"enabled": False})
    assert m._space_budget_context(_ELIG, acq, _GWS, {}) == ("off", None, None)


def test_per_instance_mode_prices_each_pool():
    m, acq = _mgr({"standard": 3800.0}, sb_cfg={"shared_pool": False})
    # shows and movies both route to 'standard' here but land in different pools
    mode, ctx, _ = m._space_budget_context(_ELIG, acq, _GWS, {})
    assert mode == "budget"
    assert abs(ctx.budgets[("radarr", "standard")] - 500.0) < 1e-9
    assert abs(ctx.budgets[("sonarr", "standard")] - 500.0) < 1e-9

"""machine_learning.acquisition.space_budget — bytes-not-counts acquisition budget.
Prices every candidate in GB, funds them in priority order out of max(0, free-U)
minus RECENT runs' committed-but-unlanded bytes, and inverts the usual fail-open
convention: incomplete information collapses to the bounded count cap, never to
unlimited. See the module docstring for the fail-direction and P-C rationale."""
from __future__ import annotations

from scripts.managers.machine_learning.acquisition.space_budget import (
    DEFAULTS,
    LEDGER_KEY,
    SHARED_POOL,
    BudgetContext,
    charge_gb,
    config_for,
    inflight_by_pool,
    make_entry,
    pool_key,
    reconcile,
    select,
)

_NOW = 1_760_000_000.0
_CFG = config_for({"space_budget": {"enabled": True}})


def _movie(gb, title="M", inst="standard", **kw):
    d = {"type": "movie", "instance": inst, "title": title, "ext_id": 1,
         "expected_size_gb": gb}
    d.update(kw)
    return d


def _show(gb, title="S", inst="standard", **kw):
    d = {"type": "show", "instance": inst, "title": title, "ext_id": 2,
         "expected_size_gb": gb, "size_unit": "per-episode"}
    d.update(kw)
    return d


def test_defaults_ship_disabled_and_config_merges():
    assert DEFAULTS["enabled"] is False          # bare-config testers keep legacy behaviour
    assert config_for({})["enabled"] is False
    cfg = config_for({"space_budget": {"enabled": True, "committed_ttl_hours": 24}})
    assert cfg["enabled"] is True and cfg["committed_ttl_hours"] == 24
    assert cfg["default_movie_gb"] == 15.0       # untouched default survives a partial block


def test_charge_prices_unknown_at_default_never_zero():
    """P-C: a candidate with no usable size must not be exempt from the budget."""
    for bad in (None, 0, -3, "x"):
        gb, defaulted = charge_gb(_movie(bad), _CFG)
        assert gb == 15.0 and defaulted
        gb, defaulted = charge_gb(_show(bad), _CFG)
        assert gb == 2.0 and defaulted
    gb, defaulted = charge_gb(_movie(22.5), _CFG)
    assert gb == 22.5 and not defaulted


def test_show_charge_is_one_pilot_episode():
    """Per-episode size is charged x1 — the pilot-floor policy pulls exactly the pilot."""
    gb, _ = charge_gb(_show(1.4), _CFG)
    assert gb == 1.4


def test_select_funds_in_order_and_never_overruns():
    ctx = BudgetContext({SHARED_POOL: 100.0}, _CFG, _NOW)
    elig = [_movie(40, "a"), _movie(40, "b"), _movie(40, "c"), _show(1.5, "d")]
    selected, skipped = select(elig, ctx)
    assert [e["title"] for e in selected] == ["a", "b", "d"]   # c refused, d fits behind it
    assert [e["title"] for e in skipped] == ["c"]
    assert skipped[0]["skip_reason"] == "space_budget"
    assert sum(e["space_charge_gb"] for e in selected) <= 100.0
    assert ctx.stats["funded"] == 3 and ctx.stats["skipped_space"] == 1


def test_skip_and_continue_lets_small_titles_past_a_big_refusal():
    """A 40 GB movie at the budget edge must not strand the cheap shows behind it —
    and the big title keeps first claim on next run's refreshed budget by staying
    top of the order, so this is not starvation."""
    ctx = BudgetContext({SHARED_POOL: 41.0}, _CFG, _NOW)
    selected, skipped = select([_movie(35, "big"), _movie(35, "big2"),
                                _show(1.5, "s1"), _show(1.5, "s2")], ctx)
    assert [e["title"] for e in selected] == ["big", "s1", "s2"]
    assert [e["title"] for e in skipped] == ["big2"]


def test_hard_max_adds_is_an_optional_ceiling():
    ctx = BudgetContext({SHARED_POOL: 9999.0}, dict(_CFG, hard_max_adds=2), _NOW)
    selected, skipped = select([_movie(1, "a"), _movie(1, "b"), _movie(1, "c")], ctx)
    assert len(selected) == 2
    assert skipped[0]["skip_reason"] == "space_budget_hard_max"


def test_unknown_pool_passes_uncharged_and_is_counted():
    """A pool the snapshot could not price (gateway down) must not turn an outage
    into a silent policy change — the add fails downstream with a real error."""
    ctx = BudgetContext({("radarr", "standard"): 50.0},
                        dict(_CFG, shared_pool=False), _NOW)
    ok, gb, _ = ctx.try_charge(_movie(500, inst="ultra"))
    assert ok and gb is None
    assert ctx.stats["uncharged_no_pool"] == 1


def test_companion_charging_shares_the_live_pool():
    """The 4K copy is priced during the add loop, out of the SAME remaining budget."""
    ctx = BudgetContext({SHARED_POOL: 60.0}, _CFG, _NOW)
    ok, _, _ = ctx.try_charge(_movie(20, "hd"))
    ok4, gb4, _ = ctx.try_charge(_movie(55, "uhd", inst="ultra"))
    assert ok and not ok4                     # 55 > 40 remaining
    ctx2 = BudgetContext({SHARED_POOL: 90.0}, _CFG, _NOW)
    ctx2.try_charge(_movie(20, "hd"))
    ok4b, _, _ = ctx2.try_charge(_movie(55, "uhd", inst="ultra"))
    assert ok4b and abs(ctx2.budgets[SHARED_POOL] - 15.0) < 1e-9


def test_reconcile_ttl_and_unparseable_committed_at():
    """Past-TTL entries release their bytes; an entry that cannot be aged is
    EXPIRED (keeping it forever would shrink the budget with no release path)."""
    ttl = _CFG["committed_ttl_hours"] * 3600
    entries = [make_entry(_movie(30, "fresh"), 30, _NOW),
               make_entry(_movie(30, "old"), 30, _NOW - ttl - 1),
               {"gb": 30, "committed_at": "garbage"},
               "not-a-dict"]
    kept, expired = reconcile(entries, _NOW, ttl)
    assert [e["title"] for e in kept] == ["fresh"]
    assert len(expired) == 3


def test_inflight_prices_unparseable_gb_at_fallback_never_zero():
    """P-C on the ledger side: unknown in-flight bytes overstate (budget shrinks —
    safe), never vanish."""
    infl = inflight_by_pool(
        [{"gb": "??", "committed_at": _NOW, "service": "radarr", "instance": "standard"},
         {"gb": 10.0, "committed_at": _NOW, "service": "radarr", "instance": "standard"}],
        shared=True, fallback_gb=15.0)
    assert abs(infl[SHARED_POOL] - 25.0) < 1e-9


def test_cross_run_netting_prevents_phantom_headroom():
    """Run N commits against headroom; run N+1 reads the SAME free (downloads still
    queued) and must see the committed bytes netted out — the GLD-RST-05 shape."""
    headroom = 500.0
    ctx = BudgetContext({SHARED_POOL: headroom}, _CFG, _NOW)
    selected, _ = select([_movie(30, f"m{i}") for i in range(20)], ctx)
    for e in selected:
        ctx.commit(e, e["space_charge_gb"])
    kept, _ = reconcile(ctx.commits, _NOW + 7200, _CFG["committed_ttl_hours"] * 3600)
    infl = inflight_by_pool(kept, True, 15.0)[SHARED_POOL]
    run2_budget = max(0.0, headroom - infl)
    assert len(selected) == 16 and abs(infl - 480.0) < 1e-9 and abs(run2_budget - 20.0) < 1e-9
    ctx2 = BudgetContext({SHARED_POOL: run2_budget}, _CFG, _NOW + 7200)
    sel2, _ = select([_movie(30, f"m{i}") for i in range(20)], ctx2)
    assert sel2 == []                         # correctly refuses while bytes are in flight


def test_commit_reprices_when_charge_is_missing():
    ctx = BudgetContext({SHARED_POOL: 100.0}, _CFG, _NOW)
    ctx.commit(_movie(None, "unk"), None)     # uncharged/unknown -> default price, never 0
    assert ctx.commits[0]["gb"] == 15.0
    assert ctx.commits[0]["committed_at"] == _NOW


def test_pool_key_routes_shows_to_sonarr():
    assert pool_key(_show(1), shared=False) == ("sonarr", "standard")
    assert pool_key(_movie(1), shared=False) == ("radarr", "standard")
    assert pool_key(_movie(1), shared=True) == SHARED_POOL


def test_ledger_key_is_stable():
    assert LEDGER_KEY == "acquisition/space_budget/committed"

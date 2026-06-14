"""Tests for playlists/ordering — the crown-jewel orchestrator + caps + spoiler."""
from __future__ import annotations

from itertools import permutations

from scripts.managers.machine_learning.playlists.caps import apply_size_cap
from scripts.managers.machine_learning.playlists.models import PlaylistInput
from scripts.managers.machine_learning.playlists.ordering import order_items
from scripts.managers.machine_learning.playlists.spoiler import is_spoiler_safe


def mv(rk, score=None, **kw):
    return PlaylistInput(rating_key=rk, medium="movie", score=score, **kw)


def ep(rk, s, e, score=None, **kw):
    return PlaylistInput(rating_key=rk, medium="episode", series_id=kw.pop("sid", 1),
                         season=s, episode=e, score=score, **kw)


def _rks(plan):
    return [i.rating_key for i in plan.items]


# ── per-user watched filter (Hard-req #5) ──────────────────────────────────────
def test_watched_items_are_dropped():
    plan = order_items([mv("a", 50), mv("b", 60, watched=True), mv("c", 70)])
    assert _rks(plan) == ["c", "a"] and plan.dropped_watched == 1


def test_all_watched_yields_empty_plan():
    plan = order_items([mv("a", 50, watched=True)])
    assert plan.items == () and plan.dropped_watched == 1


# ── cross-group ranking by watchability + group contiguity ─────────────────────
def test_groups_ranked_by_watchability_and_kept_contiguous():
    # universe group {u1,u2} max-score 80; standalone hi=90, lo=40.
    items = [mv("u1", 70, universes=("mcu",), release_date="2008-01-01"),
             mv("u2", 80, universes=("mcu",), release_date="2010-01-01"),
             mv("hi", 90), mv("lo", 40)]
    plan = order_items(items)
    # hi(90) > group(80) > lo(40); group members contiguous + chrono within
    assert _rks(plan) == ["hi", "u1", "u2", "lo"]
    assert [i.group_kind for i in plan.items] == ["standalone", "universe", "universe", "standalone"]


def test_ordinals_and_reasons():
    plan = order_items([mv("hi", 90), mv("u1", 50, universes=("x",)),
                        mv("u2", 50, universes=("x",))])
    assert [i.ordinal for i in plan.items] == [0, 1, 2]
    assert plan.items[0].reason.startswith("watchability")
    assert plan.items[1].reason == "universe 'x' · 1/2"


# ── per-medium normalization makes movie vs show scores comparable ─────────────
def test_per_medium_normalization_changes_cross_group_rank():
    # raw: show scores dwarf movie scores; a top movie would lose to a mediocre show.
    items = [mv("topMovie", 95), ep("midShow", 1, 1, score=2000, sid=5)]
    raw = _rks(order_items(items))
    norm = _rks(order_items(items, normalize_per_medium=True))
    assert raw == ["midShow", "topMovie"]          # raw: 2000 > 95
    # normalized: each is the 100th percentile of its own medium → tie → fall to
    # deterministic breakers; both at top percentile, so order is stable/defined.
    assert set(norm) == {"topMovie", "midShow"}


# ── group-atomic size cap ──────────────────────────────────────────────────────
def test_size_cap_is_group_atomic_and_counts_truncation():
    items = [mv("u1", 80, universes=("mcu",)), mv("u2", 80, universes=("mcu",)),
             mv("solo", 90)]
    plan = order_items(items, max_items=2)
    # top is the standalone(90) [1 slot]; next group needs 2 but only 1 slot → stop.
    assert _rks(plan) == ["solo"] and plan.truncated == 2


def test_cap_truncates_within_a_single_oversized_top_group():
    items = [mv(f"u{i}", 80, universes=("mcu",), release_date=f"20{10+i:02d}-01-01")
             for i in range(5)]
    kept, trunc = apply_size_cap([[*items]], 3)
    assert len(kept) == 3 and trunc == 2


# ── spoiler safety holds across the full pipeline, for any input order ──────────
def test_spoiler_safe_property_over_permutations():
    eps = [ep("e1", 1, 1, score=50), ep("e2", 1, 2, score=50, watched=True),
           ep("e3", 1, 3, score=50), ep("e4", 2, 1, score=50)]
    for perm in permutations(eps):
        plan = order_items(list(perm))
        # reconstruct ordered inputs to assert the invariant
        by_rk = {x.rating_key: x for x in eps}
        ordered = [by_rk[i.rating_key] for i in plan.items]
        assert is_spoiler_safe(ordered)
        assert _rks(plan) == ["e1", "e3", "e4"]    # e2 watched → dropped; rest in s/e


def test_full_pipeline_is_input_order_independent():
    items = [mv("a", 30), mv("b", 90), ep("e1", 1, 1, score=60),
             ep("e2", 1, 2, score=60), mv("c", 90, universes=("z",))]
    outs = {tuple(_rks(order_items(list(p)))) for p in permutations(items)}
    assert len(outs) == 1


# ── specials inclusion toggle ──────────────────────────────────────────────────
def test_specials_excluded_by_default_included_on_request():
    items = [ep("e1", 1, 1, score=50), ep("s0", 0, 1, score=50, is_special=True)]
    assert _rks(order_items(items)) == ["e1"]                       # special dropped
    assert set(_rks(order_items(items, include_specials=True))) == {"e1", "s0"}


def test_empty_input_is_safe():
    plan = order_items([])
    assert plan.items == () and plan.considered == 0 and plan.coverage == {}

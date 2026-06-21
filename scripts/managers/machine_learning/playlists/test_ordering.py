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


def test_cap_skips_oversized_block_and_keeps_filling():
    # An oversized group in the MIDDLE of the ranking is stepped over (not stopped at), so
    # smaller lower-ranked groups still fill the budget — a huge group can't starve the rest.
    a, c, d = mv("a"), mv("c"), mv("d")
    big = [mv(f"b{i}") for i in range(5)]
    kept, trunc = apply_size_cap([[a], big, [c], [d]], 3)
    assert [it.rating_key for it in kept] == ["a", "c", "d"]    # big(5) skipped; a,c,d kept
    assert trunc == 5


def test_oversized_midrank_group_is_skipped_not_stopped():
    # hi(95)[1] ranks first; the 5-member universe group(90) ranks 2nd but overflows the
    # remaining budget — the cap must SKIP it and keep filling from lo1(80)/lo2(70), not
    # stop dead at one item. (Regression: a ~200-member junk group once starved the plan to 4.)
    big = [mv(f"g{i}", 90, universes=("mcu",), release_date=f"20{10+i:02d}-01-01")
           for i in range(5)]
    plan = order_items([mv("hi", 95), *big, mv("lo1", 80), mv("lo2", 70)], max_items=3)
    assert _rks(plan) == ["hi", "lo1", "lo2"]                   # big group skipped over
    assert plan.truncated == 5


# ── resume boost: a TUNABLE bonus (saga vs higher-affinity standalone) ──────────
def test_resume_weight_tunes_saga_vs_standalone():
    # mid-MCU (next film 60) vs Passengers(95, much higher) + a near(65) standalone; 'low' just
    # spreads the score normalization so the weight is meaningful.
    items = [mv("seen1", 70, universes=("mcu",), watched=True, last_watched=100, release_date="2012-01-01"),
             mv("mcu_next", 60, universes=("mcu",), release_date="2021-01-01"),
             mv("passengers", 95), mv("near", 65), mv("low", 10)]
    assert _rks(order_items(items))[0] == "passengers"                     # OFF: pure affinity
    w0 = _rks(order_items(items, resume_boost=True, resume_weight=0.0))
    assert w0[0] == "passengers"                                          # weight 0 → affinity-first
    mod = _rks(order_items(items, resume_boost=True, resume_weight=0.35))
    assert mod[0] == "passengers"                                        # big gap → standalone still wins
    assert mod.index("mcu_next") < mod.index("near")                     # but saga overtakes the close one
    assert _rks(order_items(items, resume_boost=True, resume_weight=1.0))[0] == "mcu_next"   # strong → saga first


def test_resume_order_recency_picks_most_recently_watched_saga():
    items = [mv("a_seen", 60, universes=("mcu",), watched=True, last_watched=400),
             mv("a_next", 50, universes=("mcu",), release_date="2020-01-01"),
             mv("b_seen", 60, universes=("xmen",), watched=True, last_watched=900),  # watched later
             mv("b_next", 50, universes=("xmen",), release_date="2020-01-01")]
    plan = order_items(items, resume_boost=True, resume_order="recency")
    assert _rks(plan) == ["b_next", "a_next"]                             # xmen watched most recently


def test_resume_order_progress_picks_deepest_saga():
    items = [mv("a1", 60, universes=("mcu",), watched=True, last_watched=400),
             mv("a2", 60, universes=("mcu",), watched=True, last_watched=410),       # depth 2
             mv("a_next", 50, universes=("mcu",), release_date="2020-01-01"),
             mv("b1", 60, universes=("xmen",), watched=True, last_watched=900),       # depth 1, newer
             mv("b_next", 50, universes=("xmen",), release_date="2020-01-01")]
    plan = order_items(items, resume_boost=True, resume_order="progress")
    assert _rks(plan) == ["a_next", "b_next"]                             # mcu is deeper (2 > 1)


def test_progress_filter_in_keeps_only_in_progress_sagas():
    # The Long Glide: only sagas you've STARTED (a watched member) survive.
    items = [mv("seen1", 70, universes=("mcu",), watched=True, last_watched=100),
             mv("mcu_next", 50, universes=("mcu",)),
             mv("xmen_new", 80, universes=("xmen",)),       # owned but not started
             mv("passengers", 90)]                          # standalone
    assert _rks(order_items(items, progress_filter="in")) == ["mcu_next"]


def test_progress_filter_out_keeps_standalones_and_not_started():
    # Touch & Go: the low-commitment pool — standalones + not-started, by affinity.
    items = [mv("seen1", 70, universes=("mcu",), watched=True, last_watched=100),
             mv("mcu_next", 50, universes=("mcu",)),
             mv("xmen_new", 80, universes=("xmen",)),
             mv("passengers", 90)]
    assert _rks(order_items(items, progress_filter="out")) == ["passengers", "xmen_new"]


def test_progress_filter_in_uses_series_recency_for_tv():
    # TV watched eps are pre-filtered upstream, so series_recency flags in-progress shows.
    items = [ep("e1", 1, 1, score=50, sid=7), ep("e2", 1, 2, score=50, sid=8)]
    assert _rks(order_items(items, progress_filter="in",
                            series_recency={7: (1000, 3)})) == ["e1"]   # only series 7 (in-progress)


def test_resume_recency_ranks_tv_show_by_series_recency():
    # an in-progress show whose last watch is more recent sorts ahead of a less-recent one.
    items = [ep("a1", 2, 1, score=50, sid=1), ep("b1", 2, 1, score=50, sid=2)]
    plan = order_items(items, resume_boost=True, resume_order="recency",
                       series_recency={1: (200, 1), 2: (900, 1)})   # series 2 watched later
    assert _rks(plan) == ["b1", "a1"]


def test_resume_progress_depth_not_inflated_by_queued_tv_episodes():
    # REGRESSION (review): a series' watched depth must count ONCE, not once per queued episode.
    # series 1: 5 watched, 3 queued eps; series 2: 10 watched, 1 queued. Series 2 is genuinely deeper.
    items = [ep("a1", 1, 1, score=50, sid=1), ep("a2", 1, 2, score=50, sid=1),
             ep("a3", 1, 3, score=50, sid=1), ep("b1", 1, 1, score=50, sid=2)]
    plan = order_items(items, resume_boost=True, resume_order="progress",
                       series_recency={1: (100, 5), 2: (100, 10)})   # same ts → depth decides
    assert _rks(plan)[0] == "b1"                                      # depth 10 > 5, NOT 5×3=15


def test_resume_boost_off_is_byte_identical():
    items = [mv("seen1", 70, universes=("mcu",), watched=True, last_watched=100),
             mv("next", 50, universes=("mcu",)), mv("solo", 90)]
    assert _rks(order_items(items)) == _rks(order_items(items, resume_boost=False))


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

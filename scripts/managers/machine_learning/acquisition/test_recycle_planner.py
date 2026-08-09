"""
test_recycle_planner.py — the five guards of self-funding acquisition.
================================================================================
Every test here is about a way this could DELETE SOMETHING IT SHOULD NOT. The happy
path is one test; the rest are refusals.
"""
from scripts.managers.machine_learning.acquisition.recycle_planner import plan_recycle


def _w(season, ep, gb, watched_at):
    return {"season": season, "episode": ep, "size_gb": gb,
            "watched_at": watched_at, "episode_file_id": ep}


def _want(season, ep, gb):
    return {"season": season, "episode": ep, "est_gb": gb}


# ── the happy path ────────────────────────────────────────────────────────────
def test_funds_one_acquisition_from_the_oldest_watch():
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 2.0, "2026-01-01"), _w(1, 2, 2.0, "2026-02-01"),
                       _w(1, 3, 2.0, "2026-03-01"), _w(1, 4, 2.0, "2026-04-01")],
        wanted=[_want(1, 7, 2.0)],
        free_gb=100.0, floor_gb=500.0, rewatch_buffer=2)
    assert [e["episode"] for e in plan["acquire"]] == [7]
    # OLDEST watch recycled first, and only as much as the acquisition needs.
    assert [e["episode"] for e in plan["recycle"]] == [1]
    assert plan["freed_gb"] >= plan["cost_gb"]


def test_cost_never_exceeds_the_tolerance_band():
    # NOT `freed >= cost` -- the 40% band deliberately permits an acquisition to cost
    # more than it recycles. What must hold is the BOUND: cost <= freed x (1+tol).
    # (The older assertion passed only by coincidence of the single-pass loop's
    # arithmetic, and would have hidden a genuine overshoot.)
    plan = plan_recycle(
        watched_owned=[_w(1, i, 1.0, f"2026-0{i}-01") for i in range(1, 8)],
        wanted=[_want(1, 9, 2.5), _want(1, 10, 1.5)],
        free_gb=10.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert plan["cost_gb"] <= plan["freed_gb"] * 1.4 + 1e-9


def test_deletes_only_what_the_advance_costs():
    # Six eligible episodes (6 GB) but the advance costs 2 GB -> most of the pool must
    # stay on disk. A recycle that is not paying for a named acquisition is a RECLAIM,
    # which is a different act with a different consent.
    plan = plan_recycle(
        watched_owned=[_w(1, i, 1.0, f"2026-0{i}-01") for i in range(1, 8)],
        wanted=[_want(1, 9, 2.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert len(plan["recycle"]) <= 2, "must not empty the pool for a small advance"
    assert plan["held_gb"] > 0


def test_advances_as_far_as_the_whole_pool_allows():
    # Ten 1 GB watched episodes -> budget 14 GB. Five 2.5 GB wants = 12.5 GB, all
    # affordable. The single-pass version stopped early because it only ever considered
    # the surplus in hand, never the rest of the pool.
    plan = plan_recycle(
        watched_owned=[_w(1, i, 1.0, f"2026-01-{i:02d}") for i in range(1, 12)],
        wanted=[_want(2, e, 2.5) for e in range(1, 6)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert len(plan["acquire"]) == 5


# ── guard 5: keep-tagged ──────────────────────────────────────────────────────
def test_keep_tagged_series_is_never_recycled():
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 50.0, "2020-01-01")],
        wanted=[_want(1, 2, 1.0)],
        free_gb=0.0, floor_gb=500.0, keep_tagged=True)
    assert plan["recycle"] == [] and plan["acquire"] == []
    assert "keep-tagged" in plan["reason"]


# ── guard 2: household-watched only ───────────────────────────────────────────
def test_unwatched_episodes_are_never_recycled():
    # No watched_at == not watched by the household. Even huge, even old.
    plan = plan_recycle(
        watched_owned=[{"season": 1, "episode": 1, "size_gb": 99.0, "watched_at": None}],
        wanted=[_want(1, 2, 1.0)],
        free_gb=0.0, floor_gb=500.0)
    assert plan["recycle"] == [] and plan["acquire"] == []


# ── guard 3: rewatch buffer ───────────────────────────────────────────────────
def test_rewatch_buffer_holds_the_most_recent_watches():
    # Three watched, buffer of 2 -> only the OLDEST is a candidate.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 5.0, "2026-01-01"), _w(1, 2, 5.0, "2026-02-01"),
                       _w(1, 3, 5.0, "2026-03-01")],
        wanted=[_want(1, 4, 4.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=2)
    assert [e["episode"] for e in plan["recycle"]] == [1]


def test_buffer_larger_than_the_pool_refuses_everything():
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 5.0, "2026-01-01"), _w(1, 2, 5.0, "2026-02-01")],
        wanted=[_want(1, 3, 1.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=5)
    assert plan["acquire"] == []
    assert "rewatch buffer" in plan["reason"]


# ── guard 4: size-matched ─────────────────────────────────────────────────────
def test_partial_funding_deletes_nothing():
    # 2 GB of recyclable against a 20 GB want: NOTHING may be deleted, because a
    # deletion that does not complete an acquisition is a pure loss.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 1.0, "2026-01-01"), _w(1, 2, 1.0, "2026-02-01"),
                       _w(1, 3, 1.0, "2026-03-01")],
        wanted=[_want(1, 9, 20.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1)
    assert plan["recycle"] == [], "must not delete for an acquisition it cannot fund"
    assert plan["acquire"] == []


def test_stops_at_the_first_unfundable_want_rather_than_reordering():
    # E7 fundable, E8 not. E9 would BE fundable but taking it would silently reorder
    # what the household gets next -- the prefetch order is the priority order.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 3.0, "2026-01-01"), _w(1, 2, 3.0, "2026-02-01"),
                       _w(1, 3, 3.0, "2026-03-01")],
        wanted=[_want(1, 7, 3.0), _want(1, 8, 50.0), _want(1, 9, 1.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1)
    assert [e["episode"] for e in plan["acquire"]] == [7]


def test_min_ratio_can_demand_a_surplus():
    # tolerance 0 -> strict parity: the acquisition may not cost more than it frees.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 2.0, "2026-01-01"), _w(1, 2, 2.0, "2026-02-01"),
                       _w(1, 3, 2.0, "2026-03-01")],
        wanted=[_want(1, 7, 2.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=0, size_tolerance=0.0)
    assert plan["freed_gb"] >= plan["cost_gb"]


# ── the 40% tolerance band ──────────────────────────────────────────
def test_tolerance_allows_a_slightly_larger_next_episode():
    # Recycle 2.0 GB, want 2.7 GB. Strict parity would refuse; a 40% band allows it,
    # which is the whole point -- episode sizes vary and a stalled rotation is what
    # leaves the library stuck on pilots.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 2.0, "2026-01-01"), _w(1, 2, 2.0, "2026-02-01")],
        wanted=[_want(1, 7, 2.7)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert [e["episode"] for e in plan["acquire"]] == [7]


def test_tolerance_still_refuses_a_much_larger_episode():
    # 2.0 GB recycled cannot fund 8 GB even with the band -- that is a 4x upgrade,
    # not episode-to-episode variance.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 2.0, "2026-01-01"), _w(1, 2, 2.0, "2026-02-01")],
        wanted=[_want(1, 7, 8.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert plan["acquire"] == [] and plan["recycle"] == []


# ── resolution tiering ───────────────────────────────────────────
def test_steps_down_to_the_tier_the_recycled_space_affords():
    # Recycling ONE 1.5 GB 720p episode must not fund a 9 GB 2160p replacement.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 1.5, "2026-01-01"), _w(1, 2, 1.5, "2026-02-01")],
        wanted=[{"season": 1, "episode": 7,
                 "est_gb_by_tier": {2160: 9.0, 1080: 3.5, 720: 1.4}}],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert len(plan["acquire"]) == 1
    assert plan["acquire"][0]["tier"] == 720, "must not upgrade quality while claiming net-neutral"
    assert plan["acquire"][0]["est_gb"] == 1.4


def test_takes_the_best_tier_the_whole_pool_affords():
    # Four 3 GB episodes -> budget 16.8 GB. 2160p at 20 does not fit; 1080p does.
    # Under the old single-pass loop this passed for the WRONG reason (it stopped after
    # one episode); now the full pool is priced before any tier is chosen.
    plan = plan_recycle(
        watched_owned=[_w(1, i, 3.0, f"2026-0{i}-01") for i in range(1, 5)],
        wanted=[{"season": 1, "episode": 9,
                 "est_gb_by_tier": {2160: 20.0, 1080: 3.5, 720: 1.4}}],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert plan["acquire"][0]["tier"] == 1080


def test_a_bigger_pool_buys_a_better_tier():
    # The same want, a much larger pool -> 2160p becomes affordable. This is the
    # behaviour the single-pass loop could never reach.
    plan = plan_recycle(
        watched_owned=[_w(1, i, 3.0, f"2026-01-{i:02d}") for i in range(1, 9)],
        wanted=[{"season": 1, "episode": 9,
                 "est_gb_by_tier": {2160: 20.0, 1080: 3.5, 720: 1.4}}],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1, size_tolerance=0.4)
    assert plan["acquire"][0]["tier"] == 2160


def test_tier_is_reported_so_the_caller_can_request_it():
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 4.0, "2026-01-01"), _w(1, 2, 4.0, "2026-02-01")],
        wanted=[{"season": 1, "episode": 7, "est_gb_by_tier": {1080: 3.0, 720: 1.2}}],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=1)
    assert "1080p" in plan["reason"]
    # The caller MUST request at this tier or the arithmetic is void.
    assert plan["acquire"][0]["est_gb"] == 3.0


# ── the pressure precondition ─────────────────────────────────────────────────
def test_no_recycling_when_not_under_pressure():
    # Above the floor the NORMAL gate already allows the acquisition. Recycling here
    # would delete something for nothing.
    plan = plan_recycle(
        watched_owned=[_w(1, 1, 5.0, "2026-01-01")],
        wanted=[_want(1, 2, 1.0)],
        free_gb=900.0, floor_gb=500.0, rewatch_buffer=0)
    assert plan["recycle"] == [] and plan["acquire"] == []
    assert "not under pressure" in plan["reason"]


def test_zero_size_rows_cannot_fund_anything():
    # A row with no size would otherwise "fund" an acquisition for free.
    plan = plan_recycle(
        watched_owned=[{"season": 1, "episode": 1, "size_gb": 0.0, "watched_at": "2026-01-01"},
                       {"season": 1, "episode": 2, "size_gb": None, "watched_at": "2026-02-01"}],
        wanted=[_want(1, 3, 1.0)],
        free_gb=0.0, floor_gb=500.0, rewatch_buffer=0)
    assert plan["acquire"] == []

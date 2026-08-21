"""space.jit_backoff — send a repeatedly-fruitless series to the back of the queue.

Built from a measured 298-minute daemon window (2026-08-21): 146 step-downs
produced 14 grabs at ~112s each, and 116 of those 146 went to FIVE series that
grabbed nothing. Each ended "queued for retry next run" and had its flags reset,
so the identical three hours repeated every night.

The load-bearing property under test is that this ORDERS and never EXCLUDES.
`legacy_regrab` benches for 14 days; GLD-SON-02 records that failing at
814-of-881 scale because a disabled indexer is indistinguishable from "no release
exists". A demoted series must therefore always come due again, and any grab must
clear the demotion with no operator action."""
from __future__ import annotations

from scripts.managers.machine_learning.space import jit_backoff as jb

# The five that grabbed nothing, and the three that did.
_BARREN = ["johnny", "aot", "lasso", "abbott", "tmnt"]
_GRABBED = ["bbt", "blue", "snowfall"]


def _after_one_window() -> dict:
    led: dict = {}
    for s in _BARREN:
        led = jb.record_exhaustion(led, s, run_seq=1)
    for s in _GRABBED:
        led = jb.record_success(led, s)
    return led


# ── the queue ────────────────────────────────────────────────────────────────

def test_grabbers_lead_and_barren_series_go_to_the_back():
    led = _after_one_window()
    now, _ = jb.order_series(_BARREN + _GRABBED, led, run_seq=2)
    assert set(now[:3]) == set(_GRABBED)
    assert set(now[3:]) == set(_BARREN)


def test_incoming_order_decides_ties():
    """Demotion is an ADDITIONAL axis, not a replacement — whatever next-up
    priority produced the caller's list still orders series with equal standing."""
    now, _ = jb.order_series(["c", "a", "b"], {}, run_seq=1)
    assert now == ["c", "a", "b"]


def test_an_unseen_series_sorts_with_the_fresh_not_the_demoted():
    led = _after_one_window()
    assert jb.exhaustions(led, "brand_new") == 0
    now, _ = jb.order_series(["johnny", "brand_new"], led, run_seq=999)
    assert now[0] == "brand_new"


# ── ordering, never exclusion (the whole design) ─────────────────────────────

def test_a_demoted_series_always_comes_due_again():
    """The property that makes this a QUEUE rather than a bench. GLD-SON-02: a
    disabled or rate-limited indexer looks exactly like 'no release exists', so
    nothing may become permanently ineligible on that evidence."""
    led: dict = {}
    for run in range(1, 40):
        if jb.due(led, "johnny", run_seq=run):
            led = jb.record_exhaustion(led, "johnny", run_seq=run)
    # deeply demoted, and still attempted within the cap
    assert jb.exhaustions(led, "johnny") >= jb.DEMOTE_AFTER
    horizon = range(40, 40 + jb.ATTEMPT_EVERY_N_RUNS_CAP + 1)
    assert any(jb.due(led, "johnny", run_seq=r) for r in horizon)


def test_the_soft_cooldown_thins_attempts_without_stopping_them():
    led: dict = {}
    attempts = 0
    for run in range(1, 21):
        if jb.due(led, "johnny", run_seq=run):
            attempts += 1
            led = jb.record_exhaustion(led, "johnny", run_seq=run)
    assert attempts < 20            # thinned
    assert attempts > 0             # never silenced


def test_one_grab_clears_the_demotion_with_no_operator_action():
    """Self-healing is the reason a bench is not needed: re-enable an indexer and
    the first successful grab restores full priority on its own."""
    led = _after_one_window()
    assert jb.exhaustions(led, "johnny") > 0
    led = jb.record_success(led, "johnny")
    assert jb.exhaustions(led, "johnny") == 0
    assert jb.due(led, "johnny", run_seq=1) is True


def test_record_functions_do_not_mutate_the_caller_s_ledger():
    """They return a COPY so a crash mid-pass cannot leave a half-written ledger."""
    led = {"a": {"exhaustions": 1, "last_run": 1}}
    before = dict(led["a"])
    jb.record_exhaustion(led, "a", run_seq=2)
    jb.record_success(led, "a")
    assert led["a"] == before


# ── the policy is baked in, not configurable ─────────────────────────────────

def test_no_config_surface_exists():
    """A knob here would be a knob to re-enable the defect, and an operator who
    set it wrong would see no error — only a daemon quietly taking three hours
    again. Safe to hard-code because both limits degrade ORDER, never eligibility."""
    assert not hasattr(jb, "config_for")
    assert not hasattr(jb, "DEFAULTS")
    assert jb.DEMOTE_AFTER >= 1
    assert jb.MAX_CONSECUTIVE_MISSES >= 1
    assert jb.ATTEMPT_EVERY_N_RUNS_CAP >= 1


# ── keys ─────────────────────────────────────────────────────────────────────

def test_the_three_jit_cache_keys_never_collide():
    """`jit/failed_upgrades` answers 'is this EPISODE still owed a grab?' and
    `jit/backoff` answers 'how often has this SERIES come up empty?'. Merging them
    — or clearing the second alongside the first — wipes the counters every run
    and restores the loop this module exists to break."""
    keys = {
        jb.ledger_key("standard"),
        jb.run_seq_key("standard"),
        "sonarr/standard/jit/failed_upgrades",
    }
    assert len(keys) == 3


def test_keys_are_per_instance():
    assert jb.ledger_key("a") != jb.ledger_key("b")
    assert jb.run_seq_key("a") != jb.run_seq_key("b")


# ── housekeeping and hostile input ───────────────────────────────────────────

def test_prune_drops_only_departed_series():
    led = _after_one_window()
    kept = jb.prune(led, keep_sids=["johnny", "aot"])
    assert set(kept) == {"johnny", "aot"}


def test_summarise_reports_the_demotion():
    """A demotion nobody can see is the defect this session hit repeatedly
    (GLD-ACQS-20, GLD-SON-24): an outcome counted and omitted from the line an
    operator actually reads."""
    s = jb.summarise(_after_one_window())
    assert s["demoted"] == len(_BARREN)
    assert s["worst_n"] >= 1
    assert jb.summarise({})["demoted"] == 0
    assert jb.summarise(None)["demoted"] == 0


def test_garbage_input_never_raises():
    for bad in (None, {}, {"x": "not-a-dict"}, {"x": {"exhaustions": "n"}},
                {"x": {"exhaustions": -5}}, {"x": {"last_run": "soon"}}):
        assert jb.exhaustions(bad, "x") >= 0
        assert isinstance(jb.due(bad, "x", run_seq=1), bool)
        now, later = jb.order_series(["x"], bad, run_seq=1)
        assert len(now) + len(later) == 1
        assert isinstance(jb.summarise(bad), dict)
        assert isinstance(jb.prune(bad, keep_sids=["x"]), dict)


def test_a_missing_last_run_is_treated_as_due():
    """An entry written by an older build, or a partial write, must not strand a
    series: unknown scheduling information means attempt it."""
    led = {"x": {"exhaustions": 5}}
    assert jb.due(led, "x", run_seq=1) is True

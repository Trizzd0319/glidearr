"""The recalibrated likelihood curve: graded engagement floors, 1080p default, 4K reserved for
regular rewatches, affinity alone NEVER reaching 4K, and a sticky 720p low end."""
from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    movie_universe_credits,
    profile_id_for_likelihood,
    resolution_cap_for_likelihood,
    series_universe_credits,
    universe_credit,
    watch_likelihood,
)


def _L(**row):
    return watch_likelihood(row, config=None)   # config=None → recalibrated _DEFAULTS


def test_engagement_floors_are_graded_by_watch_count():
    assert _L(watch_count=1) == 50          # one watch
    assert _L(watch_count=2) == 64          # +rewatch_step
    assert _L(watch_count=3) == 78
    assert _L(watch_count=4) == 90          # capped at rewatch_floor
    assert _L(watch_count=12) == 90         # stays capped


def test_single_watch_is_1080p_not_4k():
    one = _L(watch_count=1)
    assert resolution_cap_for_likelihood(one, config=None) == 1080
    assert profile_id_for_likelihood(one, config=None) == 7     # HD Bluray + WEB (1080p), not a 4K id


def test_twice_watched_still_1080p():
    assert resolution_cap_for_likelihood(_L(watch_count=2), config=None) == 1080


def test_regular_rewatch_earns_4k():
    assert resolution_cap_for_likelihood(_L(watch_count=3), config=None) == 2160
    assert resolution_cap_for_likelihood(_L(watch_count=4), config=None) == 2160
    assert profile_id_for_likelihood(_L(watch_count=4), config=None) == 10   # UHD Bluray + WEB (top 4K)


def test_affinity_alone_never_reaches_4k():
    # A never-watched but highly-liked title tops out at 1080p — taste must not buy 4K.
    aff = _L(watch_count=0, watchability_score=80)
    assert aff == 75                                            # capped at affinity_cap
    assert resolution_cap_for_likelihood(aff, config=None) == 1080
    assert profile_id_for_likelihood(aff, config=None) == 8     # Remux + WEB 1080p — the ceiling for taste


def test_untouched_low_stays_720p():
    low = _L(watch_count=0, watchability_score=0)               # affinity = untouched_base (12)
    assert resolution_cap_for_likelihood(low, config=None) == 720
    assert profile_id_for_likelihood(low, config=None) == 3     # HD-720p — the sticky floor


def test_started_and_abandoned_below_default():
    assert _L(percent_complete=50) == 40                        # started_floor
    assert _L(percent_complete=10, watchability_score=80) == 25  # abandoned: CAPPED at the ceiling
    assert _L(percent_complete=10) == 12                         # low affinity → stays at affinity


# ── universe / franchise propagation ──────────────────────────────────────────
def test_universe_credit_is_zero_for_cold_or_tiny_groups():
    assert universe_credit(0, 34) == 0.0           # no rewatched siblings
    assert universe_credit(3, 1) == 0.0            # single-member "group"


def test_universe_credit_hot_universe_vs_loose_megagroup():
    hot = universe_credit(10, 34)                  # MCU: heat ~0.29 → ~full credit
    loose = universe_credit(4, 167)                # catch-all bucket: heat ~0.024 → self-diluted
    assert hot >= 1.8                              # near the cap (2.0)
    assert loose < 0.3 and loose < hot             # a loose mega-group can't promote its siblings


def test_universe_credit_decays_with_recency():
    fresh = universe_credit(10, 34, days_since_watch=0)
    stale = universe_credit(10, 34, days_since_watch=30)   # one half-life
    assert abs(stale - fresh / 2) < 0.05


def test_single_watch_plus_hot_universe_elevates_to_4k():
    assert resolution_cap_for_likelihood(_L(watch_count=1), config=None) == 1080      # one watch alone
    boosted = watch_likelihood({"watch_count": 1, "universe_credit": 2.0}, config=None)
    assert boosted == 78                                                               # effective wc 3
    assert resolution_cap_for_likelihood(boosted, config=None) == 2160                 # → 4K immediately


def test_universe_credit_alone_wont_4k_an_unwatched_title():
    cold = watch_likelihood({"watch_count": 0, "universe_credit": 2.0}, config=None)
    assert resolution_cap_for_likelihood(cold, config=None) == 1080                    # elevated, not 4K


def test_series_universe_credits_elevates_hot_saga_members():
    fran = {1: "sw", 2: "sw", 3: "sw", 9: "solo"}          # 'sw' = a 3-series saga; 'solo' single-member
    stats = {1: {"watch_count": 5, "days_since": 0}, 2: {"watch_count": 3, "days_since": 10},
             3: {"watch_count": 1, "days_since": 5}, 9: {"watch_count": 4, "days_since": 0}}
    out = series_universe_credits(fran, stats, config=None)
    assert out.get(3, 0) > 0                                # the single-watch SIBLING is elevated
    assert out[1] == out[2] == out[3]                       # group-level credit, every member same
    assert 9 not in out                                    # single-member group → no credit


def test_series_universe_credits_cold_group_is_empty():
    fran = {1: "x", 2: "x"}
    stats = {1: {"watch_count": 1}, 2: {"watch_count": 0}}  # no rewatched sibling → no heat
    assert series_universe_credits(fran, stats, config=None) == {}


# ── movie universe propagation (pipe-separated multi-membership) ───────────────
def test_movie_universe_credits_elevates_hot_saga_member():
    uni = {1: "mcu", 2: "mcu", 3: "mcu"}                      # a 3-film saga
    stats = {1: {"watch_count": 5, "days_since": 0}, 2: {"watch_count": 3, "days_since": 5},
             3: {"watch_count": 1, "days_since": 2}}          # film 3 watched once
    out = movie_universe_credits(uni, stats, config=None)
    assert out.get(3, 0) > 0                                  # the single-watch SIBLING is elevated
    assert out[1] == out[2] == out[3]                         # group-level credit, every member same


def test_movie_universe_credits_multi_universe_keeps_hottest():
    # Film 1 sits in a TIGHT hot saga ('mcu', ~full credit) AND a LOOSE bucket ('bucket', heat self-
    # diluted to ~nothing). It must keep the HOTTER (max) MCU credit, not the diluted bucket value.
    uni = {1: "mcu|bucket", 2: "mcu", 3: "mcu"}
    uni.update({100 + i: "bucket" for i in range(60)})       # 61-member loose bucket incl. film 1
    stats = {1: {"watch_count": 5, "days_since": 0}, 2: {"watch_count": 3, "days_since": 0},
             3: {"watch_count": 1, "days_since": 0}}
    out = movie_universe_credits(uni, stats, config=None)
    assert out[2] >= 1.8                                     # a pure-MCU member sits near the cap
    assert out[1] == out[2]                                  # film 1 takes the hotter MCU credit (max)
    assert out[100] < 0.3                                    # bucket-only members get the diluted value


def test_movie_universe_credits_loose_megagroup_self_dilutes():
    # A huge catch-all "bucket" with one rewatch can't promote its 200 cold siblings.
    uni = {i: "bucket" for i in range(200)}
    stats = {0: {"watch_count": 5, "days_since": 0}}          # exactly one rewatched film
    out = movie_universe_credits(uni, stats, config=None)
    assert all(v < 0.3 for v in out.values())                # heat 1/200 → negligible


def test_movie_universe_credits_cold_group_is_empty():
    assert movie_universe_credits({1: "x", 2: "x"},
                                  {1: {"watch_count": 1}, 2: {"watch_count": 1}}, config=None) == {}
    assert movie_universe_credits({1: "solo"}, {1: {"watch_count": 9}}, config=None) == {}  # single-member


def test_universe_credit_future_date_does_not_exceed_cap():
    # A future-dated last_watched (clock skew / bad metadata) -> negative days_since must NOT overshoot
    # the cap via a >1 decay; it's clamped to "just watched".
    assert universe_credit(10, 34, days_since_watch=-30) <= 2.0
    assert universe_credit(3, 3, days_since_watch=-30) == universe_credit(3, 3, days_since_watch=0)


def test_movie_universe_credits_dedupes_repeated_label():
    # A repeated label in one film's pipe string ("g|g") must not double-count it in group_size /
    # rewatched-sibling count — identical to the correctly-deduped membership.
    stats = {1: {"watch_count": 5, "days_since": 0}}
    dup   = movie_universe_credits({1: "g|g", 2: "g", 3: "g", 4: "g"}, stats, config=None)
    clean = movie_universe_credits({1: "g",   2: "g", 3: "g", 4: "g"}, stats, config=None)
    assert dup == clean and dup[1] > 0


def test_movie_universe_credits_drops_placeholder_groups():
    # The bare "universe" / "franchise" placeholders (keep_policy stamps universe_name="universe" for
    # bare-universe films) must NOT fuse unrelated movies into one bogus saga and lend false credit.
    from scripts.managers.machine_learning.playlists.models import PLACEHOLDER_AFFINITY
    uni = {1: "universe", 2: "universe", 3: "franchise",
           4: "Star Wars Collection|universe", 5: "Star Wars Collection"}
    stats = {1: {"watch_count": 5, "days_since": 0}, 4: {"watch_count": 5, "days_since": 0}}
    out = movie_universe_credits(uni, stats, config=None, drop_labels=PLACEHOLDER_AFFINITY)
    assert 1 not in out and 2 not in out and 3 not in out   # placeholder-only -> no group, no credit
    assert out.get(4, 0) > 0 and out[4] == out.get(5)        # the REAL collection survives, placeholder dropped

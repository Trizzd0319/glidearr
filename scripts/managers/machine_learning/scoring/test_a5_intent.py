"""scoring/test_a5_intent.py — the Group-A5 explicit-intent signal.
================================================================================
Covers the four things A5 has to get right, plus the shield's expiry:

  1. INERT at cap 0 — the default. Both scorers must return the identical score AND an
     otherwise-identical breakdown, so shipping the signal disabled is a no-op. (The
     golden-corpus fixture in test_score_golden is the byte-identity proof for the whole
     500-case corpus; this is the targeted version.)
  2. SOURCE grading — a watchlist outranks a recommendation outranks a seasonal chart,
     reusing services/acquisition/scorer._SOURCE_SCORE's ranking rather than a second one.
  3. MEMBER grading — more household members asking = more credit, and (critically for
     THIS household, where 258 of 259 titles have exactly one watchlister) a solo entry
     still scores a real, non-degenerate value.
  4. STALENESS — a 2020 Trakt listing decays; an UNDATED Plex entry does not, because
     the union carries no per-item timestamp and inventing one would be a fabrication.
  5. SHIELD EXPIRY — the hold releases once the member who asked goes dormant.
  6. THE DECAY KNOBS — ``scoring.watchlist_intent.half_life_days`` / ``stale_floor`` were
     documentation-only until they were threaded through ``resolve_intent_inputs``: they
     were pinned in config.json, written up in the onboarding schema, and read by nothing.
     Now they drive the decay, still defaulting to the module constants so the pinned
     365.0 / 0.25 reproduce every previous score exactly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.machine_learning.scoring._shared import (
    INTENT_DORMANCY_DAYS,
    intent_hold_active,
    intent_recency_factor,
    resolve_intent_inputs,
    watchlist_intent_score,
)
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie
from scripts.managers.machine_learning.scoring.show_scorer import score_show

NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def _entry(sources=("plex_watchlist",), members=("trizzd",), dated=None, anchor=None):
    return {"sources": tuple(sources), "members": tuple(members),
            "dated": dict(dated or {}), "anchor": anchor}


def _movie(**kw):
    base = dict(movie={"tmdbId": 1, "genres": ["Action"]}, completion_pct=0.0,
                completion_threshold=0.9, collection_members={}, watched_tmdb_ids=set(),
                genre_affinity={}, credits={})
    base.update(kw)
    return base


# ── 1. inert at cap 0 ─────────────────────────────────────────────────────────

def test_a5_is_inert_at_the_default_cap_for_both_scorers():
    """cap 0.0 (the scorer default) → identical score AND an A5_intent of exactly 0.0,
    even when a fat intent entry is passed. Shipping it disabled changes nothing."""
    entry = _entry(members=("a", "b", "c", "d"))
    for kwargs in (_movie(), _movie(intent_entry=entry)):
        plain, bd = score_movie(**kwargs, return_breakdown=True)
        assert bd["A5_intent"] == 0.0
        assert plain == score_movie(**_movie())

    s_plain, s_bd = score_show({"genres": ["Drama"]}, intent_entry=entry,
                               return_breakdown=True)
    assert s_bd["A5_intent"] == 0.0
    assert s_plain == score_show({"genres": ["Drama"]})


def test_a5_key_is_always_present_so_the_breakdown_vocabulary_is_stable():
    """Every consumer keyed on the group vocabulary (the persisted breakdown schema, the
    gems taste filter, the ML snapshot columns) must see A5 whether or not it fired."""
    _, bd = score_movie(**_movie(), return_breakdown=True)
    assert "A5_intent" in bd
    _, sbd = score_show({"genres": ["Drama"]}, return_breakdown=True)
    assert "A5_intent" in sbd


def test_a5_raises_the_score_by_exactly_its_own_contribution():
    off = score_movie(**_movie())
    on, bd = score_movie(**_movie(intent_entry=_entry(), intent_cap=8.0, intent_now=NOW),
                         return_breakdown=True)
    assert bd["A5_intent"] == 4.8                       # 8.0 * 1.00 * 0.60 (solo, undated)
    assert on == off + round(bd["A5_intent"])           # the scorer rounds the total


# ── 2. source grading ─────────────────────────────────────────────────────────

def test_source_ladder_matches_the_acquisition_scorer_ranking():
    """watchlist (100) > recommendations (65) > seasonal (55), reused not reinvented."""
    wl = watchlist_intent_score(_entry(sources=("plex_watchlist",)), 8.0, now=NOW)
    rec = watchlist_intent_score(_entry(sources=("trakt_recommendations",)), 8.0, now=NOW)
    seas = watchlist_intent_score(_entry(sources=("mal_seasonal",)), 8.0, now=NOW)
    assert wl > rec > seas > 0
    assert rec == round(wl * 0.65, 3)
    assert seas == round(wl * 0.55, 3)


def test_trakt_and_plex_and_mal_watchlists_are_the_same_top_tier():
    args = dict(members=("trizzd",))
    a = watchlist_intent_score(_entry(sources=("plex_watchlist",), **args), 8.0, now=NOW)
    b = watchlist_intent_score(_entry(sources=("trakt_watchlist",), **args), 8.0, now=NOW)
    c = watchlist_intent_score(_entry(sources=("mal_plantowatch",), **args), 8.0, now=NOW)
    assert a == b == c


def test_best_source_wins_so_a_stale_trakt_copy_cannot_drag_down_a_live_plex_one():
    both = _entry(sources=("plex_watchlist", "trakt_watchlist"),
                  dated={"trakt_watchlist": "2020-05-01T00:00:00Z"})
    plex_only = _entry(sources=("plex_watchlist",))
    assert watchlist_intent_score(both, 8.0, now=NOW) == \
        watchlist_intent_score(plex_only, 8.0, now=NOW)


def test_an_unknown_feed_earns_nothing_rather_than_a_guessed_tier():
    assert watchlist_intent_score(_entry(sources=("some_future_feed",)), 8.0, now=NOW) == 0.0


# ── 3. member grading ─────────────────────────────────────────────────────────

def test_member_count_grades_without_degenerating_on_a_solo_household():
    """THIS household is 258 solo / 1 pair today. A solo entry must still be worth a real
    number (not a floor sentinel), and a second member must visibly add."""
    solo = watchlist_intent_score(_entry(members=("trizzd",)), 8.0, now=NOW)
    pair = watchlist_intent_score(_entry(members=("trizzd", "aiden")), 8.0, now=NOW)
    trio = watchlist_intent_score(_entry(members=("a", "b", "c")), 8.0, now=NOW)
    quad = watchlist_intent_score(_entry(members=("a", "b", "c", "d")), 8.0, now=NOW)
    assert solo == 4.8 and pair == 5.76 and trio == 6.72 and quad == 7.68
    assert watchlist_intent_score(_entry(members=("a", "b", "c", "d", "e")), 8.0, now=NOW) == 8.0
    assert watchlist_intent_score(_entry(members=tuple("abcdefg")), 8.0, now=NOW) == 8.0
    assert solo / 8.0 == 0.60                             # a real value, not a floor


def test_member_ladder_mirrors_the_next_watch_ranker():
    """One household-intent opinion, two consumers — 60/+12/100 either way."""
    from scripts.managers.machine_learning.next_watch import watchlist_intent
    union = [{"title": "X", "type": "movie", "ids": {"tmdb": 1}, "watchlisted_by": ["a", "b"]}]
    assert watchlist_intent(union)["1"]["intent"] == 72.0
    assert watchlist_intent_score(_entry(members=("a", "b")), 100.0, now=NOW) == 72.0


# ── 4. staleness ──────────────────────────────────────────────────────────────

def test_stale_trakt_intent_decays_and_fresh_intent_does_not():
    fresh = _entry(sources=("trakt_watchlist",),
                   dated={"trakt_watchlist": (NOW - timedelta(days=3)).isoformat()})
    old = _entry(sources=("trakt_watchlist",), dated={"trakt_watchlist": "2020-05-01T09:59:02.000Z"})
    assert watchlist_intent_score(fresh, 8.0, now=NOW) > watchlist_intent_score(old, 8.0, now=NOW)
    # This household's ENTIRE Trakt watchlist is May 2020 → ~6.2 years → on the floor.
    assert watchlist_intent_score(old, 8.0, now=NOW) == 1.2       # 8 * 0.25 * 0.60
    assert watchlist_intent_score(fresh, 8.0, now=NOW) > 4.7


def test_stale_mal_plan_to_watch_lands_on_the_floor():
    """THIS household's whole plan-to-watch list is 2023-05 to 2023-07 — three-plus years,
    ~3.3 half-lives — so every MAL-only entry decays to ``stale_floor`` and is worth
    8 * 1.00 * 0.25 * 0.60 = 1.2. That is the CORRECT answer, not a number to tune away
    from: a list nobody has touched since 2023 is honest evidence of *stale* intent, and
    the floor is what keeps it above "no intent at all"."""
    mal = _entry(sources=("mal_plantowatch",),
                 dated={"mal_plantowatch": "2023-05-17T07:27:34+00:00"})
    assert watchlist_intent_score(mal, 8.0, now=NOW) == 1.2
    assert intent_recency_factor("2023-05-17T07:27:34+00:00", NOW) == 0.25


def test_mal_is_graded_at_full_source_strength_before_it_decays():
    """``mal_plantowatch`` sits at 1.00 in INTENT_SOURCE_STRENGTH — the same rung as a
    watchlist, because "plan to watch" IS a watchlist. Only the staleness term should be
    holding it down."""
    fresh_mal = _entry(sources=("mal_plantowatch",),
                       dated={"mal_plantowatch": (NOW - timedelta(days=1)).isoformat()})
    fresh_plex = _entry(sources=("plex_watchlist",))
    assert abs(watchlist_intent_score(fresh_mal, 8.0, now=NOW)
               - watchlist_intent_score(fresh_plex, 8.0, now=NOW)) < 0.02


def test_a_live_plex_entry_outranks_the_same_titles_stale_mal_one():
    """Dragon Ball Kai is on BOTH feeds here. Best-source-wins is applied AFTER per-source
    decay, so the fresh Plex listing speaks for it and the 2023 MAL copy does not drag it
    down to the floor."""
    both = _entry(sources=("plex_watchlist", "mal_plantowatch"),
                  dated={"mal_plantowatch": "2023-07-05T18:43:58+00:00"})
    assert watchlist_intent_score(both, 8.0, now=NOW) == \
        watchlist_intent_score(_entry(sources=("plex_watchlist",)), 8.0, now=NOW)


def test_a_one_year_old_listing_is_worth_half_a_fresh_one():
    year = intent_recency_factor((NOW - timedelta(days=365)).isoformat(), NOW)
    assert abs(year - 0.5) < 1e-6


def test_undated_plex_intent_does_not_decay():
    """The union has no per-item timestamp and the rolling snapshots retain under a day —
    a derived 'first seen' would date every title to yesterday. So: no decay, ever."""
    assert intent_recency_factor(None, NOW) == 1.0
    assert intent_recency_factor("", NOW) == 1.0
    assert intent_recency_factor("not-a-date", NOW) == 1.0


def test_a_future_listing_reads_as_fresh_not_as_a_bonus():
    assert intent_recency_factor((NOW + timedelta(days=400)).isoformat(), NOW) == 1.0


def test_decay_is_floored_so_ancient_intent_still_beats_none():
    ancient = intent_recency_factor("1999-01-01T00:00:00Z", NOW)
    assert ancient == 0.25
    assert watchlist_intent_score(_entry(sources=("trakt_watchlist",),
                                         dated={"trakt_watchlist": "1999-01-01T00:00:00Z"}),
                                  8.0, now=NOW) > 0.0


# ── 5. the shield's expiry ────────────────────────────────────────────────────

def test_shield_is_live_while_the_watchlister_is_active():
    e = _entry(anchor=(NOW - timedelta(days=2)).isoformat())
    assert intent_hold_active(e, NOW) is True


def test_shield_expires_on_watchlister_dormancy():
    """The whole point: a title watchlisted once and forgotten must NOT hold disk forever."""
    just_inside = _entry(anchor=(NOW - timedelta(days=INTENT_DORMANCY_DAYS - 1)).isoformat())
    just_outside = _entry(anchor=(NOW - timedelta(days=INTENT_DORMANCY_DAYS + 1)).isoformat())
    assert intent_hold_active(just_inside, NOW) is True
    assert intent_hold_active(just_outside, NOW) is False


def test_shield_expiry_anchors_on_the_MEMBER_not_on_the_listing_age():
    """A 2020 listing by somebody who watched last night is live intent; a listing from
    last week by a dormant account is not. Same rule saga_retention already uses."""
    old_listing_active_member = _entry(sources=("trakt_watchlist",),
                                       dated={"trakt_watchlist": "2020-05-01T00:00:00Z"},
                                       anchor=(NOW - timedelta(days=1)).isoformat())
    new_listing_dormant_member = _entry(dated={}, anchor=(NOW - timedelta(days=400)).isoformat())
    assert intent_hold_active(old_listing_active_member, NOW) is True
    assert intent_hold_active(new_listing_dormant_member, NOW) is False


def test_shield_fails_closed_on_an_unresolvable_anchor():
    """Holding on unknown provenance is exactly the 'held forever' failure this guards
    against — and the title keeps its A5 POINTS either way."""
    assert intent_hold_active(_entry(anchor=None), NOW) is False
    assert intent_hold_active(None, NOW) is False
    assert watchlist_intent_score(_entry(anchor=None), 8.0, now=NOW) > 0.0


# ── config gate ───────────────────────────────────────────────────────────────

def test_resolve_intent_inputs_forces_cap_zero_when_disabled_or_empty():
    idx = {1: _entry()}
    assert resolve_intent_inputs({}, idx)[1] == 8.0
    assert resolve_intent_inputs({"scoring": {"watchlist_intent": {"enabled": False}}}, idx)[1] == 0.0
    assert resolve_intent_inputs({}, {})[1] == 0.0                # nothing watchlisted → inert
    assert resolve_intent_inputs({}, None)[1] == 0.0
    assert resolve_intent_inputs(
        {"scoring": {"watchlist_intent": {"cap": 3.0}}}, idx)[1] == 3.0
    assert resolve_intent_inputs(
        {"scoring": {"watchlist_intent": {"cap": "junk"}}}, idx)[1] == 8.0


# ── the decay knobs: config-driven, constant-defaulted ────────────────────────
# ``half_life_days`` and ``stale_floor`` were pinned in config.json and written up in the
# onboarding schema from the day A5 shipped, and NOTHING READ THEM: resolve_intent_inputs
# returned only (index, cap) and the decay ran off the module constants, which happen to
# hold the same 365.0 / 0.25. An operator editing either got silence. These pin the wiring
# AND the thing that makes the wiring safe — that at the pinned values nothing moves.

def test_resolve_intent_inputs_returns_the_decay_knobs_from_config():
    idx = {1: _entry()}
    _, _, hl, floor = resolve_intent_inputs(
        {"scoring": {"watchlist_intent": {"half_life_days": 30.0, "stale_floor": 0.1}}}, idx)
    assert (hl, floor) == (30.0, 0.1)


def test_the_decay_knobs_default_to_the_module_constants():
    """An absent key, a blank config and a garbage value all reproduce today's numbers —
    a typo in a decay knob must not be able to change scores OR fail a pass."""
    from scripts.managers.machine_learning.scoring._shared import (
        INTENT_HALF_LIFE_DAYS, INTENT_STALE_FLOOR,
    )
    idx = {1: _entry()}
    for cfg in ({}, {"scoring": {}}, {"scoring": {"watchlist_intent": {}}},
                {"scoring": {"watchlist_intent": {"half_life_days": "soon",
                                                  "stale_floor": None}}}):
        _, _, hl, floor = resolve_intent_inputs(cfg, idx)
        assert (hl, floor) == (INTENT_HALF_LIFE_DAYS, INTENT_STALE_FLOOR), cfg
    assert (INTENT_HALF_LIFE_DAYS, INTENT_STALE_FLOOR) == (365.0, 0.25)


def test_the_decay_knobs_are_still_resolved_when_the_term_is_inert():
    """cap 0.0 short-circuits A5, but the knobs still have to come back well-formed — both
    memo CONTEXT hashes fold them in unconditionally, and a None there would poison the
    digest for an install that merely has A5 switched off."""
    for cfg in ({"scoring": {"watchlist_intent": {"enabled": False}}}, {}):
        idx = {} if cfg == {} else {1: _entry()}
        index, cap, hl, floor = resolve_intent_inputs(cfg, idx)
        assert cap == 0.0 and hl == 365.0 and floor == 0.25


def test_a_stale_floor_outside_zero_to_one_is_clamped_not_trusted():
    """``stale_floor: 5`` would make max(5.0, 2**-age) a 5x BONUS on every ancient listing
    — the opposite of a floor. Clamp, because a fat-fingered knob must not invert a term."""
    idx = {1: _entry()}
    assert resolve_intent_inputs(
        {"scoring": {"watchlist_intent": {"stale_floor": 5.0}}}, idx)[3] == 1.0
    assert resolve_intent_inputs(
        {"scoring": {"watchlist_intent": {"stale_floor": -1.0}}}, idx)[3] == 0.0


def test_the_scorers_honour_the_threaded_decay_knobs():
    """Same entry, same cap, same clock — only the half-life differs, and the score moves.
    A 2020 Trakt listing sits ON the floor at the shipped 365d; a half-life long enough to
    make it 'recent' lifts it off."""
    old = _entry(sources=("trakt_watchlist",),
                 dated={"trakt_watchlist": "2020-05-01T09:59:02.000Z"})
    floored = watchlist_intent_score(old, 8.0, now=NOW)
    lifted = watchlist_intent_score(old, 8.0, now=NOW, half_life_days=100_000.0)
    assert floored == 1.2 and lifted > floored

    _, bd_default = score_movie(**_movie(intent_entry=old, intent_cap=8.0, intent_now=NOW),
                                return_breakdown=True)
    _, bd_long = score_movie(**_movie(intent_entry=old, intent_cap=8.0, intent_now=NOW,
                                      intent_half_life_days=100_000.0), return_breakdown=True)
    assert bd_default["A5_intent"] == floored
    assert bd_long["A5_intent"] == lifted

    _, sbd_default = score_show({"genres": ["Drama"]}, intent_entry=old, intent_cap=8.0,
                                intent_now=NOW, return_breakdown=True)
    _, sbd_floor = score_show({"genres": ["Drama"]}, intent_entry=old, intent_cap=8.0,
                              intent_now=NOW, intent_stale_floor=0.5, return_breakdown=True)
    assert sbd_default["A5_intent"] == 1.2          # 8 * 1.00 * 0.25 * 0.60
    assert sbd_floor["A5_intent"] == 2.4            # 8 * 1.00 * 0.50 * 0.60


def test_passing_the_pinned_values_explicitly_is_byte_identical_to_omitting_them():
    """The whole safety argument for threading these: config.json pins 365.0 / 0.25, which
    ARE the constants, so wiring them up must not move a single score."""
    entries = [_entry(),
               _entry(sources=("trakt_watchlist",),
                      dated={"trakt_watchlist": "2020-05-01T09:59:02.000Z"}),
               _entry(sources=("mal_plantowatch",), members=("a", "b"),
                      dated={"mal_plantowatch": "2023-05-17T07:27:34+00:00"})]
    for e in entries:
        assert watchlist_intent_score(e, 8.0, now=NOW) == \
            watchlist_intent_score(e, 8.0, now=NOW, half_life_days=365.0, stale_floor=0.25)
        assert score_movie(**_movie(intent_entry=e, intent_cap=8.0, intent_now=NOW)) == \
            score_movie(**_movie(intent_entry=e, intent_cap=8.0, intent_now=NOW,
                                 intent_half_life_days=365.0, intent_stale_floor=0.25))
        assert score_show({"genres": ["Drama"]}, intent_entry=e, intent_cap=8.0,
                          intent_now=NOW) == \
            score_show({"genres": ["Drama"]}, intent_entry=e, intent_cap=8.0, intent_now=NOW,
                       intent_half_life_days=365.0, intent_stale_floor=0.25)


def test_the_decay_knobs_cannot_resurrect_a_disabled_term():
    """cap 0.0 wins over any knob value — A5 stays byte-identical when config-disabled."""
    e = _entry(sources=("trakt_watchlist",), dated={"trakt_watchlist": "2020-05-01T00:00:00Z"})
    assert watchlist_intent_score(e, 0.0, now=NOW, half_life_days=1.0, stale_floor=1.0) == 0.0
    _, bd = score_movie(**_movie(intent_entry=e, intent_now=NOW,
                                 intent_half_life_days=1.0, intent_stale_floor=1.0),
                        return_breakdown=True)
    assert bd["A5_intent"] == 0.0

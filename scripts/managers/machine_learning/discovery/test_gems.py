"""Tests for the Hidden Gems pure pipeline: the TASTE-ONLY score (engagement excluded,
penalties applied), candidate selection (owned + never-watched-by-THIS-profile, minus
already-planned / in-window / age-gated), the diversity caps, and the 30-day outcome join
across a frozen clock."""
from __future__ import annotations

import json

from scripts.managers.machine_learning.discovery.gems import (
    EXCLUDED_CODES,
    GEM_CONFIDENCE_TIERS,
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_PENDING,
    PENALTY_CODES,
    TASTE_CAPS,
    TASTE_MAX,
    apply_diversity_caps,
    classify_outcome,
    credited_people,
    franchise_key,
    franchise_name,
    gem_candidates,
    hit_rate_confidence,
    hit_rate_summary,
    median_taste,
    parse_breakdown,
    signal_code,
    taste_score,
)
from scripts.managers.machine_learning.thresholds.derive import (
    CONFIDENCE_NONE,
    CONFIDENCE_TIERS,
)

_DAY = 86400.0
_T0 = 1_800_000_000.0            # frozen clock anchor (all tests are relative to it)

# The SAME taste signals on two titles; only the engagement half differs.
_TASTE_HALF = {"B1_actor_affinity": 8.0, "B2_director_affinity": 6.0,
               "B4_genre_affinity": 4.0, "C1_collection": 8.0,
               "F1_critic_consensus": 14.0, "F2_popularity": 1.5}
_ENGAGED = {**_TASTE_HALF, "A1_keep_policy": 15.0, "A2_completion": 12.0,
            "A3_rewatch": 8.0, "A4_user_rating": 10.0,
            "D1_device_capability": 6.0, "D2_transcode_avoidance": 5.0,
            "E1_kids_alignment": 6.0, "F3_recency": 2.0,
            "_total_raw": 105.5, "_total_final": 100}
_NEVER = {**_TASTE_HALF, "A1_keep_policy": 0.0, "A2_completion": 0.0, "A3_rewatch": 0.0,
          "A4_user_rating": 0.0, "D1_device_capability": 6.0,
          "D2_transcode_avoidance": 5.0, "E1_kids_alignment": 6.0, "F3_recency": 0.0,
          "_total_raw": 58.5, "_total_final": 59}


def _row(tmdb, breakdown, **kw):
    row = {"tmdb_id": tmdb, "title": f"Movie {tmdb}", "year": 2000 + (tmdb % 20),
           "has_file": True, "certification": kw.pop("cert", "PG-13"),
           "watchability_breakdown": (json.dumps(breakdown)
                                      if isinstance(breakdown, dict) else breakdown)}
    row.update(kw)
    return row


# ── the taste score: engagement OUT, penalties IN ─────────────────────────────
def test_taste_score_ignores_engagement_so_watched_and_unwatched_tie():
    """The core invariant, and Robert's exact objection: a heavily-watched title and a
    never-watched one with IDENTICAL affinity must score IDENTICALLY. Otherwise the shelf
    would just re-rank the household's favourites instead of surfacing its backlog."""
    print("test_taste_score_ignores_engagement_so_watched_and_unwatched_tie:")
    assert taste_score(_ENGAGED) == taste_score(_NEVER)
    # …even though the blended watchability scores differ enormously.
    assert _ENGAGED["_total_final"] != _NEVER["_total_final"]


def test_taste_score_excludes_every_non_taste_group():
    """Group A (engagement), D (device fit), E (audience alignment) and F3 (recency) must not
    move the score AT ALL — flipping any of them from 0 to its cap changes nothing."""
    print("test_taste_score_excludes_every_non_taste_group:")
    base = dict(_TASTE_HALF)
    baseline = taste_score(base)
    for code in EXCLUDED_CODES:
        polluted = {**base, f"{code}_whatever": 99.0}
        assert taste_score(polluted) == baseline, code


def test_taste_score_renormalises_against_the_taste_only_maximum():
    print("test_taste_score_renormalises_against_the_taste_only_maximum:")
    # 75.0 = 67.0 + A5's 8.0. DERIVED from TASTE_CAPS, never a hardcoded literal in the
    # module — this assertion is the guard that the two can't drift apart.
    assert TASTE_MAX == sum(TASTE_CAPS.values()) == 75.0
    maxed = {f"{code}_x": cap for code, cap in TASTE_CAPS.items()}
    assert taste_score(maxed) == 100.0                       # every taste signal at its cap
    raw = sum(_TASTE_HALF.values())
    assert taste_score(_TASTE_HALF) == round(100.0 * raw / TASTE_MAX, 2)


def test_penalties_still_apply_and_a_floored_title_reads_zero():
    """A gem you can't play isn't a gem: the G group still subtracts, and a title buried by
    penalties floors at 0 rather than going negative."""
    print("test_penalties_still_apply_and_a_floored_title_reads_zero:")
    clean = taste_score(_TASTE_HALF)
    penalised = taste_score({**_TASTE_HALF, "G1_language": -8.0, "G4_not_available": -5.0})
    assert penalised < clean
    assert penalised == round(100.0 * (sum(_TASTE_HALF.values()) - 13.0) / TASTE_MAX, 2)
    floored = taste_score({"B4_genre_affinity": 1.0,
                           **{f"{c}_x": -25.0 for c in PENALTY_CODES}})
    assert floored == 0.0


def test_missing_or_unparseable_breakdown_returns_none_not_zero():
    """'unknown taste' and 'zero taste' are different answers — the first must EXCLUDE the
    title (and be counted), never rank it last with a silent 0."""
    print("test_missing_or_unparseable_breakdown_returns_none_not_zero:")
    for bad in (None, float("nan"), "", "   ", "{not json", "[1,2,3]", 42,
                json.dumps({"_total_raw": 5.0}), json.dumps({})):
        assert taste_score(bad) is None, bad
    assert taste_score(json.dumps({"B4_genre_affinity": 0.0})) == 0.0   # genuinely zero taste


def test_signal_codes_match_on_group_not_spelling():
    """The movie and show scorers spell the same slot differently (B5_studio_affinity vs
    B5_network_affinity) and the snapshot parquet prefixes 'sig_'. All must resolve."""
    print("test_signal_codes_match_on_group_not_spelling:")
    assert signal_code("B5_studio_affinity") == signal_code("B5_network_affinity") == "B5"
    assert signal_code("sig_B4_genre_affinity") == "B4"
    assert signal_code("_total_raw") is None and signal_code("nonsense") is None
    assert parse_breakdown({"sig_B1_actor_affinity": 8.0}) == {"B1": 8.0}
    assert taste_score({"B5_network_affinity": 3.0}) == taste_score({"B5_studio_affinity": 3.0})


# ── candidate selection ───────────────────────────────────────────────────────
def test_candidate_selection_excludes_watched_planned_and_age_gated():
    print("test_candidate_selection_excludes_watched_planned_and_age_gated:")
    rows = [
        _row(1, _NEVER),                                  # the gem
        _row(2, _NEVER),                                  # watched by THIS profile
        _row(3, _NEVER),                                  # already on another plan
        _row(4, _NEVER, cert="R"),                        # age-gated
        _row(5, None),                                    # no breakdown -> excluded + counted
        _row(6, _NEVER, has_file=False),                  # not owned
        _row(7, _NEVER),                                  # library the profile can't reach
    ]
    ranked, stats = gem_candidates(
        rows,
        seen=lambda r: r["tmdb_id"] == 2,
        excluded_ids={3},
        age_ok=lambda r: r.get("certification") != "R",
        reachable=lambda r: r["tmdb_id"] != 7)
    assert [c["tmdb_id"] for c in ranked] == [1]
    assert stats["watched"] == 1 and stats["excluded"] == 1 and stats["age_gated"] == 1
    assert stats["no_breakdown"] == 1 and stats["not_owned"] == 1 and stats["unreachable"] == 1
    assert stats["considered"] == 7 and stats["eligible"] == 1


def test_a_title_watched_by_another_profile_is_still_a_gem():
    """Per-USER, not household: dad finishing a film must not remove it from the kid's shelf.
    The predicate is the caller's per-profile watched set, so an empty one keeps everything."""
    print("test_a_title_watched_by_another_profile_is_still_a_gem:")
    rows = [_row(1, _NEVER)]
    kid, _ = gem_candidates(rows, seen=lambda r: False)          # kid never watched it
    dad, _ = gem_candidates(rows, seen=lambda r: True)           # dad did
    assert [c["tmdb_id"] for c in kid] == [1] and dad == []


def test_candidates_rank_by_taste_descending_and_deterministically():
    print("test_candidates_rank_by_taste_descending_and_deterministically:")
    rows = [_row(1, {"B4_genre_affinity": 1.0}), _row(2, {"F1_critic_consensus": 20.0}),
            _row(3, {"B1_actor_affinity": 8.0}), _row(4, {"B4_genre_affinity": 1.0})]
    ranked, _ = gem_candidates(rows)
    assert [c["tmdb_id"] for c in ranked] == [2, 3, 1, 4]        # ties broken by title/id
    assert ranked == gem_candidates(list(reversed(rows)))[0]     # input order irrelevant


# ── diversity caps ────────────────────────────────────────────────────────────
def _cand(tmdb, taste, *, franchise=None, people=()):
    return {"tmdb_id": tmdb, "title": f"M{tmdb}", "taste_score": taste,
            "franchise": franchise, "people": list(people), "certification": "PG"}


def test_franchise_cap_stops_one_saga_filling_the_shelf():
    print("test_franchise_cap_stops_one_saga_filling_the_shelf:")
    ranked = [_cand(i, 90 - i, franchise="marvel") for i in range(1, 6)] + \
             [_cand(99, 10, franchise=None)]
    picks, stats = apply_diversity_caps(ranked, size=5, max_per_franchise=2, max_per_person=0)
    assert [p["tmdb_id"] for p in picks] == [1, 2, 99]           # 3,4,5 capped; standalone kept
    assert stats["capped_franchise"] == 3
    assert [p["rank"] for p in picks] == [0, 1, 2]               # rank is shelf position


def test_person_cap_stops_one_actor_owning_the_shelf():
    print("test_person_cap_stops_one_actor_owning_the_shelf:")
    ranked = [_cand(i, 90 - i, people=["nicolas cage"]) for i in range(1, 6)]
    picks, stats = apply_diversity_caps(ranked, size=5, max_per_franchise=0, max_per_person=3)
    assert [p["tmdb_id"] for p in picks] == [1, 2, 3]
    assert stats["capped_person"] == 2


def test_caps_are_greedy_and_never_waste_a_slot():
    """A capped candidate is SKIPPED, not fatal: the walk continues and the slot goes to the
    next-best eligible title."""
    print("test_caps_are_greedy_and_never_waste_a_slot:")
    ranked = [_cand(1, 99, franchise="a"), _cand(2, 98, franchise="a"),
              _cand(3, 97, franchise="a"), _cand(4, 96, franchise="b")]
    picks, _ = apply_diversity_caps(ranked, size=3, max_per_franchise=2, max_per_person=0)
    assert [p["tmdb_id"] for p in picks] == [1, 2, 4]
    # cap <= 0 disables it entirely
    all_picks, _ = apply_diversity_caps(ranked, size=4, max_per_franchise=0, max_per_person=0)
    assert [p["tmdb_id"] for p in all_picks] == [1, 2, 3, 4]


def test_held_picks_lead_the_shelf_and_charge_the_caps():
    """A shelf that tops itself up must not drift past the caps one top-up at a time: the picks
    already holding slots pre-charge both budgets."""
    print("test_held_picks_lead_the_shelf_and_charge_the_caps:")
    held = [_cand(10, 50, franchise="a", people=["x"]),
            _cand(11, 40, franchise="a", people=["x"])]
    ranked = [_cand(1, 99, franchise="a", people=["x"]),      # franchise "a" is already full
              _cand(2, 98, franchise="b", people=["x"])]      # person "x" is not (cap 3)
    picks, stats = apply_diversity_caps(ranked, size=5, max_per_franchise=2, max_per_person=3,
                                        held=held)
    assert [p["tmdb_id"] for p in picks] == [10, 11, 2]       # held lead, in their order
    assert stats["held"] == 2 and stats["capped_franchise"] == 1
    assert [p["rank"] for p in picks] == [0, 1, 2]            # rank = shelf position, held too
    # held alone can fill the shelf; then nothing new is added
    full, s2 = apply_diversity_caps(ranked, size=2, max_per_franchise=0, max_per_person=0,
                                    held=held)
    assert [p["tmdb_id"] for p in full] == [10, 11] and s2["picked"] == 2


def test_size_bounds_the_shelf():
    print("test_size_bounds_the_shelf:")
    ranked = [_cand(i, 100 - i) for i in range(1, 40)]
    picks, stats = apply_diversity_caps(ranked, size=25)
    assert len(picks) == 25 and stats["picked"] == 25 and stats["ranked"] == 39


def test_franchise_and_person_keys_normalise_parquet_cells():
    print("test_franchise_and_person_keys_normalise_parquet_cells:")
    assert franchise_key({"collection_name": "The Matrix Collection"}) == "the matrix"
    assert franchise_key({"collection_name": None, "universe_name": "mcu|dc"}) == "mcu"
    assert franchise_key({"collection_name": None, "universe_name": None}) is None
    # the display half keeps the operator's spelling (the log mirror shows it verbatim)
    assert franchise_name({"collection_name": "The Matrix Collection"}) == "The Matrix Collection"
    assert franchise_name({"collection_name": None, "universe_name": None}) is None
    people = credited_people({"director_names": "Ridley Scott",
                              "cast_names": "A|B|C|D|E|F"})
    assert people == ["ridley scott", "a", "b", "c"]     # directors + top-3 billed only


# ── the 30-day outcome join ───────────────────────────────────────────────────
def test_outcome_classifies_pending_hit_and_miss_across_a_frozen_clock():
    print("test_outcome_classifies_pending_hit_and_miss_across_a_frozen_clock:")
    # no play, window still open -> PENDING (never a failure)
    assert classify_outcome(_T0, None, _T0 + 29 * _DAY) == (OUTCOME_PENDING, None)
    # no play, window elapsed -> MISS
    assert classify_outcome(_T0, None, _T0 + 30 * _DAY) == (OUTCOME_MISS, None)
    # played on day 5 -> HIT, with days-to-watch
    assert classify_outcome(_T0, _T0 + 5 * _DAY, _T0 + 40 * _DAY) == (OUTCOME_HIT, 5.0)
    # played on the very last day -> still a HIT (inclusive boundary)
    assert classify_outcome(_T0, _T0 + 30 * _DAY, _T0 + 31 * _DAY) == (OUTCOME_HIT, 30.0)
    # played AFTER the window -> MISS, not a late hit
    assert classify_outcome(_T0, _T0 + 31 * _DAY, _T0 + 40 * _DAY) == (OUTCOME_MISS, None)
    # played BEFORE we recommended it -> cannot have been caused by the pick
    assert classify_outcome(_T0, _T0 - _DAY, _T0 + 40 * _DAY) == (OUTCOME_MISS, None)
    # a non-default window is honoured
    assert classify_outcome(_T0, None, _T0 + 8 * _DAY, window_days=7) == (OUTCOME_MISS, None)


def test_hit_rate_is_over_matured_picks_only():
    """Pending picks must not appear in the numerator OR the denominator — counting open picks
    as failures would drag every freshly-published shelf's hit rate to zero."""
    print("test_hit_rate_is_over_matured_picks_only:")
    s = hit_rate_summary([(OUTCOME_HIT, 3.0), (OUTCOME_HIT, 9.0), (OUTCOME_MISS, None),
                          (OUTCOME_PENDING, None), (OUTCOME_PENDING, None)])
    assert s["published"] == 5 and s["pending"] == 2 and s["matured"] == 3
    assert s["hits"] == 2 and s["misses"] == 1
    assert s["hit_rate"] == round(2 / 3, 4)
    assert s["median_days_to_watch"] == 6.0            # hits only
    assert hit_rate_summary([])["hit_rate"] is None    # nothing matured -> no claim


def test_small_samples_are_labelled_directional():
    print("test_small_samples_are_labelled_directional:")
    assert hit_rate_confidence(0) == CONFIDENCE_NONE
    assert hit_rate_confidence(1) == hit_rate_confidence(29) == "directional"
    assert hit_rate_confidence(30) == "usable"
    assert hit_rate_confidence(100) == "stable"
    assert hit_rate_confidence(300) == "magnitude-grade"
    assert hit_rate_summary([(OUTCOME_HIT, 1.0)])["confidence"] == "directional"


def test_confidence_vocabulary_is_the_thresholds_modules():
    """Same words, different floors — the labels are imported, so the two surfaces can never
    drift apart in vocabulary while each keeps its own sample-size ladder."""
    print("test_confidence_vocabulary_is_the_thresholds_modules:")
    assert [lbl for _f, lbl in GEM_CONFIDENCE_TIERS] == [lbl for _f, lbl in CONFIDENCE_TIERS]
    assert [f for f, _l in GEM_CONFIDENCE_TIERS] == [300, 100, 30, 1]


def test_median_taste_reports_shelf_strength():
    print("test_median_taste_reports_shelf_strength:")
    assert median_taste([{"taste_score": 10.0}, {"taste_score": 30.0}]) == 20.0
    assert median_taste([]) is None


# ── Group-A5 watchlist intent on the taste side (Robert's override) ────────────

def test_a5_is_the_one_group_a_signal_that_moves_the_taste_score():
    """A1-A4 are inert (engagement); A5 is not (forward intent about an unplayed title)."""
    base = taste_score(_TASTE_HALF)
    for code in ("A1_keep_policy", "A2_completion", "A3_rewatch", "A4_user_rating"):
        assert taste_score({**_TASTE_HALF, code: 15.0}) == base
    assert taste_score({**_TASTE_HALF, "A5_intent": 8.0}) > base


def test_a5_re_ranks_rather_than_dominates():
    """8.0 of a 75.0 denominator: a watchlisted title climbs, but cannot leapfrog a title
    with a genuinely stronger taste profile."""
    weak_but_wanted = taste_score({"B1_actor_affinity": 1.0, "A5_intent": 8.0})
    strong_untouched = taste_score(_TASTE_HALF)
    assert weak_but_wanted < strong_untouched
    assert round(100.0 * 8.0 / TASTE_MAX, 2) == round(taste_score({"A5_intent": 8.0}), 2)


def test_a5_cannot_put_a_played_title_back_on_the_shelf():
    """The never-played filter is the ``seen`` predicate, applied BEFORE taste is computed —
    so admitting A5 to the taste side cannot leak an already-watched title onto the shelf,
    no matter how strongly it was watchlisted."""
    watchlisted_and_played = _row(1, {**_ENGAGED, "A5_intent": 8.0})
    watchlisted_unplayed = _row(2, {**_NEVER, "A5_intent": 8.0})
    ranked, stats = gem_candidates([watchlisted_and_played, watchlisted_unplayed],
                                   seen=lambda r: r["tmdb_id"] == 1)
    assert [c["tmdb_id"] for c in ranked] == [2]
    assert stats["watched"] == 1


def test_a5_does_not_disturb_the_diversity_caps():
    """The caps walk the RANKED pool; a higher taste score changes the order, never the
    per-franchise / per-person budgets."""
    rows = [_row(i, {**_NEVER, "A5_intent": 8.0}, collection_name="Saga") for i in range(1, 6)]
    ranked, _ = gem_candidates(rows)
    picks, stats = apply_diversity_caps(ranked, size=5, max_per_franchise=2)
    assert len(picks) == 2
    assert stats["capped_franchise"] == 3

"""aggregate_person_affinity — watched-set + forward map → {person_id: weight}.

The weight of one credit is ``role_weight × billing_weight(rank) × title_engagement``.
Billing decay and the engagement multiplier were added when the matrix was wired into
production: without them a tenth-billed cameo counted as much as a lead, and a film
abandoned at 20% counted as much as one rewatched five times.
"""
from __future__ import annotations

from scripts.managers.machine_learning.affinity.genre_affinity import (
    aggregate_person_affinity,
    title_engagement_weight,
)
from scripts.managers.machine_learning.people_matrix.build import billing_weight

_EMPTY = {"writers": [], "composers": [], "producers": [],
          "cinematographers": [], "editors": []}

FWD = {
    ("movie", 24428):  {"cast": [1245, 3223], "directors": [100], **_EMPTY},
    ("movie", 271110): {"cast": [3223], "directors": [100], **_EMPTY},
    ("show", 5):       {"cast": [777], "directors": [], **_EMPTY},
}


def test_tallies_role_weighted_over_watched():
    w = aggregate_person_affinity({("movie", 24428)}, FWD)
    # Top-billed cast carries role weight 1.0; the director carries 0.7 (operator ruling:
    # "producer/writer affinity less impactful, casting higher"). Both at full billing credit.
    assert w[1245] == 1.0
    assert w[100] == 0.7
    # second-billed cast keeps role weight 1.0 but only 1/(1+0.25) of the billing credit
    assert round(w[3223], 4) == 0.8


def test_accumulates_across_titles_and_sorts_desc():
    w = aggregate_person_affinity({("movie", 24428), ("movie", 271110)}, FWD)
    assert round(w[100], 4) == 1.4            # director of both, 0.7 each
    assert round(w[3223], 4) == 1.8           # 2nd-billed once (0.8) + 1st-billed once (1.0)
    assert w[1245] == 1.0
    assert list(w.values()) == sorted(w.values(), reverse=True)   # ranked descending


def test_all_seven_roles_are_weighted_and_ordered():
    fwd = {("movie", 1): {
        "cast": [1], "directors": [2], "writers": [9], "composers": [8],
        "producers": [7], "cinematographers": [6], "editors": [5],
    }}
    w = aggregate_person_affinity({("movie", 1)}, fwd)
    assert w[1] == 1.0 and w[2] == 0.7        # lead + director
    assert w[9] == 0.375                      # writer (0.6 -> 0.3 -> 0.375, "split the difference")
    assert w[8] == 0.4                        # composer
    assert w[7] == 0.15 and w[6] == 0.3       # producer, cinematographer
    assert w[5] == 0.2                        # editor
    # Robert's requirement, asserted as an ordering rather than as magic numbers:
    # a lead / director must outweigh an editor.
    assert w[1] > w[5] and w[2] > w[5]
    # Composer now outranks writer, and producer sits LAST: the measured producer/writer
    # dominance was largely franchise continuation and graph density, already monetised
    # by the saga/universe machinery - this table expresses PEOPLE-following.
    assert w[8] > w[9] > w[6] > w[5] > w[7]


def test_billing_order_decays_monotonically_for_cast_only():
    fwd = {("movie", 1): {"cast": [10, 11, 12, 13], "directors": [20, 21], **_EMPTY}}
    w = aggregate_person_affinity({("movie", 1)}, fwd)
    assert w[10] > w[11] > w[12] > w[13]      # cast decays with billing rank
    assert w[20] == w[21] == 0.7              # crew order carries no billing meaning


def test_billing_decay_can_be_disabled():
    fwd = {("movie", 1): {"cast": [10, 11, 12], "directors": [], **_EMPTY}}
    w = aggregate_person_affinity({("movie", 1)}, fwd, billing_decay=0.0)
    assert w[10] == w[11] == w[12] == 1.0


def test_billing_weight_shape():
    assert billing_weight(0) == 1.0
    assert round(billing_weight(1), 4) == 0.8
    assert round(billing_weight(9), 4) == 0.3077
    assert billing_weight(-3) == 1.0          # nonsense rank → unbilled
    assert billing_weight("x") == 1.0


# ── engagement weighting ─────────────────────────────────────────────────────

def test_engagement_scales_a_titles_whole_contribution():
    eng = {("movie", 24428): 2.0, ("movie", 271110): 0.25}
    w = aggregate_person_affinity(set(FWD) - {("show", 5)}, FWD, engagement=eng)
    # director of both: 0.7*2.0 + 0.7*0.25
    assert round(w[100], 4) == 1.575
    assert round(w[1245], 4) == 2.0           # lead of the rewatched film only


def test_engagement_absent_key_defaults_to_one():
    plain = aggregate_person_affinity({("movie", 24428)}, FWD)
    with_eng = aggregate_person_affinity({("movie", 24428)}, FWD, engagement={})
    assert plain == with_eng


def test_zero_engagement_drops_the_title_entirely():
    w = aggregate_person_affinity({("movie", 24428)}, FWD,
                                  engagement={("movie", 24428): 0.0})
    assert w == {}


def test_title_engagement_weight_shape():
    assert title_engagement_weight(0, 1.0) == 0.0        # never played → no contribution
    assert title_engagement_weight(1, 1.0) == 1.0        # watched once, finished
    assert title_engagement_weight(1, 0.3) == 0.3        # abandoned early
    assert title_engagement_weight(3, 1.0) == 2.0        # two rewatches
    assert title_engagement_weight(20, 1.0) == 3.0       # rewatch credit caps out
    assert title_engagement_weight(6, 1.0) == 3.0
    assert title_engagement_weight(1, 90) == 0.9         # 0-100 percent column accepted
    assert title_engagement_weight(1, None) == 1.0       # missing completion → full play
    assert title_engagement_weight(1, 0.0) == 1.0        # a play with no measured progress
    assert title_engagement_weight("x", "y") == 0.0


def test_unwatched_and_unknown_keys_ignored():
    assert aggregate_person_affinity(set(), FWD) == {}
    assert aggregate_person_affinity({("movie", 99999)}, FWD) == {}   # not in forward map

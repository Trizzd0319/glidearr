"""C4 person-affinity term threaded into score_movie/score_show:
byte-identical at the default cap=0.0, a positive bump when opted in."""
from __future__ import annotations

from scripts.managers.machine_learning.scoring.movie_scorer import score_movie
from scripts.managers.machine_learning.scoring.show_scorer import score_show

CREDITS = {"cast": [{"name": "A", "id": 123, "order": 0}], "crew": []}
MOVIE_ARGS = ({"tmdbId": 603, "genres": []}, 0.0, 0.9, {}, set(), {}, CREDITS)


def test_movie_c4_byte_identical_at_default():
    base, bd_base = score_movie(*MOVIE_ARGS, return_breakdown=True)
    # passing person_weights but cap=0.0 (default) must NOT change the score
    same, _ = score_movie(*MOVIE_ARGS, person_weights={123: 5}, person_affinity_cap=0.0,
                          return_breakdown=True)
    assert same == base
    assert bd_base["C4_person_affinity"] == 0.0          # key present, inert (mirrors C3)


def test_movie_c4_bumps_when_opted_in():
    base = score_movie(*MOVIE_ARGS)
    on, bd = score_movie(*MOVIE_ARGS, person_weights={123: 5}, person_affinity_cap=8.0,
                         return_breakdown=True)
    assert bd["C4_person_affinity"] > 0
    assert on > base                                     # the household-favourite cast raised it


def test_movie_c4_zero_when_no_overlap():
    # household likes person 999, this movie's cast is 123 → no overlap → 0.0
    _, bd = score_movie(*MOVIE_ARGS, person_weights={999: 5}, person_affinity_cap=8.0,
                        return_breakdown=True)
    assert bd["C4_person_affinity"] == 0.0


def test_show_c4_byte_identical_at_default_and_bumps():
    show = {"genres": [], "network": "", "certification": ""}
    base, bd_base = score_show(show, credits=CREDITS, return_breakdown=True)
    same, _ = score_show(show, credits=CREDITS, person_weights={123: 5},
                         person_affinity_cap=0.0, return_breakdown=True)
    assert same == base and bd_base["C4_person_affinity"] == 0.0
    on, bd = score_show(show, credits=CREDITS, person_weights={123: 5},
                        person_affinity_cap=8.0, return_breakdown=True)
    assert bd["C4_person_affinity"] > 0 and on >= base

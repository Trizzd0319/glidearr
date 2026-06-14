"""aggregate_person_affinity — watched-set + forward map → {person_id: weight}."""
from __future__ import annotations

from scripts.managers.machine_learning.affinity.genre_affinity import aggregate_person_affinity

FWD = {
    ("movie", 24428):  {"cast": [1245, 3223], "directors": [100], "writers": [], "composers": [], "producers": []},
    ("movie", 271110): {"cast": [3223], "directors": [100], "writers": [], "composers": [], "producers": []},
    ("show", 5):       {"cast": [777], "directors": [], "writers": [], "composers": [], "producers": []},
}


def test_tallies_role_weighted_over_watched():
    w = aggregate_person_affinity({("movie", 24428)}, FWD)
    assert w[1245] == 1.0 and w[3223] == 1.0 and w[100] == 1.0   # cast + director both weight 1.0


def test_accumulates_across_titles_and_sorts_desc():
    # 3223 + 100 appear in BOTH watched movies → weight 2.0; 1245 only in one → 1.0
    w = aggregate_person_affinity({("movie", 24428), ("movie", 271110)}, FWD)
    assert w[3223] == 2.0 and w[100] == 2.0 and w[1245] == 1.0
    assert list(w.values()) == sorted(w.values(), reverse=True)   # ranked descending


def test_writer_composer_weaker_than_lead():
    fwd = {("movie", 1): {"cast": [], "directors": [], "writers": [9], "composers": [8], "producers": [7]}}
    w = aggregate_person_affinity({("movie", 1)}, fwd)
    assert w[9] == 0.6 and w[8] == 0.4 and w[7] == 0.3


def test_unwatched_and_unknown_keys_ignored():
    assert aggregate_person_affinity(set(), FWD) == {}
    assert aggregate_person_affinity({("movie", 99999)}, FWD) == {}   # not in forward map

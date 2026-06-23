"""Tests for per-user shelf assembly — fail-closed age gate, owned→ratingKey resolution (pilot-first),
the net-new preview split, and the cap. Candidates arrive pre-scored (watchability-desc)."""
from __future__ import annotations

from scripts.managers.machine_learning.discovery.shelf import (
    gated_plan,
    movie_resolver,
    personalize,
    show_resolver,
)
from scripts.managers.machine_learning.playlists.cert_gate import LITTLE_KID, ADULT

_MOVIE_INV = {"1": {"rating_key": "rk1"}, "2": {"rating_key": "rk2"}}
_EP_INV = {"10:1:1": {"rating_key": "pilot10"}, "10:3:4": {"rating_key": "anniv10"},
           "11:3:4": {"rating_key": "anniv11"}}


def _movie(tmdb, score, *, owned=True, cert=None, csm=None):
    return {"media": "movie", "tmdb_id": tmdb, "title": f"M{tmdb}", "owned": owned,
            "score": score, "why": "w", "certification": cert, "csm_age": csm, "years_ago": 10}


def test_owned_movies_resolve_to_rating_keys_in_score_order_and_cap():
    scored = [_movie(1, 90), _movie(2, 80), _movie(3, 70)]   # tmdb 3 not in inventory
    items, net_new = gated_plan(scored, level=ADULT, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert [i["rating_key"] for i in items] == ["rk1", "rk2"]  # 3 unresolved → dropped
    assert [i["ordinal"] for i in items] == [0, 1] and net_new == []


def test_cap_limits_owned_items():
    scored = [_movie(1, 90), _movie(2, 80)]
    items, _ = gated_plan(scored, level=ADULT, cap=1, resolve=movie_resolver(_MOVIE_INV))
    assert [i["rating_key"] for i in items] == ["rk1"]         # only the top one


def test_net_new_unowned_go_to_preview_not_playlist():
    scored = [_movie(1, 90, owned=False), _movie(2, 80, owned=True)]
    items, net_new = gated_plan(scored, level=ADULT, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert [i["rating_key"] for i in items] == ["rk2"]         # owned only in the playlist
    assert [n["tmdb_id"] for n in net_new] == [1] and net_new[0]["owned"] is False


def test_age_gate_fail_closed_for_restricted_profile():
    scored = [_movie(1, 90, cert="R"), _movie(2, 80, cert="G"), _movie(2, 80, cert=None)]
    items, _ = gated_plan(scored, level=LITTLE_KID, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert [i["rating_key"] for i in items] == ["rk2"]         # R excluded; unknown-cert fail-closed
    # an adult keeps all three resolvable ones
    items2, _ = gated_plan(scored, level=ADULT, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert len(items2) == 3


def test_age_gate_csm_fallback_admits_kid_safe_title():
    scored = [_movie(1, 90, cert=None, csm=5)]                 # no cert, CSM age 5 → little-kid ok
    items, _ = gated_plan(scored, level=LITTLE_KID, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert [i["rating_key"] for i in items] == ["rk1"]


def test_personalize_reorders_by_user_genre_affinity():
    scored = [
        {"media": "movie", "tmdb_id": 1, "title": "Comedy Hit", "genres": ["Comedy"], "score": 90},
        {"media": "movie", "tmdb_id": 2, "title": "Action Flick", "genres": ["Action"], "score": 80},
    ]
    assert [c["tmdb_id"] for c in scored] == [1, 2]            # household order (Comedy higher)
    action_lover = personalize(scored, {"action": 1.0, "comedy": 0.0}, hh_max=90,
                               weights=(0.9, 0.1, 0.65))
    assert [c["tmdb_id"] for c in action_lover] == [2, 1]      # Action lover flips the order
    assert action_lover[0]["score"] > action_lover[1]["score"]


def test_personalize_no_affinity_preserves_household_order():
    scored = [{"tmdb_id": 1, "genres": ["Comedy"], "score": 90},
              {"tmdb_id": 2, "genres": ["Action"], "score": 80}]
    assert [c["tmdb_id"] for c in personalize(scored, {}, hh_max=90, weights=(0.9, 0.1, 0.65))] == [1, 2]


def test_show_resolver_prefers_pilot_then_anniversary_episode():
    r = show_resolver(_EP_INV)
    assert r({"tvdb_id": 10, "season": 3, "episode": 4}) == "pilot10"   # pilot owned → entry point
    assert r({"tvdb_id": 11, "season": 3, "episode": 4}) == "anniv11"   # no pilot → anniversary ep
    assert r({"tvdb_id": 99, "season": 1, "episode": 1}) is None        # nothing owned
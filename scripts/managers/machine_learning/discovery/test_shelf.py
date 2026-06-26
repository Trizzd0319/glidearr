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


def test_owned_freebies_uncapped_but_net_new_is_capped():
    # The cap bounds only NET-NEW (budgeted) picks; owned freebies are all included (free, no grab) so
    # they sit below the net-new with no upper limit.
    owned = [_movie(1, 90), _movie(2, 80)]
    items, _ = gated_plan(owned, level=ADULT, cap=1, resolve=movie_resolver(_MOVIE_INV))
    assert [i["rating_key"] for i in items] == ["rk1", "rk2"]   # cap=1 does NOT limit owned freebies
    net = [_movie(1, 90, owned=False), _movie(2, 80, owned=False), _movie(5, 70, owned=False)]
    _, net_new = gated_plan(net, level=ADULT, cap=2, resolve=movie_resolver(_MOVIE_INV))
    assert [n["tmdb_id"] for n in net_new] == [1, 2]            # net-new capped at 2


def test_on_this_day_leads_net_new_over_higher_watchability():
    # NET-NEW (grab) picks: a title whose anniversary is EXACTLY today leads its group even with a lower
    # watchability — the "on this very day" hook is what justifies spending a grab on it.
    today = {"media": "movie", "tmdb_id": 1, "owned": False, "score": 70, "on_this_day": True, "title": "Today"}
    week = {"media": "movie", "tmdb_id": 2, "owned": False, "score": 90, "on_this_day": False, "title": "ThisWeek"}
    _, net_new = gated_plan([week, today], level=ADULT, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert [n["tmdb_id"] for n in net_new] == [1, 2]              # today first despite lower score
    assert [n["on_this_day"] for n in net_new] == [True, False]


def test_owned_tiers_sort_by_score_not_on_this_day():
    # OWNED tiers sort by per-user watchability ALONE: a higher-score title leads even when a lower-score
    # one falls on today (on_this_day is retained as a display flag but never forces the top), so the
    # owned shelf stays personalized — contrast the net-new today-first behavior above.
    today = {"media": "movie", "tmdb_id": 1, "owned": True, "score": 70, "on_this_day": True, "title": "Today"}
    week = {"media": "movie", "tmdb_id": 2, "owned": True, "score": 90, "on_this_day": False, "title": "ThisWeek"}
    inv = {"1": {"rating_key": "rkToday"}, "2": {"rating_key": "rkWeek"}}
    items, _ = gated_plan([today, week], level=ADULT, cap=5, resolve=movie_resolver(inv))
    assert [i["rating_key"] for i in items] == ["rkWeek", "rkToday"]   # higher score first; today not forced up
    assert [i["on_this_day"] for i in items] == [False, True]         # flag still carried for a badge


def test_seen_owned_demoted_to_the_bottom_tier():
    scored = [_movie(1, 90), _movie(2, 80)]                     # both owned + resolvable
    seen = lambda c, rk: rk == "rk1"                            # tmdb 1 (higher score) already seen
    items, _ = gated_plan(scored, level=ADULT, cap=5, resolve=movie_resolver(_MOVIE_INV), seen=seen)
    assert [i["rating_key"] for i in items] == ["rk2", "rk1"]   # unwatched first, the seen rewatch at bottom
    assert [i["seen"] for i in items] == [False, True]
    assert [i["ordinal"] for i in items] == [0, 1]             # ordinals reassigned across the two tiers


def test_net_new_unowned_go_to_preview_not_playlist():
    scored = [_movie(1, 90, owned=False), _movie(2, 80, owned=True)]
    items, net_new = gated_plan(scored, level=ADULT, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert [i["rating_key"] for i in items] == ["rk2"]         # owned only in the playlist
    assert [n["tmdb_id"] for n in net_new] == [1] and net_new[0]["owned"] is False


def test_net_new_rows_carry_genres_and_votes_for_demand_ordering():
    scored = [{"media": "movie", "tmdb_id": 7, "owned": False, "score": 80,
               "genres": ["Action", "Sci-Fi"], "votes": 1234, "title": "Net New"}]
    _, net_new = gated_plan(scored, level=ADULT, cap=5, resolve=movie_resolver(_MOVIE_INV))
    assert net_new[0]["genres"] == ["Action", "Sci-Fi"] and net_new[0]["votes"] == 1234


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


# ── per-section library scoping (fail-CLOSED) ──────────────────────────────────
_MOVIE_INV_SECTIONED = {"1": {"rating_key": "rk1", "section": "10"},   # Kids Movies
                        "2": {"rating_key": "rk2", "section": "20"}}   # Adult Movies


def test_movie_resolver_allows_only_granted_section():
    r = movie_resolver(_MOVIE_INV_SECTIONED, allowed={"10"})           # shared Kids only
    assert r({"tmdb_id": 1}) == "rk1"                                  # section 10 granted
    assert r({"tmdb_id": 2}) is None                                   # section 20 un-shared → excluded


def test_movie_resolver_no_allowed_keeps_household_wide():
    r = movie_resolver(_MOVIE_INV_SECTIONED)                           # allowed=None → preview/unscoped
    assert r({"tmdb_id": 1}) == "rk1" and r({"tmdb_id": 2}) == "rk2"


def test_movie_resolver_sectionless_entry_excluded_when_scoped():
    r = movie_resolver({"1": {"rating_key": "rk1"}}, allowed={"10"})   # legacy entry, no section
    assert r({"tmdb_id": 1}) is None                                   # unknown section → fail-closed
    assert movie_resolver({"1": {"rating_key": "rk1"}})({"tmdb_id": 1}) == "rk1"   # unscoped still ok


def test_show_resolver_respects_allowed_section():
    inv = {"10:1:1": {"rating_key": "pilot10", "section": "30"}}
    assert show_resolver(inv, allowed={"30"})({"tvdb_id": 10, "season": 1, "episode": 1}) == "pilot10"
    assert show_resolver(inv, allowed={"99"})({"tvdb_id": 10, "season": 1, "episode": 1}) is None
"""next_watch/test_intent_index.py — the forward-intent index the scorer + shield read.

``build_intent_index`` is the join point where three feeds with three different id
conventions become one ``{tmdb: entry}`` / ``{tvdb: entry}`` map. The invariants that
matter: movies key on TMDb and shows on TVDb (what Radarr and Sonarr actually carry),
one human appearing on two feeds is ONE member, and only feeds with a real timestamp
end up dated.
"""
from __future__ import annotations

from scripts.managers.machine_learning.next_watch import build_intent_index


def _plex(title, *, tmdb=None, tvdb=None, kind="movie", by=("trizzd",)):
    ids = {}
    if tmdb is not None:
        ids["tmdb"] = tmdb
    if tvdb is not None:
        ids["tvdb"] = tvdb
    return {"title": title, "type": kind, "ids": ids,
            "watchlisted_by": list(by), "source": "plex_watchlist"}


def _trakt_show(tvdb, listed_at, tmdb=None):
    ids = {"tvdb": tvdb}
    if tmdb is not None:
        ids["tmdb"] = tmdb
    return {"type": "show", "show": {"ids": ids, "title": "S"}, "listed_at": listed_at}


def test_movies_key_on_tmdb_and_shows_key_on_tvdb():
    idx = build_intent_index(plex_union=[
        _plex("A film", tmdb=101),
        _plex("A series", tmdb=999, tvdb=202, kind="show"),
    ])
    assert set(idx["movies"]) == {101}
    assert set(idx["shows"]) == {202}          # NOT 999 — Sonarr joins on TVDb


def test_plex_entries_are_undated_and_trakt_entries_are_dated():
    idx = build_intent_index(
        plex_union=[_plex("S", tvdb=5, kind="show")],
        trakt_shows=[_trakt_show(7, "2020-05-01T09:59:02.000Z")],
    )
    assert idx["shows"][5]["dated"] == {}       # the union carries no per-item timestamp
    assert idx["shows"][7]["dated"] == {"trakt_watchlist": "2020-05-01T09:59:02.000Z"}


def test_one_human_on_two_feeds_counts_as_one_member():
    """Trizzd watchlisting a show on BOTH Plex and Trakt is one person asking, not two —
    otherwise the member ladder would reward owning more accounts."""
    idx = build_intent_index(
        plex_union=[_plex("S", tvdb=9, kind="show", by=("trizzd",))],
        trakt_shows=[_trakt_show(9, "2024-01-01T00:00:00Z")],
        trakt_member="trizzd",
    )
    ent = idx["shows"][9]
    assert ent["members"] == ("trizzd",)
    assert set(ent["sources"]) == {"plex_watchlist", "trakt_watchlist"}


def test_two_humans_count_as_two_members():
    idx = build_intent_index(plex_union=[_plex("F", tmdb=1, by=("trizzd", "aiden"))])
    assert idx["movies"][1]["members"] == ("aiden", "trizzd")


def test_the_newest_listing_per_source_wins():
    """Re-adding a title to your watchlist is FRESH intent — the old copy must not speak
    for it."""
    idx = build_intent_index(trakt_shows=[
        _trakt_show(3, "2020-05-01T00:00:00Z"),
        _trakt_show(3, "2026-07-01T00:00:00Z"),
    ])
    assert idx["shows"][3]["dated"]["trakt_watchlist"] == "2026-07-01T00:00:00Z"


def test_anchor_is_the_most_recent_activity_among_the_asking_members():
    idx = build_intent_index(
        plex_union=[_plex("F", tmdb=1, by=("dormant", "active"))],
        member_anchors={"dormant": "2025-01-01T00:00:00+00:00",
                        "active": "2026-07-26T00:00:00+00:00",
                        "unrelated": "2026-07-27T00:00:00+00:00"},
    )
    assert idx["movies"][1]["anchor"] == "2026-07-26T00:00:00+00:00"


def test_entries_without_a_usable_id_are_dropped_not_guessed():
    idx = build_intent_index(plex_union=[
        _plex("No ids at all"),                       # movie with no tmdb
        _plex("Show with no tvdb", tmdb=5, kind="show"),
    ])
    assert idx["movies"] == {} and idx["shows"] == {}


def test_index_is_order_independent_and_idempotent():
    a = [_plex("F", tmdb=1, by=("x",)), _plex("F", tmdb=1, by=("y",))]
    assert build_intent_index(plex_union=a) == build_intent_index(plex_union=list(reversed(a)))
    assert build_intent_index(plex_union=a) == build_intent_index(plex_union=a + a)


def test_empty_inputs_produce_an_empty_index_not_a_crash():
    assert build_intent_index() == {"movies": {}, "shows": {}}
    assert build_intent_index(plex_union=[None, "junk", 3]) == {"movies": {}, "shows": {}}


def test_memo_fingerprint_moves_when_a_watchlist_entry_changes():
    """The proof for the memo-invalidation half of the change: adding a title, adding a
    member, or re-listing one MUST change the digest folded into both context hashes."""
    from scripts.managers.services._intent_index import intent_memo_fingerprint
    base = build_intent_index(plex_union=[_plex("F", tmdb=1)])
    added = build_intent_index(plex_union=[_plex("F", tmdb=1), _plex("G", tmdb=2)])
    member = build_intent_index(plex_union=[_plex("F", tmdb=1, by=("trizzd", "aiden"))])
    assert intent_memo_fingerprint(base) != intent_memo_fingerprint(added)
    assert intent_memo_fingerprint(base) != intent_memo_fingerprint(member)
    # …but a COSMETIC change (same ids, same members, re-ordered) must NOT.
    reordered = build_intent_index(plex_union=[{**_plex("F renamed", tmdb=1)}])
    assert intent_memo_fingerprint(base) == intent_memo_fingerprint(reordered)


# ── MAL plan-to-watch (bridged rows) ──────────────────────────────────────────

def _mal_show(tvdb, updated_at="2023-05-17T07:27:34+00:00", mal=1):
    """The shape services/mal/id_bridge emits: already joined to a library id."""
    return {"mal": mal, "title": "Anime", "ids": {"tvdb": tvdb}, "updated_at": updated_at}


def test_mal_plan_to_watch_folds_in_as_a_dated_source():
    """MAL's ``list_status.updated_at`` is a REAL per-item timestamp, so unlike the Plex
    union these entries are dated and decay."""
    idx = build_intent_index(mal_shows=[_mal_show(76885)], trakt_member="trizzd")
    ent = idx["shows"][76885]
    assert ent["sources"] == ("mal_plantowatch",)
    assert ent["dated"] == {"mal_plantowatch": "2023-05-17T07:27:34+00:00"}


def test_mal_movies_key_on_tmdb_and_mal_shows_on_tvdb():
    """The routing half of the MALManager._norm fix, at the index level: an anime FILM
    must land in the movie bucket, where Radarr can find it."""
    idx = build_intent_index(
        mal_shows=[_mal_show(76885)],
        mal_movies=[{"mal": 49523, "ids": {"tmdb": 1234}, "updated_at": "2023-05-17T08:33:08+00:00"}])
    assert set(idx["shows"]) == {76885} and set(idx["movies"]) == {1234}


def test_mal_is_the_same_human_as_trakt_not_a_second_watchlister():
    """THE member-inflation guard. One person keeping a Plex watchlist, a Trakt watchlist
    AND a MAL list is one person asking; counting three would walk a solo title from 0.60
    of the cap to the full cap on account sprawl alone."""
    idx = build_intent_index(
        plex_union=[_plex("S", tvdb=88031, kind="show", by=("trizzd",))],
        trakt_shows=[_trakt_show(88031, "2024-01-01T00:00:00Z")],
        mal_shows=[_mal_show(88031)],
        trakt_member="trizzd")
    ent = idx["shows"][88031]
    assert ent["members"] == ("trizzd",)
    assert set(ent["sources"]) == {"plex_watchlist", "trakt_watchlist", "mal_plantowatch"}


def test_mal_inherits_the_trakt_members_activity_anchor():
    """The shield anchors on the ASKER's last play; MAL rows must carry the same one Trakt
    rows do, or a MAL-only title could never be held."""
    idx = build_intent_index(mal_shows=[_mal_show(5)], trakt_member="trizzd",
                             member_anchors={"trizzd": "2026-07-26T00:00:00+00:00"})
    assert idx["shows"][5]["anchor"] == "2026-07-26T00:00:00+00:00"


def test_an_unbridged_mal_row_is_dropped_not_guessed():
    idx = build_intent_index(mal_shows=[{"mal": 1, "ids": {}, "updated_at": "2023-01-01T00:00:00Z"},
                                        {"mal": 2, "updated_at": "2023-01-01T00:00:00Z"}])
    assert idx["shows"] == {}


def test_the_memo_fingerprint_moves_when_a_mal_entry_resolves():
    """The MAL id_map has to reach BOTH score-memo context hashes, or a newly-bridged
    anime would keep serving the score it had while it was unresolvable. It reaches them
    through the index — so the fingerprint must move."""
    from scripts.managers.services._intent_index import intent_memo_fingerprint
    without = build_intent_index(plex_union=[_plex("F", tmdb=1)])
    with_mal = build_intent_index(plex_union=[_plex("F", tmdb=1)], mal_shows=[_mal_show(76885)])
    assert intent_memo_fingerprint(without) != intent_memo_fingerprint(with_mal)
    # …and re-touching the MAL entry (a fresh updated_at) moves it again: the staleness
    # term reads that timestamp, so a memo blind to it would serve a decayed score forever.
    retouched = build_intent_index(plex_union=[_plex("F", tmdb=1)],
                                   mal_shows=[_mal_show(76885, updated_at="2026-07-01T00:00:00Z")])
    assert intent_memo_fingerprint(with_mal) != intent_memo_fingerprint(retouched)

"""services/test_intent_identity.py — one human, one watchlister.

``gather_intent_index`` is the ONLY place the app decides which household member a Trakt
(and therefore a MAL) list belongs to. It resolves ``trakt.household_member`` — falling
back to ``trakt.username`` — and normalises the result with the same ``_norm_member`` the
Plex union and the Tautulli history go through, so all three feeds meet on one key.

THE BUG THIS FILE PINS DOWN. This household's Trakt handle is ``BuckITrizzd`` while Plex
and Tautulli both call the same human ``Trizzd``. With no explicit mapping the fallback
normalised to ``buckitrizzd``, which is nobody, and TWO things broke at once:

  * MEMBER INFLATION — every Trakt and MAL entry counted as a SECOND watchlister on any
    title also on the Plex watchlist. Group-A5's member ladder pays +0.12 of the cap per
    extra member, so a solo title read as a two-person one (0.60 → 0.72 of the cap:
    4.80 → 5.76 at cap 8). Six movies and two shows were being paid for account sprawl.
  * A DEAD SHIELD ANCHOR — ``buckitrizzd`` has no Tautulli activity, so a Trakt-ONLY or
    MAL-ONLY title had ``anchor: None`` and ``intent_hold_active`` failed CLOSED. The
    delete shield could never fire for anything the household asked for on Trakt or MAL
    alone, which is precisely the population the shield was built for.

Both failures are silent — the score is plausible and the shield simply never appears —
so they get tests rather than a comment.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.managers.machine_learning.scoring._shared import intent_hold_active
from scripts.managers.services._intent_index import (
    gather_intent_index,
    member_last_activity,
)

NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)

#: The real spellings. Plex attributes the union to "Trizzd"; Tautulli reports the same
#: account as "Trizzd" (user id 8592385); Trakt's handle is "BuckITrizzd".
PLEX_NAME = "Trizzd"
TRAKT_USER = "BuckITrizzd"

_TVDB = 88031          # Dragon Ball Z Kai — on the Plex watchlist AND MAL plan-to-watch
_TMDB = 680            # Pulp Fiction — on the Plex watchlist AND the Trakt watchlist

_ACTIVE = 1_785_000_000.0        # 2026-07-24-ish → well inside the 90d dormancy window


class _Cache:
    """The two methods the gather calls. Writes are no-ops (the MAL bridge persists)."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, *a, **k):
        return self.data.get(key)

    def set(self, key, value, *a, **k):
        self.data[key] = value


def _config(household_member=None):
    cfg = {"trakt": {"username": TRAKT_USER}}
    if household_member is not None:
        cfg["trakt"]["household_member"] = household_member
    return cfg


def _cache(*, plex=True, trakt_movie=True, mal=False):
    data = {
        "tautulli/history/all": [{"user": PLEX_NAME, "date": _ACTIVE}],
    }
    if plex:
        data["plex/watchlist/union"] = [
            {"title": "Pulp Fiction", "type": "movie", "ids": {"tmdb": _TMDB},
             "watchlisted_by": [PLEX_NAME], "source": "plex_watchlist"},
            {"title": "Dragon Ball Z Kai", "type": "show", "ids": {"tvdb": _TVDB},
             "watchlisted_by": [PLEX_NAME], "source": "plex_watchlist"},
        ]
    if trakt_movie:
        data[f"trakt/{TRAKT_USER}/watchlist/movies"] = [
            {"type": "movie", "movie": {"ids": {"tmdb": _TMDB}, "title": "Pulp Fiction"},
             "listed_at": "2020-05-09T00:13:44.000Z"},
        ]
    if mal:
        # Already-bridged rows, cached the way services/mal/id_bridge writes them, so the
        # gather takes the cached branch and never scans a library.
        data["mal/default/plan_to_watch"] = [
            {"node": {"id": 6033, "title": "Dragon Ball Kai", "media_type": "tv",
                      "alternative_titles": {"en": "", "synonyms": []}},
             "list_status": {"status": "plan_to_watch",
                             "updated_at": "2023-07-05T18:43:58+00:00"}}]
        from scripts.managers.services.mal.id_bridge import plan_fingerprint
        import time as _t
        data["mal/default/id_map"] = {
            "plan_fingerprint": plan_fingerprint(data["mal/default/plan_to_watch"]),
            "built_at_epoch": _t.time(), "movies": [], "unresolved": [],
            "shows": [{"mal": 6033, "title": "Dragon Ball Kai", "ids": {"tvdb": _TVDB},
                       "updated_at": "2023-07-05T18:43:58+00:00"}]}
    return _Cache(data)


# ── the identity itself ───────────────────────────────────────────────────────

def test_household_member_is_the_key_that_is_read_and_it_is_normalised():
    """The config key ``trakt.household_member`` — not ``trakt.username``, not a Plex
    setting — and it goes through the same normaliser the other two feeds do, so the
    operator may spell it "Trizzd" exactly as Plex and Tautulli do."""
    idx = gather_intent_index(_cache(plex=False), _config(PLEX_NAME))
    assert idx["movies"][_TMDB]["members"] == ("trizzd",)


def test_without_the_mapping_the_trakt_handle_is_used_verbatim():
    """The fallback, pinned so the regression is legible: no mapping → the Trakt username
    normalises to a member name nobody else in the household uses."""
    idx = gather_intent_index(_cache(plex=False), _config())
    assert idx["movies"][_TMDB]["members"] == ("buckitrizzd",)


def test_a_blank_mapping_falls_back_rather_than_resolving_to_nothing():
    """The onboarding skeleton ships ``household_member: ""``; an empty string must mean
    'not configured', not 'the member with no name'."""
    idx = gather_intent_index(_cache(plex=False), _config(""))
    assert idx["movies"][_TMDB]["members"] == ("buckitrizzd",)


# ── consequence 1: member inflation ───────────────────────────────────────────

def test_a_title_on_both_plex_and_trakt_counts_as_ONE_watchlister():
    """THE headline. Same human, two lists, one member — so the member ladder pays 0.60 of
    the cap (solo), not 0.72."""
    from scripts.managers.machine_learning.scoring._shared import watchlist_intent_score
    fixed = gather_intent_index(_cache(), _config(PLEX_NAME))["movies"][_TMDB]
    assert fixed["members"] == ("trizzd",)
    assert set(fixed["sources"]) == {"plex_watchlist", "trakt_watchlist"}
    assert watchlist_intent_score(fixed, 8.0, now=NOW) == 4.8       # 8 * 1.00 * 1.0 * 0.60

    split = gather_intent_index(_cache(), _config())["movies"][_TMDB]
    assert len(split["members"]) == 2                                # the bug
    assert watchlist_intent_score(split, 8.0, now=NOW) == 5.76       # the unearned +0.96


def test_mal_rides_the_same_identity_as_trakt():
    """One human keeping a Plex watchlist AND a MAL list is one person asking. This is the
    Dragon Ball Z Kai case: 5.76 under the split identity, 4.80 once it is one member."""
    from scripts.managers.machine_learning.scoring._shared import watchlist_intent_score
    fixed = gather_intent_index(_cache(mal=True), _config(PLEX_NAME))["shows"][_TVDB]
    assert fixed["members"] == ("trizzd",)
    assert set(fixed["sources"]) == {"plex_watchlist", "mal_plantowatch"}
    assert watchlist_intent_score(fixed, 8.0, now=NOW) == 4.8

    split = gather_intent_index(_cache(mal=True), _config())["shows"][_TVDB]
    assert watchlist_intent_score(split, 8.0, now=NOW) == 5.76


def test_a_genuinely_second_household_member_still_counts_twice():
    """The fix must not flatten REAL multi-member intent — the member term exists to grade
    a title two people both asked for."""
    cache = _cache()
    cache.data["plex/watchlist/union"][0]["watchlisted_by"] = [PLEX_NAME, "Aiden"]
    idx = gather_intent_index(cache, _config(PLEX_NAME))
    assert idx["movies"][_TMDB]["members"] == ("aiden", "trizzd")


# ── consequence 2: the dormancy anchor ────────────────────────────────────────

def test_a_trakt_only_title_gains_a_working_dormancy_anchor():
    """``intent_hold_active`` fails CLOSED on a missing anchor. Under the split identity a
    Trakt-only title had none — no Tautulli row is attributed to 'buckitrizzd' — so its
    shield could never fire however active the household was."""
    cache = _cache(plex=False)
    broken = gather_intent_index(cache, _config())["movies"][_TMDB]
    assert broken["anchor"] is None
    assert intent_hold_active(broken, NOW, dormancy_days=90) is False

    fixed = gather_intent_index(cache, _config(PLEX_NAME))["movies"][_TMDB]
    assert fixed["anchor"] == member_last_activity(cache.data["tautulli/history/all"])["trizzd"]
    assert intent_hold_active(fixed, NOW, dormancy_days=90) is True


def test_a_mal_only_title_gains_a_working_dormancy_anchor():
    """The four MAL-only entries on this install (Cowboy Bebop, Fullmetal Alchemist:
    Brotherhood, Steins;Gate, Hunter x Hunter 2011) are the population that had NO anchor
    at all — they are on no other feed, so nothing else could supply one."""
    cache = _cache(plex=False, trakt_movie=False, mal=True)
    broken = gather_intent_index(cache, _config())["shows"][_TVDB]
    assert broken["anchor"] is None and intent_hold_active(broken, NOW, dormancy_days=90) is False

    fixed = gather_intent_index(cache, _config(PLEX_NAME))["shows"][_TVDB]
    assert fixed["anchor"] is not None
    assert intent_hold_active(fixed, NOW, dormancy_days=90) is True


def test_the_fix_never_REMOVES_a_shield():
    """A title already anchored through Plex keeps its anchor: resolving the Trakt member
    onto the SAME human can only add an anchor, never take one away. (Measured on the live
    cache: 94 movies + 43 shows gain a shield, 0 lose one.)"""
    cache = _cache(mal=True)
    before = gather_intent_index(cache, _config())
    after = gather_intent_index(cache, _config(PLEX_NAME))
    for media in ("movies", "shows"):
        for key, ent in before[media].items():
            if intent_hold_active(ent, NOW, dormancy_days=90):
                assert intent_hold_active(after[media][key], NOW, dormancy_days=90), (media, key)


def test_the_dormancy_window_still_expires_the_shield():
    """Resolving the identity must not make the hold unconditional — a dormant Trizzd still
    releases every title he asked for."""
    cache = _cache(plex=False)
    cache.data["tautulli/history/all"] = [{"user": PLEX_NAME, "date": 1_600_000_000.0}]  # 2020
    ent = gather_intent_index(cache, _config(PLEX_NAME))["movies"][_TMDB]
    assert ent["anchor"] is not None
    assert intent_hold_active(ent, NOW, dormancy_days=90) is False


# ── the memo half: a moved member count must invalidate ───────────────────────

def test_the_memo_fingerprint_moves_when_the_identity_collapses_a_member():
    """No SCORER_REVISION bump is needed for this change BECAUSE the member COUNT is one of
    the fields ``intent_memo_fingerprint`` digests. Proven the same way the watchlist union
    and the Trakt ratings were: the digest moves for exactly the titles whose count moves,
    and stands still for the ones that do not."""
    from scripts.managers.services._intent_index import intent_memo_fingerprint
    cache = _cache(mal=True)
    before = intent_memo_fingerprint(gather_intent_index(cache, _config()))
    after = intent_memo_fingerprint(gather_intent_index(cache, _config(PLEX_NAME)))
    assert before != after

    # …and it is the MEMBER COUNT that moved it, not the source list or the dates: the
    # entries for the two affected ids differ in exactly the count field (index 3).
    b = {(row[0], row[1]): row for row in before}
    a = {(row[0], row[1]): row for row in after}
    assert set(b) == set(a)
    changed = [k for k in b if b[k] != a[k]]
    assert changed, "the digest moved but no entry did"
    for k in changed:
        assert b[k][3] == 2 and a[k][3] == 1, (k, b[k], a[k])
        assert b[k][2] == a[k][2] and b[k][4] == a[k][4]     # sources + dates unchanged


def test_a_title_the_identity_does_not_touch_keeps_its_digest_entry():
    """The other half — the fingerprint must not churn wholesale, or the fix would force a
    needless full reseed of every memoized row in the library."""
    from scripts.managers.services._intent_index import intent_memo_fingerprint
    cache = _cache(mal=True)
    cache.data["plex/watchlist/union"].append(
        {"title": "Untouched", "type": "movie", "ids": {"tmdb": 999999},
         "watchlisted_by": [PLEX_NAME], "source": "plex_watchlist"})
    b = {(r[0], r[1]): r for r in intent_memo_fingerprint(gather_intent_index(cache, _config()))}
    a = {(r[0], r[1]): r for r in
         intent_memo_fingerprint(gather_intent_index(cache, _config(PLEX_NAME)))}
    assert b[("movies", 999999)] == a[("movies", 999999)]

"""radarr/quality/test_intent_memo_invalidation.py — the A5 memo-invalidation proof.

THE FAILURE THIS EXISTS TO CATCH, stated plainly: both per-row score memos are keyed on
INPUTS. Radarr's per-row key is ``_h(row)`` over the whole parquet row; Sonarr's is an
explicit ``_SCORE_COLS`` list of episode columns. **The watchlist is in neither** — it
lives in ``plex/watchlist/union``, outside the parquet entirely. So bumping
``SCORER_REVISION`` alone forces exactly ONE full rescore and then the memo goes cold
again: adding a title to your watchlist afterwards would never invalidate its memoized
score, A5 would silently never fire for it, and nothing would report an error.

The fix is folding ``intent_memo_fingerprint`` into both CONTEXT hashes. These tests drive
the REAL ``_build_score_map`` against a stub cache and prove:

  * changing a watchlist entry invalidates the memo (the score is recomputed, and it MOVES);
  * an unrelated cache change does not (the memo still hits — no needless full reseed);
  * a cosmetic union edit does not.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _Cache:
    """Minimal global_cache: a dict with the two methods the score pass calls."""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.sets = 0

    def get(self, key, *a, **k):
        return self.data.get(key)

    def set(self, key, value, *a, **k):
        self.data[key] = value
        self.sets += 1


class _Logger:
    def __init__(self):
        self.infos: list = []

    def log_info(self, m):
        self.infos.append(str(m))

    def log_warning(self, m):
        self.infos.append(str(m))

    def log_debug(self, m):
        pass

    def log_error(self, m):
        pass

    def log_table(self, *a, **k):
        pass

    def log_grid(self, *a, **k):
        pass


_TMDB = 4242
_HISTORY = [{"user": "trizzd", "date": 4_102_444_800, "media_type": "movie",
             "title": "Whatever", "percent_complete": 100}]      # far-future → never dormant


def _union(members=("trizzd",)):
    return [{"title": "Held Film", "type": "movie", "ids": {"tmdb": _TMDB},
             "watchlisted_by": list(members), "source": "plex_watchlist"}]


def _mgr(cache):
    m = RadarrSpacePressureManager.__new__(RadarrSpacePressureManager)
    m.global_cache = cache
    m.logger = _Logger()
    m.config = {"scoring": {"show_score_memo_audit_pct": 0.0}}
    m.registry = None
    m.radarr_api = None
    m.dry_run = True
    return m


def _df():
    return pd.DataFrame([{
        "tmdb_id": _TMDB, "movie_id": 1, "movie_file_id": 10, "title": "Held Film",
        "year": 2020, "genres": "Action", "percent_complete": 0.0, "watch_count": 0,
        "resolution": 1080, "video_codec": "h264", "size_bytes": 5 * 1024 ** 3,
        "runtime_minutes": 120, "keep_policy": None, "is_franchise_entry": False,
        "has_file": True,
        # A released, well-reviewed title, so the total sits WELL clear of the [0, 100]
        # clamp — otherwise G4/G2 bury it at 0 and a real A5 contribution is invisible.
        "is_available": True, "physical_release_date": "2020-06-01T00:00:00Z",
        "digital_release_date": "2020-06-01T00:00:00Z", "imdb_rating": 8.4,
    }])


def _score(cache):
    """One full _build_score_map pass; returns (score, memo-hit-count)."""
    mgr = _mgr(cache)
    out = mgr._build_score_map(_df(), "standard")
    hits = sum(1 for line in mgr.logger.infos if "score memo" in line and "unchanged" in line)
    return list(out.values())[0], mgr.logger.infos, hits


def test_a5_fires_through_the_real_score_pass():
    """Baseline: with the title on the watchlist the score is HIGHER than without it."""
    cold = _Cache({"tautulli/history/all": _HISTORY})
    warm = _Cache({"tautulli/history/all": _HISTORY, "plex/watchlist/union": _union()})
    assert _score(warm)[0] > _score(cold)[0]


def test_adding_a_title_to_the_watchlist_invalidates_the_memo():
    """The whole point. Run once with an empty watchlist (memo warms), then add the title
    and run again on an IDENTICAL parquet row: the memo must MISS and the score must move."""
    cache = _Cache({"tautulli/history/all": _HISTORY})
    before, _, _ = _score(cache)
    assert "radarr/standard/movie_score_memo" in cache.data       # the memo was written

    cache.data["plex/watchlist/union"] = _union()                 # …the only change
    after, infos, _ = _score(cache)
    assert after > before, (before, after)
    # …and the memo genuinely MISSED rather than being bypassed: a hit would have replayed
    # `before`, which is the exact silent failure this guards.
    assert not any("1/1 unchanged" in i for i in infos), infos


def test_a_second_watchlister_invalidates_the_memo_again():
    """Member count is part of the graded signal, so it must be part of the key too."""
    cache = _Cache({"tautulli/history/all": _HISTORY, "plex/watchlist/union": _union()})
    solo, _, _ = _score(cache)
    cache.data["plex/watchlist/union"] = _union(members=("trizzd", "aiden"))
    pair, _, _ = _score(cache)
    assert pair >= solo
    # (the +0.96 raw bump may or may not survive the int round; the CONTEXT must move
    # regardless, which the fingerprint test in next_watch/test_intent_index asserts)


def test_removing_a_title_from_the_watchlist_invalidates_the_memo():
    cache = _Cache({"tautulli/history/all": _HISTORY, "plex/watchlist/union": _union()})
    on, _, _ = _score(cache)
    cache.data["plex/watchlist/union"] = []
    off, _, _ = _score(cache)
    assert off < on


def test_an_unchanged_watchlist_still_HITS_the_memo():
    """The other half: the fingerprint must not churn. A reseed costs a 39s run 211s
    (support/PERF_BASELINE.md), so a key that moved every run would be worse than no key."""
    cache = _Cache({"tautulli/history/all": _HISTORY, "plex/watchlist/union": _union()})
    _score(cache)
    _, infos, _ = _score(cache)
    assert any("1/1 unchanged" in i for i in infos), infos


def test_a_cosmetic_union_edit_does_not_force_a_reseed():
    """Same ids, same members, different title text → same digest → memo still hits."""
    cache = _Cache({"tautulli/history/all": _HISTORY, "plex/watchlist/union": _union()})
    _score(cache)
    u = _union()
    u[0]["title"] = "Held Film (2020 Remaster)"
    u[0]["rating_key"] = "changed"
    cache.data["plex/watchlist/union"] = u
    _, infos, _ = _score(cache)
    assert any("1/1 unchanged" in i for i in infos), infos


# ── the MAL half of the same proof ────────────────────────────────────────────
# MAL plan-to-watch reaches A5 through ``services/mal/id_bridge``, whose resolved map is
# PERSISTED at ``mal/{user}/id_map`` so the ~1.7s library scan happens once rather than
# twice per pass. That persistence is exactly where a memo can go blind: the id_map is a
# per-PASS household input living outside the parquet, so if it did not reach the CONTEXT
# hash, an anime that only just became resolvable (or one whose plan entry was re-touched)
# would keep serving the score it had while it was invisible. It reaches the hash THROUGH
# the index — these tests drive the real ``_build_score_map`` to prove it end to end.

import time as _time

from scripts.managers.services.mal.id_bridge import plan_fingerprint

_MAL_PLAN = [{"node": {"id": 49523, "title": "Held Anime Film", "media_type": "movie",
                       "alternative_titles": {"en": "", "synonyms": []}},
              "list_status": {"status": "plan_to_watch",
                              "updated_at": "2023-05-17T08:33:08+00:00"}}]


def _id_map(rows, plan=_MAL_PLAN):
    """A cached bridge result the way ``resolve_mal_id_map`` writes it."""
    return {"plan_fingerprint": plan_fingerprint(plan), "built_at_epoch": _time.time(),
            "shows": [], "movies": rows, "unresolved": []}


def _mal_cache(rows, plan=_MAL_PLAN):
    return _Cache({"tautulli/history/all": _HISTORY,
                   "mal/default/plan_to_watch": plan,
                   "mal/default/id_map": _id_map(rows, plan)})


def _mal_row(tmdb=_TMDB, updated_at="2023-05-17T08:33:08+00:00"):
    return {"mal": 49523, "title": "Held Anime Film", "ids": {"tmdb": tmdb},
            "updated_at": updated_at}


def test_a_resolved_mal_entry_scores_through_the_real_pass():
    """A5 fires from MAL alone — no Plex union, no Trakt watchlist."""
    assert _score(_mal_cache([_mal_row()]))[0] > _score(_mal_cache([]))[0]


def test_a_newly_resolved_mal_title_invalidates_the_memo():
    """THE POINT. Warm the memo while the anime is unresolvable, then let the bridge
    resolve it (a title added to Sonarr, a TTL rebuild) on an IDENTICAL parquet row: the
    memo must MISS and the score must move."""
    cache = _mal_cache([])
    before, _, _ = _score(cache)
    cache.data["mal/default/id_map"] = _id_map([_mal_row()])
    after, infos, _ = _score(cache)
    assert after > before, (before, after)
    assert not any("1/1 unchanged" in i for i in infos), infos


def test_re_touching_the_mal_entry_invalidates_it_too():
    """``list_status.updated_at`` IS the staleness input, so a memo blind to it would serve
    a floored score forever after the household re-touched the entry."""
    cache = _mal_cache([_mal_row(updated_at="2023-05-17T08:33:08+00:00")])
    stale, _, _ = _score(cache)
    cache.data["mal/default/id_map"] = _id_map([_mal_row(updated_at="2026-07-20T00:00:00Z")])
    fresh, infos, _ = _score(cache)
    assert fresh > stale, (stale, fresh)
    assert not any("1/1 unchanged" in i for i in infos), infos


def test_an_unchanged_mal_map_still_HITS_the_memo():
    cache = _mal_cache([_mal_row()])
    _score(cache)
    _, infos, _ = _score(cache)
    assert any("1/1 unchanged" in i for i in infos), infos


def test_a_mal_entry_that_resolves_to_a_different_title_does_not_move_this_one():
    cache = _mal_cache([_mal_row(tmdb=_TMDB + 1)])
    assert _score(cache)[0] == _score(_mal_cache([]))[0]


def test_a5_stays_inert_at_cap_zero_even_with_mal_resolved():
    """Config-disabling the signal must return the byte-identical pre-A5 score whatever the
    intent feeds say — the same guarantee scoring/test_a5_intent pins at the pure level,
    asserted here through the whole service path."""
    off = _Cache({"tautulli/history/all": _HISTORY, "mal/default/plan_to_watch": _MAL_PLAN,
                  "mal/default/id_map": _id_map([_mal_row()])})
    cfg = {"scoring": {"show_score_memo_audit_pct": 0.0, "watchlist_intent": {"enabled": False}}}
    mgr = _mgr(off)
    mgr.config = cfg
    disabled = list(mgr._build_score_map(_df(), "standard").values())[0]
    assert disabled == _score(_mal_cache([]))[0]


# ── the DECAY KNOBS, driven end to end ────────────────────────────────────────
# ``scoring.watchlist_intent.half_life_days`` / ``stale_floor`` were pinned in config.json
# and read by NOTHING: resolve_intent_inputs returned only (index, cap) and the decay ran
# off the module constants, which hold the same 365.0 / 0.25. Threading them created a new
# way for a memo to go blind — the knobs are per-PASS config that moves every DATED title's
# score without moving one parquet column — so they are in the CONTEXT hash, and that is
# asserted here through the real pass rather than at the unit call.

def _score_with(cache, watchlist_intent):
    mgr = _mgr(cache)
    mgr.config = {"scoring": {"show_score_memo_audit_pct": 0.0,
                              "watchlist_intent": dict(watchlist_intent)}}
    out = mgr._build_score_map(_df(), "standard")
    return list(out.values())[0], mgr.logger.infos


#: A 2020 Trakt listing — the shape this household's ENTIRE Trakt watchlist has. At the
#: shipped 365d half-life it is ~6.2 half-lives old and sits ON the stale floor.
_TRAKT_2020 = [{"type": "movie", "movie": {"ids": {"tmdb": _TMDB}, "title": "Held Film"},
                "listed_at": "2020-05-08T23:38:13.000Z"}]


def _trakt_cache():
    return _Cache({"tautulli/history/all": _HISTORY,
                   "trakt/default/watchlist/movies": list(_TRAKT_2020)})


def test_the_pinned_decay_values_score_exactly_as_the_constants_do():
    """The safety argument for threading them at all: config.json pins 365.0 / 0.25, which
    ARE ``INTENT_HALF_LIFE_DAYS`` / ``INTENT_STALE_FLOOR``, so wiring them up moves nothing.
    Driven through the whole pass, not just the pure helper."""
    implicit, _ = _score_with(_trakt_cache(), {})
    explicit, _ = _score_with(_trakt_cache(), {"half_life_days": 365.0, "stale_floor": 0.25})
    assert implicit == explicit


def test_a_changed_half_life_actually_moves_the_score():
    """The knob was documentation-only before this. A half-life long enough to make a 2020
    listing read as recent must lift it off the floor — end to end, through _build_score_map."""
    floored, _ = _score_with(_trakt_cache(), {})
    lifted, _ = _score_with(_trakt_cache(), {"half_life_days": 100_000.0})
    assert lifted > floored, (floored, lifted)


def test_a_changed_stale_floor_actually_moves_the_score():
    raised, _ = _score_with(_trakt_cache(), {"stale_floor": 1.0})
    shipped, _ = _score_with(_trakt_cache(), {})
    assert raised > shipped, (shipped, raised)


def test_changing_a_decay_knob_invalidates_the_memo():
    """THE memo trap for this change: the knobs are per-PASS config that no per-row key can
    see. Warm the memo at the shipped values, change ONLY the half-life, and the pass must
    MISS and recompute — a hit would replay the old score forever."""
    cache = _trakt_cache()
    before, _ = _score_with(cache, {})
    assert "radarr/standard/movie_score_memo" in cache.data
    after, infos = _score_with(cache, {"half_life_days": 100_000.0})
    assert after > before, (before, after)
    assert not any("1/1 unchanged" in i for i in infos), infos


def test_unchanged_decay_knobs_still_HIT_the_memo():
    """The other half — a knob in the context hash must not churn, or every run would pay
    the full reseed (a 39s run becomes 211s)."""
    cache = _trakt_cache()
    _score_with(cache, {"half_life_days": 365.0, "stale_floor": 0.25})
    _, infos = _score_with(cache, {"half_life_days": 365.0, "stale_floor": 0.25})
    assert any("1/1 unchanged" in i for i in infos), infos


# ── the IDENTITY fix, driven end to end ───────────────────────────────────────

def test_resolving_the_trakt_identity_lowers_a_double_counted_score():
    """A title on BOTH the Plex union and the Trakt watchlist was two members (0.72 of the
    cap) because the Trakt handle normalised to a name nobody else uses. One human → one
    member → 0.60. Six movies and two shows in this library were paying the difference."""
    cache = _Cache({"tautulli/history/all": _HISTORY,
                    "plex/watchlist/union": _union(),
                    "trakt/default/watchlist/movies": list(_TRAKT_2020)})

    def _run(household_member=None):
        mgr = _mgr(cache)
        mgr.config = {"scoring": {"show_score_memo_audit_pct": 0.0},
                      "trakt": {"username": "default", **({"household_member": household_member}
                                                          if household_member else {})}}
        return list(mgr._build_score_map(_df(), "standard").values())[0], mgr.logger.infos

    split, _ = _run()
    fixed, infos = _run("trizzd")
    assert fixed < split, (split, fixed)
    # …and the memo MISSED on the identity change alone: the member count is digested by
    # intent_memo_fingerprint, so no SCORER_REVISION bump is required to make it land.
    assert not any("1/1 unchanged" in i for i in infos), infos

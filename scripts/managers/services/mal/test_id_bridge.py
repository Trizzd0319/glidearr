"""services/mal/test_id_bridge.py — the MAL → library id bridge.

MAL is the one forward-intent feed carrying no joinable id, so Group-A5 could not read it.
The bridge is an EXACT normalized-title match, and the whole risk lives in that word
"exact": a fuzzy match here would hand A5 points AND delete-shield immunity to a title
nobody asked for. These tests pin the five entries that resolve on this household's real
library, and — more importantly — the four ways a match must be REFUSED.

The fixtures below are the shapes the live caches actually hold (MAL
``mal/{user}/plan_to_watch`` nodes, Sonarr letter-bucket series rows, Radarr library rows),
trimmed to the fields the bridge reads.
"""
from __future__ import annotations

import json

from scripts.managers.services.mal import id_bridge
from scripts.managers.services.mal.id_bridge import (
    build_title_index,
    mal_candidate_titles,
    plan_fingerprint,
    resolve_mal_id_map,
    resolve_plan_to_watch,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _mal(mal_id, title, *, en="", synonyms=(), media_type="tv", updated_at="2023-05-17T07:27:34+00:00"):
    return {"node": {"id": mal_id, "title": title, "media_type": media_type,
                     "alternative_titles": {"en": en, "synonyms": list(synonyms), "ja": "…"}},
            "list_status": {"status": "plan_to_watch", "updated_at": updated_at}}


def _series(sid, tvdb, title, *, series_type="anime", clean=None, alternates=()):
    return {"id": sid, "tvdbId": tvdb, "title": title, "seriesType": series_type,
            "cleanTitle": clean if clean is not None else title.lower().replace(" ", ""),
            "alternateTitles": [{"title": t} for t in alternates]}


def _movie(tmdb, title, *, genres=("Animation",), alternates=()):
    return {"tmdbId": tmdb, "title": title, "cleanTitle": title.lower().replace(" ", ""),
            "genres": list(genres), "alternateTitles": [{"title": t} for t in alternates]}


#: The five plan-to-watch entries that resolve against this household's Sonarr library,
#: verbatim from ``mal/trizzd/plan_to_watch`` (2023-05 to 2023-07 timestamps and all).
PLAN = [
    _mal(1, "Cowboy Bebop", en="Cowboy Bebop", updated_at="2023-05-17T07:27:34+00:00"),
    _mal(49523, "Digimon Adventure 02: The Beginning", en="", media_type="movie",
         synonyms=["Digimon Adventure 02 Movie"], updated_at="2023-05-17T08:33:08+00:00"),
    _mal(6033, "Dragon Ball Kai", en="Dragon Ball Z Kai",
         synonyms=["Dragonball Kai", "DBK", "DB Kai", "DBZ Kai"],
         updated_at="2023-07-05T18:43:58+00:00"),
    _mal(5114, "Fullmetal Alchemist: Brotherhood", en="Fullmetal Alchemist: Brotherhood",
         synonyms=["Hagane no Renkinjutsushi: Fullmetal Alchemist", "FMA", "FMAB"],
         updated_at="2023-05-17T07:26:49+00:00"),
    _mal(11061, "Hunter x Hunter (2011)", en="Hunter x Hunter", synonyms=["HxH (2011)"],
         updated_at="2023-05-17T07:27:11+00:00"),
    _mal(9253, "Steins;Gate", en="Steins;Gate", updated_at="2023-05-17T07:27:02+00:00"),
]

#: The matching Sonarr rows, including BOTH Hunter x Hunter series — the 1999 original and
#: the 2011 remake share the normalised alias ``hunter x hunter``, which is exactly the
#: collision the ambiguity guard exists for.
LIBRARY = [
    _series(17660, 76885, "Cowboy Bebop"),
    _series(17249, 88031, "Dragon Ball Kai", alternates=["Dragon Ball Z Kai", "Dragonball Kai"]),
    _series(17584, 85249, "Fullmetal Alchemist: Brotherhood"),
    _series(19306, 252322, "Hunter x Hunter (2011)", alternates=["Hunter x Hunter"]),
    _series(11111, 79076, "Hunter x Hunter", alternates=["Hunter x Hunter"]),
    _series(17701, 244061, "Steins;Gate"),
    _series(99999, 111111, "Cowboy Bebop: The Movie"),        # the near-miss
    _series(88888, 222222, "Steins Gate 0"),                  # another near-miss
    _series(77777, 333333, "Cowboy Bebop", series_type="standard"),  # a NON-anime homonym
]

EXPECTED = {1: 76885, 6033: 88031, 5114: 85249, 11061: 252322, 9253: 244061}


def _resolve(plan=PLAN, library=LIBRARY, movies=()):
    return resolve_plan_to_watch(
        plan,
        show_index=build_title_index([s for s in library
                                      if str(s.get("seriesType", "")).lower() == "anime"], "tvdbId"),
        movie_index=build_title_index(movies, "tmdbId"))


# ── 1. the five that resolve ──────────────────────────────────────────────────

def test_the_five_owned_plan_to_watch_entries_resolve_to_their_real_tvdb_ids():
    """The whole point of the bridge, pinned to the ids verified against the live
    library: 76885 / 88031 / 85249 / 252322 / 244061."""
    got = {r["mal"]: r["ids"]["tvdb"] for r in _resolve()["shows"]}
    assert got == EXPECTED, got


def test_every_resolved_row_carries_mals_own_updated_at_so_it_can_decay():
    """``list_status.updated_at`` is MAL's only real timestamp; without it on the row the
    entry would enter the index UNDATED and be scored at full strength forever."""
    for row in _resolve()["shows"]:
        assert row["updated_at"], row
        assert row["updated_at"].startswith("2023-"), row


# ── 2. the refusals ───────────────────────────────────────────────────────────

def test_a_near_miss_title_is_refused():
    """"Cowboy Bebop: The Movie" and "Steins Gate 0" are DIFFERENT works that normalise to
    different strings. Exact equality must not reach for them — and must not be tempted by
    a shared prefix."""
    got = {r["mal"]: r["ids"]["tvdb"] for r in _resolve()["shows"]}
    assert 111111 not in got.values()
    assert 222222 not in got.values()


def test_a_near_miss_alone_resolves_to_nothing_at_all():
    """With the exact row REMOVED, the near-miss must not become the answer."""
    lib = [s for s in LIBRARY if s["tvdbId"] != 76885]
    got = {r["mal"]: r["ids"]["tvdb"] for r in _resolve(plan=[PLAN[0]], library=lib)["shows"]}
    assert got == {}, got
    assert _resolve(plan=[PLAN[0]], library=lib)["unresolved"][0]["mal"] == 1


def test_an_alias_claimed_by_two_series_is_dropped_not_guessed():
    """``hunter x hunter`` is tvdb 79076 (1999) AND 252322 (2011) in this library. The
    ambiguous alias resolves to NOTHING; MAL entry 11061 still lands on 252322 because its
    own ``title`` — "Hunter x Hunter (2011)" — is unambiguous."""
    idx = build_title_index([s for s in LIBRARY if s["seriesType"] == "anime"], "tvdbId")
    assert "hunter x hunter" not in idx
    assert idx["hunter x hunter 2011"] == 252322
    got = {r["mal"]: r["ids"]["tvdb"] for r in _resolve()["shows"]}
    assert got[11061] == 252322


def test_two_aliases_pointing_at_two_different_series_refuse_the_whole_entry():
    """Entry-level ambiguity, not just title-level: if one MAL entry's aliases reach two
    different library rows, neither wins."""
    lib = [_series(1, 500, "Alpha"), _series(2, 600, "Beta")]
    plan = [_mal(7, "Alpha", en="Beta")]
    out = _resolve(plan=plan, library=lib)
    assert out["shows"] == []
    assert out["unresolved"][0]["mal"] == 7


def test_non_anime_series_are_not_candidates():
    """The candidate pool is ``seriesType == "anime"`` only — a same-named live-action
    series must never absorb a MAL entry. (Here the anime row wins because the non-anime
    homonym was filtered out BEFORE the index was built; if the filter regressed, the two
    would collide and the ambiguity guard would drop Cowboy Bebop entirely.)"""
    got = {r["mal"]: r["ids"]["tvdb"] for r in _resolve()["shows"]}
    assert got[1] == 76885


def test_empty_alternative_titles_contribute_nothing():
    """MAL stores ``"en": ""`` for entries with no English title (the Digimon film). An
    empty string must not become a matchable key that swallows every other empty one."""
    assert mal_candidate_titles(PLAN[1]["node"]) == {
        "digimon adventure 02 the beginning", "digimon adventure 02 movie"}
    idx = build_title_index([_series(1, 500, "")], "tvdbId")
    assert idx == {}


# ── 3. movie/show routing (the MALManager._norm bug) ──────────────────────────

def test_a_mal_movie_is_routed_to_the_radarr_index_never_sonarr():
    """Entry 49523 carries ``media_type: "movie"``. Routed to Sonarr it can never match a
    series row — which is exactly why it was invisible."""
    movies = [_movie(1234, "Digimon Adventure 02: The Beginning")]
    out = _resolve(movies=movies)
    assert [r["mal"] for r in out["movies"]] == [49523]
    assert out["movies"][0]["ids"] == {"tmdb": 1234}
    assert 49523 not in {r["mal"] for r in out["shows"]}


def test_an_unowned_mal_movie_resolves_to_nothing_and_that_is_correct():
    """Today the Digimon film is genuinely not in the library. The right answer is an
    empty movie bucket and an ``unresolved`` entry — not a stray show match."""
    out = _resolve()
    assert out["movies"] == []
    assert [u["mal"] for u in out["unresolved"]] == [49523]
    assert out["unresolved"][0]["media_type"] == "movie"


def test_malmanager_norm_reads_the_real_media_type():
    """The one-line bug: ``_norm`` hardcoded ``"type": "show"``, so every anime FILM was
    handed to Sonarr by the acquisition path too."""
    from scripts.managers.services.mal import MALManager
    assert MALManager._norm(PLAN[1], "mal_plantowatch")["type"] == "movie"
    assert MALManager._norm(PLAN[0], "mal_plantowatch")["type"] == "show"
    # everything that is not a movie stays Sonarr-shaped
    for mt in ("tv", "ona", "ova", "special", "music", "", None):
        node = {"node": {"id": 1, "title": "X", "media_type": mt}}
        assert MALManager._norm(node, "mal_plantowatch")["type"] == "show", mt


def test_norm_still_carries_the_rest_of_the_candidate_shape():
    """The routing fix must not disturb the fields AcquisitionManager reads."""
    from scripts.managers.services.mal import MALManager
    out = MALManager._norm(PLAN[2], "mal_plantowatch")
    assert out["title"] == "Dragon Ball Kai"
    assert out["ids"]["mal"] == 6033 and out["is_anime"] is True
    assert out["source"] == "mal_plantowatch"


# ── 4. the persisted id_map (cache + refresh policy) ──────────────────────────

class _Cache:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.sets: list = []

    def get(self, key, *a, **k):
        return self.data.get(key)

    def set(self, key, value, *a, **k):
        self.data[key] = json.loads(json.dumps(value, default=str))
        self.sets.append(key)
        return True


def _cache_with_plan(plan=PLAN):
    return _Cache({"mal/trizzd/plan_to_watch": plan})


_CFG = {"mal": {"username": "trizzd"}, "sonarr_instances": {"standard": {}},
        "radarr_instances": {"standard": {}}}


def _patched(monkeypatch, library=LIBRARY, movies=()):
    monkeypatch.setattr(id_bridge, "_anime_series",
                        lambda gc, cfg: [s for s in library if s["seriesType"] == "anime"])
    monkeypatch.setattr(id_bridge, "_animation_movies", lambda gc, cfg: list(movies))


def test_the_id_map_is_persisted_so_resolution_happens_once(monkeypatch):
    """The libraries cost ~1.7s to scan and ``gather_intent_index`` runs twice per pass per
    instance — so the resolved map is written, and the second call must not rescan."""
    _patched(monkeypatch)
    cache = _cache_with_plan()
    first = resolve_mal_id_map(cache, _CFG)
    assert {r["ids"]["tvdb"] for r in first["shows"]} == set(EXPECTED.values())
    assert cache.sets == ["mal/trizzd/id_map"]

    calls = []
    monkeypatch.setattr(id_bridge, "_anime_series",
                        lambda gc, cfg: calls.append(1) or [])
    second = resolve_mal_id_map(cache, _CFG)
    assert calls == [], "the cached map was ignored — the scan ran again"
    assert {r["ids"]["tvdb"] for r in second["shows"]} == set(EXPECTED.values())


def test_changing_the_plan_list_rebuilds_the_map(monkeypatch):
    """A title added to (or removed from) plan-to-watch must not be served from a map built
    before it existed."""
    _patched(monkeypatch)
    cache = _cache_with_plan()
    resolve_mal_id_map(cache, _CFG)
    cache.data["mal/trizzd/plan_to_watch"] = [PLAN[0]]
    out = resolve_mal_id_map(cache, _CFG)
    assert {r["mal"] for r in out["shows"]} == {1}
    assert cache.sets == ["mal/trizzd/id_map", "mal/trizzd/id_map"]


def test_a_cosmetic_mal_edit_does_not_rebuild_the_map(monkeypatch):
    """A rebuild reads two whole libraries; artwork and ``mean`` must not trigger one."""
    _patched(monkeypatch)
    cache = _cache_with_plan()
    resolve_mal_id_map(cache, _CFG)
    touched = json.loads(json.dumps(PLAN))
    touched[0]["node"]["mean"] = 9.99
    touched[0]["node"]["main_picture"] = {"medium": "http://example/new.jpg"}
    cache.data["mal/trizzd/plan_to_watch"] = touched
    calls = []
    monkeypatch.setattr(id_bridge, "_anime_series", lambda gc, cfg: calls.append(1) or [])
    resolve_mal_id_map(cache, _CFG)
    assert calls == []


def test_a_stale_map_is_rebuilt_once_the_ttl_passes(monkeypatch):
    """The other refresh direction: the plan is unchanged but the LIBRARY has since gained
    the series. Bounded by ID_MAP_TTL_S rather than left frozen forever."""
    _patched(monkeypatch, library=[])
    cache = _cache_with_plan()
    assert resolve_mal_id_map(cache, _CFG)["shows"] == []
    cache.data["mal/trizzd/id_map"]["built_at_epoch"] -= id_bridge.ID_MAP_TTL_S + 1
    _patched(monkeypatch)
    assert {r["ids"]["tvdb"] for r in resolve_mal_id_map(cache, _CFG)["shows"]} == set(EXPECTED.values())


def test_the_bridge_fails_open_on_every_missing_input():
    """A5 must never be the reason a score pass dies."""
    assert resolve_mal_id_map(None, _CFG) == {"shows": [], "movies": []}
    assert resolve_mal_id_map(_Cache(), _CFG) == {"shows": [], "movies": []}
    assert resolve_mal_id_map(_Cache({"mal/trizzd/plan_to_watch": []}), {}) == {"shows": [], "movies": []}


def test_the_plan_fingerprint_is_order_independent():
    assert plan_fingerprint(PLAN) == plan_fingerprint(list(reversed(PLAN)))
    assert plan_fingerprint(PLAN) != plan_fingerprint(PLAN[:-1])

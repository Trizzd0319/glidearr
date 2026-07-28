"""
services/mal/id_bridge.py — the MAL → library id bridge (Group-A5's anime feed).
================================================================================
MAL is the only forward-intent feed that carries NO id the rest of the app can join on:
``MALManager._norm`` emits ``ids {"tvdb": None, "tmdb": None, "mal": <id>}``, and the MAL
API returns neither a TVDb nor a TMDb id. That single gap is why ``plan_to_watch`` sat
outside ``next_watch.build_intent_index`` while ``INTENT_SOURCE_STRENGTH`` and
``DATED_SOURCES`` had already ranked it. This module closes it.

THE JOIN IS EXACT NORMALIZED-TITLE EQUALITY, AND DELIBERATELY NOTHING MORE.
``services/calendar._library_ids_by_title`` set the precedent and states the reason:
*"conservative by design: fuzzy matching could monitor the wrong series."* The stake here
is higher than a monitoring flip — a wrong match hands A5 points AND a delete shield to a
title nobody asked for — so the same bar applies, plus two guards:

  * CANDIDATE POOL. Shows are matched only against ``seriesType == "anime"`` Sonarr rows
    (1,926 of 11,986 on this install); movies only against Radarr rows carrying an
    animation genre (1,779 of 24,849). MAL is an ANIME database — matching its titles
    against the whole library would put "Sing" the anime and "Sing" the Illumination film
    one string comparison apart, and the anime filter is the same discrimination
    ``seriesType`` already encodes.
  * AMBIGUITY DROPS. A normalized title claimed by two different library ids resolves to
    NOTHING. This is not theoretical: ``hunter x hunter`` is both tvdb 79076 (1999) and
    252322 (2011) in this library, and MAL's English title for entry 11061 is exactly
    "Hunter x Hunter". Picking either would be a coin flip; its MAL ``title``
    ("Hunter x Hunter (2011)") matches the 2011 row unambiguously, so the right answer is
    reached by the unambiguous alias and the ambiguous one is discarded.

Title candidates: MAL ``title`` + ``alternative_titles.en`` + ``alternative_titles.synonyms``
against Sonarr ``title`` / ``cleanTitle`` / ``alternateTitles[].title`` (Radarr: the same
three fields). ``writeback._util.norm_title`` does the normalising — one definition of
"same title" in the app, not a second one here.

NO NETWORK. ``trakt.lookup.search_show_by_title_and_year`` would resolve some misses, but
this bridge is built from inside the SCORE PASS (``services/_intent_index``), and a live
search there would put a Trakt round-trip per unresolved MAL entry on the deletion path —
with its own cache key, its own refresh policy and its own failure mode. Deliberately out;
a miss simply contributes no A5.

CACHED, because the scan is not free: ~0.7s to read the Sonarr letter buckets plus ~1.0s
for the Radarr library, and ``gather_intent_index`` runs twice per pass per instance. The
resolved map is persisted to ``mal/{user}/id_map`` and rebuilt only when the plan-to-watch
list itself changes (fingerprint) or the map ages past ``ID_MAP_TTL_S``, so a title the
household ADDS to Sonarr later is picked up within a week without a per-pass rescan.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time

from scripts.managers.services.writeback._util import norm_title

#: How long a resolved map may be reused before the libraries are re-scanned. One week —
#: the same TTL ``sonarr/sync/media`` uses for library-shaped data. The map is ALSO
#: rebuilt immediately whenever the plan-to-watch list changes, so this bound only covers
#: the other direction: a MAL entry that was unowned and has since been added to the
#: library.
ID_MAP_TTL_S = 7 * 24 * 3600

#: Sonarr's own anime marker. The show-side candidate pool is exactly these rows.
ANIME_SERIES_TYPE = "anime"

#: The movie-side analogue of ``seriesType == "anime"``: Radarr has no series type, so an
#: animation genre is the closest thing to the same statement. Unioned with the operator's
#: ``animeGenres`` (which on this install is ``["anime"]`` — a Sonarr-shaped value that
#: never appears on a TMDb movie, hence the explicit "animation" here).
_ANIMATION_GENRES = frozenset({"animation", "anime"})

_ID_MAP_KEY = "mal/{user}/id_map"


# ── pure matching ─────────────────────────────────────────────────────────────

def mal_candidate_titles(node) -> set:
    """Every title a MAL node answers to, normalised. ``title`` + ``alternative_titles.en``
    + ``alternative_titles.synonyms``; empties dropped (MAL stores "" for a missing ``en``)."""
    if not isinstance(node, dict):
        return set()
    alt = node.get("alternative_titles") or {}
    raw = [node.get("title"), alt.get("en") if isinstance(alt, dict) else None]
    raw += list((alt.get("synonyms") or []) if isinstance(alt, dict) else [])
    return {n for n in (norm_title(t) for t in raw) if n}


def library_titles(row) -> set:
    """Every title an Arr row answers to, normalised: ``title``, ``cleanTitle`` and each
    ``alternateTitles[].title``."""
    if not isinstance(row, dict):
        return set()
    raw = [row.get("title"), row.get("cleanTitle")]
    for at in (row.get("alternateTitles") or []):
        raw.append(at.get("title") if isinstance(at, dict) else at)
    return {n for n in (norm_title(t) for t in raw) if n}


def build_title_index(rows, id_field: str) -> dict:
    """``{normalised title: library id}`` over ``rows``, with AMBIGUITY DROPPED.

    A normalised title claimed by two different ids is removed entirely rather than
    resolved to either — see the module docstring's ``hunter x hunter`` case. Rows with no
    ``id_field`` contribute nothing."""
    seen: dict = {}
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        try:
            lid = int(row.get(id_field))
        except (TypeError, ValueError):
            continue
        for name in library_titles(row):
            prev = seen.get(name, lid)
            seen[name] = lid if prev == lid else None
    return {k: v for k, v in seen.items() if v is not None}


def resolve_plan_to_watch(plan, *, show_index: dict, movie_index: dict) -> dict:
    """Bridge MAL plan-to-watch entries onto library ids.

    Returns ``{"shows": [row, …], "movies": [row, …], "unresolved": [{mal, title}, …]}``
    where each row is ``{"mal": id, "title": str, "ids": {"tvdb"|"tmdb": int},
    "updated_at": iso}`` — exactly the shape ``next_watch.build_intent_index`` consumes.

    ROUTING IS BY ``node.media_type``, not by assumption: a MAL ``movie`` goes to the
    Radarr index and a series to the Sonarr one. (``MALManager._norm`` hardcoded
    ``"type": "show"`` for every entry, which sent MAL's movies to Sonarr where they can
    never match — fixed alongside this.) Anything that is not ``movie`` is treated as a
    series: MAL's ``tv``/``ona``/``ova``/``special`` are all Sonarr-side on this install.

    PURE: no I/O, deterministic, order-independent."""
    shows: list = []
    movies: list = []
    unresolved: list = []
    for entry in (plan or []):
        if not isinstance(entry, dict):
            continue
        node = entry.get("node", entry) if isinstance(entry.get("node", entry), dict) else {}
        is_movie = str(node.get("media_type") or "").lower() == "movie"
        index, id_key, bucket = ((movie_index, "tmdb", movies) if is_movie
                                 else (show_index, "tvdb", shows))
        hit = None
        for name in sorted(mal_candidate_titles(node)):
            found = (index or {}).get(name)
            if found is None:
                continue
            if hit is not None and hit != found:
                # Two different library rows via two different aliases — the entry is
                # ambiguous at the ENTRY level, not just the title level. Refuse it.
                hit = None
                break
            hit = found
        if hit is None:
            unresolved.append({"mal": node.get("id"), "title": node.get("title"),
                               "media_type": node.get("media_type")})
            continue
        bucket.append({
            "mal": node.get("id"),
            "title": node.get("title"),
            "ids": {id_key: int(hit)},
            "updated_at": (entry.get("list_status") or {}).get("updated_at"),
        })
    return {"shows": shows, "movies": movies, "unresolved": unresolved}


def plan_fingerprint(plan) -> str:
    """Stable digest of the plan-to-watch list's SCORE-RELEVANT content (id + every title
    alias + the timestamp). Changing it forces a rebuild; a cosmetic MAL field (artwork,
    ``mean``) does not."""
    payload = []
    for entry in (plan or []):
        if not isinstance(entry, dict):
            continue
        node = entry.get("node", entry) if isinstance(entry.get("node", entry), dict) else {}
        payload.append([node.get("id"), str(node.get("media_type") or ""),
                        sorted(mal_candidate_titles(node)),
                        (entry.get("list_status") or {}).get("updated_at")])
    payload.sort(key=lambda r: str(r[0]))
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str)
                        .encode("utf-8", "replace")).hexdigest()


# ── I/O ───────────────────────────────────────────────────────────────────────

def _instance_names(config, service: str) -> list:
    insts = ((config or {}).get(f"{service}_instances", {}) or {})
    names = [k for k, v in insts.items() if k != "default_instance" and isinstance(v, dict)]
    return names or ([insts.get("default_instance")] if insts.get("default_instance") else [])


def _anime_series(global_cache, config) -> list:
    """Every ``seriesType == "anime"`` row across the cached Sonarr letter buckets.

    Read straight off ``{cache_root}/sonarr/{inst}/library/*.json.gz`` rather than through
    ``SonarrCacheSeriesManager``: this runs inside the score pass, which has no Sonarr
    manager handle on the Radarr side, and the bucket layout is stable (it is the same
    path ``SonarrCacheSeriesManager._letter_file`` builds)."""
    out: list = []
    base = getattr(getattr(global_cache, "key_builder", None), "base_dir", None)
    if base is None:
        return out
    for inst in _instance_names(config, "sonarr"):
        folder = base / "sonarr" / str(inst) / "library"
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json.gz")):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    rows = json.load(fh)
            except Exception:
                continue
            out.extend(r for r in (rows or [])
                       if isinstance(r, dict)
                       and str(r.get("seriesType") or "").lower() == ANIME_SERIES_TYPE)
    return out


def _animation_movies(global_cache, config) -> list:
    """Every animation-genre row across the cached Radarr full libraries."""
    wanted = set(_ANIMATION_GENRES) | {str(g).lower() for g in
                                       ((config or {}).get("animeGenres") or []) if g}
    out: list = []
    for inst in _instance_names(config, "radarr"):
        try:
            rows = global_cache.get(f"radarr.movies.{inst}.full") or []
        except Exception:
            continue
        out.extend(r for r in rows if isinstance(r, dict)
                   and any(str(g).lower() in wanted for g in (r.get("genres") or [])))
    return out


def _mal_user(config) -> str:
    return ((config or {}).get("mal", {}) or {}).get("username", "default") or "default"


def resolve_mal_id_map(global_cache, config, logger=None) -> dict:
    """``{"shows": [...], "movies": [...]}`` for MAL plan-to-watch, cached + fail-open.

    Reads ``mal/{user}/plan_to_watch``; returns empty buckets when MAL is unconfigured, the
    list is empty, or anything at all goes wrong — A5 must never be the reason a score pass
    fails. Rebuilds (and persists to ``mal/{user}/id_map``) only when the cached map is
    missing, older than :data:`ID_MAP_TTL_S`, or built from a different plan list."""
    empty = {"shows": [], "movies": []}
    if global_cache is None:
        return empty
    try:
        user = _mal_user(config)
        plan = global_cache.get(f"mal/{user}/plan_to_watch") or []
        if not plan:
            return empty
        want = plan_fingerprint(plan)
        key = _ID_MAP_KEY.format(user=user)

        cached = global_cache.get(key) or {}
        if (isinstance(cached, dict) and cached.get("plan_fingerprint") == want
                and float(cached.get("built_at_epoch") or 0) > time.time() - ID_MAP_TTL_S):
            return {"shows": cached.get("shows") or [], "movies": cached.get("movies") or []}

        resolved = resolve_plan_to_watch(
            plan,
            show_index=build_title_index(_anime_series(global_cache, config), "tvdbId"),
            movie_index=build_title_index(_animation_movies(global_cache, config), "tmdbId"),
        )
        try:
            global_cache.set(key, {"plan_fingerprint": want,
                                   "built_at_epoch": time.time(),
                                   **resolved})
        except Exception:                       # pragma: no cover — persistence is a cache
            pass
        if logger:
            logger.log_debug(
                f"[Intent] MAL id bridge: {len(resolved['shows'])} show(s) + "
                f"{len(resolved['movies'])} movie(s) resolved of {len(plan)} plan-to-watch "
                f"entries ({len(resolved['unresolved'])} unmatched).")
        return {"shows": resolved["shows"], "movies": resolved["movies"]}
    except Exception as e:                      # pragma: no cover — defensive
        if logger:
            logger.log_debug(f"[Intent] MAL id bridge unavailable ({e}) — no MAL intent.")
        return empty

"""
features/movie_features.py — the movie row -> MovieFeatureRow -> score adapter.
================================================================================
The single boundary between the Radarr movie_files cache and the pure movie
scorer (ML Step 3c). ``build_movie_feature_row`` marshals a Parquet row's COLUMN
NAMES into the typed ``MovieFeatureRow`` (this is the ONE place the cache schema is
known); ``score_movie_features`` reconstructs the exact ``score_movie`` call from a
feature row + the shared library context. PURE — no HTTP, no global_cache, no I/O:
the service does the cache reads (credits, related set, affinity maps) and passes
them in.

Public API:
  * build_movie_feature_row(row, *, credits=None, related_tmdb_ids=None,
                            user_rating=None) -> MovieFeatureRow
  * score_movie_features(fr, *, <library context>, return_breakdown=False) -> int | (int, dict)
"""
from __future__ import annotations

import json

import pandas as pd

from scripts.managers.machine_learning.contracts.feature_rows import MovieFeatureRow
from scripts.managers.machine_learning.scoring._shared import (
    intent_decay_kwargs as _intent_decay_kwargs,
    preferred_language_available,
)
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie


def build_movie_feature_row(
    row,
    *,
    credits: dict | None = None,
    related_tmdb_ids=None,
    user_rating: float | None = None,
) -> MovieFeatureRow:
    """Marshal a movie_files Parquet row into a MovieFeatureRow. ``credits`` (Trakt
    people), ``related_tmdb_ids`` (daemon-cached C3 neighbours) and ``user_rating``
    (the household's own Trakt rating, 0-10) are fetched by the service (I/O) and
    passed in. ``percent_complete`` is stored as a 0-1 fraction.
    Mirrors the column reads previously inlined in space_pressure._score_row.

    ``user_rating`` is a KWARG rather than a column read because the rating lives in
    ``trakt/{user}/ratings/movies``, not in movie_files — the same reason the show side
    passes it in from ``_build_user_show_rating_map`` instead of reading the parquet."""
    def _f(col):
        v = row.get(col)
        return float(v) if pd.notna(v) and v is not None else None

    def _s(col):
        v = row.get(col)
        return str(v) if pd.notna(v) and v else None

    def _b(col):
        v = row.get(col)
        return bool(v) if pd.notna(v) and v is not None else False

    def _i(col):
        v = row.get(col)
        if v is None or not pd.notna(v):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    tmdb_raw = row.get("tmdb_id")
    pct_raw = row.get("percent_complete")
    wc_raw = row.get("watch_count")
    genres_raw = row.get("genres")
    try:
        genres = json.loads(genres_raw) if genres_raw and pd.notna(genres_raw) else []
    except Exception:
        genres = []
    coll_tmdb_raw = row.get("collection_tmdb_id")
    coll_name = row.get("collection_name")

    return MovieFeatureRow(
        tmdb_id=int(tmdb_raw) if pd.notna(tmdb_raw) else None,
        genres=tuple(genres),
        percent_complete=float(pct_raw) / 100.0 if pd.notna(pct_raw) else 0.0,
        watch_count=int(wc_raw) if wc_raw and pd.notna(wc_raw) else 0,
        credits=credits or {},
        imdb_rating=_f("imdb_rating"),
        tmdb_rating=_f("tmdb_rating"),
        trakt_rating=_f("trakt_rating"),
        rotten_tomatoes_score=_f("rotten_tomatoes_score"),
        metacritic_score=_f("metacritic_score"),
        popularity=_f("popularity"),
        certification=_s("certification"),
        original_language=_s("original_language"),
        in_cinemas_date=_s("in_cinemas_date"),
        physical_release_date=_s("physical_release_date"),
        digital_release_date=_s("digital_release_date"),
        keep_policy=_s("keep_policy"),
        is_franchise_entry=_b("is_franchise_entry"),
        universe_name=_s("universe_name"),
        is_available=_b("is_available"),
        # GROUP D — the HELD file's playback characteristics. These two columns have
        # always been in movie_files but were never marshalled, so score_movie ran with
        # target_resolution=None (D1/D3 = 0 for every movie, always) and no codec (D2
        # pinned to the constant +2.0 "unknown" branch). The show path has read the
        # equivalent episode_files columns since it was written — this closes the
        # asymmetry. See score_movie's `target_resolution` docstring for why "held"
        # rather than "would-acquire" is the right resolution to answer D1/D3 with.
        resolution=_i("resolution"),
        video_codec=_s("video_codec"),
        # GROUP D v2 — the rest of the file's playback facts. ``size_bytes`` +
        # ``runtime_minutes`` are marshalled because 45% of this library's movie rows
        # report ``video_bitrate = 0``; without the size÷runtime fallback the whole
        # bitrate axis would be blind on nearly half the library and Group D would slide
        # back toward the constant this redesign exists to remove.
        video_bitrate=_f("video_bitrate"),
        size_bytes=_f("size_bytes"),
        runtime_minutes=_f("runtime_minutes"),
        audio_codec=_s("audio_codec"),
        audio_channels=_f("audio_channels"),
        audio_languages=_s("audio_languages"),
        subtitles=_s("subtitles"),
        relative_path=_s("relative_path"),
        # single-file movie: consumable fraction is 1.0 (has en dub/sub) or 0.0; None
        # when no track data so G1 falls back to the legacy penalty (byte-identical).
        language_consumable_fraction=(
            (1.0 if preferred_language_available(_s("audio_languages"), _s("subtitles"), ["en"]) else 0.0)
            if (_s("audio_languages") or _s("subtitles")) else None
        ),
        collection_tmdb_id=int(coll_tmdb_raw) if pd.notna(coll_tmdb_raw) else None,
        collection_name=coll_name if pd.notna(coll_name) and coll_name else None,
        related_tmdb_ids=tuple(related_tmdb_ids) if related_tmdb_ids is not None else None,
        # GROUP A4 — the household's declared Trakt rating. None → A4 is 0.0, exactly as
        # every movie scored before this was threaded.
        user_rating=float(user_rating) if user_rating is not None else None,
    )


def score_movie_features(
    fr: MovieFeatureRow,
    *,
    genre_affinity: dict,
    watched_tmdb_ids,
    collection_members: dict,
    platform_usage: dict | None = None,
    transcode_stats: dict | None = None,
    device_capabilities: dict | None = None,
    # GROUP D v2 — the household transcode profile, built ONCE per pass by
    # device_fit.build_transcode_profile. None (the default) → score_movie takes the
    # legacy D1/D2/D3 path, byte-identical.
    transcode_profile=None,
    per_user_affinity: dict | None = None,
    kids_users: list | None = None,
    adult_users: list | None = None,
    completion_threshold: float = 0.9,
    affinity_boost: float = 1.0,
    related_graph_cap: float = 4.0,
    person_weights: dict | None = None,
    person_affinity_cap: float = 0.0,
    # GROUP A5 — the household's forward-intent index, {tmdb_id: entry} from
    # next_watch.build_intent_index, built ONCE per pass by the service. cap DEFAULT 0.0 →
    # A5 contributes 0.0 → byte-identical until a caller opts in.
    intent_index: dict | None = None,
    intent_cap: float = 0.0,
    intent_now=None,
    # The A5 staleness knobs (scoring.watchlist_intent.half_life_days / stale_floor),
    # resolved once per pass by _shared.resolve_intent_inputs. None → the scorer's own
    # module-constant defaults, i.e. byte-identical for any caller that omits them.
    intent_half_life_days: float | None = None,
    intent_stale_floor: float | None = None,
    language_consumability: bool = False,
    return_breakdown: bool = False,
):
    """Reconstruct the exact ``score_movie`` call from a MovieFeatureRow + the shared
    library context. Byte-identical to the marshalling previously inline in
    space_pressure._score_row. ``person_weights``/``person_affinity_cap`` feed Group-C4
    (cast/crew taste overlap); cap DEFAULT 0.0 → C4 contributes 0.0 → byte-identical until
    a caller opts in (space_pressure gates it on config + a built people-matrix).
    ``intent_index``/``intent_cap`` feed Group-A5 (explicit watchlist intent) on the same
    terms — the index is keyed on TMDb id, so the lookup is the feature row's own id."""
    movie = {
        "tmdbId": fr.tmdb_id,
        "genres": list(fr.genres),
        "collection": (
            {"tmdbId": fr.collection_tmdb_id, "name": fr.collection_name}
            if fr.collection_name is not None else {}
        ),
    }
    related = set(fr.related_tmdb_ids) if fr.related_tmdb_ids is not None else None
    return score_movie(
        movie=movie,
        completion_pct=fr.percent_complete,
        completion_threshold=completion_threshold,
        collection_members=collection_members,
        watched_tmdb_ids=watched_tmdb_ids,
        genre_affinity=genre_affinity,
        credits=fr.credits,
        watch_count=fr.watch_count,
        # GROUP A4 — the household's own Trakt verdict. Structurally absent until now: the
        # kwarg has existed on score_movie since it was written, but nothing ever passed
        # it, so A4_user_rating was 0.0 on every one of this library's movies while the
        # show scorer had read the equivalent since its first line. Same asymmetry D1/D3
        # had, closed the same way.
        user_rating=fr.user_rating,
        platform_usage=platform_usage,
        transcode_stats=transcode_stats,
        # GROUP D — the held file's resolution/codec. Mirrors score_show_features'
        # long-standing target_resolution=/video_codec= threading; without these two
        # lines D1 and D3 are structurally 0 and D2 is a constant, regardless of how
        # much Tautulli platform/transcode data the household has.
        target_resolution=fr.resolution,
        video_codec=fr.video_codec,
        # None = the shipped cold-start capability matrix; a caller with config in hand
        # passes _shared.resolve_device_capabilities(config) to honour
        # scoring.device_capabilities.
        device_capabilities=device_capabilities,
        # GROUP D v2 — the profile is the switch; the rest are the file facts its risk
        # axes read. Runtime is converted to SECONDS here (the parquet stores minutes for
        # movies and seconds for episodes) so the scorer speaks one unit.
        transcode_profile=transcode_profile,
        video_bitrate=fr.video_bitrate,
        size_bytes=fr.size_bytes,
        runtime_seconds=(fr.runtime_minutes * 60.0) if fr.runtime_minutes else None,
        audio_codec=fr.audio_codec,
        audio_channels=fr.audio_channels,
        audio_languages=fr.audio_languages,
        subtitles=fr.subtitles,
        relative_path=fr.relative_path,
        per_user_affinity=per_user_affinity,
        kids_users=kids_users,
        adult_users=adult_users,
        imdb_rating=fr.imdb_rating,
        tmdb_rating=fr.tmdb_rating,
        trakt_rating=fr.trakt_rating,
        metacritic_score=fr.metacritic_score,
        rotten_tomatoes_score=fr.rotten_tomatoes_score,
        popularity=fr.popularity,
        certification=fr.certification,
        in_cinemas_date=fr.in_cinemas_date,
        physical_release_date=fr.physical_release_date,
        digital_release_date=fr.digital_release_date,
        original_language=fr.original_language,
        # File-aware G1 is OPT-IN (oracle-mover): pass the consumable fraction only when
        # enabled, else None → legacy household-language penalty → byte-identical.
        language_consumable_fraction=(fr.language_consumable_fraction if language_consumability else None),
        is_franchise_entry=fr.is_franchise_entry,
        universe_name=fr.universe_name,
        keep_policy=fr.keep_policy,
        is_available=fr.is_available,
        affinity_boost=affinity_boost,
        related_tmdb_ids=related,
        related_graph_cap=related_graph_cap,
        person_weights=person_weights,
        person_affinity_cap=person_affinity_cap,
        intent_entry=(intent_index or {}).get(fr.tmdb_id),
        intent_cap=intent_cap,
        intent_now=intent_now,
        # Omit rather than forward None: score_movie's defaults ARE the module constants,
        # and passing None would make float(None) raise inside the decay.
        **_intent_decay_kwargs(intent_half_life_days, intent_stale_floor),
        return_breakdown=return_breakdown,
    )

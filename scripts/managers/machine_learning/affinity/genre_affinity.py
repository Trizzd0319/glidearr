"""
affinity/genre_affinity.py — taste maps from watch history (pure).
================================================================================
Relocated from ``services/tautulli/users`` (ML Step 3a). Turns pre-fetched
Tautulli history + a metadata index into ranked {name: weight} affinity maps
(household-wide and per-user). PURE — no HTTP, no Tautulli API, no logging, no
global_cache. The Tautulli users manager keeps only FETCH (users/history) and
the cache-write of the SAME keys (``tautulli/affinity``,
``tautulli/users/{user}/affinity``); it now delegates the computation here.

Temporal decay (opt-in): with ``half_life_days`` set, each watch is weighted by
``exp(-age_days / half_life_days)`` (from its unix ``date``) so recent taste outweighs
stale taste. DEFAULT (``half_life_days`` None/0) — each watch counts 1 (int), so the
maps are byte-identical to the legacy raw counts.

Public API:
  * aggregate_affinity(history_entries, metadata_index, *, half_life_days=None, now=None) -> dict
        {genres, actors, directors, composers, producers, studios, format_metrics}
        each a {name: weight} map sorted descending.
  * per_user_affinity(history_entries, metadata_index, user_list, *, half_life_days=None, now=None) -> dict
        {username: aggregate_affinity(...)} — entries grouped by the ``user`` field;
        users with zero matching entries are omitted.
  * build_library_index(history_entries, movie_genres_by_title, series_genres_by_title) -> dict
        {rating_key: {"genres": [...]}} resolved from OWNED-library (Radarr/Sonarr) genres via a
        stable title join — fills the holes the sparse per-key Tautulli fetch leaves.
  * merge_library_first(library_index, tautulli_index) -> dict
        library genres win; Tautulli supplies people/studios + the not-owned fallback.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone


def _entry_weight(entry: dict, half_life_days, now):
    """Per-watch tally weight. Default (half_life_days falsy) -> 1 (int), so counts
    stay byte-identical. With a half-life, an entry decays as
    ``exp(-age_days / half_life_days)`` from its unix ``date``; a missing/unparseable
    date -> 1.0 (neutral, no decay rather than dropping the watch)."""
    if not half_life_days or half_life_days <= 0:
        return 1
    try:
        watched = datetime.fromtimestamp(int(entry.get("date")), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return 1.0
    age_days = max(0.0, (now - watched).total_seconds() / 86400.0)
    return math.exp(-age_days / half_life_days)


def _norm_title(s) -> str:
    """Normalise a title for the STABLE (drift-proof) join: lowercase, keep only ``[a-z0-9]``.
    So 'Bluey (2018)' → 'bluey2018' and 'The Fault in Our Stars' → 'thefaultinourstars'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _lookup_title(by_title: dict, raw) -> list | None:
    """Title lookup with a trailing-year fallback so 'Bluey (2018)' also matches a 'Bluey'
    entry (and vice-versa). Returns the genre list or None."""
    if not raw:
        return None
    key = _norm_title(raw)
    if key in by_title:
        return by_title[key]
    stripped = _norm_title(re.sub(r"\s*\(\d{4}\)\s*$", "", str(raw)))
    if stripped != key and stripped in by_title:
        return by_title[stripped]
    return None


def build_library_index(history_entries: list, movie_genres_by_title: dict,
                        series_genres_by_title: dict) -> dict:
    """A ``{rating_key: {"genres": [...]}}`` metadata index resolved from the OWNED-LIBRARY
    genres (Radarr movies / Sonarr series) via a STABLE identity — the movie title, or an
    episode's ``grandparent_title`` (series name) — rather than the churn-prone Tautulli
    ``rating_key``. This fills the coverage holes the per-key Tautulli metadata fetch leaves:
    a low-volume, movie-only profile whose handful of rating_keys never made the sampled index
    would otherwise score affinity=0 and collapse to the flat household ranking. PURE.

    ``*_by_title`` map a title to a genre list; keys may be raw (any case/punctuation) — they
    are normalised here, so callers can pass straight from a Radarr/Sonarr row."""
    mv = {_norm_title(k): v for k, v in (movie_genres_by_title or {}).items() if k}
    sv = {_norm_title(k): v for k, v in (series_genres_by_title or {}).items() if k}
    out: dict[str, dict] = {}
    for e in history_entries:
        rk = str(e.get("rating_key") or "")
        if not rk:
            continue
        mt = e.get("media_type")
        if mt == "movie":
            genres = _lookup_title(mv, e.get("title"))
        elif mt == "episode":
            genres = _lookup_title(sv, e.get("grandparent_title"))
        else:
            genres = None
        if genres:
            out[rk] = {"genres": list(genres)}
    return out


def merge_library_first(library_index: dict, tautulli_index: dict) -> dict:
    """Merge the library-derived index OVER the Tautulli per-key index. LIBRARY-FIRST: where the
    library resolved genres for a rating_key, those genres WIN — one consistent Radarr/Sonarr
    taxonomy that also matches the candidate items the scorers/genre-match compare against —
    while Tautulli still supplies actors/directors/studios for that key and remains the sole
    (fallback) source for watched-but-not-owned keys the library can't resolve. PURE; neither
    input is mutated."""
    merged = {rk: dict(v) for rk, v in (tautulli_index or {}).items()}
    for rk, lib in (library_index or {}).items():
        base = dict(merged.get(rk) or {})
        if lib.get("genres"):
            base["genres"] = list(lib["genres"])
        merged[rk] = base
    return merged


def aggregate_affinity(history_entries: list, metadata_index: dict, *,
                       half_life_days=None, now=None) -> dict:
    """Core affinity computation over an arbitrary entry list.

    Each history entry's ``rating_key`` is looked up in ``metadata_index``; the
    metadata's genres/actors/directors/composers/producers/studios/codecs/audio
    languages are tallied (by 1, or by a recency weight when ``half_life_days`` is
    set). Returns ranked {name: weight} maps. Pure.
    """
    if half_life_days and now is None:
        now = datetime.now(tz=timezone.utc)

    genre_map       = defaultdict(int)
    actor_map       = defaultdict(int)
    director_map    = defaultdict(int)
    composer_map    = defaultdict(int)
    producer_map    = defaultdict(int)
    studio_map      = defaultdict(int)
    video_codec_map = defaultdict(int)
    audio_codec_map = defaultdict(int)
    audio_lang_map  = defaultdict(int)

    for entry in history_entries:
        rk = str(entry.get("rating_key", ""))
        meta = metadata_index.get(rk)
        if not isinstance(meta, dict):
            continue
        w = _entry_weight(entry, half_life_days, now)
        for genre in meta.get("genres", []) or []:
            genre_map[genre] += w
        for actor in meta.get("actors", []) or []:
            actor_map[actor] += w
        for director in meta.get("directors", []) or []:
            director_map[director] += w
        for composer in meta.get("composers", []) or []:
            composer_map[composer] += w
        for producer in meta.get("producers", []) or []:
            producer_map[producer] += w
        studios = meta.get("studios", [])
        if studios and studios[0]:
            studio_map[studios[0]] += w
        if meta.get("video_codec"):
            video_codec_map[meta["video_codec"]] += w
        if meta.get("audio_codec"):
            audio_codec_map[meta["audio_codec"]] += w
        for lang in meta.get("audio_language") or []:
            audio_lang_map[lang] += w

    return {
        "genres":    dict(sorted(genre_map.items(),    key=lambda x: x[1], reverse=True)),
        "actors":    dict(sorted(actor_map.items(),    key=lambda x: x[1], reverse=True)),
        "directors": dict(sorted(director_map.items(), key=lambda x: x[1], reverse=True)),
        "composers": dict(sorted(composer_map.items(), key=lambda x: x[1], reverse=True)),
        "producers": dict(sorted(producer_map.items(), key=lambda x: x[1], reverse=True)),
        "studios":   dict(sorted(studio_map.items(),   key=lambda x: x[1], reverse=True)),
        "format_metrics": {
            "video_codec":    dict(video_codec_map),
            "audio_codec":    dict(audio_codec_map),
            "audio_language": dict(audio_lang_map),
        },
    }


def aggregate_person_affinity(
    watched_media_keys,
    media_people_fwd: dict,
    *,
    role_weights: dict | None = None,
) -> dict:
    """Household person-affinity from the watched-set + the people_matrix forward map.

    For each watched title key ``(medium, ext_id)`` present in ``media_people_fwd``,
    add each of its people's role weight (cast/director strong, writer/composer weaker
    — :data:`people_matrix.PERSON_ROLE_WEIGHTS`) to that ``tmdb_person_id``'s running
    total. Returns ``{tmdb_person_id: weight}`` sorted descending — the watched-set-
    derived weight vector the Group-C4 scorer term reads. Pure (defaultdict tally +
    sorted-descending, mirroring :func:`aggregate_affinity`).

    No recency decay yet: the household watched-set is an undated id set. When a dated
    watch history is threaded, a ``half_life_days`` knob mirroring ``_entry_weight``
    can decay each title's contribution; deferred to avoid dead complexity.
    """
    from scripts.managers.machine_learning.people_matrix.build import PERSON_ROLE_WEIGHTS
    role_weights = role_weights or PERSON_ROLE_WEIGHTS

    weights: dict = defaultdict(float)
    for key in watched_media_keys:
        roles = media_people_fwd.get(key)
        if not roles:
            continue
        for role, pids in roles.items():
            rw = role_weights.get(role, 0.0)
            if rw <= 0:
                continue
            for pid in pids:
                weights[pid] += rw
    return dict(sorted(weights.items(), key=lambda x: x[1], reverse=True))


def per_user_affinity(
    history_entries: list,
    metadata_index: dict,
    user_list: list,
    *,
    half_life_days=None,
    now=None,
) -> dict:
    """Per-user affinity matrices: join ``history_entries`` to ``user_list`` and run
    :func:`aggregate_affinity` independently for every user. Returns ``{username: affinity}``
    (keyed by login username so the cache path / builder lookup are unchanged); users with
    zero matching entries are omitted. Pure (the service keeps the surrounding logging).

    The join is on the STABLE Tautulli ``user_id`` (present on both the history rows and the
    user_list, and the same key the playlist builder's watched-set uses), falling back to the
    friendly-name ``user`` field. Grouping by ``user_id`` rather than the name fixes the silent
    drop when an account's Tautulli friendly_name (e.g. ``Aiden / Raina``) differs from its
    login username (e.g. ``Aiden``): the old code bucketed history by the friendly name but
    looked it up by the username, so every such user matched nothing and lost its affinity."""
    if half_life_days and now is None:
        now = datetime.now(tz=timezone.utc)

    by_id: dict[str, list] = defaultdict(list)
    by_name: dict[str, list] = defaultdict(list)
    for entry in history_entries:
        uid = str(entry.get("user_id") or "")
        if uid:
            by_id[uid].append(entry)
        name = str(entry.get("user") or "")
        if name:
            by_name[name].append(entry)

    result: dict[str, dict] = {}
    for user in user_list:
        username = str(user.get("username") or user.get("user_id") or "")
        if not username:
            continue
        # user_id join first (stable); then the history `user` friendly name; then the
        # username itself (legacy match for accounts whose friendly_name == username).
        entries = (
            by_id.get(str(user.get("user_id") or ""))
            or by_name.get(str(user.get("friendly_name") or ""))
            or by_name.get(username)
        )
        if not entries:
            continue
        result[username] = aggregate_affinity(
            entries, metadata_index, half_life_days=half_life_days, now=now
        )
    return result

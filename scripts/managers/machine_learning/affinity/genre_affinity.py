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
"""
from __future__ import annotations

import math
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
    """Per-user affinity matrices: group ``history_entries`` by the ``user`` field,
    then run :func:`aggregate_affinity` independently for every user in
    ``user_list``. Returns ``{username: affinity}``; users with zero matching
    entries are omitted. Pure (the service keeps the surrounding logging)."""
    if half_life_days and now is None:
        now = datetime.now(tz=timezone.utc)

    user_entries: dict[str, list] = defaultdict(list)
    for entry in history_entries:
        username = str(entry.get("user") or "")
        if username:
            user_entries[username].append(entry)

    result: dict[str, dict] = {}
    for user in user_list:
        username = str(user.get("username") or user.get("user_id", ""))
        if not username:
            continue
        entries = user_entries.get(username, [])
        if not entries:
            continue
        result[username] = aggregate_affinity(
            entries, metadata_index, half_life_days=half_life_days, now=now
        )
    return result

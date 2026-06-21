"""
features/show_features.py — the series rows -> ShowFeatureRow -> score adapter.
================================================================================
The single boundary between the Sonarr episode_files cache and the pure show
scorer (ML Step 3c). ``build_show_feature_row`` aggregates a series' episode rows
(+ the Sonarr series object) up to series level — the ONE place the episode_files
column schema is known; ``score_show_features`` reconstructs the exact
``score_show`` call from the feature row + the shared library context. PURE — no
HTTP, no global_cache: the service does the I/O (credits, ratings, related set,
user rating, series cache) and passes them in, plus the run-stable ``now``.

Public API:
  * build_show_feature_row(rows, series_obj, now, *, credits, trakt_rating,
        trakt_votes, user_rating, related_tvdb_ids) -> ShowFeatureRow
  * score_show_features(fr, *, <library context>, return_breakdown=False) -> int | (int, dict)
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.contracts.feature_rows import ShowFeatureRow
from scripts.managers.machine_learning.scoring._shared import preferred_language_available
from scripts.managers.machine_learning.scoring.show_scorer import score_show


def _modal_str(col) -> "str | None":
    """Most-common non-null string value in a column (representative codec)."""
    try:
        s = col.dropna()
        if not len(s):
            return None
        m = s.mode()
        return str(m.iloc[0]) if len(m) else str(s.iloc[0])
    except Exception:
        return None


def _max_int(col) -> "int | None":
    """Max numeric value in a column as int (target resolution / rewatch)."""
    try:
        s = pd.to_numeric(col, errors="coerce").dropna()
        return int(s.max()) if len(s) else None
    except Exception:
        return None


def _consumable_fraction(rows, preferred=("en",)) -> "float | None":
    """Fraction (0..1) of the series' EPISODE files watchable in a preferred language —
    each episode checked individually for a preferred-language audio (dub) OR subtitle
    (sub) track. This is deliberately NOT a union: a series with an English dub/sub on
    only some episodes is only PARTLY watchable (the union would wrongly pass the whole
    series). None when neither track column is present."""
    has_audio = "audio_languages" in rows.columns
    has_subs = "subtitles" in rows.columns
    if not has_audio and not has_subs:
        return None
    n = len(rows)
    if n == 0:
        return None
    pref = list(preferred)
    audio = rows["audio_languages"].tolist() if has_audio else [None] * n
    subs = rows["subtitles"].tolist() if has_subs else [None] * n
    ok = sum(1 for a, s in zip(audio, subs) if preferred_language_available(a, s, pref))
    return ok / n


def build_show_feature_row(
    rows,
    series_obj: dict,
    now,
    *,
    credits: dict | None = None,
    trakt_rating=None,
    trakt_votes=None,
    user_rating=None,
    related_tvdb_ids=None,
) -> ShowFeatureRow:
    """Aggregate a series' episode_files rows + the Sonarr series object into a
    ShowFeatureRow. ``now`` is the run-stable timestamp (passed in so recency is
    consistent across the library); credits / ratings / related / user_rating are
    fetched by the service (I/O) and passed in. Mirrors the aggregation previously
    inlined in episode_files._build_show_score_map."""
    series_obj = series_obj or {}

    if "is_watched" in rows.columns:
        watched_episodes = int((rows["is_watched"] == True).sum())   # noqa: E712
    else:
        watched_episodes = 0

    days_since = None
    if "last_watched_at" in rows.columns:
        lw = pd.to_datetime(rows["last_watched_at"], utc=True, errors="coerce").dropna()
        if len(lw):
            days_since = (now - lw.max()).total_seconds() / 86400.0

    max_wc = _max_int(rows["watch_count"]) if "watch_count" in rows.columns else 0
    max_wc = max_wc or 0

    stats = series_obj.get("statistics") or {}
    total_eps = int(stats.get("episodeCount") or stats.get("totalEpisodeCount") or 0)
    if total_eps <= 0:
        total_eps = max(watched_episodes, 1)

    keep_policy = None
    if "keep_policy" in rows.columns:
        kp = rows["keep_policy"].dropna()
        keep_policy = str(kp.iloc[0]) if len(kp) else None

    video_codec = _modal_str(rows["video_codec"]) if "video_codec" in rows.columns else None
    target_resolution = _max_int(rows["resolution"]) if "resolution" in rows.columns else None
    language_consumable_fraction = _consumable_fraction(rows)

    latest_air = None
    if "air_date_utc" in rows.columns:
        air = pd.to_datetime(rows["air_date_utc"], utc=True, errors="coerce").dropna()
        if len(air):
            latest_air = air.max().isoformat()

    ol = series_obj.get("originalLanguage")
    sonarr_rating = (series_obj.get("ratings") or {}).get("value")

    return ShowFeatureRow(
        tvdb_id=series_obj.get("tvdbId"),
        genres=tuple(series_obj.get("genres") or []),
        network=series_obj.get("network"),
        certification=series_obj.get("certification"),
        original_language=ol.get("name") if isinstance(ol, dict) else ol,
        keep_policy=keep_policy,
        watched_episodes=watched_episodes,
        total_episodes=total_eps,
        days_since_last_watch=days_since,
        max_episode_watch_count=max_wc,
        video_codec=video_codec,
        target_resolution=target_resolution,
        latest_air_date=latest_air,
        user_rating=user_rating,
        sonarr_rating=sonarr_rating,
        trakt_rating=trakt_rating,
        trakt_votes=trakt_votes,
        credits=credits or {},
        related_tvdb_ids=tuple(related_tvdb_ids) if related_tvdb_ids is not None else None,
        language_consumable_fraction=language_consumable_fraction,
    )


def score_show_features(
    fr: ShowFeatureRow,
    *,
    genre_affinity: dict,
    platform_usage: dict | None = None,
    transcode_stats: dict | None = None,
    per_user_affinity: dict | None = None,
    kids_users: list | None = None,
    adult_users: list | None = None,
    watched_tvdb_ids=None,
    related_graph_cap: float = 4.0,
    person_weights: dict | None = None,
    person_affinity_cap: float = 0.0,
    ur_slope: float = 1.5,
    ur_pos_cap: float = 8.0,
    ur_neg_cap: float = -3.0,
    ur_conf_divisor: float = 8.0,
    language_consumability: bool = False,
    return_breakdown: bool = False,
):
    """Reconstruct the exact ``score_show`` call from a ShowFeatureRow + the shared
    library context. Byte-identical to the marshalling previously inline in
    episode_files._build_show_score_map. ``person_weights``/``person_affinity_cap`` feed
    Group-C4 (cast/crew taste overlap); cap DEFAULT 0.0 → C4 is byte-identical until the
    caller opts in (episode_files gates it on config + a built people-matrix)."""
    show = {
        "genres": list(fr.genres),
        "network": fr.network,
        "certification": fr.certification,
        "original_language": fr.original_language,
    }
    related = set(fr.related_tvdb_ids) if fr.related_tvdb_ids is not None else None
    return score_show(
        show,
        return_breakdown=return_breakdown,
        watched_episodes=fr.watched_episodes,
        total_episodes=fr.total_episodes,
        days_since_last_watch=fr.days_since_last_watch,
        max_episode_watch_count=fr.max_episode_watch_count,
        keep_policy=fr.keep_policy,
        user_rating=fr.user_rating,
        genre_affinity=genre_affinity,
        credits=fr.credits,
        platform_usage=platform_usage,
        transcode_stats=transcode_stats,
        target_resolution=fr.target_resolution,
        video_codec=fr.video_codec,
        per_user_affinity=per_user_affinity,
        kids_users=kids_users,
        adult_users=adult_users,
        sonarr_rating=float(fr.sonarr_rating) if fr.sonarr_rating else None,
        trakt_rating=float(fr.trakt_rating) if fr.trakt_rating else None,
        trakt_votes=int(fr.trakt_votes) if fr.trakt_votes else None,
        latest_air_date=fr.latest_air_date,
        related_tvdb_ids=related,
        watched_tvdb_ids=watched_tvdb_ids,
        person_weights=person_weights,
        person_affinity_cap=person_affinity_cap,
        # File-aware G1 is OPT-IN (oracle-mover): only pass the per-episode consumable
        # fraction when the caller enables it; otherwise None → score_show falls back to
        # the legacy household-language penalty → byte-identical.
        language_consumable_fraction=(fr.language_consumable_fraction if language_consumability else None),
        related_graph_cap=related_graph_cap,
        ur_slope=ur_slope,
        ur_pos_cap=ur_pos_cap,
        ur_neg_cap=ur_neg_cap,
        ur_conf_divisor=ur_conf_divisor,
    )

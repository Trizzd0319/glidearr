"""Feature rows — the typed input the brain consumes.

A service builds one of these from a cached Parquet row + affinity context (the
ONLY place a column name / API JSON shape is known); scorers/planners consume
them. Optional fields default to None/0 so a partially-enriched row is safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MovieFeatureRow:
    """One Radarr movie, fully resolved for scoring/planning."""
    tmdb_id: int | None = None
    movie_id: int | None = None
    movie_file_id: int | None = None
    title: str | None = None
    genres: tuple[str, ...] = ()
    runtime_minutes: float | None = None
    size_bytes: float | None = None
    resolution: int | None = None
    quality_profile_id: int | None = None
    # engagement
    is_watched: bool = False
    watch_count: int = 0
    percent_complete: float = 0.0
    last_watched_at: str | None = None
    date_added: str | None = None
    # affinity-bearing
    watchability_score: float | None = None
    watchability_percentile: float | None = None
    credits: dict = field(default_factory=dict)        # {cast:[...], crew:[...]}
    # critic ratings (0-10 / 0-100 as sourced)
    imdb_rating: float | None = None
    tmdb_rating: float | None = None
    trakt_rating: float | None = None
    rotten_tomatoes_score: float | None = None
    metacritic_score: float | None = None
    popularity: float | None = None
    original_language: str | None = None
    in_cinemas_date: str | None = None
    physical_release_date: str | None = None
    digital_release_date: str | None = None
    # GROUP C3 — Trakt-related neighbour tmdb ids (daemon-cached), set by the service
    related_tmdb_ids: tuple[int, ...] | None = None
    # classification / protection
    certification: str | None = None
    keep_policy: str | None = None
    is_franchise_entry: bool = False
    universe_name: str | None = None
    collection_tmdb_id: int | None = None
    collection_name: str | None = None
    is_available: bool = True
    marked_for_deletion: bool = False
    # fraction (0..1) of the movie's file(s) watchable in a preferred language (audio
    # dub OR subtitle); for a single-file movie this is 1.0 or 0.0. Feeds file-aware G1.
    language_consumable_fraction: float | None = None


@dataclass(frozen=True)
class ShowFeatureRow:
    """One Sonarr series, aggregated from its episode rows + Trakt/Tautulli."""
    series_id: int | None = None
    tvdb_id: int | None = None
    title: str | None = None
    genres: tuple[str, ...] = ()
    network: str | None = None
    certification: str | None = None
    original_language: str | None = None
    keep_policy: str | None = None
    watched_episodes: int = 0
    total_episodes: int = 0
    days_since_last_watch: float | None = None
    max_episode_watch_count: int = 0
    video_codec: str | None = None              # modal codec across the series' files
    target_resolution: int | None = None        # max resolution across the series' files
    latest_air_date: str | None = None
    user_rating: float | None = None            # household Trakt show rating 0-10
    sonarr_rating: float | None = None
    trakt_rating: float | None = None
    trakt_votes: int | None = None
    credits: dict = field(default_factory=dict)
    # GROUP C3 — Trakt-related neighbour tvdb ids (daemon-cached), set by the service
    related_tvdb_ids: tuple[int, ...] | None = None
    watchability_score: float | None = None
    watchability_percentile: float | None = None
    # fraction (0..1) of the series' EPISODE files watchable in a preferred language
    # (each episode counted individually — a dub/sub on only some episodes does NOT
    # pass the whole series). Feeds the proportional file-aware G1 penalty.
    language_consumable_fraction: float | None = None


@dataclass(frozen=True)
class EpisodeFeatureRow:
    """One Sonarr episode file row (carries the broadcast series score)."""
    series_id: int | None = None
    episode_file_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    series_title: str | None = None
    is_pilot: bool = False
    is_watched: bool = False
    watch_count: int = 0
    percent_complete: float = 0.0
    last_watched_at: str | None = None
    air_date_utc: str | None = None
    size_bytes: float | None = None
    runtime_seconds: float | None = None
    resolution: int | None = None
    keep_policy: str | None = None
    all_household_watched: bool | None = None
    watchability_score: float | None = None
    watchability_percentile: float | None = None
    marked_for_deletion: bool = False

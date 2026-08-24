"""
RadarrCacheMovieFilesManager
============================
Parquet-backed cache of movie file metadata for Radarr libraries.

FRANCHISE ENTRIES
    The first movie in a collection/franchise (determined by smallest
    release year within a collection).  Franchise entries are NEVER
    deleted — they provide the quality/codec fingerprint for the
    collection and represent the user's entry point.

WATCHED FILES
    Movie file metadata for every movie found in Tautulli watch
    history, enriched with watch stats (count, last_watched_at,
    percent_complete).  This is the strongest ML signal: "what
    quality did the user actually choose to consume?"

Schema: see SCHEMA_COLUMNS — flat, ML-ready columns suitable for
feature engineering without further unpacking.

Storage
    ``{key_builder.base_dir}/radarr/{instance}/movie_files.parquet``
    (Snappy-compressed Parquet via pyarrow)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.classification.franchise import (
    build_franchise_file_ids,
    resolve_franchise_entries,
)
from scripts.managers.machine_learning.classification.keep_policy import build_keep_policy_map
from scripts.managers.machine_learning.space.downgrade_planner import UNIVERSE_PROTECT_MIN
from scripts.managers.machine_learning.space.deletion_log import (
    coverage,
    intersection_drift,
)
from scripts.managers.machine_learning.lifecycle.watched_definition import (
    play_is_watched,
    resolve_watched_percent,
)
from scripts.managers.machine_learning.lifecycle.grace_policy import (
    grace_mark,
    grace_window_multiplier,
    movie_grace_decision,
)
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_targets import deletions_disabled_reason, deletions_enabled


class RadarrCacheMovieFilesManager(BaseManager, ComponentManagerMixin):

    CACHE_MAX_AGE = 172_800   # 48 hours
    GRACE_HOURS = 24          # 24h grace before deletion

    # ── Parquet schema ──────────────────────────────────────────────────────────
    SCHEMA_COLUMNS = [
        # Identity
        "movie_id", "movie_file_id", "tmdb_id", "imdb_id",
        "title", "original_title", "year", "instance",
        # Classification
        "genres",                  # JSON array string
        "keywords",                # JSON array string
        "certification",
        "original_language",
        "spoken_languages",        # JSON array string
        "production_countries",    # JSON array string
        "collection_name",
        "collection_tmdb_id",
        # People (pipe-separated)
        "director_names", "producer_names", "writer_names",
        "composer_names",           # Original Music Composer
        "cinematographer_names",    # Director of Photography
        "editor_names",
        # cast_* are THREE PARALLEL ARRAYS: index i of each describes the same actor.
        # Nothing structurally enforces that -- they are three independent pipe-joined
        # strings -- so any writer must rewrite all three together or clear the ones it
        # cannot supply. _extract_people builds them from one sorted list (safe);
        # refresh_enrichment used to rewrite only cast_names (not safe -- see ENRICH_COLS).
        # A literal "|" in a name or character would also shift one array relative to the
        # others; not observed, but the encoding permits it.
        "cast_names",               # top 10 billed actors
        "cast_characters",          # parallel pipe-separated
        "cast_order",               # parallel billing integers -- read by people_matrix's
                                    # billing decay, so a shift silently mis-weights affinity
        # Studio
        "studio", "production_companies",  # JSON array string
        # Release
        "runtime_minutes", "in_cinemas_date", "physical_release_date",
        "digital_release_date", "added_at",
        # Ratings
        "imdb_rating", "imdb_votes",
        "tmdb_rating", "tmdb_vote_count",
        "metacritic_score", "rotten_tomatoes_score",
        "trakt_rating", "trakt_vote_count",
        "popularity",
        # File/Technical
        "relative_path", "path", "size_bytes", "date_added",
        "quality_name", "quality_source", "resolution",
        "video_codec", "video_bitrate", "video_fps", "video_bit_depth",
        "width", "height", "scan_type",
        "hdr", "hdr_type",
        "audio_codec", "audio_channels", "audio_bitrate",
        "audio_languages", "audio_stream_count",
        "subtitles", "release_group", "scene_name", "edition",
        "custom_formats",      # pipe-separated format names
        # Lifecycle
        "is_franchise_entry", "is_watched", "watch_count",
        "plays",               # UNGATED play count. ``watch_count`` is gated on the
                               # completion bar; ``plays`` is every recorded playback.
        "last_watched_at", "percent_complete",
        "marked_for_deletion", "available_until",
        "keep_policy",         # "keep_forever" | "keep_movie" | "universe" | None
        "universe_name",       # pipe-sep universe labels, e.g. "mcu" or "dc|mcu"
        "quality_action",      # "downgrade" | "upgrade" | None — pending quality change
        # Radarr metadata
        "monitored", "has_file",
        "quality_profile_id", "quality_profile_name",
        "quality_cutoff_not_met",
        "tags",           # JSON array of tag IDs
        "tag_labels",     # pipe-separated labels
        # Decision ledger — populated every run (incl. dry_run) so the Parquet is
        # a queryable "what the system would do, and why".
        "watchability_score",  # 0-100 affinity score (SpacePressure.refresh_scores)
        "watchability_percentile",  # 0-100 rank within library (watch-likelihood Option 1)
        "watchability_breakdown",   # JSON flat dict of every signal-group contribution
                                    # (A1..G4 + _total_raw/_total_final) — explains the score
        "planned_action",      # "delete" | "downgrade" | "upgrade" | None
        "plan_reason",         # human-readable why
        "plan_reclaim_gb",     # +GiB freed (delete/downgrade) / -GiB consumed (upgrade)
    ]

    _NUMERIC_COLUMNS = (
        "movie_id", "movie_file_id", "tmdb_id",
        "year", "runtime_minutes",
        "imdb_rating", "imdb_votes",
        "tmdb_rating", "tmdb_vote_count",
        "metacritic_score", "rotten_tomatoes_score",
        "trakt_rating", "trakt_vote_count",
        "popularity",
        "size_bytes", "resolution",
        "video_bitrate", "video_fps", "video_bit_depth",
        "width", "height",
        "audio_channels", "audio_bitrate", "audio_stream_count",
        "watch_count", "plays", "percent_complete",
        "quality_profile_id", "collection_tmdb_id",
        "watchability_score", "watchability_percentile", "plan_reclaim_gb",
    )

    # ── Init ────────────────────────────────────────────────────────────────────

    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        **kwargs,
    ):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.manager = manager

        # Resolve radarr_api (RadarrInstanceManager)
        _api = kwargs.get("radarr_api") or getattr(manager, "radarr_api", None)
        if _api is not None and not hasattr(_api, "_make_request"):
            _api = None
        if _api is None and self.registry:
            try:
                _radarr_mgr = self.registry.get("manager", "RadarrManager")
                _api = getattr(_radarr_mgr, "radarr_api", None) if _radarr_mgr else None
            except Exception:
                pass
        self.radarr_api = _api

        self.instance_manager = (
            kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        )

        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(manager, "dry_run", None)
        if _dry_run is None and self.registry:
            try:
                _root = self.registry.get("manager", "RadarrManager")
                _dry_run = getattr(_root, "dry_run", None) if _root else None
            except Exception:
                pass
        # Final fallback: check the global Main/root manager so that a top-level
        # dry_run=True is never silently ignored by a sub-manager constructed
        # without an explicit kwarg.
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"RadarrManager, or Main. Refusing to initialize without an explicit value "
                f"from config.json to prevent accidental destructive operations."
            )
        self.dry_run = bool(_dry_run)
        if self.dry_run:
            self.logger.log_debug(f"🛡️ {self.__class__.__name__} dry_run=True — no destructive operations will run")

        # Run-scoped write-through cache of the loaded movie_files dataframe, keyed by
        # resolved instance name. load() serves a copy from here instead of re-reading
        # the parquet on every orchestration task (~8-11 reads/instance otherwise);
        # save() writes through to disk AND refreshes it. reset_run_cache() clears it at
        # the start of each orchestration / coordinator run. The copies in/out keep
        # behaviour identical to always re-reading from disk.
        self._df_cache: dict = {}

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__}")

    # ── Instance resolution ─────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _all_instances(self) -> "list[str]":
        """Every configured Radarr instance name (empty when unresolvable)."""
        api = self.radarr_api
        if api is not None and hasattr(api, "get_all_radarr_apis"):
            try:
                return list(api.get_all_radarr_apis().keys())
            except Exception:
                pass
        try:
            cfg = self.config or {}
            names = list((cfg.get("radarr_instances") or {}).keys())
            if names:
                return names
        except Exception:
            pass
        return []

    # ── Cross-instance mirrors (one physical file, two Radarr records) ──────────

    @staticmethod
    def _movie_file_size(movie: dict):
        """The movie's file size in bytes, or None when it has no file."""
        if not movie.get("hasFile"):
            return None
        size = (movie.get("movieFile") or {}).get("size")
        try:
            return int(size) if size else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _movie_file_resolution(movie: dict) -> int:
        q = (((movie.get("movieFile") or {}).get("quality") or {}).get("quality") or {})
        try:
            return int(q.get("resolution") or 0)
        except (TypeError, ValueError):
            return 0

    def _authoritative_instance(self, resolution: int, instances: "list[str]") -> str:
        """Which instance SHOULD own a file of this resolution, given
        ``radarr_instances_categorized`` ({"720p": …, "1080p": …, "4K": …}).

        A 2160p file belongs on the instance categorised "4K"; anything below belongs on
        the 1080p/720p instance. When the categorised instance is not in the group at all
        (or there is no categorisation), fall back to the alphabetically-first name — an
        arbitrary but DETERMINISTIC choice, which is what matters: every instance's
        refresh must reach the same verdict, or two runs would each drop the other's row
        and the title would vanish entirely."""
        cats = {}
        try:
            cats = (self.config or {}).get("radarr_instances_categorized") or {}
        except Exception:
            cats = {}
        want = None
        if isinstance(cats, dict):
            if resolution >= 2160:
                want = cats.get("4K") or cats.get("4k") or cats.get("2160p")
            else:
                want = cats.get("1080p") or cats.get("720p")
        if want and want in instances:
            return want
        return sorted(instances)[0]

    def _mirrored_tmdb_ids(self, instance: str, movies: "list[dict]") -> set:
        """tmdb ids whose file on THIS instance is a byte-identical mirror of a copy
        ANOTHER Radarr instance holds, and for which this instance is NOT the
        authoritative holder — i.e. rows that would double-count one physical file.

        WHY THESE EXIST (root cause). ``routing.movies.4k_policy == "both"`` drives
        ``CrossInstanceMove.relocate``, which imports the standard instance's existing
        2160p into the 4K instance with ``importMode=copy`` — on the shared storage the
        flow is gated on, Radarr HARDLINKS it. That is deliberate make-before-break: the
        source file stays until the standard record's ``retune_baseline`` grabs a ≤1080p
        replacement. Until that grab lands (and for a title with no 1080p release
        available, that is indefinitely), BOTH instances legitimately report
        ``hasFile=True`` for what is ONE file on disk. ``refresh`` is a faithful
        per-instance mirror of Radarr with no cross-instance view, so it wrote both rows
        at full size and every space consumer — greenfield accounting, the delete pool's
        reclaim estimate, storage summaries — counted the bytes twice. Worse, the
        redundant row advertises reclaim that deleting it cannot deliver: unlinking one
        of two hardlinks frees nothing.

        THE GUARD AGAINST FALSE POSITIVES: only BYTE-IDENTICAL sizes qualify. A genuine
        dual-version holding (a 2160p on the 4K instance + a real 1080p baseline on the
        standard one) has two different sizes and is never touched — on this library
        that is 37 of the 43 standard/ultra overlaps.

        FAIL-SAFE: if another instance's movie list is not cached, that instance is
        simply not considered — an unknown neighbour can never cause a row to be
        dropped. Reads only ``radarr.movies.<inst>.full`` from global_cache (already
        populated by run_movie_data_pull), so this costs no API calls.
        """
        others = [i for i in self._all_instances() if i and i != instance]
        if not others or not self.global_cache:
            return set()

        mine: dict = {}
        for m in movies:
            tmdb, size = m.get("tmdbId"), self._movie_file_size(m)
            if tmdb and size:
                mine[int(tmdb)] = (size, self._movie_file_resolution(m))
        if not mine:
            return set()

        holders: dict = {}          # tmdb -> {size: [instance, ...]}
        for other in others:
            try:
                lst = self.global_cache.get(f"radarr.movies.{other}.full") or []
            except Exception:
                continue
            for m in lst:
                tmdb, size = m.get("tmdbId"), self._movie_file_size(m)
                if not tmdb or not size:
                    continue
                tmdb = int(tmdb)
                if tmdb in mine and mine[tmdb][0] == size:
                    holders.setdefault(tmdb, {}).setdefault(size, []).append(other)

        mirrors: set = set()
        for tmdb, by_size in holders.items():
            size, resolution = mine[tmdb]
            group = sorted(set(by_size.get(size, [])) | {instance})
            if len(group) < 2:
                continue
            if self._authoritative_instance(resolution, group) != instance:
                mirrors.add(tmdb)
        return mirrors

    # ── Path helpers ─────────────────────────────────────────────────────────────

    def _parquet_path(self, instance: str):
        p = (
            self.global_cache.key_builder.base_dir
            / "radarr"
            / instance
            / "movie_files.parquet"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ── Concat helper ───────────────────────────────────────────────────────────

    @staticmethod
    @timeit("_safe_concat")
    def _safe_concat(df: "pd.DataFrame", df_new: "pd.DataFrame") -> "pd.DataFrame":
        """
        Concatenate two schema-conformant DataFrames without triggering the
        FutureWarning about all-NA column dtype inference.
        """
        if df_new.empty:
            return df

        if df.empty:
            all_cols = list(df.columns) + [c for c in df_new.columns if c not in df.columns]
            return df_new.reindex(columns=all_cols)

        all_cols = list(df.columns) + [c for c in df_new.columns if c not in df.columns]

        na_df     = [c for c in df.columns     if df[c].isna().all()]
        na_df_new = [c for c in df_new.columns if df_new[c].isna().all()]

        left  = df.drop(columns=na_df)         if na_df     else df
        right = df_new.drop(columns=na_df_new) if na_df_new else df_new

        result = pd.concat([left, right], ignore_index=True)
        return result.reindex(columns=all_cols)

    # ── Franchise file ID helper ─────────────────────────────────────────────────

    @staticmethod
    @timeit("_build_franchise_file_ids")
    def _build_franchise_file_ids(df: "pd.DataFrame") -> "frozenset":
        """
        Return the frozenset of ``movie_file_id`` values that must NEVER be deleted.

        Two categories of protection:

        1. Real franchise entries: is_franchise_entry=True AND movie_file_id not NaN.
        2. De-facto franchise: for collections with no resolved franchise entry,
           include the earliest-year watched movie's file_id.
        """
        # Delegated to the brain (classification.franchise.build_franchise_file_ids).
        return build_franchise_file_ids(df)

    # ── Formatting helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _fmt_bytes(n: "int | float | None") -> str:
        if n is None or n != n:
            return "0 B"
        n = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ── People extraction ────────────────────────────────────────────────────────

    @staticmethod
    @timeit("_extract_people")
    def _extract_people(credits_dict: dict) -> dict:
        """
        Parse credits dict (from Radarr movie detail) into flat pipe-separated columns.
        Returns dict with keys: director_names, producer_names, writer_names,
        composer_names, cinematographer_names, editor_names,
        cast_names, cast_characters, cast_order.
        """
        crew = credits_dict.get("crew") or []
        cast = credits_dict.get("cast") or []

        directors, producers, writers, composers, cinematographers, editors = [], [], [], [], [], []

        for member in crew:
            job = (member.get("job") or "").strip()
            dept = (member.get("department") or "").strip().lower()
            name = (member.get("name") or "").strip()
            if not name:
                continue
            if job == "Director":
                directors.append(name)
            elif dept == "production" and "producer" in job.lower():
                producers.append(name)
            elif dept == "writing" or job in ("Screenplay", "Story", "Writer"):
                writers.append(name)
            elif job == "Original Music Composer":
                composers.append(name)
            elif job == "Director of Photography":
                cinematographers.append(name)
            elif job == "Editor":
                editors.append(name)

        # Top 10 cast sorted by order
        sorted_cast = sorted(
            [c for c in cast if c.get("name")],
            key=lambda c: c.get("order", 9999)
        )[:10]

        return {
            "director_names":       "|".join(directors) or None,
            "producer_names":       "|".join(producers) or None,
            "writer_names":         "|".join(writers) or None,
            "composer_names":       "|".join(composers) or None,
            "cinematographer_names": "|".join(cinematographers) or None,
            "editor_names":         "|".join(editors) or None,
            "cast_names":           "|".join(c.get("name", "") for c in sorted_cast) or None,
            "cast_characters":      "|".join(c.get("character", "") for c in sorted_cast) or None,
            "cast_order":           "|".join(str(c.get("order", "")) for c in sorted_cast) or None,
        }

    # ── Media info extraction ────────────────────────────────────────────────────

    @staticmethod
    @timeit("_extract_media_info")
    def _extract_media_info(media_info_dict: dict) -> dict:
        """Flatten mediaInfo dict from Radarr moviefile record into tech-spec columns."""
        m = media_info_dict or {}
        hdr_val = m.get("videoDynamicRange") or ""
        return {
            "video_codec":       m.get("videoCodec"),
            "video_bitrate":     m.get("videoBitrate"),
            "video_fps":         m.get("videoFps"),
            "video_bit_depth":   m.get("videoBitDepth"),
            "width":             m.get("width"),
            "height":            m.get("height"),
            "scan_type":         m.get("scanType"),
            "hdr":               bool(hdr_val),
            "hdr_type":          m.get("videoDynamicRangeType") or None,
            "audio_codec":       m.get("audioCodec"),
            "audio_channels":    m.get("audioChannels"),
            "audio_bitrate":     m.get("audioBitrate"),
            "audio_languages":   m.get("audioLanguages"),
            "audio_stream_count": m.get("audioStreamCount"),
            "subtitles":         m.get("subtitles"),
        }

    # ── Row builder ──────────────────────────────────────────────────────────────

    @timeit("_build_row")
    def _build_row(
        self,
        movie: dict,
        movie_file: dict,
        watch_data: dict,
        tag_label_map: dict,
        quality_profile_map: dict,
        is_franchise_entry: bool,
        keep_policy: str | None,
        universe_name: str | None = None,
    ) -> dict:
        """Build a single parquet row dict from Radarr movie + file data."""
        movie_id    = movie.get("id")
        file_id     = movie_file.get("id")
        tmdb_id     = movie.get("tmdbId")
        imdb_id     = movie.get("imdbId")
        title       = movie.get("title")
        orig_title  = movie.get("originalTitle")
        year        = movie.get("year")

        # Classification
        movie_file_quality = movie_file.get("quality") or {}
        qq = (movie_file_quality.get("quality") or {})
        media_info = movie_file.get("mediaInfo") or {}

        # Collection — Radarr v4/v5 payloads name the field 'title', v3 'name'; read
        # both (v4+ has NO 'name' key, which left collection_name all-NULL on modern
        # instances and silently starved the collection scoring signals + saga-credit
        # collection features that consume this column).
        coll = movie.get("collection") or {}
        coll_name     = coll.get("title") or coll.get("name")
        coll_tmdb_id  = coll.get("tmdbId")

        # Genres / keywords
        genres   = movie.get("genres") or []
        keywords = movie.get("keywords") or []

        # People
        credits = movie.get("credits") or {}
        people  = self._extract_people(credits)

        # Studio
        studio = movie.get("studio")
        prod_companies = movie.get("productionCompanies") or []

        # Ratings
        ratings     = movie.get("ratings") or {}
        imdb_rat    = (ratings.get("imdb") or {})
        tmdb_rat    = (ratings.get("tmdb") or {})
        mc_rat      = (ratings.get("metacritic") or {})
        rt_rat      = (ratings.get("rottenTomatoes") or {})
        trakt_rat   = (ratings.get("trakt") or {})

        # Media info
        mi = self._extract_media_info(media_info)

        # Tags
        tag_ids  = movie.get("tags") or []
        tag_lbls = "|".join(tag_label_map.get(tid, str(tid)) for tid in tag_ids)

        # Quality profile
        qp_id   = movie.get("qualityProfileId")
        qp_name = quality_profile_map.get(qp_id, "")

        # Custom formats
        cf = movie_file.get("customFormats") or []
        cf_names = "|".join(f.get("name", "") for f in cf if f.get("name"))

        # Watch data. ``watch_count`` counts WATCHES, not plays (``_fetch_watch_map``
        # already applied lifecycle.watched_definition), so this derivation now means
        # what Plex and Tautulli mean — identical to the Sonarr episode path.
        watch_count     = watch_data.get("watch_count", 0)
        plays           = watch_data.get("plays", 0)
        last_watched_at = watch_data.get("last_watched_at")
        pct_complete    = watch_data.get("percent_complete")
        # ``is_watched`` means ENGAGED, not COMPLETED. ``watch_count`` stays the
        # completion-gated count. The old ``watch_count > 0`` made a film played to
        # 80% twenty times read as *never watched*, and downgrade_planner's
        # never-watched branch then stepped it to the 720p floor on that basis.
        # Set lifecycle.watched_definition.engagement_is_watched=false to restore
        # the completion-only rule byte-for-byte.
        is_watched      = watch_count > 0 or (self._engagement_is_watched() and plays > 0)

        row = {
            "movie_id":             movie_id,
            "movie_file_id":        file_id,
            "tmdb_id":              tmdb_id,
            "imdb_id":              imdb_id,
            "title":                title,
            "original_title":       orig_title,
            "year":                 year,
            "instance":             None,  # filled by caller
            "genres":               json.dumps(genres) if genres else None,
            "keywords":             json.dumps(keywords) if keywords else None,
            "certification":        movie.get("certification"),
            "original_language":    (movie.get("originalLanguage") or {}).get("name") if isinstance(movie.get("originalLanguage"), dict) else movie.get("originalLanguage"),
            "spoken_languages":     json.dumps(movie.get("spokenLanguages") or []) or None,
            "production_countries": json.dumps(movie.get("productionCountries") or []) or None,
            "collection_name":      coll_name,
            "collection_tmdb_id":   coll_tmdb_id,
            **people,
            "studio":               studio,
            "production_companies": json.dumps([c.get("name") for c in prod_companies if c.get("name")]) or None,
            "runtime_minutes":      movie.get("runtime"),
            "in_cinemas_date":      movie.get("inCinemas"),
            "physical_release_date": movie.get("physicalRelease"),
            "digital_release_date": movie.get("digitalRelease"),
            "added_at":             movie.get("added"),
            "imdb_rating":          imdb_rat.get("value"),
            "imdb_votes":           imdb_rat.get("votes"),
            "tmdb_rating":          tmdb_rat.get("value"),
            "tmdb_vote_count":      tmdb_rat.get("votes"),
            "metacritic_score":     mc_rat.get("value"),
            "rotten_tomatoes_score": rt_rat.get("value"),
            "trakt_rating":         trakt_rat.get("value"),
            "trakt_vote_count":     trakt_rat.get("votes"),
            "popularity":           movie.get("popularity"),
            "relative_path":        movie_file.get("relativePath"),
            "path":                 movie_file.get("path"),
            "size_bytes":           movie_file.get("size"),
            "date_added":           movie_file.get("dateAdded"),
            "quality_name":         qq.get("name"),
            "quality_source":       qq.get("source"),
            "resolution":           qq.get("resolution") or mi.get("height"),
            **mi,
            "release_group":        movie_file.get("releaseGroup"),
            "scene_name":           movie_file.get("sceneName"),
            "edition":              movie_file.get("edition"),
            "custom_formats":       cf_names or None,
            "is_franchise_entry":   is_franchise_entry,
            "is_watched":           is_watched,
            "watch_count":          watch_count,
            "plays":                plays,
            "last_watched_at":      last_watched_at,
            "percent_complete":     pct_complete,
            "marked_for_deletion":  False,
            "available_until":      None,
            "keep_policy":          keep_policy,
            "universe_name":        universe_name,
            "quality_action":       None,
            "monitored":            movie.get("monitored"),
            "has_file":             movie.get("hasFile"),
            "quality_profile_id":   qp_id,
            "quality_profile_name": qp_name,
            "quality_cutoff_not_met": movie_file.get("qualityCutoffNotMet"),
            "tags":                 json.dumps(tag_ids) if tag_ids else None,
            "tag_labels":           tag_lbls or None,
        }
        return row

    # ── Load / Save ─────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("load_movie_files")
    def load(self, instance: str) -> pd.DataFrame:
        cache = getattr(self, "_df_cache", None)
        if cache is not None and instance in cache:
            return cache[instance].copy()   # copy-out: callers may mutate without saving
        path = self._parquet_path(instance)
        if path.exists():
            try:
                df = pd.read_parquet(path)
                for col in self._NUMERIC_COLUMNS:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                if cache is not None:
                    cache[instance] = df.copy()
                return df
            except Exception as e:
                self.logger.log_warning(
                    f"Could not read movie_files.parquet for '{instance}': {e}"
                )
        # Do NOT cache the empty fallback — let the next call re-attempt the disk read.
        return pd.DataFrame(columns=self.SCHEMA_COLUMNS)

    @LoggerManager().log_function_entry
    @timeit("save_movie_files")
    def save(self, instance: str, df: pd.DataFrame) -> bool:
        path = self._parquet_path(instance)
        try:
            df_out = df.sort_values(
                ["title", "year"],
                na_position="last",
            ).reset_index(drop=True)
            df_out.to_parquet(path, index=False, engine="pyarrow", compression="snappy")
            # Write-through: refresh the run cache with the SORTED df_out (a copy) so a
            # later load() is indistinguishable from re-reading this file from disk, and
            # a caller still mutating its local df after save() cannot alter the snapshot.
            cache = getattr(self, "_df_cache", None)
            if cache is not None:
                cache[instance] = df_out.copy()
            # debug: fired 13× per run across instances/stages — pure bookkeeping.
            self.logger.log_debug(
                f"Movie file cache saved for '{instance}': "
                f"{len(df_out)} rows -> {path.name}"
            )
            return True
        except Exception as e:
            self.logger.log_warning(
                f"Failed to save movie_files.parquet for '{instance}': {e}"
            )
            return False

    def reset_run_cache(self) -> None:
        """Drop the run-scoped movie_files dataframe cache so the next load() reads
        fresh from disk. Called at the start of each Radarr orchestration run (and
        defensively by the space coordinator) to bound the cache to a single run."""
        self._df_cache = {}

    # ── Enrichment broadcast (Radarr twin of episode_files.refresh_enrichment) ───
    def _get_movie_cache(self):
        """Lazily build (and cache) the TraktMovieCacheManager gz reader."""
        cached = getattr(self, "_movie_cache", None)
        if cached is None:
            try:
                from scripts.managers.services.trakt.movies.cache import TraktMovieCacheManager
                cached = TraktMovieCacheManager(
                    logger=self.logger, config=self.config,
                    global_cache=self.global_cache, registry=self.registry,
                    dry_run=self.dry_run,
                )
            except Exception as e:
                self.logger.log_debug(f"[MovieEnrich] movie cache unavailable: {e}")
                cached = False
            self._movie_cache = cached
        return cached or None

    def refresh_enrichment(self, instance: str) -> int:
        """Broadcast per-movie CAST/CREW + Trakt rating from the enrich daemon's per-tmdbId
        movie buckets onto movie_files rows — the Radarr twin of
        ``episode_files.refresh_enrichment``. The daemon ALREADY fetches + caches
        ``movies/{tmdb}/people`` + ``movie_ratings``; the Radarr BULK /movie payload omits
        credits, so without this merge cast/crew stay 0%. CACHE-ONLY (the daemon owns
        fetching → zero Trakt calls here). Genres are left untouched (Radarr supplies them).
        Best-effort: a movie the daemon hasn't enriched yet gets None columns this run and
        fills in later. Persisted even in dry_run (a non-destructive annotation)."""
        from scripts.managers.factories.daemons.bucket_merge import (
            flatten_trakt_people,
            trakt_rating_cols,
        )
        instance = self._resolve_instance(instance)
        df = self.load(instance)
        if df.empty or "tmdb_id" not in df.columns:
            return 0
        cache = self._get_movie_cache()
        if cache is None:
            return 0

        cols_by_tmdb: dict[int, dict] = {}
        n_people = 0
        for tmdb in pd.to_numeric(df["tmdb_id"], errors="coerce").dropna().unique():
            t = int(tmdb)
            people  = cache.get_people(t)
            ratings = cache.get_ratings(t)
            if not people and not ratings:
                continue
            if people.get("cast"):
                n_people += 1
            cols_by_tmdb[t] = {**flatten_trakt_people(people), **trakt_rating_cols(ratings)}
        if not cols_by_tmdb:
            return 0

        # cast_characters + cast_order are PARALLEL ARRAYS to cast_names -- position i in
        # each describes the same actor. They must be rewritten together or not at all.
        #
        # THE BUG THIS FIXES: this list used to carry cast_names WITHOUT its two parallels,
        # so every enrichment pass replaced the cast list with Trakt's while leaving
        # Radarr's characters and billing positions in place. Different source, different
        # ordering, different length -- misaligned by construction, on every run. Downstream
        # that means `people_matrix`'s billing decay (1/(1+0.25*rank)) weights TRAKT's
        # actors by RADARR's billing order, and any actor->character zip pairs the wrong two.
        #
        # Including them here is correct whether or not flatten_trakt_people supplies them:
        # if it does, all three are rewritten from one source; if it does not, `.get()`
        # returns None and the stale parallels are CLEARED rather than left describing a
        # cast list that no longer exists. Both outcomes are aligned; the old one never was.
        ENRICH_COLS = ("cast_names", "cast_characters", "cast_order",
                       "director_names", "producer_names", "writer_names",
                       "composer_names", "trakt_rating", "trakt_vote_count")
        _t = pd.to_numeric(df["tmdb_id"], errors="coerce")
        for col in ENRICH_COLS:
            df[col] = _t.map(
                lambda v, _c=col: (cols_by_tmdb.get(int(v)) or {}).get(_c) if pd.notna(v) else None
            ).astype(object)
        self.save(instance, df)
        self.logger.log_info(
            f"[MovieEnrich] '{instance}': enriched {len(cols_by_tmdb)} movie(s) "
            f"({n_people} with daemon cast/crew) -> movie rows")
        return len(cols_by_tmdb)

    # ── Tautulli helpers ────────────────────────────────────────────────────────

    # ── Watch-history sweep tunables ────────────────────────────────────────────
    # A single get_history call returns the most RECENT N rows across every media
    # type. At length=5000 with an episodic household that surfaced ~163 distinct
    # movie titles against a 2000-movie library, so ~97% of the library could only
    # ever read watch_count=0 and scored as never-watched. The sweep now filters
    # server-side to movies and walks ``start`` until a short page ends it.
    _HISTORY_PAGE          = 1000   # rows per get_history call
    _HISTORY_MAX_PAGES     = 250    # hard stop (250k movie plays) so a bad
                                    # recordsFiltered can never loop forever
    _METADATA_TOPUP_BUDGET = 750    # NEW rating_keys resolved to tmdb per run

    def _engagement_is_watched(self) -> bool:
        """Whether an ENGAGED play (not just a completed one) sets ``is_watched``.

        Default True. False restores the historical completion-only rule.
        """
        if getattr(self, "_engagement_flag", None) is None:
            try:
                _wd = ((self.config or {}).get("lifecycle", {}) or {}).get(
                    "watched_definition", {}) or {}
                self._engagement_flag = bool(_wd.get("engagement_is_watched", True))
            except Exception:
                self._engagement_flag = True
        return self._engagement_flag

    def _resolve_tmdb_by_rating_key(self, api, rating_keys: set) -> dict:
        """``{rating_key(str): tmdb_id(int)}`` via the shared Tautulli metadata index.

        The index is 7-day cached and carries its own negative cache for keys Plex
        re-scanned away, so on the steady state this is a cache read. The per-run
        budget bounds the FIRST sweep after pagination landed — when years of
        rating_keys the index has never seen appear at once. Anything past the
        budget resolves on a later run and falls back to the title join meanwhile,
        so the map is never worse than the pre-pagination behaviour.
        """
        out: dict = {}
        if not rating_keys:
            return out
        try:
            from scripts.managers.services.tautulli.metadata import TautulliMetadataManager
            meta = None
            if self.registry:
                try:
                    meta = self.registry.get("manager", "TautulliMetadataManager")
                except Exception:
                    meta = None
            if meta is None or not getattr(meta, "tautulli_api", None):
                meta = TautulliMetadataManager(
                    logger=self.logger, config=self.config,
                    global_cache=self.global_cache, registry=self.registry,
                    tautulli_api=api,
                )
            keys = sorted(str(rk) for rk in rating_keys)
            budget = self._METADATA_TOPUP_BUDGET
            # Hand the manager a BOUNDED slice (that is what it tops up), then look
            # every key up in the FULL merged index it returns — keys past the
            # budget still resolve if a previous run already cached them.
            index = meta.get_metadata_index_cached(keys[:budget] if budget > 0 else keys)
            for rk in keys:
                tmdb = ((index or {}).get(rk) or {}).get("tmdb_id")
                if tmdb:
                    try:
                        out[rk] = int(tmdb)
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            self.logger.log_debug(
                f"[MovieFiles] tmdb resolution unavailable, title join only: {e}")
        return out

    def _merge_trakt_history(self, aggregated: dict, titles_for_key: dict) -> None:
        """Fold Trakt's movie history into the Tautulli watch map, keyed by tmdb.

        WHY: Tautulli only serves what its OWN history retains - here 6 months,
        398 movie plays across 181 titles against a ~2000-movie library, so ~91%
        of the library reads ``watch_count=0`` and lands in downgrade_planner's
        never-watched branch. Trakt keeps years (2020-> on this account). This is
        the feed that closes that gap.

        MERGE RULE - ``max()``, never ``+=``. Plex scrobbles to Trakt, so a single
        watch appears in BOTH feeds; summing would double-count every corroborated
        play. ``max()`` also makes the merge MONOTONIC: it can only ever RAISE a
        count, so even a bad Trakt fetch can make a title look more valuable - it
        can never cause one to be deleted or downgraded that would otherwise have
        survived. That is the safe direction for a feed we are still proving out.

        Tautulli stays authoritative for ``percent_complete``: Trakt carries no
        completion figure (its scrobbler applied one upstream, before the row
        ever existed).

        Disable with ``scoring.trakt_history_merge.enabled = false``.
        """
        _cfg = ((self.config or {}).get("scoring", {}) or {}).get("trakt_history_merge", {}) or {}
        if not bool(_cfg.get("enabled", True)):
            return
        mgr = None
        try:
            mgr = self.registry.get("manager", "TraktHistoryManager") if self.registry else None
        except Exception:
            mgr = None
        if mgr is None or not hasattr(mgr, "history_dataframe"):
            return

        df = mgr.history_dataframe("movies")
        if df is None or df.empty:
            return
        df = df[df["tmdb_id"].notna()]
        if df.empty:
            return

        # One row per film: how many plays Trakt has logged, when the last one
        # was, and a title for the fallback lookup key.
        stats = df.groupby("tmdb_id").agg(
            trakt_plays=("play_id", "size"),
            last_watched=("watched_at", "max"),
            title=("title", "first"),
        )

        added = raised = 0
        for tmdb, row in stats.iterrows():
            key    = int(tmdb)
            plays  = int(row["trakt_plays"])
            _ts    = row["last_watched"]
            iso    = _ts.isoformat() if pd.notna(_ts) else None
            title  = row["title"]
            rec    = aggregated.get(key)
            if rec is None:
                # A film Tautulli has no memory of at all.
                aggregated[key] = {
                    "watch_count": plays, "plays": plays,
                    "last_watched_at": iso, "percent_complete": 0,
                }
                if title:
                    titles_for_key.setdefault(key, title)
                added += 1
                continue
            _before = rec["watch_count"]
            rec["watch_count"] = max(rec["watch_count"], plays)
            rec["plays"]       = max(rec["plays"], plays)
            if iso and (rec["last_watched_at"] is None or iso > rec["last_watched_at"]):
                rec["last_watched_at"] = iso
            if title:
                titles_for_key.setdefault(key, title)
            if rec["watch_count"] > _before:
                raised += 1

        self.logger.log_info(
            f"\U0001f4ca Trakt history merge: {len(stats)} film(s) with Trakt plays - "
            f"+{added} Tautulli had never seen, {raised} existing count(s) raised; "
            f"watch map now {len(aggregated)} title(s)."
        )

    def _get_movie_history_pages(self, api, inst_name: str) -> list:
        """Every movie history row for one Tautulli instance, paginated."""
        rows: list = []
        start = 0
        for _ in range(self._HISTORY_MAX_PAGES):
            try:
                response = api.get_history(
                    length=self._HISTORY_PAGE, start=start, media_type="movie"
                )
            except Exception as e:
                self.logger.log_warning(
                    f"Tautulli '{inst_name}' history request failed at offset {start}: {e}"
                )
                break
            entries = ((response or {}).get("response") or {}).get("data", {})
            if isinstance(entries, dict):
                entries = entries.get("data", [])
            if not isinstance(entries, list) or not entries:
                break
            rows.extend(entries)
            if len(entries) < self._HISTORY_PAGE:
                break                      # short page → end of history
            start += self._HISTORY_PAGE
        else:
            self.logger.log_warning(
                f"Tautulli '{inst_name}': history sweep hit the "
                f"{self._HISTORY_MAX_PAGES}-page safety stop; watch counts may be partial."
            )
        return rows

    @timeit("_fetch_watch_map")
    def _fetch_watch_map(self, instance: str) -> dict:
        """
        Try to get movie watch history from Tautulli via registry.
        Returns {} if unavailable.

        Keys: tmdb id (int) where the play's rating_key resolved, PLUS the movie
        title (str) for every record — the same dict object is published under both,
        so a caller may look up by either. tmdb is preferred: it survives Plex
        re-scans and title drift, which a bare title match does not.
        Values: dict with watch_count/plays/last_watched_at/percent_complete.
        """
        try:
            from scripts.managers.services.tautulli.instances.api import TautulliAPI as TautulliInstanceAPI

            tautulli_config = (self.config or {}).get("tautulli", {})
            if not tautulli_config:
                return {}

            if all(isinstance(v, str) for v in tautulli_config.values()):
                instance_configs: dict[str, dict] = {"default": tautulli_config}
            else:
                instance_configs = {
                    k: v for k, v in tautulli_config.items() if isinstance(v, dict)
                }

            # WATCHES, not plays — the same bar the Sonarr episode path applies
            # (lifecycle.watched_definition.play_is_watched: Tautulli's own
            # ``watched_status``, else ``percent_complete >= watched_threshold.percent``).
            # The two producers used to inline the SAME threshold-free ``+= 1``; they now
            # share one definition so a movie and an episode cannot disagree about what
            # "watched" means. ``percent_complete`` / ``last_watched_at`` stay
            # threshold-free (raw playback facts — see watched_definition's docstring).
            _watch_pct = resolve_watched_percent(self.config)
            aggregated: dict[str, dict] = defaultdict(
                lambda: {"watch_count": 0, "plays": 0,
                         "last_watched_at": None, "percent_complete": 0}
            )
            _sub_threshold = 0

            titles_for_key: dict = {}
            _rows = 0

            for inst_name, inst_config in instance_configs.items():
                try:
                    api = TautulliInstanceAPI(
                        logger=self.logger,
                        instance_config=inst_config,
                        cache=self.global_cache,
                    )
                except Exception as e:
                    self.logger.log_warning(
                        f"Tautulli '{inst_name}' client construction failed: {e}"
                    )
                    continue

                entries = self._get_movie_history_pages(api, inst_name)
                if not entries:
                    continue

                # Resolve rating_key → tmdb ONCE per instance, then key the aggregate
                # on tmdb. The old bare-title key silently merged or dropped plays
                # whenever Plex and Radarr spelled a title differently.
                _rks = {str(e.get("rating_key")) for e in entries if e.get("rating_key")}
                tmdb_by_rk = self._resolve_tmdb_by_rating_key(api, _rks)

                for entry in entries:
                    # Belt-and-braces: the server-side media_type filter is the
                    # primary gate, this catches a Tautulli that ignores it.
                    if entry.get("media_type") != "movie":
                        continue
                    title = entry.get("title") or entry.get("grandparent_title")
                    played = entry.get("date")
                    pct    = entry.get("percent_complete", 0)
                    tmdb   = tmdb_by_rk.get(str(entry.get("rating_key") or ""))
                    if tmdb:
                        key = int(tmdb)
                    elif title:
                        key = f"t:{title}"        # unresolved → title-keyed fallback
                    else:
                        continue
                    rec = aggregated[key]
                    if title:
                        titles_for_key.setdefault(key, title)
                    rec["plays"] += 1
                    _rows += 1
                    if play_is_watched(entry, threshold_pct=_watch_pct):
                        rec["watch_count"] += 1
                    else:
                        _sub_threshold += 1
                    rec["percent_complete"] = max(rec["percent_complete"], pct or 0)
                    if played:
                        ts = datetime.fromtimestamp(int(played), tz=timezone.utc).isoformat()
                        if rec["last_watched_at"] is None or ts > rec["last_watched_at"]:
                            rec["last_watched_at"] = ts

            # Trakt keeps years of history where Tautulli keeps only what it has
            # retained. Fold it in BEFORE publishing so both the tmdb and title
            # keys below see the merged counts. Fully wrapped - a Trakt failure
            # must never cost us the Tautulli map we already built.
            try:
                self._merge_trakt_history(aggregated, titles_for_key)
            except Exception as e:
                self.logger.log_debug(
                    f"[MovieFiles] Trakt history merge skipped: {e}")

            # Publish under BOTH the tmdb id and the title. tmdb-keyed records are
            # emitted first so a title collision resolves to the tmdb-backed record
            # rather than an unresolved title-only one.
            out: dict = {}
            for key, rec in aggregated.items():
                if not isinstance(key, int):
                    continue
                out[key] = rec
                _t = titles_for_key.get(key)
                if _t:
                    out.setdefault(_t, rec)
            for key, rec in aggregated.items():
                if isinstance(key, int):
                    continue
                _t = titles_for_key.get(key) or str(key)[2:]
                if _t:
                    out.setdefault(_t, rec)

            if out:
                _watched  = sum(1 for v in aggregated.values() if v["watch_count"] > 0)
                _engaged  = sum(1 for v in aggregated.values() if v["plays"] > 0)
                _by_tmdb  = sum(1 for k in aggregated if isinstance(k, int))
                self.logger.log_info(
                    f"📊 Tautulli movie watch map: {_rows} play(s) → {len(aggregated)} "
                    f"title(s) ({_by_tmdb} tmdb-keyed, {len(aggregated) - _by_tmdb} "
                    f"title-only); watched bar ≥{_watch_pct:g}% (or watched_status=1) — "
                    f"{_watched} count as WATCHED, {_engaged} engaged; "
                    f"{_sub_threshold} sub-threshold play(s) recorded as sampled only"
                )
            return out
        except Exception as e:
            self.logger.log_debug(f"Tautulli watch map unavailable: {e}")
            return {}

    # ── Keep policy map ──────────────────────────────────────────────────────────

    @timeit("_build_keep_policy_map")
    def _build_keep_policy_map(
        self, movies: list[dict], tag_label_map: dict
    ) -> tuple[dict[int, str | None], dict[int, str | None]]:
        """
        Build keep-policy and universe-name maps from tag assignments.

        Priority (highest first):
          "keep" | "keep-forever"                    → "keep_forever"
          "keep-movie"                               → "keep_movie"
          "keep-universe" | "keep-universe-<name>"   → "keep_universe"
            NEVER deleted. Quality-change only (downgrade when tight,
            upgrade when space available).
          bare "universe" (without keep- prefix)     → "universe"
            Can be upgraded/downgraded AND deleted as absolute last resort
            after all non-universe movies have been exhausted.

        Universe label resolution (in order):
          1. Suffix of "keep-universe-<name>"  e.g. "keep-universe-mcu" → "mcu"
          2. Known franchise hint tags on the same movie
          3. Falls back to "universe"

        Returns:
            (policy_map, universe_name_map)
        """
        # Delegated to the brain (classification.keep_policy.build_keep_policy_map).
        return build_keep_policy_map(movies, tag_label_map)

    @timeit("_resolve_membership_maps")
    def _resolve_membership_maps(
        self, instance: str, movies: list[dict], tag_label_map: dict
    ) -> tuple[dict[int, str | None], dict[int, str | None]]:
        """Mode-aware ``(keep_policy_map, universe_name_map)`` for the Parquet stamp —
        the tagless-universe seam (quality.universe_membership).

        Top-level config ``universe_membership``:
          * "tags" (DEFAULT)  — delegates VERBATIM to ``_build_keep_policy_map`` (same
            function, same args): byte-identical maps AND logs.
          * "derived"/"hybrid" — untagged movies gain universe membership from TMDB
            collections / learned franchise maps / mdblist universe lists with BARE-
            universe policy (``keep_policy='universe'``: grouping, saga credit, the
            universe quality ladder, downgrade-first; deletable as last resort). The
            ``keep-universe`` never-delete pin remains EXPLICIT-TAG-ONLY — derived
            membership can never mint ``keep_universe``. Logs ONE line per instance
            (mode + membership counts by source) and stashes the counts blob for the
            universe audit / future GUI.
        """
        # Lazy import (house pattern for cross-package reads, cf. space_pressure's
        # universe_order readers): keeps this module's import graph unchanged.
        from scripts.managers.services.radarr.quality.universe_membership import (
            MODE_TAGS,
            build_membership_maps,
            gather_derived_maps,
            membership_counts_key,
            membership_mode,
        )

        mode = membership_mode(self.config)
        if mode == MODE_TAGS:
            return self._build_keep_policy_map(movies, tag_label_map)

        derived = gather_derived_maps(self.global_cache)
        policy_map, universe_name_map, counts = build_membership_maps(
            movies, tag_label_map, mode=mode,
            franchise_maps=derived["franchise_maps"],
            mdblist_maps=derived["mdblist_maps"],
        )
        self.logger.log_info(
            f"[Universe] '{instance}' membership mode={mode}: {counts['members']} universe "
            f"member(s) — {counts['tag']} tag, {counts['collection']} TMDB collection, "
            f"{counts['franchise']} franchise-map, {counts['mdblist']} mdblist; "
            f"{counts['keep_universe']} keep-universe (tag-pinned, never deleted), "
            f"{counts['bare_universe']} bare-universe (deletable last resort)."
        )
        if self.global_cache:
            try:
                self.global_cache.set(membership_counts_key(instance), counts)
            except Exception as e:
                self.logger.log_debug(
                    f"[Universe] could not stash membership counts for '{instance}': {e}"
                )
        return policy_map, universe_name_map

    # ── Franchise entry resolution ───────────────────────────────────────────────

    @staticmethod
    @timeit("_resolve_franchise_entries")
    def _resolve_franchise_entries(movies: list[dict]) -> set[int]:
        """
        Return set of movie_ids that are franchise entries (earliest year per collection).
        Movies not in any collection are never franchise entries.
        """
        # Delegated to the brain (classification.franchise.resolve_franchise_entries).
        return resolve_franchise_entries(movies)

    # ── Refresh from API ─────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("refresh_movie_files")
    def refresh(self, instance: str, persist: bool = True) -> dict:
        """
        Fetch all movie + file data from Radarr, build parquet rows, save.
        Returns stats dict.

        When ``persist=False`` the freshly built DataFrame is NOT written to
        disk; it is stashed on ``self._refreshed_df`` so ``run()`` can hand it
        directly to ``apply_grace_period`` and avoid a redundant Parquet write
        (a refresh-save immediately followed by a grace-period-save).
        """
        instance = self._resolve_instance(instance)
        stats = {"movies": 0, "with_file": 0, "rows_built": 0, "saved": False}
        self._refreshed_df = None

        if self.radarr_api is None:
            self.logger.log_warning(
                f"radarr_api not available — cannot refresh movie_files for '{instance}'"
            )
            return stats

        # ── Movies: prefer global_cache (populated by run_movie_data_pull) ───────────────────
        # run_movie_data_pull runs before us in the orchestration pipeline and
        # stores the full 20k list.  Re-using it here saves one slow API call.
        movies: list[dict] = []
        if self.global_cache:
            movies = self.global_cache.get(f"radarr.movies.{instance}.full") or []
        if not movies:
            movies = self.radarr_api._make_request(
                instance, "movie", fallback=[]
            ) or []
        stats["movies"] = len(movies)

        if not movies:
            self.logger.log_info(f"No movies found in Radarr for '{instance}'")
            return stats

        # ── Tags + quality profiles: prefer global_cache ─────────────────────────────────
        if self.global_cache:
            raw_tags     = self.global_cache.get(f"radarr.tags.{instance}") or []
            raw_profiles = self.global_cache.get(f"radarr.quality.{instance}") or []
        else:
            raw_tags     = []
            raw_profiles = []
        if not raw_tags:
            raw_tags = self.radarr_api._make_request(instance, "tag", fallback=[]) or []
        if not raw_profiles:
            raw_profiles = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
        tag_label_map      = {t["id"]: t["label"] for t in raw_tags if t.get("id") is not None}
        quality_profile_map = {p["id"]: p["name"] for p in raw_profiles if p.get("id") is not None}

        # Watch history from Tautulli
        watch_map = self._fetch_watch_map(instance)

        # Resolve keep policies, universe names + franchise entries. Membership is
        # mode-aware (config universe_membership): "tags" (default) = tag-only,
        # byte-identical; "derived"/"hybrid" also derive TAGLESS bare-universe
        # membership from TMDB collections / franchise maps / mdblist lists.
        keep_policy_map, universe_name_map = self._resolve_membership_maps(
            instance, movies, tag_label_map
        )
        franchise_ids = self._resolve_franchise_entries(movies)

        # Cross-instance mirrors: one physical (hardlinked) file surfaced by two Radarr
        # records. The non-authoritative side is SKIPPED so the parquet stops advertising
        # bytes that do not exist and reclaim that deleting cannot deliver. Because
        # refresh is a full rebuild, previously-written mirror rows self-heal out of the
        # parquet on this very pass — no hand-editing, no migration.
        mirror_tmdbs: set = set()
        try:
            mirror_tmdbs = self._mirrored_tmdb_ids(instance, movies)
        except Exception as e:
            self.logger.log_debug(f"[MovieFiles] mirror detection skipped for '{instance}': {e}")
        stats["cross_instance_mirrors"] = 0
        mirror_bytes = 0

        rows: list[dict] = []
        for movie in movies:
            if not movie.get("hasFile"):
                continue

            _tmdb = movie.get("tmdbId")
            if _tmdb and int(_tmdb) in mirror_tmdbs:
                stats["cross_instance_mirrors"] += 1
                mirror_bytes += self._movie_file_size(movie) or 0
                continue

            mid = movie.get("id")
            movie_file = movie.get("movieFile") or {}
            if not movie_file:
                # Try fetching file separately
                file_list = self.radarr_api._make_request(
                    instance, f"moviefile?movieId={mid}", fallback=[]
                ) or []
                movie_file = file_list[0] if file_list else {}

            if not movie_file:
                continue

            stats["with_file"] += 1

            title        = movie.get("title", "")
            # tmdb first — stable across Plex re-scans and title drift. Title is the
            # fallback for plays whose rating_key never resolved to a tmdb id.
            watch_data   = {}
            if _tmdb:
                try:
                    watch_data = watch_map.get(int(_tmdb)) or {}
                except (TypeError, ValueError):
                    watch_data = {}
            if not watch_data:
                watch_data = watch_map.get(title) or {}
            is_fe        = mid in franchise_ids
            keep_pol     = keep_policy_map.get(mid)
            universe_name = universe_name_map.get(mid)

            row = self._build_row(
                movie=movie,
                movie_file=movie_file,
                watch_data=watch_data,
                tag_label_map=tag_label_map,
                quality_profile_map=quality_profile_map,
                is_franchise_entry=is_fe,
                keep_policy=keep_pol,
                universe_name=universe_name,
            )
            row["instance"] = instance
            rows.append(row)
            stats["rows_built"] += 1

        if stats["cross_instance_mirrors"]:
            self.logger.log_info(
                f"[MovieFiles] '{instance}': skipped {stats['cross_instance_mirrors']} "
                f"cross-instance mirror row(s) ({self._fmt_bytes(mirror_bytes)}) — same tmdb, "
                f"byte-identical file already counted on the instance that owns that "
                f"resolution tier. Dual-version copies (differing sizes) are unaffected.")

        if rows:
            df_new = pd.DataFrame(rows, columns=self.SCHEMA_COLUMNS)
            for col in self._NUMERIC_COLUMNS:
                if col in df_new.columns:
                    df_new[col] = pd.to_numeric(df_new[col], errors="coerce")

            # The Parquet is a read-only mirror of the Radarr library (built from
            # GET requests only), so it materialises even in dry_run — same as the
            # JSON data-pull caches. dry_run still gates the actual *arr writes
            # (downgrade/delete), which live in delete_marked_files / SpacePressure.
            self._refreshed_df = df_new
            if persist:
                stats["saved"] = self.save(instance, df_new)
                if self.dry_run:
                    self.logger.log_debug(
                        f"[dry_run] Built movie_files cache for '{instance}' "
                        f"({len(df_new)} rows) — local write only, no Radarr changes."
                    )
                # GLD-DEL-08 — audit AFTER the save so both sides read the same state.
                # `movies` is the library walk this refresh already fetched, so the
                # *arr side costs nothing extra.
                self._check_parquet_drift(instance, df_new, movies)
                # GLD-DEL-12 — resolve any upgrades we triggered last pass. Delegated to
                # the space-pressure manager, which owns the worklist because it also
                # owns the trigger; splitting them would put half a contract in each
                # service (P-E).
                try:
                    _sp = self.registry.get("manager", "RadarrSpacePressureManager") \
                        if getattr(self, "registry", None) else None
                    if _sp is not None:
                        _sp._reconcile_upgrade_intents(instance, df_new)
                except Exception as e:
                    self.logger.log_debug(
                        f"  movie upgrade reconcile not reachable for '{instance}': {e}")

        return stats

    def _arr_file_index(self, movies):
        """``{movie_file_id: size_bytes}`` from the library walk already in hand.

        Radarr embeds ``movieFile`` in ``GET /movie``, so unlike the Sonarr twin —
        which reads per-series episodefile caches — this needs no cache lookup and no
        extra request. The list passed in is the SAME one the refresh built its rows
        from, which is what makes the comparison a like-for-like audit rather than
        two reads of different moments.
        """
        out = {}
        for m in (movies or []):
            if not isinstance(m, dict):
                continue
            mf = m.get("movieFile") or {}
            fid, sz = mf.get("id"), mf.get("size")
            if fid is None:
                continue
            try:
                out[int(fid)] = int(sz or 0)
            except (TypeError, ValueError):
                continue
        return out

    def _check_parquet_drift(self, instance, df, movies=None):
        """Audit the parquet against Radarr for the files it CLAIMS — GLD-DEL-08.

        ⚠️ INTERSECTION ONLY — see the Sonarr twin for the full rationale. Comparing
        totals would breach on a coverage gap that is by design; what matters is
        whether the parquet is WRONG about a file it does track (orphaned rows, stale
        byte counts), because both corrupt every space decision reading them.

        Radarr's mirror is built from a FULL library walk rather than a watch-history
        working set, so its coverage should sit near 1.0 — which makes the coverage
        gauge more meaningful here than on the TV side, where 65% is normal. A
        coverage collapse on THIS side would be a real signal.

        There is no rebuild step: the *arr side is the walk itself, so re-reading it
        would only repeat the same fetch. A breach here means the parquet diverged
        from the very list it was built from, which is a defect in the build, not
        staleness a resync could fix.

        Never raises.
        """
        try:
            if df is None or df.empty or "movie_file_id" not in df.columns:
                return None
            owned = df[df["movie_file_id"].notna() & df["size_bytes"].notna()]
            if owned.empty:
                return None
            pq = {}
            for fid, sz in zip(owned["movie_file_id"], owned["size_bytes"]):
                try:
                    pq[int(fid)] = int(sz)
                except (TypeError, ValueError):
                    continue
            arr = self._arr_file_index(movies)

            result = intersection_drift(pq, arr)
            cov = coverage(len(pq), len(arr))
            if cov.get("ratio") is not None:
                self.logger.log_debug(
                    f"  \U0001f4d0 parquet coverage '{instance}': {cov['parquet']:,} of "
                    f"{cov['arr']:,} library files ({cov['ratio'] * 100:.0f}%) — gauge only")
            if result.get("breach"):
                self.logger.log_warning(
                    f"  ⚠\ufe0f parquet drift on '{instance}' — " + "; ".join(result["reasons"])
                    + " — the parquet disagrees with the library walk it was built from. "
                      "See GLD-DEL-08.")
            elif result.get("reasons"):
                self.logger.log_debug(
                    f"  parquet audit '{instance}': {result['reasons'][0]}")
            return result
        except Exception as e:
            try:
                self.logger.log_debug(f"  parquet audit skipped for '{instance}': {e}")
            except Exception:
                pass
            return None

    # ── Grace period ─────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("apply_grace_period")
    def apply_grace_period(self, instance: str, df: pd.DataFrame | None = None) -> pd.DataFrame:
        """
        Mark/clear marked_for_deletion based on available_until.
        NEVER marks franchise entries or keep_policy movies.
        Saves and returns the updated DataFrame.

        ``df`` may be passed by a caller that already holds the freshly built
        rows in memory (``run`` does this) to skip a redundant load+save round
        trip. When omitted, the Parquet is loaded from disk.
        """
        instance = self._resolve_instance(instance)
        if df is None:
            df = self.load(instance)
        if df.empty:
            return df

        grace_td = timedelta(hours=self.GRACE_HOURS)
        now      = datetime.now(tz=timezone.utc)
        # Optional score-scaled grace window (config grace_window_ramp; default {} ->
        # multiplier is exactly 1.0 -> byte-identical fixed window).
        _grace_ramp = (self.config or {}).get("grace_window_ramp", {}) or {}

        # Ensure correct column dtypes
        if "available_until" in df.columns and df["available_until"].dtype != object:
            df["available_until"] = df["available_until"].astype(object)
        if "marked_for_deletion" in df.columns and df["marked_for_deletion"].dtype not in (bool, "bool"):
            df["marked_for_deletion"] = (
                df["marked_for_deletion"].infer_objects(copy=False).fillna(0).astype(bool)
            )

        franchise_file_ids = self._build_franchise_file_ids(df)

        for idx in df.index:
            # Read the guard signals (optional columns are col-guarded; is_watched /
            # last_watched_at are core schema), then let the brain decide the precedence.
            is_fe = bool(df.at[idx, "is_franchise_entry"]) if "is_franchise_entry" in df.columns else False
            fid   = df.at[idx, "movie_file_id"] if "movie_file_id" in df.columns else None
            keep_protected = (
                "keep_policy" in df.columns
                and df.at[idx, "keep_policy"] in ("keep_forever", "keep_movie", "universe")
            )
            lw = df.at[idx, "last_watched_at"]
            decision = movie_grace_decision(
                is_franchise_entry=is_fe,
                fid_franchise_protected=bool(pd.notna(fid) and fid in franchise_file_ids),
                keep_protected=keep_protected,
                is_watched=df.at[idx, "is_watched"],
                has_last_watched=bool(lw),
            )
            if decision == "clear":     # franchise / franchise-file / keep — never mark
                df.at[idx, "marked_for_deletion"] = False
                continue
            if decision == "skip":      # watched but no last-watched stamp — leave as-is
                continue

            row_td = grace_td
            if _grace_ramp:             # score-scaled window (favourites longer, forgettables shorter)
                _pct = df.at[idx, "watchability_percentile"] if "watchability_percentile" in df.columns else None
                row_td = grace_td * grace_window_multiplier(_pct, _grace_ramp)
            au, marked = grace_mark(lw, row_td, now)
            if au is not None:          # parse ok → set the window; on failure leave as-is
                df.at[idx, "available_until"]     = au
                df.at[idx, "marked_for_deletion"] = marked

        # ── Decision ledger ──────────────────────────────────────────────────────
        # Grace-marking sets marked_for_deletion (deletion ELIGIBILITY) but does NOT
        # itself delete: Radarr defers grace-marked movies to the space-pressure
        # target loop (run_deletions) / coordinator, which delete only under pressure
        # and stamp planned_action='delete' on EXACTLY what they remove (persisted in
        # dry_run). Pre-stamping every grace-marked row here made the dry-run ledger
        # report ~hundreds of GB of movies that would NOT be deleted this run. So we
        # only RESET any prior 'delete' plan; the real deleter re-stamps its selection.
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        # Reloaded all-null Parquet columns come back as float64; force the
        # string-plan columns to object so assignments don't trip pandas'
        # incompatible-dtype FutureWarning.
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)
        # Reset BOTH space-pressure-owned plan actions — 'delete' AND 'downgrade'.
        # Space-pressure short-circuits when free >= the floor, so on a healthy run it
        # never reaps its OWN stale stamps; the deleter/downgrader re-stamps exactly its
        # current selection under pressure (incl. bare-"universe" last-resort downgrades),
        # so a prior run's stamps must not linger in the dry-run ledger. (Universe
        # 'upgrade' stamps are reaped by the universe pass itself — it always runs.)
        _prior_sp = df["planned_action"].isin(["delete", "downgrade"])
        df.loc[_prior_sp, "planned_action"]  = None
        df.loc[_prior_sp, "plan_reason"]     = None
        df.loc[_prior_sp, "plan_reclaim_gb"] = None

        # Grace-period marks (marked_for_deletion / available_until) are local
        # cache state, not Radarr changes — persist them even in dry_run so the
        # SpacePressure deletion pass can be previewed. Actual file deletes stay
        # gated in delete_marked_files.
        self.save(instance, df)
        return df

    # ── Delete marked files ──────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("delete_marked_files")
    def delete_marked_files(self, instance: str) -> dict:
        """
        Delete movie files from Radarr for every row marked for deletion.

        FRANCHISE ENTRIES ARE NEVER DELETED — hard franchise guard unconditionally
        clears any flags and logs a warning.
        """
        instance = self._resolve_instance(instance)
        stats: dict = {
            "checked":              0,
            "deleted":              0,
            "failed":               0,
            "skipped_franchise":    0,
            "skipped_keep":         0,
            "skipped_universe":     0,
            "skipped_no_file":      0,
            "bytes_freed":          0.0,
            "dry_run":              self.dry_run,
        }

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available — cannot delete files")
            return stats
        if not deletions_enabled(self.config):
            # HARD SAFETY GATE: no operator-set free_space_limit → no deletions.
            # Grace MARKING is unaffected; only this destructive pass skips.
            self.logger.log_warning(
                f"[MovieFiles] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
                "skipping the grace-marked movie delete pass."
            )
            return stats

        df = self.load(instance)
        if df.empty or "marked_for_deletion" not in df.columns:
            return stats

        marked_mask = df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
        if not marked_mask.any():
            return stats

        franchise_file_ids = self._build_franchise_file_ids(df)

        for idx in df.index[marked_mask]:
            stats["checked"] += 1
            title = df.at[idx, "title"] or f"movie {df.at[idx, 'movie_id']}"

            # HARD FRANCHISE GUARD
            is_fe = bool(df.at[idx, "is_franchise_entry"]) if "is_franchise_entry" in df.columns else False
            if is_fe:
                self.logger.log_warning(
                    f"FRANCHISE GUARD: '{title}' is marked for deletion but is a "
                    f"franchise entry — clearing flag. FRANCHISE ENTRIES ARE NEVER DELETED."
                )
                df.at[idx, "marked_for_deletion"] = False
                stats["skipped_franchise"] += 1
                continue

            # Franchise file guard (secondary)
            fid_pre = df.at[idx, "movie_file_id"]
            if pd.notna(fid_pre) and fid_pre in franchise_file_ids:
                self.logger.log_warning(
                    f"FRANCHISE FILE GUARD: '{title}' (movieFileId={int(fid_pre)}) "
                    f"is the franchise entry file — clearing deletion flag."
                )
                df.at[idx, "marked_for_deletion"] = False
                stats["skipped_franchise"] += 1
                continue

            # Universe guard — never delete; eligible for quality changes only
            if "keep_policy" in df.columns:
                policy = df.at[idx, "keep_policy"]
                if policy == "universe":
                    uni = df.at[idx, "universe_name"] if "universe_name" in df.columns else None
                    self.logger.log_warning(
                        f"UNIVERSE GUARD: '{title}' belongs to universe '{uni or 'unknown'}' — "
                        f"clearing deletion flag. Universe movies are never deleted; "
                        f"use quality downgrade/upgrade instead."
                    )
                    df.at[idx, "marked_for_deletion"] = False
                    stats["skipped_universe"] += 1
                    continue

            # Keep-policy guard (secondary / defence-in-depth)
            if "keep_policy" in df.columns:
                policy = df.at[idx, "keep_policy"]
                if policy in ("keep_forever", "keep_movie"):
                    self.logger.log_warning(
                        f"KEEP GUARD: '{title}' has keep_policy='{policy}' — "
                        f"clearing deletion flag."
                    )
                    df.at[idx, "marked_for_deletion"] = False
                    stats["skipped_keep"] += 1
                    continue

            # Borrowed franchise/universe credit guard: an UNTAGGED hot-saga member (recency-decayed
            # credit >= UNIVERSE_PROTECT_MIN, from TMDB-collection membership — NOT a keep tag) resists
            # grace deletion just as plan_movie_downgrades makes it resist a quality step-down. Unlike the
            # PERMANENT guards above, this protection DECAYS, so the flag is left set (not cleared): once
            # the saga goes stale the credit drops below the floor and the still-marked movie is deleted.
            if "universe_credit" in df.columns:
                _uc = df.at[idx, "universe_credit"]
                try:
                    if _uc is not None and pd.notna(_uc) and float(_uc) >= UNIVERSE_PROTECT_MIN:
                        self.logger.log_info(
                            f"UNIVERSE CREDIT GUARD: '{title}' carries hot franchise/universe credit "
                            f"({float(_uc):.1f}) — sparing from deletion (deletion must not be more "
                            f"aggressive than the downgrade it resists)."
                        )
                        stats["skipped_universe"] += 1
                        continue
                except (TypeError, ValueError):
                    pass

            fid = df.at[idx, "movie_file_id"]
            if pd.isna(fid):
                stats["skipped_no_file"] += 1
                continue

            fid = int(fid)
            _sz   = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
            _sz_f = float(_sz) if pd.notna(_sz) else 0.0

            # ── Build reason string for logging ─────────────────────────────
            _lw   = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
            _avail = df.at[idx, "available_until"] if "available_until" in df.columns else None
            _wc   = df.at[idx, "watch_count"] if "watch_count" in df.columns else 0
            _pct  = df.at[idx, "percent_complete"] if "percent_complete" in df.columns else None
            _lw_str = str(_lw)[:10] if _lw else "unknown"
            _pct_str = f"{int(_pct)}%" if _pct is not None and pd.notna(_pct) else "?%"
            reason = (
                f"watched {_wc}x ({_pct_str} complete), "
                f"last watched {_lw_str}, "
                f"grace period expired {str(_avail)[:16] if _avail else 'N/A'}"
            )

            if self.dry_run:
                stats["bytes_freed"] += _sz_f
                self.logger.log_info(
                    f"  🗑️ [dry_run] Would delete: '{title}' ({self._fmt_bytes(_sz_f)}) — {reason}"
                )
                stats["deleted"] += 1
                continue

            try:
                self.radarr_api._make_request(
                    instance,
                    f"moviefile/{fid}",
                    method="DELETE",
                )
                stats["bytes_freed"] += _sz_f
                self.logger.log_info(
                    f"  🗑️ Deleted: '{title}' ({self._fmt_bytes(_sz_f)}) — {reason}"
                )
                stats["deleted"] += 1
            except Exception as e:
                self.logger.log_warning(
                    f"Delete failed for '{title}' (movieFileId={fid}): {e}"
                )
                stats["failed"] += 1

        if stats["checked"]:
            prefix = "[dry_run] " if self.dry_run else ""
            verb   = "Would free" if self.dry_run else "Freed"
            self.logger.log_table(
                ["Outcome", "Count"],
                [
                    ["deleted",            stats["deleted"]],
                    ["failed",             stats["failed"]],
                    ["franchise guard",    stats["skipped_franchise"]],
                    ["universe guard",     stats["skipped_universe"]],
                    ["keep-policy guard",  stats["skipped_keep"]],
                    ["no file id",         stats["skipped_no_file"]],
                ],
                title=f"{prefix}Radarr deletion pass - '{instance}' ({verb} {self._fmt_bytes(stats['bytes_freed'])})",
                caption="Outcome of the grace-marked movie file deletion pass for this instance.",
                descriptions=[
                    "movie files removed from Radarr this pass",
                    "delete calls that errored",
                    "rows skipped because they are franchise entries",
                    "rows skipped because they belong to a universe",
                    "rows skipped by a keep-forever/keep-movie policy",
                    "marked rows with no movie file id to delete",
                ],
            )

        needs_save = stats["deleted"] or stats["skipped_franchise"] or stats["skipped_universe"]
        if needs_save and not self.dry_run:
            self.save(instance, df)

        return stats

    # ── Free space helper ──────────────────────────────────────────────────────

    @timeit("_get_free_space_gb")
    def _get_free_space_gb(self, instance: str) -> float:
        """Free space (GiB) across this instance's disks, deduped by mount."""
        if self.radarr_api is None:
            return float("inf")
        return self.radarr_api.disk_free_gb(instance)

    # ── Full lifecycle run ───────────────────────────────────────────────────────

    # Minimum free space before the grace-period deletion pipeline is allowed
    # to run. Below this threshold the SpacePressureManager handles deletions
    # via its own downgrade-first logic. Above it, normal grace-period
    # expiry still applies so watched files are cleaned up as expected.
    DELETION_MIN_FREE_GB = 25.0

    @LoggerManager().log_function_entry
    @timeit("run_movie_files")
    def run(self, instance: str) -> dict:
        """
        refresh -> apply_grace_period -> delete_marked_files

        Deletion only runs when free space is above DELETION_MIN_FREE_GB.
        Below that threshold the SpacePressureManager owns the deletion
        pipeline (downgrade-first, then delete as last resort).
        """
        instance = self._resolve_instance(instance)
        # Build the cache in memory (persist=False) and hand it straight to
        # apply_grace_period, which writes it once — avoids the redundant
        # refresh-save immediately followed by the grace-period-save.
        refresh_stats = self.refresh(instance, persist=False)
        self.apply_grace_period(instance, df=self._refreshed_df)
        if self._refreshed_df is not None:
            refresh_stats["saved"] = True

        free_gb = self._get_free_space_gb(instance)

        # When free_space_limit is configured it is THE floor, and the
        # SpacePressureManager owns deletion below it via its graduated, target-driven
        # loop (its watched+grace-expired tier consumes the marks we just applied).
        # So here we only mark and delegate — never blanket-delete every marked row.
        try:
            fsl = float(self.config.get("free_space_limit", 0) if self.config else 0) or 0.0
        except (TypeError, ValueError):
            fsl = 0.0
        if fsl > 0:
            self.logger.log_info(
                f"Radarr '{instance}': {free_gb:.1f} GB free (floor {fsl:.0f} GB) — grace marks "
                f"applied; deletion delegated to the space-pressure target loop."
            )
            return refresh_stats

        # Legacy fallback (no free_space_limit set): blanket grace deletion only when
        # critically low, matching the original behaviour.
        if free_gb >= self.DELETION_MIN_FREE_GB:
            self.logger.log_info(
                f"Radarr '{instance}': {free_gb:.1f} GB free — above "
                f"{self.DELETION_MIN_FREE_GB} GB threshold. "
                f"Sufficient space, skipping grace-period deletion."
            )
            return refresh_stats

        delete_stats = self.delete_marked_files(instance)
        return {**refresh_stats, **delete_stats}

    # ── Summary ──────────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_movie_file_summary")
    def get_summary(self, instance: str) -> dict:
        df = self.load(instance)
        if df.empty:
            return {"total_rows": 0, "watched_rows": 0, "franchise_entries": 0}
        universe_mask = (
            (df["keep_policy"] == "universe") if "keep_policy" in df.columns
            else pd.Series(False, index=df.index)
        )
        pending_quality_mask = (
            df["quality_action"].notna() if "quality_action" in df.columns
            else pd.Series(False, index=df.index)
        )
        return {
            "total_rows":             len(df),
            "watched_rows":           int(df["is_watched"].sum()) if "is_watched" in df.columns else 0,
            "franchise_entries":      int(df["is_franchise_entry"].sum()) if "is_franchise_entry" in df.columns else 0,
            "universe_movies":        int(universe_mask.sum()),
            "pending_quality_action": int((universe_mask & pending_quality_mask).sum()),
            "total_size_gb":          round(df["size_bytes"].sum() / 1e9, 2) if "size_bytes" in df.columns else 0.0,
            "codec_dist":             df["video_codec"].value_counts().to_dict() if "video_codec" in df.columns else {},
            "resolution_dist":        df["resolution"].value_counts().to_dict() if "resolution" in df.columns else {},
            "hdr_count":              int(df["hdr"].sum()) if "hdr" in df.columns else 0,
        }

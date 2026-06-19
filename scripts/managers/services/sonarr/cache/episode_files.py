"""
SonarrCacheEpisodeFilesManager
==============================
Parquet-backed cache of episode file metadata focused on two high-signal
subsets of a Sonarr library:

PILOT FILES
    The earliest available episode file (S01E01 or nearest non-special
    equivalent) for every series.  Populated in background batches of
    ``PILOT_BATCH_SIZE`` series per run so startup is never blocked.
    Provides a quality / codec fingerprint for each series.

WATCHED FILES
    Episode file metadata for every episode found in Tautulli watch
    history, enriched with watch stats (count, last_watched_at,
    percent_complete).  This is the strongest ML signal in the system:
    "what quality did the user actually choose to consume?"

Schema: see SCHEMA_COLUMNS — flat, ML-ready columns suitable for
feature engineering without further unpacking.

Storage
    ``{key_builder.base_dir}/sonarr/{instance}/episode_files.parquet``
    (Snappy-compressed Parquet via pyarrow)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import time

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.managers.machine_learning.sizing.size_model import (
    CALIBRATED_MB_PER_MIN,
    estimate_gb_for_profile,
    measured_mb_per_min,
    profile_max_quality,
)
from scripts.managers.machine_learning.acquisition.next_episode_planner import (
    DEFAULT_BUDGET_RAMP,
    DEFAULT_GRADUATED_CAP,
    DEFAULT_RECENCY_GATE,
    build_runtime_lookup,
    episode_cap,
    is_cold_series,
    last_watched_per_series,
    order_series_by_recency,
    series_budget_multiplier,
)
from scripts.managers.machine_learning.acquisition.pilot_stepping import (
    choose_pilot_profile,
    next_pilot_profile,
    next_pilot_profile_descend,
    pilot_backoff_interval,
    pilot_search_due,
    profile_max_resolution,
    rank_pilot_profiles,
)
from scripts.managers.machine_learning.lifecycle.grace_policy import (
    episode_grace_decision,
    grace_mark,
    grace_window_multiplier,
)
from scripts.managers.machine_learning.lifecycle.household_watch import (
    resolve_household_watch,
)
from scripts.managers.machine_learning.space.jit_planner import (
    choose_jit_profile,
    jit_reserve_gb,
    jit_row_skip,
    jit_step_down_pids,
    next_up_grab_candidates,
    target_tier_key,
)
from scripts.support.utilities.watch_likelihood import (
    resolution_cap_for_likelihood,
    watch_likelihood,
)
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import (
    coordinator_owns_deletion, deletions_enabled, space_targets,
)


class SonarrCacheEpisodeFilesManager(BaseManager, ComponentManagerMixin):

    PILOT_BATCH_SIZE  = None   # max *unwatched* series per enrichment run (None = unlimited)
    CACHE_MAX_AGE     = 172_800  # 48 hours
    GRACE_HOURS       = 3        # keep a watched file available this long before deletion
    RECENT_AIR_DAYS   = 30       # never delete an episode that aired within this many days
    PREFETCH_HOURS    = 3.0      # target runtime budget of upcoming episodes to pre-acquire per series
    MIN_FREE_SPACE_GB = 50.0     # last-resort acquire/upgrade floor only (free_space_limit unset AND
                                   # total drive unreadable); normally space_targets uses 25%-of-total
    JIT_MAX_EPISODES  = 3         # max episodes to JIT-upgrade per series per run (prevents upgrading
                                   # entire kids-cartoon library at once despite large runtime budget)
    JIT_RESERVE_PCT   = 0.05      # JIT upgrades must keep at least this fraction of total disk free
    JIT_ACTIVE_WATCH_DAYS = 30    # a series watched within this window is "actively watched" — its
                                   # next-up episodes are NEVER JIT-downgraded (same recency the
                                   # prefetch uses for 'upgrade-eligible'); cold shows still calibrate
    JIT_SEARCH_MAX_WORKERS = 6    # background step-down search runs series CONCURRENTLY (each owns its
                                   # own profile + revert, so series are independent); this bounds how
                                   # many ladders search at once so we overlap the long command waits
                                   # without flooding the one Sonarr instance with EpisodeSearch tasks

    # Fallback size model (MiB per minute) used by JIT space estimates when a
    # quality has no measured samples in the library yet. Now sourced from the
    # shared, library-calibrated table so every estimator in the app agrees.
    JIT_FALLBACK_MB_PER_MIN = CALIBRATED_MB_PER_MIN

    # ── Parquet schema ──────────────────────────────────────────────────────────
    # All columns declared up front so that missing API fields become NaN
    # rather than causing KeyErrors, and the DataFrame schema is stable
    # across partial runs.
    SCHEMA_COLUMNS = [
        # Identity
        "episode_file_id",
        "series_id",
        "series_title",
        "season_number",
        "episode_number",
        # Signal flags
        "is_pilot",
        "is_watched",
        "next_episode",       # True for the next unwatched ep in watch-sequence
        "watch_count",
        "last_watched_at",           # ISO-8601 string (UTC) — most recent any watcher
        "all_household_watched",     # True once every configured household member has watched
        "household_last_watched_at", # ISO-8601 UTC — latest watch time among household members
        "percent_complete",
        # Lifecycle
        "marked_for_deletion", # True when grace period expired; pending Sonarr removal
        "available_until",     # ISO-8601 UTC — last_watched_at + GRACE_HOURS
        "keep_policy",         # "keep_series" | "keep_season" | None — from Sonarr tags
        # File
        "relative_path",
        "path",
        "size_bytes",
        "date_added",
        "air_date_utc",        # ISO-8601 UTC broadcast date from Sonarr
        # Quality label
        "quality_name",
        "quality_source",
        "resolution",
        # Video
        "video_codec",
        "video_bitrate",
        "video_fps",
        "video_bit_depth",
        "width",
        "height",
        "runtime_seconds",
        "scan_type",
        "hdr",
        "hdr_type",
        # Audio
        "audio_codec",
        "audio_channels",
        "audio_languages",
        # Other
        "subtitles",
        "release_group",
        "scene_name",
        "quality_cutoff_not_met",
        "last_synced_at",     # ISO-8601 UTC — when this row was last written/updated
        # Decision ledger — populated every run (incl. dry_run) so the Parquet is
        # a queryable "what the system would do, and why".
        "planned_action",     # "delete" | "upgrade" | "acquire" | None
        "plan_reason",        # human-readable why
        "plan_reclaim_gb",    # +GiB freed (delete) / -GiB consumed (upgrade/acquire)
        # Watchability — per-SERIES score (0-100) broadcast onto every episode row
        # of that series by refresh_scores(); Phase 3/4 sort on it (least valuable
        # first). Computed by trakt.shows.scorer.score_show.
        "watchability_score",
        "watchability_percentile",  # 0-100 rank within library (watch-likelihood Option 1)
        "watchability_breakdown",   # JSON flat dict of every signal-group contribution
                                    # (A1..G4 + _total_raw/_total_final) for the SERIES,
                                    # broadcast onto every episode row — explains the score
        # Enrichment — per-SERIES genres + cast/crew + Trakt rating, broadcast onto
        # every episode row by refresh_enrichment(). Genres from Sonarr (daemon summary
        # fallback); cast/crew from the daemon's per-tvdbId Trakt people bucket. Mirrors
        # the movie_files people columns so cross-medium (TV↔movie) affinity reads one
        # column space (see factories/daemons/bucket_merge.py).
        "genres",
        "cast_names",
        "director_names",
        "producer_names",
        "writer_names",
        "composer_names",
        "trakt_rating",
        "trakt_vote_count",
    ]

    # ── Init ────────────────────────────────────────────────────────────────────

    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        sonarr_cache=None,
        **kwargs,
    ):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.sonarr_cache = sonarr_cache or getattr(manager, "sonarr_cache", None) or self
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.manager = manager

        # ── Resolve sonarr_api (SonarrInstanceManager) ─────────────────────
        # Preference order:
        # 1. Explicit kwarg — the real SonarrInstanceManager when passed down correctly.
        # 2. manager.sonarr_api — works when SonarrCacheManager stores the attr.
        # 3. Registry lookup via SonarrManager — last-resort when the cache layer
        #    does not receive sonarr_api at all (SonarrManager may not pass it).
        # Guard: reject anything that lacks _make_request (e.g. SonarrCacheManager
        #        used as a placeholder) so we don't silently call the wrong object.
        _api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        if _api is not None and not hasattr(_api, "_make_request"):
            _api = None
        if _api is None and self.registry:
            try:
                _sonarr_mgr = self.registry.get("manager", "SonarrManager")
                _api = getattr(_sonarr_mgr, "sonarr_api", None) if _sonarr_mgr else None
            except Exception:
                pass
        self.sonarr_api = _api

        self.instance_manager = (
            kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        )

        # Resolve dry_run — walk the chain: kwargs → parent manager → SonarrManager → Main.
        # Never default to False; raise if unresolvable to prevent silent live-mode execution.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(manager, "dry_run", None)
        if _dry_run is None and self.registry:
            try:
                _root = self.registry.get("manager", "SonarrManager")
                _dry_run = getattr(_root, "dry_run", None) if _root else None
            except Exception:
                pass
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"SonarrManager, or Main. Refusing to initialize without an explicit value "
                f"from config.json to prevent accidental destructive operations."
            )
        self.dry_run = bool(_dry_run)

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__}")

    # ── Instance resolution ─────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        """
        Return a concrete Sonarr instance name.

        Preference order:
        1. ``self.instance_manager.resolve_instance`` (set when the manager is
           wired through the full SonarrManager hierarchy)
        2. ``self.sonarr_api.resolve_instance`` (SonarrInstanceManager doubles
           as both the API gateway and the instance resolver)
        3. The raw string as-is (last resort; prevents None reaching file paths)
        """
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.sonarr_api and hasattr(self.sonarr_api, "resolve_instance"):
            return self.sonarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Path helper ─────────────────────────────────────────────────────────────

    def _parquet_path(self, instance: str):
        """Absolute path to the episode-files Parquet for this instance."""
        p = (
            self.global_cache.key_builder.base_dir
            / "sonarr"
            / instance
            / "episode_files.parquet"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ── Concat helper ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_runtime_s(raw) -> float | None:
        """
        Convert a Sonarr runtime value to seconds.

        Sonarr's mediaInfo.runTime can be:
          - A plain number (int or float) already in seconds: 1420.0
          - A 'MM:SS' string:  '23:40'  → 1420.0
          - A 'H:MM:SS' string: '1:03:40' → 3820.0
          - None / empty string → None
        """
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            return float(raw) if raw else None
        s = str(raw).strip()
        if not s:
            return None
        if ":" in s:
            parts = s.split(":")
            try:
                if len(parts) == 2:          # MM:SS
                    return float(parts[0]) * 60.0 + float(parts[1])
                elif len(parts) == 3:        # H:MM:SS
                    return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
            except (ValueError, TypeError):
                return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    @staticmethod
    @timeit("_safe_concat")
    def _safe_concat(df: "pd.DataFrame", df_new: "pd.DataFrame") -> "pd.DataFrame":
        """
        Concatenate two schema-conformant DataFrames without triggering the
        FutureWarning about all-NA column dtype inference.

        Pandas ≥ 2.1 warns during ``pd.concat`` when **either** operand has a
        column that is entirely NA, because a future version will change how
        the result dtype is inferred in that situation.  The pandas-recommended
        fix is: "exclude the relevant entries before the concat operation."

        Strategy
        --------
        1. If ``df`` is empty, return ``df_new`` reindexed to the full column
           set — no concat needed, no warning possible.
        2. Otherwise, collect the union of both column sets, drop all-NA
           columns from **each** operand independently, concat the trimmed
           frames, then ``reindex`` the result back to the full column set.
           Dropped columns reappear as all-NaN with object dtype, which is
           identical to the old concat behaviour and raises no warning.
        """
        if df_new.empty:
            return df

        # Fast path: empty base frame — no concat, just restore schema columns.
        if df.empty:
            all_cols = list(df.columns) + [c for c in df_new.columns if c not in df.columns]
            return df_new.reindex(columns=all_cols)

        # Union of both column sets (df column order first, then any extras from df_new).
        all_cols = list(df.columns) + [c for c in df_new.columns if c not in df.columns]

        # Drop all-NA columns from each operand independently.
        # Columns that are all-NA in one operand but have values in the other
        # are only dropped from the all-NA side; the values are preserved.
        na_df     = [c for c in df.columns     if df[c].isna().all()]
        na_df_new = [c for c in df_new.columns if df_new[c].isna().all()]

        left  = df.drop(columns=na_df)         if na_df     else df
        right = df_new.drop(columns=na_df_new) if na_df_new else df_new

        result = pd.concat([left, right], ignore_index=True)

        # Restore the full column set (dropped all-NA columns become NaN-filled).
        return result.reindex(columns=all_cols)

    # ── Pilot-file ID helpers ────────────────────────────────────────────────────

    @staticmethod
    @timeit("_build_pilot_file_ids")
    def _build_pilot_file_ids(df: "pd.DataFrame") -> "frozenset":
        """
        Return the frozenset of ``episode_file_id`` values that must **never** be
        deleted.

        Two categories of protection are included:

        1. **Real pilot rows** — ``is_pilot=True`` AND ``episode_file_id`` is not
           NaN.  Their file IDs are added directly.  These are the codec/quality
           fingerprint records created by ``run_pilot_batch``.

        2. **De-facto pilots** — for series that have *only* a stub pilot row
           (``is_pilot=True``, ``episode_file_id=None``) or no pilot row at all.
           In those cases the file ID of the **earliest watched non-pilot episode**
           for that series is added.  This bridges the gap between when the pilot
           batch creates a stub (no file found yet) and when it eventually resolves
           a real pilot file: the first thing the user has ever watched for a
           series is treated as the de-facto pilot and is never deleted.

        The distinction from the old inline approach
        --------------------------------------------
        The previous code only collected file IDs from ``is_pilot=True`` rows.
        When a series had a *stub* pilot (``episode_file_id=None``), the set
        contained no ID for that series, so the pilot-file guard silently failed
        and the watched S01E01 row was marked for deletion.  This method closes
        that gap.
        """
        # Delegated to the brain (classification.guards.build_pilot_file_ids).
        from scripts.managers.machine_learning.classification.guards import (
            build_pilot_file_ids,
        )
        return build_pilot_file_ids(df)

    @timeit("_build_protected_file_ids")
    def _build_protected_file_ids(
        self,
        df: "pd.DataFrame",
        now: "datetime",
        pilot_file_ids: "frozenset | None" = None,
    ) -> "frozenset":
        """
        Return the frozenset of ``episode_file_id`` values that must **never** be
        deleted because **any** episode row backed by that file hits a
        protective guard.

        Why whole-file protection is required
        -------------------------------------
        A single physical file in Sonarr can back several episode rows —
        *multi-episode files* share one ``episodeFileId`` (e.g. an S01E02-E07
        omnibus file).  The per-row guards in :meth:`_do_delete_marked_files`
        only inspect the row currently being processed, so a watched,
        grace-expired episode can trigger ``DELETE episodefile/{id}`` and
        silently destroy a *sibling* episode that is pilot / keep-protected /
        recently-aired / not-yet-watched-by-the-whole-household.  The sibling's
        own guard never fires because its row is unmarked (so the delete loop
        never visits it) or is processed only after the file is already gone.

        This method collapses every guard down to the set of file ids touched,
        so if **any** row sharing a file id is guarded the whole file id is
        protected and none of its rows are deleted.

        Guards mirrored here (same conditions as :meth:`_apply_grace_period`
        and the per-row guards in :meth:`_do_delete_marked_files`):

        * **pilot** — via :meth:`_build_pilot_file_ids` (real + de-facto pilots).
        * **keep_series** — every file id on a ``keep_series`` row.
        * **keep_season** — file ids on rows in the latest non-special season of
          a ``keep_season`` series.
        * **recent-air** — file ids on rows that aired within ``RECENT_AIR_DAYS``.
        * **household** — file ids on rows where ``all_household_watched`` is False.

        ``pilot_file_ids`` may be passed in to avoid recomputing it when the
        caller already has it; otherwise it is built here.

        The predicate computation lives in the brain
        (machine_learning.classification.guards.build_protected_file_ids); this
        service method resolves the two inputs it owns — the pilot file-id set
        (``_build_pilot_file_ids``) and ``RECENT_AIR_DAYS`` — and delegates.
        """
        from scripts.managers.machine_learning.classification.guards import (
            build_protected_file_ids,
        )

        if pilot_file_ids is None:
            pilot_file_ids = self._build_pilot_file_ids(df)
        return build_protected_file_ids(
            df, now, pilot_file_ids, recent_air_days=self.RECENT_AIR_DAYS
        )

    # ── Formatting helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _fmt_bytes(n: "int | float | None") -> str:
        """Format a raw byte count into a compact, human-readable string.

        Examples: 0 B, 512.0 MB, 4.2 GB, 1.1 TB
        """
        if n is None or n != n:   # None or NaN
            return "0 B"
        n = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ── Load / Save ─────────────────────────────────────────────────────────────

    # Columns that should always be float64 in memory.
    # Parquet reads all-NaN columns as object dtype, which causes FutureWarning
    # in pd.concat when combined with DataFrames that have proper numeric types.
    # Explicit casting in load() keeps dtypes stable regardless of row content.
    _NUMERIC_COLUMNS = (
        "episode_file_id", "series_id", "season_number", "episode_number",
        "watch_count", "percent_complete", "size_bytes",
        "video_bitrate", "video_fps", "video_bit_depth",
        "width", "height", "runtime_seconds", "audio_channels", "resolution",
        "plan_reclaim_gb", "watchability_score", "watchability_percentile",
    )

    @LoggerManager().log_function_entry
    @timeit("load_episode_files")
    def load(self, instance: str) -> pd.DataFrame:
        """
        Load the Parquet and return a schema-conformant DataFrame.

        Numeric columns are explicitly cast to float64 after reading so that
        columns which are all-NaN (e.g. ``episode_number`` in pilot-only
        Parquets) come back as float64 rather than object dtype.  This
        prevents FutureWarnings from ``pd.concat`` when merging with freshly
        constructed DataFrames that always have proper numeric types.
        """
        path = self._parquet_path(instance)
        if path.exists():
            try:
                df = pd.read_parquet(path)
                for col in self._NUMERIC_COLUMNS:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                return df
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Could not read episode_files.parquet for '{instance}': {e}"
                )
        return pd.DataFrame(columns=self.SCHEMA_COLUMNS)

    @LoggerManager().log_function_entry
    @timeit("save_episode_files")
    def save(self, instance: str, df: pd.DataFrame) -> bool:
        """
        Persist the episode-file DataFrame to Parquet (Snappy).

        Rows are sorted by ``(series_id, season_number, episode_number)``
        before writing.  Sorting keeps all rows for the same series
        contiguous, which:

        * Improves Snappy compression (repeated numeric values compress better)
        * Enables PyArrow row-group predicate pushdown when filtering by
          ``series_id`` in future readers
        * Makes the file human-readable if inspected with tools like DuckDB
        """
        path = self._parquet_path(instance)
        try:
            df_out = df.sort_values(
                ["series_id", "season_number", "episode_number"],
                na_position="last",
            ).reset_index(drop=True)
            df_out.to_parquet(path, index=False, engine="pyarrow", compression="snappy")
            self.logger.log_info(
                f"💾 Episode file cache saved for '{instance}': "
                f"{len(df_out)} rows → {path.name}"
            )
            return True
        except Exception as e:
            self.logger.log_warning(
                f"⚠️ Failed to save episode_files.parquet for '{instance}': {e}"
            )
            return False

    # ── Watchability scoring (per-series) ────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("refresh_scores")
    def refresh_scores(self, instance: str) -> int:
        """
        Compute a per-SERIES watchability score and broadcast it onto every
        episode row of that series (column ``watchability_score``).

        The Sonarr twin of ``RadarrSpacePressureManager.refresh_scores``. Unlike
        Radarr (1 row ≈ 1 movie), episode_files holds many rows per series, so the
        score is computed once per ``series_id`` and written to all of that
        series' rows. Persisted even in dry_run — the score is a non-destructive
        annotation that the Phase-3 downgrade / Phase-4 coordinator sort on
        ("least valuable first").
        """
        import json

        instance = self._resolve_instance(instance)
        df = self.load(instance)
        if df.empty:
            return 0
        # with_breakdown=True → {series_id: (score, breakdown)}. The score is
        # byte-identical to the no-breakdown path; the flat per-signal-group
        # explanation dict is persisted (broadcast onto every episode row, same as
        # the score) so the advise view can read back WHY a series scored as it did.
        bd_by_series = self._build_show_score_map(df, instance, with_breakdown=True)
        if not bd_by_series:
            return 0
        score_by_series = {sid: sb[0] for sid, sb in bd_by_series.items()}
        breakdown_json_by_series = {
            sid: (json.dumps(sb[1], separators=(",", ":")) if sb[1] else None)
            for sid, sb in bd_by_series.items()
        }
        if "watchability_score" not in df.columns:
            df["watchability_score"] = None
        if "watchability_breakdown" not in df.columns:
            df["watchability_breakdown"] = None
        # Coerce series_id to numeric FIRST so a stray non-numeric value can't make
        # int(s) raise and abort the whole broadcast (which would leave every score
        # stale/empty). Non-numeric → NaN → None score.
        _sid_num = pd.to_numeric(df["series_id"], errors="coerce")
        df["watchability_score"] = _sid_num.map(
            lambda s: score_by_series.get(int(s)) if pd.notna(s) else None
        )
        df["watchability_breakdown"] = _sid_num.map(
            lambda s: breakdown_json_by_series.get(int(s)) if pd.notna(s) else None
        ).astype(object)
        # Percentile rank among DISTINCT series (so long shows don't dominate the
        # rank), broadcast to every episode row — the rank-based input to the
        # watch-likelihood ladder (Option 1). Mirrors Radarr refresh_scores.
        _svals = pd.Series(score_by_series, dtype="float64")
        _pct_by_series = (_svals.rank(pct=True, method="average") * 100).round(1).to_dict()
        df["watchability_percentile"] = _sid_num.map(
            lambda s: _pct_by_series.get(int(s)) if pd.notna(s) else None
        )
        self.save(instance, df)
        vals = list(score_by_series.values())
        self.logger.log_info(
            f"[ShowScore] Scored {len(score_by_series)} series for '{instance}' "
            f"(range: {min(vals)}-{max(vals)})"
        )
        return len(score_by_series)

    @timeit("refresh_enrichment")
    def refresh_enrichment(self, instance: str) -> int:
        """Broadcast per-SERIES enrichment (genres + cast/crew + Trakt rating) onto every
        episode row — the Sonarr twin of the movie_files people/genre columns, so the
        cross-medium next-watch affinity reads TV taste from the same column space.

        Genres come from the Sonarr series object (daemon show summary as fallback);
        cast/crew + Trakt rating come from the enrich daemon's per-tvdbId show buckets
        (read via TraktShowCacheManager). Best-effort: a series the daemon hasn't enriched
        yet gets None columns this run and fills in later. Persisted even in dry_run (a
        non-destructive annotation, like the watchability score)."""
        from scripts.managers.factories.daemons.bucket_merge import show_enrichment_columns

        instance = self._resolve_instance(instance)
        df = self.load(instance)
        if df.empty:
            return 0

        show_cache   = self._get_show_cache()
        series_cache = getattr(self.sonarr_cache, "series", None)
        if series_cache is None:
            self.logger.log_debug("[ShowEnrich] no series cache — skipping enrichment broadcast")
            return 0
        try:
            series_by_id = {
                str(s.get("id")): s
                for s in series_cache.iter_all_series(instance)
                if s.get("id") is not None
            }
        except Exception as e:
            self.logger.log_warning(f"[ShowEnrich] series list unavailable for '{instance}': {e}")
            return 0

        cols_by_series: dict[int, dict] = {}
        n_people = 0
        for sid, series_obj in series_by_id.items():
            tvdb = series_obj.get("tvdbId")
            people  = show_cache.get_people(int(tvdb))  if (show_cache and tvdb) else {}
            ratings = show_cache.get_ratings(int(tvdb)) if (show_cache and tvdb) else {}
            summary = show_cache.get_summary(int(tvdb)) if (show_cache and tvdb) else {}
            if people.get("cast"):
                n_people += 1
            try:
                cols_by_series[int(sid)] = show_enrichment_columns(
                    people=people, ratings=ratings, summary=summary,
                    sonarr_genres=series_obj.get("genres"),
                )
            except (TypeError, ValueError):
                continue
        if not cols_by_series:
            return 0

        ENRICH_COLS = ("genres", "cast_names", "director_names", "producer_names",
                       "writer_names", "composer_names", "trakt_rating", "trakt_vote_count")
        _sid_num = pd.to_numeric(df["series_id"], errors="coerce")
        for col in ENRICH_COLS:
            df[col] = _sid_num.map(
                lambda s, _c=col: (cols_by_series.get(int(s)) or {}).get(_c) if pd.notna(s) else None
            ).astype(object)
        self.save(instance, df)
        self.logger.log_info(
            f"[ShowEnrich] '{instance}': enriched {len(cols_by_series)} series "
            f"({n_people} with daemon cast/crew) → episode rows"
        )
        return len(cols_by_series)

    @timeit("_build_show_score_map")
    def _build_show_score_map(self, df: "pd.DataFrame", instance: str,
                              with_breakdown: bool = False) -> dict:
        """Return ``{series_id: watchability_score}`` for every series in *df*.

        Aggregates the per-episode rows up to series level (watched-episode count,
        recency, rewatch, modal codec, latest air date), joins the Sonarr series
        object for genres/network/cert/ratings, and pulls credits + Trakt audience
        ratings from the daemon's per-tvdbId show cache, then calls ``score_show``.

        When ``with_breakdown`` is True the value is ``(score, breakdown)`` instead
        — the score is byte-identical, only the flat per-signal-group explanation
        dict is added (the persistence path uses this; decision paths don't).
        """
        from scripts.managers.machine_learning.features.show_features import (
            build_show_feature_row, score_show_features,
        )

        (genre_affinity, platform_usage, transcode_stats,
         per_user_affinity, kids_users, adult_users) = self._build_show_scoring_context()
        user_show_ratings = self._build_user_show_rating_map()
        # Group-A4 declared-rating knobs: config.scoring.show_user_rating overrides the
        # gentler-than-movies defaults baked into score_show. Only present keys are
        # forwarded, so the single source of default truth stays in score_show.
        _ur_cfg = ((self.config or {}).get("scoring", {}) or {}).get("show_user_rating", {}) or {}
        ur_kwargs: dict = {}
        for _ck, _param in (("slope", "ur_slope"), ("pos_cap", "ur_pos_cap"),
                            ("neg_cap", "ur_neg_cap"), ("conf_divisor", "ur_conf_divisor")):
            if _ck in _ur_cfg:
                try:
                    ur_kwargs[_param] = float(_ur_cfg[_ck])
                except (TypeError, ValueError):
                    pass
        show_cache   = self._get_show_cache()
        series_cache = getattr(self.sonarr_cache, "series", None)
        # One-shot {id: series_obj} map so each series is an O(1) lookup. Calling
        # get_cached_series_by_id() per group scans every letter bucket each time
        # (O(N) per series → O(N²) over the library).
        series_by_id: dict[str, dict] = {}
        if series_cache:
            try:
                series_by_id = {
                    str(s.get("id")): s
                    for s in series_cache.iter_all_series(instance)
                    if s.get("id") is not None
                }
            except Exception:
                series_by_id = {}
        now = datetime.now(tz=timezone.utc)

        # GROUP C3 — related-graph affinity (config.scoring.related_graph). Build the
        # household watched-set in TVDb space ONCE: any owned series with >=1 watched
        # episode. (v1 = owned watched series; unowned watched shows are not tracked
        # by tvdbId yet.) Disabled/empty -> C3 stays 0 and shows score exactly as before.
        _rg = ((self.config or {}).get("scoring", {}) or {}).get("related_graph", {}) or {}
        related_enabled = bool(_rg.get("enabled", True))
        try:
            related_graph_cap = float(_rg.get("cap", 4.0))
        except (TypeError, ValueError):
            related_graph_cap = 4.0
        # File-aware G1 language gate (oracle-mover, default OFF). When on, a series with
        # a preferred-language audio (dub) OR subtitle (sub) track is not penalised for a
        # foreign original language — fixes dubbed/subbed anime (e.g. Attack on Titan's
        # English dub, Demon Slayer's English subs).
        _lc = ((self.config or {}).get("scoring", {}) or {}).get("language_consumability", {}) or {}
        language_consumability = bool(_lc.get("enabled", False)) if isinstance(_lc, dict) else bool(_lc)
        watched_tvdb_ids: set[int] = set()
        if related_enabled and "is_watched" in df.columns:
            for _wsid, _wrows in df.groupby("series_id", sort=False):
                try:
                    if int((_wrows["is_watched"] == True).sum()) <= 0:   # noqa: E712
                        continue
                    _wso = series_by_id.get(str(int(_wsid))) or {}
                    _wtv = _wso.get("tvdbId")
                    if _wtv:
                        watched_tvdb_ids.add(int(_wtv))
                except Exception:
                    continue

        out: dict[int, int] = {}
        fallbacks = 0
        for series_id, rows in df.groupby("series_id", sort=False):
            try:
                sid = int(series_id)
            except (TypeError, ValueError):
                continue
            try:
                series_obj = series_by_id.get(str(sid)) or {}
                tvdb_id = series_obj.get("tvdbId")

                # ── per-series I/O (kept service-side): credits + Trakt ratings +
                #    the household show rating + the related-neighbour set ──
                credits, trakt_rating, trakt_votes = {}, None, None
                if tvdb_id and show_cache:
                    try:
                        credits = show_cache.get_people(int(tvdb_id)) or {}
                    except Exception:
                        credits = {}
                    try:
                        r = show_cache.get_ratings(int(tvdb_id)) or {}
                        trakt_rating, trakt_votes = r.get("rating"), r.get("votes")
                    except Exception:
                        pass

                user_rating = user_show_ratings.get(int(tvdb_id)) if tvdb_id else None

                # GROUP C3 — this show's Trakt-related neighbour TVDb ids (cache-only).
                related_tvdb_ids = None
                if related_enabled and tvdb_id and show_cache:
                    try:
                        related_tvdb_ids = {
                            int((e.get("ids") or {}).get("tvdb"))
                            for e in (show_cache.get_related(int(tvdb_id)) or [])
                            if isinstance(e, dict) and (e.get("ids") or {}).get("tvdb")
                        }
                    except Exception:
                        related_tvdb_ids = None

                # ML Step 3c: aggregate the episode rows + series object into a typed
                # ShowFeatureRow at the brain boundary, then score it.
                fr = build_show_feature_row(
                    rows, series_obj, now,
                    credits=credits, trakt_rating=trakt_rating, trakt_votes=trakt_votes,
                    user_rating=user_rating, related_tvdb_ids=related_tvdb_ids,
                )
                out[sid] = score_show_features(
                    fr,
                    genre_affinity=genre_affinity,
                    platform_usage=platform_usage,
                    transcode_stats=transcode_stats,
                    per_user_affinity=per_user_affinity,
                    kids_users=kids_users,
                    adult_users=adult_users,
                    watched_tvdb_ids=watched_tvdb_ids,
                    related_graph_cap=related_graph_cap,
                    language_consumability=language_consumability,
                    return_breakdown=with_breakdown,
                    **ur_kwargs,
                )
            except Exception as e:
                out[sid] = (30, {}) if with_breakdown else 30   # neutral fallback, mirrors Radarr _score_row
                fallbacks += 1
                self.logger.log_debug(f"[ShowScore] series {sid} fell back to 30: {e}")
        if fallbacks:
            self.logger.log_warning(
                f"[ShowScore] {fallbacks}/{len(out)} series fell back to neutral 30 "
                f"— see debug log for causes."
            )
        return out

    def _build_show_scoring_context(self):
        """Pull the affinity / device / per-user context shared with the movie
        scorer. Returns (genre_affinity, platform_usage, transcode_stats,
        per_user_affinity, kids_users, adult_users)."""
        gc = self.global_cache
        genre_affinity  = (gc.get("tautulli/affinity") if gc else None) or {}
        platform_usage  = (gc.get("tautulli/platforms") if gc else None) or None
        transcode_stats = (gc.get("tautulli/transcode") if gc else None) or None
        per_user_affinity: dict = {}
        kids_users: list[str] = []
        adult_users: list[str] = []
        try:
            import re
            cfg_groups = (self.config.get("rating_groups", {}) if self.config else {}) or {}
            for group in cfg_groups.values():
                for member in (group.get("members") or []):
                    safe = re.sub(r'[\\/:*?"<>|]', '_', member).strip()
                    ua = gc.get(f"tautulli/users/{safe}/affinity") if gc else None
                    if ua:
                        per_user_affinity[member] = ua
                for member in (group.get("grace_members") or []):
                    kids_users.append(member)
                for member in (group.get("members") or []):
                    if member not in kids_users:
                        adult_users.append(member)
        except Exception:
            pass
        return (genre_affinity, platform_usage, transcode_stats,
                (per_user_affinity or None), kids_users, adult_users)

    def _build_user_show_rating_map(self) -> dict[int, float]:
        """{tvdbId: household Trakt show rating 0-10} from the cached user
        ratings (best-effort, cache-only — no live Trakt call)."""
        out: dict[int, float] = {}
        gc = self.global_cache
        if not gc:
            return out
        # Fallback must match the WRITER (TraktRatingsManager uses .get("username",
        # "default") for the cache-key namespace) — every other Trakt cache key in
        # the app defaults to "default", so a blank username must too or the read
        # silently misses the written key.
        try:
            username = ((self.config.get("trakt", {}) if self.config else {}) or {}).get("username") or "default"
        except Exception:
            username = "default"
        for entry in (gc.get(f"trakt/{username}/ratings/shows") or []):
            try:
                tvdb = ((entry.get("show") or {}).get("ids") or {}).get("tvdb")
                rating = entry.get("rating")
                if tvdb and rating:
                    out[int(tvdb)] = float(rating)
            except Exception:
                continue
        return out

    def _get_show_cache(self):
        """Lazily build (and cache) the TraktShowCacheManager gz reader."""
        cached = getattr(self, "_show_cache", None)
        if cached is None:
            try:
                from scripts.managers.services.trakt.shows.cache import TraktShowCacheManager
                cached = TraktShowCacheManager(
                    logger=self.logger, config=self.config,
                    global_cache=self.global_cache, registry=self.registry,
                    dry_run=self.dry_run,
                )
            except Exception as e:
                self.logger.log_debug(f"[ShowScore] show cache unavailable: {e}")
                cached = False
            self._show_cache = cached
        return cached or None

    # NOTE: the series-row aggregation helpers (_modal_str / _max_int) moved to the
    # brain boundary (machine_learning.features.show_features) in ML Step 3c.

    # ── Cross-service coordinator hooks (Phase 4) ────────────────────────────────
    # build_delete_candidates + delete_selected_episode_files let the
    # SpaceCoordinatorManager merge TV episodes into the combined movie+episode
    # delete pool and delete exactly the file ids it chose (with the whole-file
    # guards), recording each for restore_recovered_episode_deletions.

    _DELETED_EPISODES_KEY = "sonarr/{inst}/deleted_episodes"
    # How long a deletion record may stay unresolved (coords not matching any current
    # Sonarr episode) before restore stops retrying and drops it. 30 days.
    _RESTORE_TRACK_MAX_AGE_S = 30 * 24 * 3600

    @timeit("build_episode_delete_candidates")
    def build_delete_candidates(self, instance: str, df=None) -> list[dict]:
        """Return EPISODE delete-candidates (one per episode_file_id) for the
        coordinator's pool: rows already marked_for_deletion (watched + grace
        expired) whose file isn't whole-file-guarded. Each dict carries the
        per-series watchability_score, size, file id, series id + episode coords."""
        out: list[dict] = []
        if df is None:
            df = self.load(instance)
        if df is None or df.empty or "episode_file_id" not in df.columns or "marked_for_deletion" not in df.columns:
            return out
        marked = (df["marked_for_deletion"] == True)  # noqa: E712 (null-safe)
        if not marked.any():
            return out
        # Refuse to contribute candidates when scores never populated (column absent
        # or entirely empty): the fallback score would rank every marked episode as
        # maximally deletable. Defer to a run where refresh_scores succeeded.
        if "watchability_score" not in df.columns or \
                pd.to_numeric(df["watchability_score"], errors="coerce").notna().sum() == 0:
            self.logger.log_warning(
                f"[EpisodeFiles] '{instance}' watchability_score is empty — refresh_scores "
                f"likely didn't run; yielding NO delete candidates (won't delete on fallback scores)."
            )
            return out
        now = datetime.now(tz=timezone.utc)
        try:
            protected = self._build_protected_file_ids(df, now)
        except Exception as e:
            # NEVER fall back to an empty guard set — that would expose pilot/keep/
            # recent-air/household-protected files to the coordinator's delete pool.
            # Fail safe: yield NO candidates this cycle.
            self.logger.log_error(
                f"[EpisodeFiles] protected-file-id build failed for '{instance}'; "
                f"yielding NO delete candidates this cycle (fail-safe): {e}"
            )
            return out
        seen: set[int] = set()
        for idx in df.index[marked]:
            fid = df.at[idx, "episode_file_id"]
            if pd.isna(fid):
                continue
            fid = int(fid)
            if fid in protected or fid in seen:
                continue
            seen.add(fid)
            size = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0
            sc = df.at[idx, "watchability_score"] if "watchability_score" in df.columns else None
            score = int(sc) if pd.notna(sc) else 5
            sid = df.at[idx, "series_id"]
            sn = df.at[idx, "season_number"] if "season_number" in df.columns else None
            en = df.at[idx, "episode_number"] if "episode_number" in df.columns else None
            title = (df.at[idx, "series_title"] if "series_title" in df.columns else None) or f"series {sid}"
            out.append({
                "service": "episode", "tier": 0, "score": score, "critic": None,
                "size_bytes": size, "size_gb": size / (1024 ** 3), "fid": fid,
                "series_id": int(sid) if pd.notna(sid) else None,
                "season": int(sn) if pd.notna(sn) else None,
                "episode": int(en) if pd.notna(en) else None,
                "title": f"{title} S{int(sn):02d}E{int(en):02d}" if (pd.notna(sn) and pd.notna(en)) else str(title),
            })
        return out

    @timeit("delete_selected_episode_files")
    def delete_selected_episode_files(self, instance: str, file_ids) -> dict:
        """Delete the chosen episode files (episodefile/{id} DELETE), applying the
        whole-file guards + multi-ep coalescing, and record them in the restore
        set. ``file_ids`` are the episode_file_ids the coordinator selected.
        dry_run logs only."""
        stats = {"deleted": 0, "failed": 0, "bytes_freed": 0.0, "skipped_guard": 0}
        want = {int(f) for f in (file_ids or [])}
        if not want:
            return stats
        if not deletions_enabled(self.config):
            # Belt-and-braces: the coordinator can't run without a floor, but never
            # delete through this APPLY primitive either when the gate is closed.
            self.logger.log_warning(
                "[EpisodeFiles] deletions DISABLED — free_space_limit is not set; "
                f"refusing coordinator delete of {len(want)} episode file(s)."
            )
            return stats
        df = self.load(instance)
        if df.empty or "episode_file_id" not in df.columns:
            return stats
        now = datetime.now(tz=timezone.utc)
        try:
            protected = self._build_protected_file_ids(df, now)
        except Exception as e:
            # Refuse to delete without the whole-file guard set — an empty set would
            # let a guarded sibling's file be destroyed. Fail safe: delete nothing.
            self.logger.log_error(
                f"[EpisodeFiles] protected-file-id build failed for '{instance}'; "
                f"REFUSING to delete this cycle (fail-safe): {e}"
            )
            return stats

        done: set[int] = set()
        deleted_fids: set[int] = set()      # files actually removed (would-be in dry_run)
        restore_add: dict[str, dict] = {}   # series_id(str) -> {episodes:[[s,e]], ts}
        _del_rows: list[list] = []          # per-file movements for the end-of-run summary
        for idx in df.index:
            fid = df.at[idx, "episode_file_id"]
            if pd.isna(fid):
                continue
            fid = int(fid)
            if fid not in want:
                continue
            if fid in protected:
                stats["skipped_guard"] += 1
                continue
            sid = df.at[idx, "series_id"]
            sn = df.at[idx, "season_number"] if "season_number" in df.columns else None
            en = df.at[idx, "episode_number"] if "episode_number" in df.columns else None
            # Which season/episode this file backs, for the deletions summary (S##E##; a
            # multi-episode file shows its first episode, "—" when the indices are unknown).
            _se = f"S{int(sn):02d}E{int(en):02d}" if (pd.notna(sn) and pd.notna(en)) else "—"
            if pd.notna(sid) and pd.notna(sn) and pd.notna(en):
                ent = restore_add.setdefault(str(int(sid)), {"episodes": [], "ts": now.isoformat()})
                ent["episodes"].append([int(sn), int(en)])
            if fid in done:   # multi-ep file already handled
                continue
            done.add(fid)
            size = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0
            title = df.at[idx, "series_title"] if "series_title" in df.columns else f"series {sid}"
            if self.dry_run:
                self.logger.log_info(f"  🗑️ [dry_run] Would delete episode file: '{title}' (fid={fid}, {self._fmt_bytes(size)})")
                _del_rows.append([str(title), _se, str(fid), self._fmt_bytes(size), "would delete"])
                stats["deleted"] += 1
                stats["bytes_freed"] += size
                deleted_fids.add(fid)
                continue
            try:
                self.sonarr_api._make_request(instance, f"episodefile/{fid}", method="DELETE")
                stats["deleted"] += 1
                stats["bytes_freed"] += size
                deleted_fids.add(fid)
                self.logger.log_info(f"  🗑️ Deleted episode file: '{title}' (fid={fid}, {self._fmt_bytes(size)})")
                _del_rows.append([str(title), _se, str(fid), self._fmt_bytes(size), "deleted"])
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Episode-file delete failed for '{title}' (fid={fid}): {e}")
                stats["failed"] += 1

        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None and _del_rows:
            _rs.add_rows("sonarr", "Deletions & movements", instance,
                         ["Title", "Ep", "FileId", "Size", "Action"], _del_rows, order=40)

        # Record deletions for restore_recovered_episode_deletions (skip in dry_run
        # so we don't track files that were never actually removed).
        if restore_add and self.global_cache and not self.dry_run:
            dkey = self._DELETED_EPISODES_KEY.format(inst=instance)
            try:
                dset = self.global_cache.get(dkey)
                dset = dset if isinstance(dset, dict) else {}
                for sk, ent in restore_add.items():
                    cur = dset.get(sk) if isinstance(dset.get(sk), dict) else {"episodes": [], "ts": ent["ts"]}
                    have = {tuple(x) for x in cur.get("episodes", [])}
                    for se in ent["episodes"]:
                        if tuple(se) not in have:
                            cur.setdefault("episodes", []).append(se)
                    cur["ts"] = ent["ts"]
                    dset[sk] = cur
                self.global_cache.set(dkey, dset)
            except Exception as e:
                # These episode files are already deleted; failing to record them
                # means restore_recovered_episode_deletions can't re-grab them.
                self.logger.log_error(
                    f"[EpisodeFiles] ⚠️ Failed to persist episode restore-set ({dkey}): {e} "
                    f"— {len(restore_add)} series' deletions are NOT restorable."
                )

        # Ledger: stamp planned_action='delete' on the coordinator's selection so
        # plan_summary reflects it (the grace pass defers stamping to us when the
        # coordinator owns deletion). Reclaim is counted ONCE per file id (multi-ep
        # dedupe). Persisted even in dry_run as an annotation-only preview.
        if deleted_fids:
            for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
                if _c not in df.columns:
                    df[_c] = None
            for _c in ("planned_action", "plan_reason"):
                if df[_c].dtype != object:
                    df[_c] = df[_c].astype(object)
            _seen_reclaim: set[int] = set()
            for idx in df.index:
                _fid = df.at[idx, "episode_file_id"]
                if pd.isna(_fid):
                    continue
                _fid = int(_fid)
                if _fid not in deleted_fids:
                    continue
                df.at[idx, "planned_action"] = "delete"
                df.at[idx, "plan_reason"]    = "coordinator space pool"
                if _fid in _seen_reclaim:
                    df.at[idx, "plan_reclaim_gb"] = None
                else:
                    _seen_reclaim.add(_fid)
                    _sz = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0
                    df.at[idx, "plan_reclaim_gb"] = round(_sz / (1024 ** 3), 2)
            self.save(instance, df)
        return stats

    @timeit("restore_recovered_episode_deletions")
    def restore_recovered_episode_deletions(self, instance: str) -> dict:
        """Re-acquire previously coordinator-deleted episodes whose series'
        watchability_score has recovered above owned_restore_score_threshold:
        re-monitor the episodes + trigger EpisodeSearch. Mirrors Radarr's
        restore_recovered_deletions. Tracked in ``sonarr/{inst}/deleted_episodes``."""
        instance = self._resolve_instance(instance)
        stats = {"tracked": 0, "restored": 0, "still_low": 0, "dropped": 0, "deferred": 0, "failed": 0}
        if self.sonarr_api is None or self.global_cache is None:
            return stats
        now = datetime.now(tz=timezone.utc)
        dkey = self._DELETED_EPISODES_KEY.format(inst=instance)
        dset = self.global_cache.get(dkey)
        dset = dset if isinstance(dset, dict) else {}
        if not dset:
            return stats
        try:
            restore_floor = int(self.config.get("owned_restore_score_threshold", 20) if self.config else 20)
        except (TypeError, ValueError):
            restore_floor = 20

        # Per-series current watchability_score (from the parquet column).
        df = self.load(instance)
        score_by_series: dict[int, int] = {}
        if not df.empty and "watchability_score" in df.columns and "series_id" in df.columns:
            for sid, rows in df.groupby("series_id", sort=False):
                sv = pd.to_numeric(rows["watchability_score"], errors="coerce").dropna()
                if len(sv):
                    try:
                        score_by_series[int(sid)] = int(sv.max())
                    except (TypeError, ValueError):
                        pass

        keep: dict[str, dict] = {}
        for sk, ent in dset.items():
            stats["tracked"] += 1
            try:
                sid = int(sk)
            except (TypeError, ValueError):
                continue
            coords = [tuple(x) for x in (ent.get("episodes") or []) if isinstance(x, (list, tuple)) and len(x) == 2]
            if not coords:
                stats["dropped"] += 1
                continue
            score = score_by_series.get(sid)
            if score is None or score <= restore_floor:
                keep[sk] = ent
                stats["still_low"] += 1
                continue
            # Resolve episode ids for the recovered coords.
            eps = self.sonarr_api._make_request(instance, f"episode?seriesId={sid}", fallback=[]) or []
            want = set(coords)
            ep_ids = [e.get("id") for e in eps
                      if (e.get("seasonNumber"), e.get("episodeNumber")) in want and e.get("id")]
            if not ep_ids:
                # Coords didn't resolve — could be a transient API miss, a series
                # mid-refresh, or ids regenerated. Do NOT silently drop the record
                # (that permanently forfeits restore): keep it to retry on a later
                # run, bounded by age so a truly-gone series can't linger forever.
                ts = ent.get("ts")
                aged_out = False
                try:
                    if ts:
                        aged_out = (now - pd.to_datetime(ts, utc=True)).total_seconds() > self._RESTORE_TRACK_MAX_AGE_S
                except Exception:
                    aged_out = False
                if aged_out:
                    stats["dropped"] += 1
                    self.logger.log_info(
                        f"  Restore record for series {sid} aged out with {len(coords)} "
                        f"unresolved coord(s) — dropping."
                    )
                else:
                    keep[sk] = ent
                    stats["deferred"] += 1
                    self.logger.log_debug(
                        f"  Restore deferred for series {sid}: {len(coords)} coord(s) "
                        f"unresolved this run — will retry."
                    )
                continue
            if self.dry_run:
                self.logger.log_info(
                    f"  [dry_run] Would RESTORE series {sid} (score {score} > {restore_floor}): "
                    f"re-monitor + EpisodeSearch {len(ep_ids)} ep(s)"
                )
                stats["restored"] += 1
                keep[sk] = ent   # still deleted in reality → keep tracking
                continue
            try:
                self.sonarr_api._make_request(instance, "episode/monitor", method="PUT",
                                              payload={"episodeIds": ep_ids, "monitored": True})
                self.sonarr_api._make_request(instance, "command", method="POST",
                                              payload={"name": "EpisodeSearch", "episodeIds": ep_ids})
                stats["restored"] += 1
                self.logger.log_info(f"  Restored series {sid}: re-monitored + searched {len(ep_ids)} ep(s)")
            except Exception as e:
                self.logger.log_warning(f"  Episode restore failed for series {sid}: {e}")
                stats["failed"] += 1
                keep[sk] = ent
        try:
            self.global_cache.set(dkey, keep)
        except Exception:
            pass
        return stats

    # ── Schema normalisation ────────────────────────────────────────────────────

    @staticmethod
    def _normalise(
        raw: dict,
        series_id: int,
        series_title: str,
        season_number: int | None,
        episode_number: int | None,
        is_pilot: bool = False,
        watch_count: int = 0,
        last_watched_at: str | None = None,
        percent_complete: int | None = None,
        air_date_utc: str | None = None,
        all_household_watched: bool = False,
        household_last_watched_at: str | None = None,
    ) -> dict:
        """Flatten a Sonarr ``/episodefile`` record into SCHEMA_COLUMNS."""
        quality = raw.get("quality") or {}
        qq = quality.get("quality") or {}
        media = raw.get("mediaInfo") or {}
        hdr_val = media.get("videoDynamicRange") or ""

        return {
            "episode_file_id":       raw.get("id"),
            "series_id":             series_id,
            "series_title":          series_title,
            "season_number":         season_number if season_number is not None else raw.get("seasonNumber"),
            "episode_number":        episode_number,
            "is_pilot":              is_pilot,
            "is_watched":                watch_count > 0,
            "next_episode":              False,
            "watch_count":               watch_count,
            "last_watched_at":           last_watched_at,
            "all_household_watched":     all_household_watched,
            "household_last_watched_at": household_last_watched_at,
            "percent_complete":          percent_complete,
            "marked_for_deletion":   False,
            "available_until":       None,
            "relative_path":         raw.get("relativePath"),
            "path":                  raw.get("path"),
            "size_bytes":            raw.get("size"),
            "date_added":            raw.get("dateAdded"),
            "air_date_utc":          air_date_utc,
            "quality_name":          qq.get("name"),
            "quality_source":        qq.get("source"),
            "resolution":            qq.get("resolution") or media.get("height"),
            "video_codec":           media.get("videoCodec"),
            "video_bitrate":         media.get("videoBitrate"),
            "video_fps":             media.get("videoFps"),
            "video_bit_depth":       media.get("videoBitDepth"),
            "width":                 media.get("width"),
            "height":                media.get("height"),
            "runtime_seconds":       SonarrCacheEpisodeFilesManager._parse_runtime_s(media.get("runTime") or media.get("runtime")),
            "scan_type":             media.get("scanType"),
            "hdr":                   bool(hdr_val),
            "hdr_type":              media.get("videoDynamicRangeType") or None,
            "audio_codec":           media.get("audioCodec"),
            "audio_channels":        media.get("audioChannels"),
            "audio_languages":       media.get("audioLanguages"),
            "subtitles":             media.get("subtitles"),
            "release_group":         raw.get("releaseGroup"),
            "scene_name":            raw.get("sceneName"),
            "quality_cutoff_not_met": raw.get("qualityCutoffNotMet"),
            "last_synced_at":        datetime.now(tz=timezone.utc).isoformat(),
        }

    # ── Sonarr API helpers ──────────────────────────────────────────────────────

    @timeit("_get_free_space_gb")
    def _get_free_space_gb(self, instance: str) -> float:
        """
        Free space (GiB) across this instance's disks, deduped by physical mount
        (root folders sharing a disk are counted once). Returns ``float('inf')``
        on failure so a transient API error never silently blocks acquisitions.
        """
        if self.sonarr_api is None:
            return float("inf")
        return self.sonarr_api.disk_free_gb(instance)

    # 24-hour TTL for on-disk episode list cache per series
    EPISODES_CACHE_TTL_S: int = 86_400

    @timeit("_get_all_episodes")
    def _get_all_episodes(self, instance: str, series_id: int,
                          series_ep_cache: dict | None = None,
                          files_session_cache: dict | None = None,
                          *, log_miss: bool = True, log_expired: bool = True) -> dict[int, list[dict]]:
        """
        Fetch ALL episodes for a series and return them bucketed by season:
        ``{season_number: [episode_obj, ...]}``.  One call instead of
        one-per-season eliminates the bottleneck on large series like Bluey.

        Cache hierarchy
        ---------------
        1. In-memory ``series_ep_cache`` (keyed by series_id) — free within
           the same sync run.
        2. On-disk JSON via GlobalCacheManager (24-hour TTL) at key
           ``sonarr/<instance>/episodes/by_series/<series_id>``.
        3. Live Sonarr API call on cache miss / TTL expiry.
        """
        if series_ep_cache is not None and series_id in series_ep_cache:
            return series_ep_cache[series_id]

        cache_key = f"sonarr/{instance}/episodes/by_series/{series_id}"
        all_eps: list[dict] = []

        if self.global_cache:
            try:
                all_eps = self.global_cache.get_or_generate_cache(
                    key=cache_key,
                    generator_function=lambda: (
                        self.sonarr_api._make_request(
                            instance, f"episode?seriesId={series_id}", fallback=[]
                        ) or []
                    ),
                    expiration_time=self.EPISODES_CACHE_TTL_S,
                    log_miss=log_miss, log_expired=log_expired,
                ) or []
                self.logger.log_debug(
                    f"  📦 _get_all_episodes series={series_id}: {len(all_eps)} eps"
                )
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ _get_all_episodes series={series_id} failed: {e}"
                )
        else:
            try:
                all_eps = (
                    self.sonarr_api._make_request(
                        instance, f"episode?seriesId={series_id}", fallback=[]
                    ) or []
                )
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ _get_all_episodes series={series_id} API failed: {e}"
                )

        # Also pre-warm files_session_cache using the episode file cache key
        # so _resolve_episode_file doesn't need a separate slow API call later.
        # episode objects have episodeFileId but not file details; pre-fetch
        # the file list from the same on-disk cache (24h TTL).
        if files_session_cache is not None and series_id not in files_session_cache:
            files_cache_key = f"sonarr/{instance}/episodefiles/by_series/{series_id}"
            try:
                if self.global_cache:
                    cached_files = self.global_cache.get_or_generate_cache(
                        key=files_cache_key,
                        generator_function=lambda: (
                            self.sonarr_api._make_request(
                                instance, f"episodefile?seriesId={series_id}", fallback=[]
                            ) or []
                        ),
                        expiration_time=self.EPISODES_CACHE_TTL_S,
                        log_miss=log_miss, log_expired=log_expired,
                    ) or []
                    files_session_cache[series_id] = cached_files
            except Exception:
                pass  # non-fatal — _resolve_episode_file will fetch lazily

        bucketed: dict[int, list[dict]] = {}
        for ep in all_eps:
            sn = ep.get("seasonNumber")
            if sn is not None:
                bucketed.setdefault(sn, []).append(ep)

        if series_ep_cache is not None:
            series_ep_cache[series_id] = bucketed
        return bucketed

    def _prewarm_by_series_episode_cache(
        self, instance: str, series_ids,
        *, season_ep_cache: dict | None = None,
        files_session_cache: dict | None = None,
        desc: str = "Episode cache",
    ) -> int:
        """Concurrently warm the per-series episode (+ episodefile) caches.

        Each serial walk that calls ``_get_all_episodes`` per series otherwise
        regenerates an expired ``by_series`` cache via one network GET at a time.
        For large batches that is the dominant latency. Warming them CONCURRENTLY
        first turns the subsequent walk into pure in-memory / on-disk cache hits.

        Thread-safety: workers use FRESH LOCAL dicts and only ever touch distinct
        on-disk cache keys (distinct files) + unlocked GETs — they NEVER mutate the
        shared ``season_ep_cache`` / ``files_session_cache`` or any DataFrame. Only
        the main thread merges results. Per-series cache logs are suppressed so the
        single tqdm bar is the only output. Returns the count of series warmed.
        """
        series_ids = list(dict.fromkeys(int(s) for s in series_ids))
        if not series_ids:
            return 0

        import sys
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            from tqdm import tqdm as _tqdm_cls
        except ImportError:
            _tqdm_cls = None
        try:
            _cfg = getattr(self, "config", None)
            _workers = int((_cfg.get("sonarr_cache_workers", 8) if _cfg else 8) or 8)
        except Exception:
            _workers = 8
        _workers = max(1, min(_workers, len(series_ids)))

        def _warm(_sid):
            # Fresh per-worker dicts; never touch the shared caches in a worker.
            _lep, _lfiles = {}, {}
            try:
                self._get_all_episodes(instance, _sid, _lep, _lfiles,
                                       log_miss=False, log_expired=False)
            except Exception:
                pass
            return _sid, _lep.get(_sid), _lfiles.get(_sid)

        self.logger.log_info(
            f"Warming episode cache for {len(series_ids)} series ({_workers} workers)..."
        )
        with ThreadPoolExecutor(max_workers=_workers) as _ex:
            _futs = [_ex.submit(_warm, _s) for _s in series_ids]
            _iter = as_completed(_futs)
            if _tqdm_cls is not None:
                _iter = _tqdm_cls(_iter, total=len(_futs), desc=desc,
                                  unit="series", file=sys.stderr)
            for _f in _iter:
                _sid, _bucketed, _files = _f.result()
                if season_ep_cache is not None and _bucketed is not None:
                    season_ep_cache[_sid] = _bucketed          # main-thread merge
                if files_session_cache is not None and _files is not None:
                    files_session_cache[_sid] = _files
        self.logger.log_success(
            f"Episode cache warmed for {len(series_ids)} series."
        )
        return len(series_ids)

    @timeit("_get_episode_files")
    def _get_episode_files(self, instance: str, series_id: int,
                           retries: int = 2, retry_delay_s: float = 2.0) -> list[dict]:
        """Fetch all episodefile records for a series (1 API call).

        Retries up to ``retries`` times with ``retry_delay_s`` seconds between
        attempts on transient failure before returning [].
        """
        last_exc = None
        for attempt in range(max(1, retries)):
            try:
                return (
                    self.sonarr_api._make_request(
                        instance, f"episodefile?seriesId={series_id}", fallback=[]
                    )
                    or []
                )
            except Exception as e:
                last_exc = e
                if attempt < retries - 1:
                    self.logger.log_debug(
                        f"  ↺ _get_episode_files series={series_id} "
                        f"attempt {attempt + 1}/{retries} failed — retrying in {retry_delay_s:.1f}s: {e}"
                    )
                    time.sleep(retry_delay_s)
        self.logger.log_warning(
            f"  ⚠️ _get_episode_files series={series_id} failed after {retries} attempt(s): {last_exc}"
        )
        return []

    @timeit("_get_episodes_for_season")
    def _get_episodes_for_season(
        self, instance: str, series_id: int, season: int,
        season_ep_cache: dict | None = None,
        retries: int = 2,
        retry_delay_s: float = 2.0,
    ) -> list[dict]:
        """Fetch episode metadata for one season — gives us episodeNumber → episodeFileId.

        If ``season_ep_cache`` is supplied (keyed by ``(series_id, season)``), results
        are stored/retrieved so the same season is never fetched twice in a sync session.

        On transient failure (timeout, connection error) retries up to ``retries`` times
        with ``retry_delay_s`` seconds between attempts before giving up and returning []
        — same pattern as the rescan/rename command polling elsewhere.
        """
        key = (series_id, season)
        if season_ep_cache is not None and key in season_ep_cache:
            return season_ep_cache[key]

        last_exc = None
        for attempt in range(max(1, retries)):
            try:
                result = (
                    self.sonarr_api._make_request(
                        instance,
                        f"episode?seriesId={series_id}&seasonNumber={season}",
                        fallback=[],
                    )
                    or []
                )
                if season_ep_cache is not None:
                    season_ep_cache[key] = result
                return result
            except Exception as e:
                last_exc = e
                if attempt < retries - 1:
                    self.logger.log_debug(
                        f"  ↺ _get_episodes_for_season series={series_id} S{season:02d} "
                        f"attempt {attempt + 1}/{retries} failed — retrying in {retry_delay_s:.1f}s: {e}"
                    )
                    time.sleep(retry_delay_s)

        self.logger.log_warning(
            f"  ⚠️ _get_episodes_for_season series={series_id} S{season:02d} "
            f"failed after {retries} attempt(s): {last_exc}"
        )
        if season_ep_cache is not None:
            season_ep_cache[key] = []  # cache the failure so we don't retry again this session
        return []

    def _pick_representative_file(self, files: list[dict]) -> dict | None:
        """
        From a list of episode files, return the one most likely to be the
        pilot (lowest non-special season, then earliest relativePath as a
        proxy for episode number when multiple files exist in that season).
        """
        non_specials = [f for f in files if (f.get("seasonNumber") or 0) > 0]
        if not non_specials:
            return None
        return min(
            non_specials,
            key=lambda f: (f.get("seasonNumber", 999), f.get("relativePath") or ""),
        )

    @timeit("_resolve_episode_file")
    def _resolve_episode_file(
        self,
        instance: str,
        series_id: int,
        season: int,
        episode: int,
        files_cache: dict,
        season_ep_cache: dict | None = None,
    ) -> tuple[dict | None, int | None]:
        """
        Resolve the episode file record for a specific season/episode.

        Uses ``files_cache`` (keyed by series_id) to avoid redundant API calls
        when multiple episodes of the same series appear in Tautulli history.

        Returns ``(file_dict | None, episode_file_id | None)``.
        """
        # Fetch all episode files for the series (cached per session)
        if series_id not in files_cache:
            files_cache[series_id] = self._get_episode_files(instance, series_id)

        all_files = files_cache[series_id]
        season_files = {f["id"]: f for f in all_files if f.get("seasonNumber") == season}

        if not season_files:
            return None, None, None

        # Fetch episodes for this season to map episodeNumber → episodeFileId
        eps = self._get_episodes_for_season(instance, series_id, season, season_ep_cache)
        for ep in eps:
            if ep.get("episodeNumber") == episode and ep.get("episodeFileId"):
                fid = ep["episodeFileId"]
                file_rec = season_files.get(fid)
                if file_rec:
                    return file_rec, fid, ep.get("airDateUtc")

        return None, None, None

    # ── Lifecycle helpers ────────────────────────────────────────────────────────

    @timeit("_compute_next_episodes")
    def _compute_next_episodes(
        self,
        df: pd.DataFrame,
        instance: str,
        files_session_cache: dict,
        prefetch_hours: float | None = None,
        season_ep_cache: dict | None = None,
    ) -> pd.DataFrame:
        """
        Mark upcoming unwatched episodes for every series that has watched rows.

        Instead of flagging only the single next episode, this method walks
        forward through the episode sequence until it has accumulated
        ``prefetch_hours`` worth of runtime (default: ``PREFETCH_HOURS``).
        Every episode within that budget is flagged ``next_episode = True``
        so that ``_do_acquire_next_episodes`` will monitor + search for all
        of them, giving the household a buffer of pre-downloaded content.

        Steps
        -----
        1. Reset all ``next_episode`` flags to False.
        2. For each series find the highest watched (season, episode).
        3. Walk forward episode by episode, accumulating runtime.
        4. Stop when the runtime budget is exhausted or no further episode
           exists in Sonarr.
        5. Mark every episode within the budget ``next_episode = True``.

        Runtime estimation
        ------------------
        Uses ``runtime_seconds`` from the Parquet row when available.  Falls
        back to the series-level ``runtime`` field from Sonarr (stored on the
        series object as minutes) converted to seconds, then to a safe default
        of 2700 s (45 min) when neither is present.
        """
        budget_seconds = (prefetch_hours if prefetch_hours is not None else self.PREFETCH_HOURS) * 3600.0
        DEFAULT_RUNTIME_S = 2700.0  # 45-minute fallback when no runtime data available

        # Next-episode prefetch tuning — RECOMMENDED ON by default. Each sub-feature
        # falls back to its DEFAULT_* (enabled) recommendation when the key is ABSENT;
        # an explicit {"enabled": False} (the canonical persisted disable, survives an
        # onboarding re-merge) or a bare {} disables just that one. The `in` check
        # honours the override instead of re-enabling it.
        _next_cfg = ((self.config or {}).get("acquisition", {}) or {}).get("next_episode", {}) or {}

        # Graduated episode cap: absent → smooth cap; explicit {enabled:False}/{} → legacy cliff.
        _graduated_cap = _next_cfg["graduated_cap"] if "graduated_cap" in _next_cfg else DEFAULT_GRADUATED_CAP

        # Recency gate: absent → walk hottest-first + skip cold (unless airing soon);
        # explicit {enabled:False}/{} → no reorder, no skip. cold_days falls back to the
        # recommended horizon when enabled but unset, so {"enabled": True} can't silently no-op.
        _recency = _next_cfg["recency_gate"] if "recency_gate" in _next_cfg else DEFAULT_RECENCY_GATE
        _cold_days = (
            _recency.get("cold_days", DEFAULT_RECENCY_GATE["cold_days"])
            if _recency.get("enabled") else None
        )
        _now_cne = datetime.now(tz=timezone.utc)

        # Per-series budget ramp: absent → scale by watchability_percentile;
        # explicit {enabled:False}/{} → flat budget (multiplier 1.0).
        _budget_ramp = _next_cfg["budget_ramp"] if "budget_ramp" in _next_cfg else DEFAULT_BUDGET_RAMP

        # Reset — always recompute from scratch so stale flags don't linger
        df["next_episode"] = False

        # Per-series resume point (highest watched season/episode) — pure brain.
        last_by_series = last_watched_per_series(df)
        if last_by_series.empty:
            return df
        if _cold_days is not None:
            last_by_series = order_series_by_recency(last_by_series)

        # Pre-cast the search columns once so the inner mask avoids both
        # repeated computation AND the FutureWarning about fillna downcasting.
        # These are safe to cache because df is not structurally mutated
        # inside the loop (only a single cell is set via df.loc[idx, col]).
        _df_series  = df["series_id"]
        _df_season  = pd.to_numeric(df["season_number"],  errors="coerce").fillna(-1).astype(int)
        _df_episode = pd.to_numeric(df["episode_number"], errors="coerce").fillna(-1).astype(int)

        # Build a fast (sid, season, episode) → runtime_seconds lookup from
        # rows already in the Parquet so we can budget without extra API calls.
        _rt_lookup = build_runtime_lookup(df)

        new_rows: list[dict] = []
        # One aligned grid of per-series prefetch decisions, printed once after the
        # walk (replaces the per-series log_info lines below). Plain-ASCII cells.
        _grid_rows: list[list[str]] = []
        _cne_start = time.time()

        # series_id(str) → recent household watcher(s); built by sync_from_tautulli from the
        # per-user Tautulli history (same source the JIT grab grid uses). Annotates the prefetch
        # grid's 'For' column — who each next-up was queued for. Best-effort: {} → 'For' shows '-'.
        _jit_watchers = (self.global_cache.get(f"sonarr/{instance}/jit_watchers")
                         if self.global_cache else None) or {}

        # ── Parallel pre-warm of the per-series episode cache (bulk batches) ──────
        # The serial walk below calls _get_all_episodes per series; each regenerates
        # an expired by_series cache via a network GET, one at a time, logging a line
        # each. For large batches, warm them CONCURRENTLY first so the walk gets pure
        # in-memory cache hits (no per-series "Expired cache" log, no serial latency).
        # Thread-safety: workers use FRESH LOCAL dicts — the shared season_ep_cache /
        # files_session_cache and the DataFrame are NOT thread-safe; only the main
        # thread merges results. Per-series cache logs are suppressed in bulk mode.
        # No force_refresh on this path; if ever added, delete keys serially on the
        # main thread BEFORE fan-out (save_json is non-atomic for the SAME key).
        series_ids = list(dict.fromkeys(int(r["series_id"]) for _, r in last_by_series.iterrows()))
        PROGRESS_BAR_THRESHOLD = 10
        use_tqdm = len(series_ids) > PROGRESS_BAR_THRESHOLD
        if use_tqdm and season_ep_cache is not None:
            self._prewarm_by_series_episode_cache(
                instance, series_ids,
                season_ep_cache=season_ep_cache,
                files_session_cache=files_session_cache,
                desc="Episode cache",
            )

        for _cne_i, (_, row) in enumerate(last_by_series.iterrows(), start=1):
            sid          = int(row["series_id"])
            series_title = str(row.get("series_title") or "")
            # Who this next-up is queued FOR — recent household watcher(s), most-recent first
            # (same attribution the JIT grab grid shows). Display-only; never affects the walk.
            _for_cell = ", ".join((_jit_watchers.get(str(sid)) or [])[:2]) or "-"
            _total_series = len(last_by_series)
            if not use_tqdm:
                self.logger.log_info(
                    f"[⏱️] compute_next_episodes [{_cne_i}/{_total_series}] — "
                    f"{time.time()-_cne_start:.1f}s elapsed, invoked at {datetime.now(tz=timezone.utc).strftime('%H:%M:%S')} UTC — '{series_title}'"
                )
            # Use pd.notna guards: `NaN or 0` evaluates to NaN (NaN is truthy)
            # so the `or 0` fallback does not protect against NaN values.
            _sn = row.get("season_number")
            _en = row.get("episode_number")
            last_season = int(_sn) if pd.notna(_sn) else 0
            last_ep     = int(_en) if pd.notna(_en) else 0

            # Fetch the Sonarr series object once for this series so we can
            # use its runtime field as a fallback when episode rows lack
            # runtime_seconds (e.g. pending-acquisition stubs).
            series_mgr = getattr(self.sonarr_cache, "series", None)
            sonarr_series_obj: dict = {}
            if series_mgr:
                try:
                    sonarr_series_obj = series_mgr.get_series_by_id(instance, sid) or {}
                except Exception:
                    pass
            # Sonarr stores series runtime in minutes
            series_runtime_s = (
                float(sonarr_series_obj.get("runtime", 0) or 0) * 60.0
                or DEFAULT_RUNTIME_S
            )

            # Recency gate (opt-in): skip a series gone cold past cold_days, unless it
            # has an episode airing soon (Sonarr nextAiring = the mid-season-break
            # exemption). No-op when disabled (_cold_days is None).
            if _cold_days is not None and is_cold_series(
                row.get("last_watched_at"), _now_cne,
                cold_days=_cold_days, has_upcoming=bool(sonarr_series_obj.get("nextAiring")),
            ):
                _grid_rows.append([series_title, "-", "cold-skip", "-", "-", "-", _for_cell])
                continue

            # Walk forward from the last watched episode, accumulating runtime
            # until we hit the prefetch budget or run out of episodes.
            # Hard caps prevent runaway loops on series with many short episodes,
            # UNLESS episodes are short (<10 min) — in that case grab them all
            # since they're small files and the whole season fits in the budget.
            SHORT_EPISODE_S     = 600.0  # 10 minutes in seconds
            MAX_EP_PER_SERIES   = 6      # cap for normal-length episodes
            MAX_TIME_PER_SERIES = 25.0   # wall-clock seconds before bailing out
            _series_start = time.time()
            _ep_cap = episode_cap(
                series_runtime_s, short_episode_s=SHORT_EPISODE_S, max_ep=MAX_EP_PER_SERIES,
                graduated=_graduated_cap,
            )
            # Per-series runtime budget (opt-in percentile ramp; multiplier is exactly
            # 1.0 when unconfigured, so series_budget == budget_seconds → byte-identical).
            series_budget = budget_seconds * series_budget_multiplier(
                row.get("watchability_percentile"), _budget_ramp
            )
            accumulated_s = 0.0
            cur_season, cur_ep = last_season, last_ep
            ep_count = 0
            # Use shared cache if provided; otherwise a per-series local dict.
            # series_ep_cache keys by series_id → {season: [ep_objs]}
            # Fetch ALL episodes for this series in one call upfront so the
            # per-episode walk never blocks on individual season requests.
            _ep_cache = season_ep_cache if season_ep_cache is not None else {}
            _series_all_eps: dict[int, list[dict]] = {}  # season → [ep_objs]
            if sid not in _ep_cache:
                _series_all_eps = self._get_all_episodes(
                    instance, sid, _ep_cache, files_session_cache,
                    log_miss=not use_tqdm, log_expired=not use_tqdm,
                )
                # Reset the per-series clock so fetch time doesn't eat the walk budget
                _series_start = time.time()
            else:
                _series_all_eps = _ep_cache[sid]

            # No episodes at all → nothing to walk into; skip
            if not _series_all_eps:
                self.logger.log_debug(
                    f"  ⏩ '{series_title}': no episodes in Sonarr — skipping prefetch"
                )
                continue

            # ── Pre-build a (season, ep) → row-index map for this series ────────
            # For series like Bluey that have hundreds of episodes in the Parquet
            # we can walk the next-episode sequence entirely from the cache without
            # any Sonarr API calls.  Only fall back to the API when the Parquet
            # has no row for an episode (genuinely new / not yet downloaded).
            _series_mask = _df_series == sid
            _series_ep_index: dict[tuple[int, int], int] = {}  # (sn, en) → df idx
            for _idx in df.index[_series_mask]:
                _sn = _df_season.at[_idx]
                _en = _df_episode.at[_idx]
                if _sn >= 0 and _en >= 0:
                    _series_ep_index[(_sn, _en)] = _idx

            while accumulated_s < series_budget and ep_count < _ep_cap:
                if time.time() - _series_start > MAX_TIME_PER_SERIES:
                    self.logger.log_warning(
                        f"  ⚠️ compute_next_episodes: '{series_title}' exceeded "
                        f"{MAX_TIME_PER_SERIES:.0f}s wall-clock budget — stopping prefetch "
                        f"({ep_count} ep(s) queued so far)"
                    )
                    break
                # Advance to the next candidate: same season next ep, then
                # season+1 ep1 if this season is exhausted.
                next_s, next_e = cur_season, cur_ep + 1

                # ── Already in Parquet and flagged as watched? Skip it. ──────
                # O(1) dict lookup replaces a full DataFrame mask scan.
                if (next_s, next_e) in _series_ep_index:
                    _existing_idx = _series_ep_index[(next_s, next_e)]
                    if df.at[_existing_idx, "is_watched"]:
                        cur_season, cur_ep = next_s, next_e
                        continue

                    # ── Existing unwatched row in Parquet ────────────────────
                    df.loc[_existing_idx, "next_episode"] = True
                    rt = _rt_lookup.get((sid, next_s, next_e), series_runtime_s)
                    accumulated_s += rt
                    ep_count += 1
                    self.logger.log_debug(
                        f"  ➡ Prefetch ep {ep_count} for '{series_title}': "
                        f"S{next_s:02d}E{next_e:02d} (in Parquet, "
                        f"+{rt/60:.0f} min, total {accumulated_s/3600:.2f} h)"
                    )
                    cur_season, cur_ep = next_s, next_e
                    continue

                # ── Not in Parquet — use cached episode data to check file status ──
                # _series_all_eps already has all episode objects with episodeFileId
                # from the single upfront API call. No need to call _get_episode_files.
                _season_eps = _series_all_eps.get(next_s, [])
                _ep_obj = next(
                    (e for e in _season_eps if e.get("episodeNumber") == next_e), None
                )

                if _ep_obj is None:
                    # Episode doesn't exist in Sonarr at all — try next season
                    _next_season_eps = _series_all_eps.get(next_s + 1, [])
                    if _next_season_eps:
                        cur_season, cur_ep = next_s + 1, 0
                        continue
                    break  # no more seasons

                _ep_file_id = _ep_obj.get("episodeFileId")

                # Default so the `if file_rec:` check below is safe when the
                # episode has no downloaded file yet — otherwise this raises
                # UnboundLocalError. The no-file case is meant to fall through to
                # the pending-acquisition logic further down.
                file_rec = None
                air_date_utc = None
                if _ep_file_id:
                    # Episode has a downloaded file — resolve full file record
                    file_rec, _, air_date_utc = self._resolve_episode_file(
                        instance, sid, next_s, next_e, files_session_cache, season_ep_cache
                    )
                if file_rec:
                    new_row = self._normalise(
                        raw=file_rec,
                        series_id=sid,
                        series_title=series_title,
                        season_number=next_s,
                        episode_number=next_e,
                        is_pilot=False,
                        air_date_utc=air_date_utc,
                    )
                    new_row["next_episode"] = True
                    new_rows.append(new_row)
                    rt_raw = (file_rec.get("mediaInfo") or {}).get("runTime") or (file_rec.get("mediaInfo") or {}).get("runtime")
                    rt = self._parse_runtime_s(rt_raw) or series_runtime_s
                    accumulated_s += rt
                    ep_count += 1
                    self.logger.log_debug(
                        f"  ➕ Prefetch ep {ep_count} added: '{series_title}' "
                        f"S{next_s:02d}E{next_e:02d} "
                        f"(+{rt/60:.0f} min, total {accumulated_s/3600:.2f} h)"
                    )
                    cur_season, cur_ep = next_s, next_e
                    continue

                # File not yet downloaded — check Sonarr episode index.
                # If the episode exists (aired, indexed, no file) create a
                # pending-acquisition row and continue budgeting.
                # If next_e doesn't exist in this season, try season+1 ep1
                # before giving up.
                advanced = False
                candidates_to_try = [(next_s, next_e)]
                if next_e > 1:  # already trying ep+1; also try next season
                    candidates_to_try.append((next_s + 1, 1))
                elif next_e == 1 and next_s > last_season:
                    pass  # already a season-boundary attempt

                for try_s, try_e in candidates_to_try:
                    try:
                        if time.time() - _series_start > MAX_TIME_PER_SERIES:
                            self.logger.log_warning(
                                f"  ⚠️ compute_next_episodes: '{series_title}' exceeded "
                                f"{MAX_TIME_PER_SERIES:.0f}s wall-clock budget — stopping prefetch "
                                f"({ep_count} ep(s) queued so far)"
                            )
                            advanced = True
                            break
                        # Use the already-fetched full episode list — no extra API call
                        eps = _series_all_eps.get(try_s, [])
                        ep_obj = next(
                            (e for e in eps if e.get("episodeNumber") == try_e),
                            None,
                        )
                        if ep_obj:
                            ep_fid      = ep_obj.get("episodeFileId")
                            ep_air_date = ep_obj.get("airDateUtc")
                            if ep_fid:
                                # File exists in Sonarr — resolve from pre-warmed cache.
                                # _get_all_episodes pre-populates files_session_cache
                                # via the episodefiles/by_series disk cache so this
                                # should never need a live API call.
                                if sid not in files_session_cache:
                                    files_session_cache[sid] = self._get_episode_files(
                                        instance, sid
                                    )
                                file_rec2 = next(
                                    (f for f in files_session_cache[sid] if f.get("id") == ep_fid),
                                    None,
                                )
                                if file_rec2:
                                    new_row2 = self._normalise(
                                        raw=file_rec2,
                                        series_id=sid,
                                        series_title=series_title,
                                        season_number=try_s,
                                        episode_number=try_e,
                                        is_pilot=False,
                                        air_date_utc=ep_air_date,
                                    )
                                    new_row2["next_episode"] = True
                                    new_rows.append(new_row2)
                                    rt_raw2 = (file_rec2.get("mediaInfo") or {}).get("runTime") or (file_rec2.get("mediaInfo") or {}).get("runtime")
                                    rt2 = self._parse_runtime_s(rt_raw2) or series_runtime_s
                                    accumulated_s += rt2
                                    ep_count += 1
                                    cur_season, cur_ep = try_s, try_e
                                    advanced = True
                                    self.logger.log_debug(
                                        f"  ➕ Prefetch ep {ep_count} (via ep lookup): "
                                        f"'{series_title}' S{try_s:02d}E{try_e:02d} "
                                        f"(+{rt2/60:.0f} min, total {accumulated_s/3600:.2f} h)"
                                    )
                                    break

                            # No file yet — pending acquisition stub
                            acq_row: dict = {col: None for col in self.SCHEMA_COLUMNS}
                            acq_row.update({
                                "series_id":           sid,
                                "series_title":        series_title,
                                "season_number":       try_s,
                                "episode_number":      try_e,
                                "is_pilot":            False,
                                "is_watched":          False,
                                "next_episode":        True,
                                "watch_count":         0,
                                "marked_for_deletion": False,
                                "hdr":                 False,
                                "air_date_utc":        ep_air_date,
                            })
                            new_rows.append(acq_row)
                            accumulated_s += series_runtime_s
                            ep_count += 1
                            cur_season, cur_ep = try_s, try_e
                            advanced = True
                            self.logger.log_debug(
                                f"  📥 Prefetch ep {ep_count} queued for acquisition: "
                                f"'{series_title}' S{try_s:02d}E{try_e:02d} "
                                f"(+{series_runtime_s/60:.0f} min est., total {accumulated_s/3600:.2f} h)"
                            )
                            break
                        else:
                            # Episode doesn't exist in this season — try next season
                            if try_s == next_s and try_e == next_e:
                                # Season exhausted; try season boundary on next iteration
                                cur_season, cur_ep = try_s + 1, 0
                                advanced = True  # advance the pointer, budget stays
                    except Exception:
                        pass

                if not advanced:
                    # Genuinely no more episodes (fully watched or not yet aired)
                    self.logger.log_debug(
                        f"  ℹ️ Prefetch budget exhausted or no more episodes for "
                        f"'{series_title}' after S{cur_season:02d}E{cur_ep:02d} "
                        f"({ep_count} ep(s) queued, {accumulated_s/3600:.2f} h)"
                    )
                    break

            # ── Decision summary ───────────────────────────────────────────────
            # Summarise what was decided for this series — collected into one
            # aligned grid printed once after the walk (see log_grid below).
            policy     = row.get("keep_policy") or None

            # Quality note: is this series a candidate for active-watcher upgrade?
            lw_raw      = row.get("last_watched_at")
            quality_note = ""  # plain-ASCII grid cell
            if lw_raw:
                try:
                    lw_dt   = pd.to_datetime(lw_raw, utc=True)
                    age_d   = (datetime.now(tz=timezone.utc) - lw_dt).days
                    cert    = str(row.get("certification") or "").lower()
                    KIDS    = {"g", "pg", "tv-g", "tv-y", "tv-y7"}
                    is_kids = cert in KIDS
                    if age_d <= 30 and not is_kids and policy not in ("keep_series", "keep_season"):
                        quality_note = "upgrade-eligible"
                    elif is_kids:
                        quality_note = "kids-skipped"
                except Exception:
                    pass

            _note_cell = quality_note or "-"
            if ep_count > 0:
                # Find the first next_episode row for this series to name it
                next_eps = df.loc[
                    (df["series_id"] == sid) & (df["next_episode"] == True)
                ] if "series_id" in df.columns else pd.DataFrame()
                if not next_eps.empty:
                    nrow = next_eps.iloc[0]
                    ns   = int(nrow.get("season_number")  or 0)
                    ne   = int(nrow.get("episode_number") or 0)
                    next_label = f"S{ns:02d}E{ne:02d}"
                else:
                    next_label = "?"
                _grid_rows.append([
                    series_title, policy or "-", f"acquire {ep_count}", next_label,
                    f"{accumulated_s/3600:.1f}h/{series_budget/3600:.1f}h", _note_cell, _for_cell,
                ])
            else:
                _grid_rows.append([
                    series_title, policy or "-", "no-new", "-", "-", _note_cell, _for_cell,
                ])

        # One aligned grid of every per-series prefetch decision, printed once
        # (replaces the old per-series log_info lines). No-op when empty.
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            _rs.add_rows("sonarr", "Next-episode prefetch", instance,
                         ["Series", "Policy", "Decision", "Next", "Budget", "Note", "For"],
                         _grid_rows, order=10)
        else:
            self.logger.log_grid(
                ["Series", "Policy", "Decision", "Next", "Budget", "Note", "For"],
                _grid_rows,
                title=(
                    f"Sonarr next-episode prefetch{' [dry_run]' if self.dry_run else ''}"
                ),
                cap=24,
            )

        if new_rows:
            df_new = pd.DataFrame(new_rows, columns=self.SCHEMA_COLUMNS)
            for col in self._NUMERIC_COLUMNS:
                if col in df_new.columns:
                    df_new[col] = pd.to_numeric(df_new[col], errors="coerce")
            df = self._safe_concat(df, df_new)

        return df

    @timeit("_apply_grace_period")
    def _apply_grace_period(
        self, df: pd.DataFrame, grace_hours: int | None = None
    ) -> pd.DataFrame:
        """
        For every watched, non-pilot, non-next-episode row:

        * Set ``available_until = last_watched_at + grace_hours``.
        * Set ``marked_for_deletion = True`` once that deadline has passed.

        Pilots and next-episode rows are deliberately excluded — they have
        independent lifetimes managed outside the grace-period logic.

        Keep-policy exemptions (populated by ``_sync_keep_policies``)
        ---------------------------------------------------------------
        ``keep_series``
            The entire series is protected.  No episode from a series with
            this policy is ever marked for deletion.

        ``keep_season``
            Episodes from the *current* (highest non-special) season for that
            series are protected.  Episodes from older seasons still age out
            via the normal grace-period cycle.  "Current season" is the
            highest ``season_number > 0`` found for that series in the df.
        """
        grace_hours = grace_hours if grace_hours is not None else self.GRACE_HOURS
        grace_td    = timedelta(hours=grace_hours)
        now         = datetime.now(tz=timezone.utc)
        # Optional score-scaled grace window (config grace_window_ramp; default {} ->
        # multiplier exactly 1.0 -> byte-identical fixed window).
        _grace_ramp = (self.config or {}).get("grace_window_ramp", {}) or {}

        # Ensure string/bool columns accept their intended value types.
        # _safe_concat's reindex can restore all-NA columns as float64; .at[]
        # assignments of a string or bool into a float64 cell raise a FutureWarning.
        if "available_until" in df.columns and df["available_until"].dtype != object:
            df["available_until"] = df["available_until"].astype(object)
        if "marked_for_deletion" in df.columns and df["marked_for_deletion"].dtype not in (bool, "bool"):
            # fillna(0) avoids the downcasting FutureWarning that fires when
            # filling a float64 column with a bool literal (False).
            df["marked_for_deletion"] = (
                df["marked_for_deletion"].infer_objects(copy=False).fillna(0).astype(bool)
            )

        # Pre-compute protected file IDs.
        # Uses _build_pilot_file_ids which covers both real pilot rows AND the
        # earliest watched episode for series that still have only a stub pilot
        # (episode_file_id=None).  Stub pilots occur when the pilot batch ran
        # but found no downloadable file yet — without the de-facto-pilot logic
        # the watched S01E01 for those series had no protection and was deleted.
        pilot_file_ids = self._build_pilot_file_ids(df)

        # Pre-compute latest non-special season per series for keep_season policy.
        # Only computed when at least one such series is in the df to avoid
        # unnecessary work on most runs.
        latest_season_for: dict[int, int] = {}
        if "keep_policy" in df.columns and (df["keep_policy"] == "keep_season").any():
            keep_season_sids = set(
                df.loc[df["keep_policy"] == "keep_season", "series_id"]
                .dropna().astype(int).unique()
            )
            _sid_num = pd.to_numeric(df["series_id"], errors="coerce").fillna(-1).astype(int)
            _sn_num  = pd.to_numeric(df["season_number"], errors="coerce")
            for sid in keep_season_sids:
                non_special = _sn_num[(_sid_num == sid) & (_sn_num > 0)].dropna()
                if not non_special.empty:
                    latest_season_for[sid] = int(non_special.max())

        for idx in df.index:
            is_pilot = bool(df.at[idx, "is_pilot"]) if "is_pilot" in df.columns else False
            is_next  = bool(df.at[idx, "next_episode"]) if "next_episode" in df.columns else False
            lw = df.at[idx, "last_watched_at"]

            # ── Guard signals (same col-guards / pd.notna as before) ─────────────
            # _build_pilot_file_ids covers real pilots AND the de-facto pilot (the
            # earliest watched ep of a stub-pilot series).
            _fid = df.at[idx, "episode_file_id"]
            fid_protected = bool(pd.notna(_fid) and _fid in pilot_file_ids)

            keep_series = keep_season_current = False
            if "keep_policy" in df.columns:
                policy = df.at[idx, "keep_policy"]
                keep_series = (policy == "keep_series")   # entire series exempt
                if policy == "keep_season":
                    sid = df.at[idx, "series_id"]
                    sn  = df.at[idx, "season_number"]
                    if pd.notna(sid) and pd.notna(sn):
                        latest = latest_season_for.get(int(sid))
                        keep_season_current = (latest is not None and int(sn) >= latest)

            # Recently aired (within RECENT_AIR_DAYS) — protect currently-airing seasons.
            recent_aired = False
            if "air_date_utc" in df.columns:
                _air = df.at[idx, "air_date_utc"]
                if pd.notna(_air) and _air:
                    try:
                        recent_aired = (now - pd.to_datetime(_air, utc=True)).days < self.RECENT_AIR_DAYS
                    except Exception:
                        pass

            # Household: hold the file while any configured member hasn't finished.
            # NaN = legacy/no-household → not blocked (preserve pre-household behaviour).
            household_blocked = False
            if "all_household_watched" in df.columns:
                _ahw = df.at[idx, "all_household_watched"]
                household_blocked = bool(pd.notna(_ahw) and not bool(_ahw))

            decision = episode_grace_decision(
                is_pilot=is_pilot, is_next=is_next, is_watched=df.at[idx, "is_watched"],
                has_last_watched=bool(lw), fid_protected=fid_protected,
                keep_series=keep_series, keep_season_current=keep_season_current,
                recent_aired=recent_aired, household_blocked=household_blocked,
            )
            if decision == "clear":     # pilot/next/protected/keep/recent/household — never mark
                df.at[idx, "marked_for_deletion"] = False
                continue
            if decision == "skip":      # not watched / no last-watched — leave as-is
                continue

            # ── Mark: anchor on the latest household watch when set, else last_watched_at,
            # so the grace window starts from when the *last* member finished.
            lw_anchor = lw
            if "household_last_watched_at" in df.columns:
                _hlw = df.at[idx, "household_last_watched_at"]
                if pd.notna(_hlw) and _hlw:
                    lw_anchor = _hlw
            row_td = grace_td
            if _grace_ramp:             # score-scaled window (favourites longer, forgettables shorter)
                _pct = df.at[idx, "watchability_percentile"] if "watchability_percentile" in df.columns else None
                row_td = grace_td * grace_window_multiplier(_pct, _grace_ramp)
            au, marked = grace_mark(lw_anchor, row_td, now)
            if au is not None:
                df.at[idx, "available_until"]     = au
                df.at[idx, "marked_for_deletion"] = marked

        return df

    @timeit("_do_purge_sonarr_deleted")
    def _do_purge_sonarr_deleted(
        self, instance: str, df: pd.DataFrame
    ) -> tuple[pd.DataFrame, dict]:
        """
        Check Sonarr for every row marked for deletion.  Rows whose episode
        file no longer exists in Sonarr are dropped from the DataFrame.

        One API call per unique series (fetches all episode files at once).

        Returns ``(updated_df, stats)``.
        """
        stats = {"checked": 0, "purged": 0, "still_pending": 0}

        if "marked_for_deletion" not in df.columns:
            return df, stats

        pending_mask = df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
        if not pending_mask.any():
            return df, stats

        # One episodefile API call per series
        series_ids = df.loc[pending_mask, "series_id"].dropna().unique()
        live_file_ids: dict[int, set | None] = {}
        for sid in series_ids:
            try:
                files = self._get_episode_files(instance, int(sid))
                live_file_ids[int(sid)] = {f.get("id") for f in files if f.get("id")}
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Could not verify episode files for series {sid}: {e}"
                )
                live_file_ids[int(sid)] = None  # unknown — leave row in place

        drop_indices: list = []
        for idx in df.index[pending_mask]:
            stats["checked"] += 1
            sid = df.at[idx, "series_id"]
            fid = df.at[idx, "episode_file_id"]
            if pd.isna(sid):
                stats["still_pending"] += 1
                continue
            live = live_file_ids.get(int(sid))
            if live is None:  # API call failed — leave
                stats["still_pending"] += 1
                continue
            title  = df.at[idx, "series_title"] or f"series {sid}"
            s_num  = int(df.at[idx, "season_number"]  or 0)
            e_num  = int(df.at[idx, "episode_number"]  or 0)
            if pd.isna(fid):
                # Orphan: marked for deletion but never had a Sonarr file id
                # (e.g. a watched stub). Nothing to confirm — drop as cleanup.
                drop_indices.append(idx)
                stats["purged"] += 1
                self.logger.log_info(
                    f"  🧹 Dropped orphan: '{title}' S{s_num:02d}E{e_num:02d} "
                    f"(no episode_file_id — never tracked in Sonarr)"
                )
            elif fid not in live:
                drop_indices.append(idx)
                stats["purged"] += 1
                self.logger.log_info(
                    f"  🗑️ Purged: '{title}' S{s_num:02d}E{e_num:02d} "
                    f"(file {int(fid)} confirmed deleted from Sonarr)"
                )
            else:
                stats["still_pending"] += 1

        if drop_indices:
            df = df.drop(index=drop_indices).reset_index(drop=True)
            self.logger.log_info(
                f"🧹 Deletion purge: {stats['purged']} row(s) removed, "
                f"{stats['still_pending']} still pending Sonarr removal."
            )

        return df, stats

    @timeit("_do_cleanup_non_essential")
    def _do_cleanup_non_essential(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """
        Drop genuine orphan rows that carry no actionable value.

        Keep:
        * Pilots (``is_pilot=True``) — codec / quality fingerprint.
        * Next-episode rows (``next_episode=True``) — ingestion target.
        * ALL watched rows (``is_watched=True``), regardless of grace-period
          status — rows marked for deletion must remain until
          ``_do_purge_sonarr_deleted`` confirms the file is gone from Sonarr.
          Removing them early would lose the lifecycle state needed for the
          deletion handshake.

        Remove:
        * Unwatched, non-pilot, non-next-episode rows — true orphans from
          half-completed syncs or stale intermediate states.
        """
        if df.empty:
            return df, 0

        def _col(name: str, default: bool = False) -> pd.Series:
            return df[name].infer_objects(copy=False).fillna(default).astype(bool) if name in df.columns else pd.Series(default, index=df.index)

        is_pilot  = _col("is_pilot")
        is_next   = _col("next_episode")
        is_watched = _col("is_watched")

        keep_mask = is_pilot | is_next | is_watched
        removed   = int((~keep_mask).sum())

        if removed:
            df = df[keep_mask].reset_index(drop=True)
            self.logger.log_info(
                f"🧹 Non-essential cleanup: {removed} orphaned row(s) removed "
                "(not pilot, not next-episode, and never watched)."
            )

        return df, removed

    @timeit("_resolve_keep_policy_map")
    def _resolve_keep_policy_map(
        self, instance: str, df: pd.DataFrame
    ) -> "dict[int, str | None]":
        """
        Build a ``{series_id: keep_policy}`` map from Sonarr tag assignments.

        Fetches the tag catalogue from Sonarr (``GET /api/v3/tag``) and
        matches labels against ``"keep_series"`` and ``"keep_season"``.
        The per-series tag assignments are taken from the letter-bucketed
        series cache (``sonarr_cache.series.get_series_tags_map``) — no
        extra API call per series.

        Policy values
        -------------
        ``"keep_series"``
            Series carries the ``keep_series`` Sonarr tag.  No episode from
            this series will ever be marked for deletion.

        ``"keep_season"``
            Series carries the ``keep_season`` Sonarr tag.  Only episodes
            from the current (highest non-special) season are protected;
            older seasons remain eligible for the normal grace-period
            deletion cycle.

        ``None``
            No keep tag — default grace-period behaviour applies.

        ``keep_series`` takes precedence when both tags are present.

        Returns an empty dict when neither tag label exists in Sonarr, or
        when the series cache is unavailable.
        """
        if self.sonarr_api is None:
            return {}

        # 1. Fetch tag catalogue → build label → id mapping
        raw_tags = self.sonarr_api._make_request(instance, "tag", fallback=[]) or []
        tag_id_for: dict[str, int] = {
            t["label"].lower(): t["id"]
            for t in raw_tags
            if t.get("label") and t.get("id") is not None
        }

        keep_series_id = tag_id_for.get("keep_series")
        keep_season_id = tag_id_for.get("keep_season")

        if keep_series_id is None and keep_season_id is None:
            self.logger.log_debug(
                "  ℹ️ No 'keep_series' or 'keep_season' tags exist in Sonarr — "
                "keep-policy resolution skipped."
            )
            return {}

        # 2. Get per-series tag-id lists from the cached series data
        series_cache = getattr(self.sonarr_cache, "series", None)
        if not series_cache:
            self.logger.log_debug(
                "  ⚠️ sonarr_cache.series not available — "
                "keep-policy resolution skipped."
            )
            return {}

        tags_map: dict[int, list] = series_cache.get_series_tags_map(instance)
        # {series_id: [tag_ids, ...]}

        # 3. Resolve policy for every series referenced by the Parquet — the
        #    per-series decision is delegated to the brain (the FETCH above stays here).
        from scripts.managers.machine_learning.classification.keep_policy import (
            series_keep_policy,
        )
        policy_map: dict[int, str | None] = {}
        for sid in df["series_id"].dropna().astype(int).unique():
            policy_map[sid] = series_keep_policy(
                tags_map.get(sid) or [], keep_series_id, keep_season_id
            )

        return policy_map

    @timeit("_sync_keep_policies")
    def _sync_keep_policies(
        self, df: pd.DataFrame, instance: str
    ) -> pd.DataFrame:
        """
        Stamp the ``keep_policy`` column on every row from Sonarr tag state.

        Calls ``_resolve_keep_policy_map`` once per sync cycle to build the
        mapping, then iterates rows to set the value.  Safe to call even when
        ``sonarr_api`` is not wired — returns df unchanged in that case.

        This must run **before** ``_apply_grace_period`` so that the grace
        period logic can read the policy flag when deciding whether to set
        ``marked_for_deletion``.
        """
        if self.sonarr_api is None:
            return df

        if "keep_policy" not in df.columns:
            df["keep_policy"] = pd.Series([None] * len(df), dtype=object, index=df.index)
        elif df["keep_policy"].dtype != object:
            # Reindex can restore an all-NA column as float64; cast before writing strings.
            df["keep_policy"] = df["keep_policy"].astype(object)

        policy_map = self._resolve_keep_policy_map(instance, df)
        if not policy_map:
            return df

        sid_series = pd.to_numeric(df["series_id"], errors="coerce").fillna(-1).astype(int)
        for idx in df.index:
            sid = sid_series.at[idx]
            if sid >= 0:
                df.at[idx, "keep_policy"] = policy_map.get(sid)

        n_keep_series = sum(1 for v in policy_map.values() if v == "keep_series")
        n_keep_season = sum(1 for v in policy_map.values() if v == "keep_season")
        if n_keep_series or n_keep_season:
            self.logger.log_info(
                f"🔒 Keep-policy sync: {n_keep_series} series tagged 'keep_series', "
                f"{n_keep_season} tagged 'keep_season'."
            )

        return df

    @timeit("_do_acquire_next_episodes")
    def _do_acquire_next_episodes(
        self, instance: str, df: pd.DataFrame
    ) -> dict:
        """
        For every pending-acquisition row (``next_episode=True`` and
        ``episode_file_id`` is null), enable monitoring (and clear stale queue
        items). The actual EpisodeSearch is DEFERRED to ``run_jit_quality_upgrades``,
        which grabs these fresh episodes at the reserve-aware JIT tier (the same path
        as on-disk re-quality), so a "just-in-time" acquire is space-calibrated too.

        Flow per row
        ------------
        1. Look up the episode in Sonarr by ``(series_id, season, episode_number)``.
        2. If an existing queue item is found for this episode, cancel it first
           (removeFromClient=true, blocklist=false) so the fresh search at the
           best-scored quality profile can replace it.
        3. ``PUT /api/v3/episode/monitor`` with ``monitored=True``.
        4. ``POST /api/v3/command`` ``{name: "EpisodeSearch", episodeIds: [ep_id]}``.

        If the episode already has a file in Sonarr (downloaded since we last
        checked), the row is noted but no search is triggered — the file will
        be picked up at the next ``sync_from_tautulli`` run.

        Respects ``self.dry_run`` — no requests are made in dry-run mode.

        Returns a stats dict with keys:
        ``checked``, ``triggered``, ``already_have_file``,
        ``not_in_sonarr``, ``failed``, ``queue_cancelled``.
        """
        stats: dict = {
            "checked":           0,
            "triggered":         0,
            "already_have_file": 0,
            "not_in_sonarr":     0,
            "failed":            0,
            "queue_cancelled":   0,
        }

        if "next_episode" not in df.columns or "episode_file_id" not in df.columns:
            return stats

        # Pending rows: flagged as next_episode but no file downloaded yet
        pending_mask = (
            df["next_episode"].infer_objects(copy=False).fillna(False).astype(bool)
            & df["episode_file_id"].isna()
        )
        if not pending_mask.any():
            return stats

        # Space check — bail early if Sonarr reports insufficient free space.
        # Uses the same rootfolder endpoint Radarr uses for its universe quality pass.
        free_gb = self._get_free_space_gb(instance)
        # Gate the stay-ahead prefetch at the band TOP U (= free_space_limit + headroom, or
        # 25% of the total drive when unset), NOT the floor T. Prefetch is a "consume space"
        # op like upgrades/JIT, so it pauses across the whole pressure band [T, U): once free
        # dips below U the space-pressure downgrade/delete passes own that band, and grabbing
        # new episodes there would just fight them (grab-high → re-grab-low churn). Prefetch
        # resumes only once free recovers above U. MIN_FREE_SPACE_GB is the last-resort
        # fallback (free_space_limit unset AND total drive unreadable).
        _total_gb = self._get_total_space_gb(instance)
        alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, _total_gb)
        _, acquire_floor = space_targets(
            self.config, fallback_gb=self.MIN_FREE_SPACE_GB, total_gb=_total_gb,
        )
        if free_gb < acquire_floor:
            self.logger.log_info(
                f"📦 Acquisition skipped for '{instance}': {free_gb:.1f} GB free "
                f"< {acquire_floor:.0f} GB band top (in the space-pressure band). "
                f"{int(pending_mask.sum())} episode(s) remain queued."
            )
            stats["checked"] = int(pending_mask.sum())
            return stats

        self.logger.log_debug(
            f"📦 Free space check for '{instance}': {free_gb:.1f} GB — proceeding with acquisition."
        )

        # ── Batch by series: 1 season fetch/season, 1 PUT, 1 POST/series ──────────────────────────
        # Previous: 3 API calls per episode (season fetch + PUT + POST) = 60 calls for 20 eps
        # Now:      1 season fetch per unique season + 1 PUT (all eps) + 1 POST per series
        #           = ~7 calls for 20 eps across 5 series
        pending_by_series: dict[int, list[tuple]] = {}  # sid → [(sn, en, title), ...]
        for idx in df.index[pending_mask]:
            sid   = df.at[idx, "series_id"]
            sn    = df.at[idx, "season_number"]
            en    = df.at[idx, "episode_number"]
            title = df.at[idx, "series_title"] or f"series {sid}"
            if pd.isna(sid) or pd.isna(sn) or pd.isna(en):
                stats["not_in_sonarr"] += 1
                continue
            pending_by_series.setdefault(int(sid), []).append((int(sn), int(en), title))

        all_monitor_ids: list[int] = []
        per_series_search: dict[int, tuple[str, list[int]]] = {}  # sid → (title, [ep_ids])
        monitored_eps: list[tuple] = []   # (series_title, sn, en, sid) → which eps this pass monitored

        # series_id(str) → recent household watcher(s) for the 'For' column — same attribution the
        # prefetch + JIT grab grids use. Best-effort: {} when unavailable → 'For' shows '-'.
        _jit_watchers = (self.global_cache.get(f"sonarr/{instance}/jit_watchers")
                         if self.global_cache else None) or {}

        for sid, episodes in pending_by_series.items():
            series_title = episodes[0][2]
            seasons_needed = {sn for sn, _en, _t in episodes}

            # One season fetch per unique season for this series
            ep_map: dict[tuple[int, int], dict] = {}  # (sn, en) → sonarr ep obj
            for sn in seasons_needed:
                try:
                    for ep_obj in self._get_episodes_for_season(instance, sid, sn):
                        ep_map[(sn, ep_obj.get("episodeNumber"))] = ep_obj
                except Exception as e:
                    self.logger.log_warning(
                        f"  ⚠️ Season fetch failed for '{series_title}' S{sn:02d}: {e}"
                    )

            for sn, en, title in episodes:
                stats["checked"] += 1
                ep_obj = ep_map.get((sn, en))
                if not ep_obj:
                    self.logger.log_debug(f"  ↵ '{title}' S{sn:02d}E{en:02d} not in Sonarr")
                    stats["not_in_sonarr"] += 1
                    continue

                ep_id = ep_obj.get("id")
                fid   = ep_obj.get("episodeFileId")
                if fid:
                    self.logger.log_debug(f"  ✅ '{title}' S{sn:02d}E{en:02d} already downloaded")
                    stats["already_have_file"] += 1
                    continue

                if ep_id:
                    all_monitor_ids.append(ep_id)
                    monitored_eps.append((series_title, sn, en, sid))
                    _t, _ids = per_series_search.get(sid, (series_title, []))
                    _ids.append(ep_id)
                    per_series_search[sid] = (_t, _ids)
                    stats["triggered"] += 1

        if not all_monitor_ids:
            if stats["checked"]:
                self.logger.log_info(
                    f"📥 Acquisition monitor pass: 0 monitored, "
                    f"{stats['already_have_file']} already downloaded, "
                    f"{stats['not_in_sonarr']} not in Sonarr."
                )
            return stats

        if self.dry_run:
            self.logger.log_info(
                f"  [dry_run] Would monitor {len(all_monitor_ids)} next-up ep(s) "
                f"(searched at the calibrated tier by the JIT grab pass)"
            )
        else:
            # Cancel any stale queue items for these episodes (wrong quality / old release) so
            # the unified JIT grab pass searches fresh at the reserve-aware calibrated tier.
            from scripts.managers.factories.mixins.queue_cancel import QueueCancelMixin
            _qc = QueueCancelMixin()
            _qc.sonarr_api = self.sonarr_api
            _qc.logger     = self.logger
            _qc.dry_run    = False  # already checked dry_run above
            for sid, (series_title, ep_ids) in per_series_search.items():
                stats["queue_cancelled"] += _qc._cancel_sonarr_queue_for_episodes(
                    instance, ep_ids, series_title=series_title
                )

            # One PUT monitors every pending next-up episode across all series at once. The
            # actual EpisodeSearch is DEFERRED to run_jit_quality_upgrades, which grabs these
            # fresh episodes AND on-disk re-quality episodes at the reserve-aware JIT tier via
            # the shared step-down worker (one QP flip per series-tier group, no over-grab).
            try:
                self.sonarr_api._make_request(
                    instance, "episode/monitor", method="PUT",
                    payload={"episodeIds": all_monitor_ids, "monitored": True},
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Batch monitor PUT failed: {e}")
                stats["failed"] += len(all_monitor_ids)
                stats["triggered"] -= len(all_monitor_ids)
                return stats

        if stats["checked"]:
            prefix = "[dry_run] " if self.dry_run else ""
            self.logger.log_info(
                f"📥 {prefix}Acquisition monitor pass: {stats['triggered']} monitored "
                f"(searched at the calibrated tier by the JIT grab pass), "
                f"{stats['queue_cancelled']} stale queue item(s) cancelled, "
                f"{stats['already_have_file']} already downloaded, "
                f"{stats['not_in_sonarr']} not in Sonarr, "
                f"{stats['failed']} failed."
            )

        # Per-episode detail of WHICH next-ups this pass monitored — the one-line count above
        # stays in the live log; the detail moves to the end-of-run summary so it sits next to
        # the Next-episode prefetch (order 10) and JIT next-up grab plan (order 12) grids, which
        # carry the same episodes through the prefetch → monitor → grab pipeline. ASCII cells.
        if monitored_eps:
            _mon_rows = [
                [title, f"S{sn:02d}E{en:02d}",
                 ", ".join((_jit_watchers.get(str(sid)) or [])[:2]) or "-"]
                for (title, sn, en, sid) in sorted(monitored_eps, key=lambda e: (e[0].lower(), e[1], e[2]))
            ]
            _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
            if _rs is not None:
                _rs.add_rows("sonarr", "Next-up monitored", instance,
                             ["Series", "Ep", "For"], _mon_rows, order=11)
            else:
                self.logger.log_grid(
                    ["Series", "Ep", "For"], _mon_rows,
                    title=(
                        f"Next-up monitored - '{instance}'"
                        f"{' [dry_run]' if self.dry_run else ''}"
                    ),
                    cap=24,
                )

        return stats

    @timeit("_do_delete_marked_files")
    def _do_delete_marked_files(
        self, instance: str, df: pd.DataFrame
    ) -> tuple[pd.DataFrame, dict]:
        """
        Delete episode files from Sonarr for every row marked for deletion.

        Safety rules
        ------------
        * **PILOTS ARE NEVER DELETED.**  If a row has both
          ``marked_for_deletion=True`` *and* ``is_pilot=True``, the deletion
          flag is unconditionally cleared and the row is left untouched.
          A warning is logged so the anomaly is always visible in the log.
        * ``next_episode=True`` rows are skipped — they are the current
          acquisition target and must not be removed while awaiting download.
        * Rows with no ``episode_file_id`` are skipped — nothing to delete.
        * ``self.dry_run`` — no DELETE requests are sent; only log lines are
          emitted so the behaviour can be verified before going live.

        Returns ``(updated_df, stats)`` where stats keys are:
        ``checked``, ``deleted``, ``failed``,
        ``skipped_pilot``, ``skipped_no_file``, ``dry_run``.
        """
        stats: dict = {
            "checked":              0,
            "deleted":              0,
            "failed":               0,
            "skipped_pilot":        0,
            "skipped_keep":         0,
            "skipped_recent_air":   0,
            "skipped_household":    0,
            "skipped_shared_file":  0,     # rows skipped because a multi-ep sibling is guarded
            "skipped_no_file":      0,
            "coalesced_multiep":    0,     # extra rows sharing an already-handled file id
            "bytes_freed":          0.0,   # bytes deleted (or would-be deleted in dry_run)
            "dry_run":              self.dry_run,
        }
        now = datetime.now(tz=timezone.utc)

        if "marked_for_deletion" not in df.columns:
            return df, stats

        marked_mask = df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
        if not marked_mask.any():
            return df, stats

        if not deletions_enabled(self.config):
            # HARD SAFETY GATE (single choke point — covers the standalone wrapper AND
            # the sync_from_tautulli call site): no operator-set free_space_limit → no
            # deletions. Grace MARKING is unaffected; rows stay marked for when a floor
            # is configured. main.py emits the loud end-of-run banner.
            self.logger.log_warning(
                "[EpisodeFiles] deletions DISABLED — free_space_limit is not set; "
                f"leaving {int(marked_mask.sum())} marked row(s) untouched."
            )
            return df, stats

        # Pre-compute protected file IDs (defence-in-depth mirror of _apply_grace_period).
        # Uses _build_pilot_file_ids so stub-pilot series are also covered: if a
        # series has no resolved pilot file yet, the earliest watched episode is
        # treated as the de-facto pilot and its file ID is included in the set.
        pilot_file_ids = self._build_pilot_file_ids(df)

        # Pre-compute the WHOLE-FILE protected set: every episode_file_id that
        # has ANY row hitting a protective guard (pilot, keep_series,
        # keep_season-in-latest-season, recent-air, household-not-all-watched).
        # Multi-episode files share one episodeFileId across several rows, so a
        # watched/grace-expired row here may share its file with a guarded
        # sibling whose row is unmarked (never visited by this loop) or visited
        # only after the file is already deleted.  Deleting per-row would destroy
        # that sibling.  Skipping any row whose fid is in this set makes
        # protection whole-file.  (reuses pilot_file_ids computed above.)
        protected_file_ids = self._build_protected_file_ids(df, now, pilot_file_ids)

        # Pre-compute latest season per keep_season series for the secondary guard.
        # (Primary guard is _apply_grace_period — this is defence-in-depth.)
        latest_season_for: dict[int, int] = {}
        if "keep_policy" in df.columns and (df["keep_policy"] == "keep_season").any():
            keep_season_sids = set(
                df.loc[df["keep_policy"] == "keep_season", "series_id"]
                .dropna().astype(int).unique()
            )
            _sid_num = pd.to_numeric(df["series_id"], errors="coerce").fillna(-1).astype(int)
            _sn_num  = pd.to_numeric(df["season_number"], errors="coerce")
            for sid in keep_season_sids:
                non_special = _sn_num[(_sid_num == sid) & (_sn_num > 0)].dropna()
                if not non_special.empty:
                    latest_season_for[sid] = int(non_special.max())

        # Multi-episode files share one episodeFileId across several episode
        # rows; coalesce so each physical file is deleted (and counted) once.
        attempted_fids: set[int] = set()

        for idx in df.index[marked_mask]:
            stats["checked"] += 1
            is_pilot = bool(df.at[idx, "is_pilot"]) if "is_pilot" in df.columns else False
            title    = df.at[idx, "series_title"] or f"series {df.at[idx, 'series_id']}"
            sn       = df.at[idx, "season_number"]
            en       = df.at[idx, "episode_number"]
            sn_str   = f"S{int(sn):02d}" if pd.notna(sn) else "S??"
            en_str   = f"E{int(en):02d}" if pd.notna(en) else "E??"

            # ── HARD PILOT GUARD ──────────────────────────────────────────────
            # This check MUST come first — before any file-id or policy check —
            # so a pilot row with a file can never slip through to the DELETE call.
            if is_pilot:
                self.logger.log_warning(
                    f"  🛡️ PILOT GUARD: '{title}' {sn_str}{en_str} is marked for "
                    f"deletion but is a pilot — clearing flag, skipping. "
                    f"PILOTS ARE NEVER DELETED."
                )
                df.at[idx, "marked_for_deletion"] = False
                stats["skipped_pilot"] += 1
                continue

            # ── PILOT FILE GUARD (secondary / defence-in-depth) ──────────────
            # The row-level pilot guard above already handles is_pilot=True rows.
            # This catches watched rows (is_pilot=False) that share a file_id with
            # a pilot row — deleting the file would silently destroy the pilot.
            fid_pre = df.at[idx, "episode_file_id"]
            if pd.notna(fid_pre) and fid_pre in pilot_file_ids:
                self.logger.log_warning(
                    f"  🛡️ PILOT FILE GUARD: '{title}' {sn_str}{en_str} "
                    f"(episodeFileId={int(fid_pre)}) is the pilot file for this "
                    f"series — clearing deletion flag, skipping."
                )
                df.at[idx, "marked_for_deletion"] = False
                stats["skipped_pilot"] += 1
                continue

            # ── KEEP-POLICY GUARD (secondary / defence-in-depth) ─────────────
            # _apply_grace_period is the primary gatekeeper; this catches any
            # rows that slipped through due to a policy change between runs.
            if "keep_policy" in df.columns:
                policy = df.at[idx, "keep_policy"]
                if policy == "keep_series":
                    self.logger.log_warning(
                        f"  🔒 KEEP GUARD: '{title}' {sn_str}{en_str} has "
                        f"keep_series policy — clearing deletion flag, skipping."
                    )
                    df.at[idx, "marked_for_deletion"] = False
                    stats["skipped_keep"] += 1
                    continue
                if policy == "keep_season":
                    sid = df.at[idx, "series_id"]
                    if pd.notna(sid) and pd.notna(sn):
                        latest = latest_season_for.get(int(sid))
                        if latest is not None and int(sn) >= latest:
                            self.logger.log_warning(
                                f"  🔒 KEEP GUARD: '{title}' {sn_str}{en_str} is "
                                f"in the latest season (S{latest:02d}) with "
                                f"keep_season policy — clearing flag, skipping."
                            )
                            df.at[idx, "marked_for_deletion"] = False
                            stats["skipped_keep"] += 1
                            continue

            # ── RECENTLY-AIRED GUARD (secondary / defence-in-depth) ─────────────
            # _apply_grace_period is the primary gatekeeper; this catches rows
            # that were marked before the guard was introduced or whose
            # air_date_utc was populated after the grace-period pass ran.
            if "air_date_utc" in df.columns:
                _air = df.at[idx, "air_date_utc"]
                if pd.notna(_air) and _air:
                    try:
                        days_since_air = (now - pd.to_datetime(_air, utc=True)).days
                        if days_since_air < self.RECENT_AIR_DAYS:
                            self.logger.log_warning(
                                f"  🛡️ RECENT AIR GUARD: '{title}' {sn_str}{en_str} "
                                f"aired {days_since_air}d ago — clearing deletion flag, skipping."
                            )
                            df.at[idx, "marked_for_deletion"] = False
                            stats["skipped_recent_air"] += 1
                            continue
                    except Exception:
                        pass

            # ── HOUSEHOLD WATCH GUARD (secondary / defence-in-depth) ─────────────
            # Mirrors the check in _apply_grace_period.  Catches rows that were
            # marked in a previous run before household tracking was active, or
            # when a household member's watch was logged after the grace-period
            # pass already ran.
            if "all_household_watched" in df.columns:
                _ahw = df.at[idx, "all_household_watched"]
                if pd.notna(_ahw) and not bool(_ahw):
                    self.logger.log_warning(
                        f"  🛡️ HOUSEHOLD GUARD: '{title}' {sn_str}{en_str} — "
                        "not all household members have watched — "
                        "clearing deletion flag, skipping."
                    )
                    df.at[idx, "marked_for_deletion"] = False
                    stats["skipped_household"] += 1
                    continue

            fid = df.at[idx, "episode_file_id"]
            if pd.isna(fid):
                stats["skipped_no_file"] += 1
                continue

            fid = int(fid)

            # ── WHOLE-FILE PROTECTION GUARD ──────────────────────────────────
            # The per-row guards above only inspected THIS row.  A multi-episode
            # file backs several episode rows under one episodeFileId; this row
            # may be watched/grace-expired with no guard of its own, yet share
            # its file with a SIBLING episode that is pilot/keep/recent-air/
            # household protected.  protected_file_ids is the union of every
            # guard across ALL rows (marked or not), so if any sibling is
            # protected we skip the whole file — `DELETE episodefile/{fid}`
            # would otherwise destroy that sibling too.
            if fid in protected_file_ids:
                self.logger.log_warning(
                    f"  🛡️ SHARED-FILE GUARD: '{title}' {sn_str}{en_str} "
                    f"(episodeFileId={fid}) shares a multi-episode file with a "
                    f"guarded episode — clearing deletion flag, skipping."
                )
                df.at[idx, "marked_for_deletion"] = False
                stats["skipped_shared_file"] += 1
                continue

            # Coalesce multi-episode files: one episodeFileId backs N episode
            # rows. Deleting/counting per row would inflate reclaimed bytes N×
            # and fire N-1 redundant DELETEs (guaranteed 404s). Handle each
            # unique file id once; the sibling rows stay marked and are removed
            # by _do_purge_sonarr_deleted once Sonarr confirms the file is gone.
            if fid in attempted_fids:
                stats["coalesced_multiep"] += 1
                continue
            attempted_fids.add(fid)

            _sz = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
            _sz_f = float(_sz) if pd.notna(_sz) else 0.0

            # ── Build reason string for logging ─────────────────────────────
            _lw    = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
            _avail = df.at[idx, "available_until"] if "available_until" in df.columns else None
            _wc    = df.at[idx, "watch_count"] if "watch_count" in df.columns else 0
            _pct   = df.at[idx, "percent_complete"] if "percent_complete" in df.columns else None
            _lw_str   = str(_lw)[:10] if _lw else "unknown"
            _pct_str  = f"{int(_pct)}%" if _pct is not None and pd.notna(_pct) else "?%"
            reason = (
                f"watched {_wc}x ({_pct_str} complete), "
                f"last watched {_lw_str}, "
                f"grace period expired {str(_avail)[:16] if _avail else 'N/A'}"
            )

            if self.dry_run:
                stats["bytes_freed"] += _sz_f
                self.logger.log_info(
                    f"  🗑️ [dry_run] Would delete: '{title}' {sn_str}{en_str} "
                    f"({self._fmt_bytes(_sz_f)}) — {reason}"
                )
                stats["deleted"] += 1
                continue

            try:
                self.sonarr_api._make_request(
                    instance,
                    f"episodefile/{fid}",
                    method="DELETE",
                )
                stats["bytes_freed"] += _sz_f
                self.logger.log_info(
                    f"  🗑️ Deleted: '{title}' {sn_str}{en_str} "
                    f"({self._fmt_bytes(_sz_f)}) — {reason}"
                )
                stats["deleted"] += 1
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Delete failed for '{title}' {sn_str}{en_str} "
                    f"(episodeFileId={fid}): {e}"
                )
                stats["failed"] += 1

        if stats["checked"]:
            prefix = "[dry_run] " if self.dry_run else ""
            verb   = "would free" if self.dry_run else "freed"
            self.logger.log_table(
                ["Outcome", "Count"],
                [
                    ["deleted",            stats["deleted"]],
                    ["failed",             stats["failed"]],
                    ["pilot guard",        stats["skipped_pilot"]],
                    ["keep-policy guard",  stats["skipped_keep"]],
                    ["recent-air guard",   stats["skipped_recent_air"]],
                    ["household guard",    stats["skipped_household"]],
                    ["shared-file guard",  stats["skipped_shared_file"]],
                    ["no file id",         stats["skipped_no_file"]],
                    ["multi-ep coalesced", stats["coalesced_multiep"]],
                ],
                title=f"🗑️ {prefix}Sonarr deletion pass '{instance}' ({verb} {self._fmt_bytes(stats['bytes_freed'])})",
                caption="Per-pass outcome of the Sonarr file deletion sweep: how many "
                        "episode files were removed and how many were held back by each guard.",
                descriptions=[
                    "episode files actually deleted this pass",
                    "delete calls that errored",
                    "files kept: protected pilot episode",
                    "files kept: keep_series / keep_season tag",
                    "files kept: episode aired too recently",
                    "files kept: a household member has not watched",
                    "files kept: file shared by another tracked episode",
                    "rows skipped: no Sonarr episode file id",
                    "extra rows folded into one multi-episode file delete",
                ],
            )

        return df, stats

    # ── Public lifecycle methods (standalone callable) ──────────────────────────

    @LoggerManager().log_function_entry
    @timeit("purge_sonarr_deleted")
    def purge_sonarr_deleted(self, instance: str) -> dict:
        """
        Load the Parquet, purge rows whose Sonarr episode file is gone, save.
        Safe to call standalone between full syncs.
        """
        instance = self._resolve_instance(instance)
        df = self.load(instance)
        df, stats = self._do_purge_sonarr_deleted(instance, df)
        if stats["purged"] and not self.dry_run:
            self.save(instance, df)
        elif stats["purged"] and self.dry_run:
            self.logger.log_info(
                f"[dry_run] Skipping Parquet write for '{instance}' "
                f"— {stats['purged']} purge(s) computed but not saved."
            )
        return stats

    @LoggerManager().log_function_entry
    @timeit("cleanup_non_essential")
    def cleanup_non_essential(self, instance: str) -> int:
        """
        Load the Parquet, remove non-essential rows, save.
        Safe to call standalone between full syncs.
        """
        instance = self._resolve_instance(instance)
        df = self.load(instance)
        df, removed = self._do_cleanup_non_essential(df)
        if removed and not self.dry_run:
            self.save(instance, df)
        elif removed and self.dry_run:
            self.logger.log_info(
                f"[dry_run] Skipping Parquet write for '{instance}' "
                f"— {removed} cleanup(s) computed but not saved."
            )
        return removed

    @LoggerManager().log_function_entry
    @timeit("delete_marked_files")
    def delete_marked_files(self, instance: str) -> dict:
        """
        Load the Parquet, delete all non-pilot files marked for deletion from
        Sonarr, then purge confirmed-deleted rows and save.

        **PILOTS ARE NEVER DELETED** — the hard pilot guard inside
        ``_do_delete_marked_files`` clears any accidental flags and logs a
        warning so the anomaly is always visible.

        Safe to call standalone between full ``sync_from_tautulli`` runs
        (e.g. from a scheduled maintenance job).

        Returns a combined stats dict with keys from ``_do_delete_marked_files``
        plus ``"purged"`` from ``_do_purge_sonarr_deleted``.
        """
        instance = self._resolve_instance(instance)
        if not deletions_enabled(self.config):
            # HARD SAFETY GATE: no operator-set free_space_limit → no deletions.
            # Grace MARKING is unaffected; only this destructive pass skips.
            self.logger.log_warning(
                "[EpisodeFiles] deletions DISABLED — free_space_limit is not set; "
                "skipping the grace-marked episode delete pass."
            )
            return {"checked": 0, "deleted": 0, "failed": 0, "purged": 0,
                    "skipped_disabled": True}
        df = self.load(instance)
        df, delete_stats = self._do_delete_marked_files(instance, df)
        df, purge_stats  = self._do_purge_sonarr_deleted(instance, df)
        combined = {**delete_stats, "purged": purge_stats["purged"]}
        needs_save = delete_stats["deleted"] or purge_stats["purged"] or delete_stats["skipped_pilot"]
        if needs_save and not self.dry_run:
            self.save(instance, df)
        elif needs_save and self.dry_run:
            self.logger.log_info(
                f"[dry_run] Skipping Parquet write for '{instance}' "
                f"— changes computed but not saved."
            )
        return combined

    # ── Tautulli helpers ────────────────────────────────────────────────────────

    @timeit("_collect_tautulli_episode_history")
    def _collect_tautulli_episode_history(self) -> dict:
        """
        Pull episode history from every configured Tautulli instance and
        aggregate by ``(grandparent_title, season, episode_number)``.

        Uses ``TautulliAPI`` from ``tautulli.instances.api`` directly — the
        top-level ``TautulliManager`` has broken imports (missing ``api.py``
        and ``validator.py``) so it is deliberately bypassed here.

        Config shapes supported:

        Flat single-instance::

            tautulli:
              url: localhost
              port: "8181"
              api: <key>

        Multi-instance::

            tautulli:
              home:
                url: localhost
                port: "8181"
                api: <key>

        Returns a dict keyed by ``(series_title, season, episode)``:
        ::

            {
                ("Breaking Bad", 2, 5): {
                    "watch_count": 3,
                    "last_watched_at": "2024-01-15T22:00:00+00:00",
                    "percent_complete": 95,
                }
            }
        """
        # Import the working low-level API — NOT TautulliManager (broken imports)
        from scripts.managers.services.tautulli.instances.api import TautulliAPI as TautulliInstanceAPI

        tautulli_config = (self.config or {}).get("tautulli", {})
        if not tautulli_config:
            self.logger.log_info(
                "ℹ️ No 'tautulli' key in config — skipping episode history sync."
            )
            return {}

        # Resolve per-instance config dicts
        if all(isinstance(v, str) for v in tautulli_config.values()):
            # Flat single-instance: {"url": "...", "port": "...", "api": "..."}
            instance_configs: dict[str, dict] = {"default": tautulli_config}
        else:
            # Multi-instance: {"home": {...}, "remote": {...}}
            instance_configs = {
                k: v for k, v in tautulli_config.items() if isinstance(v, dict)
            }

        if not instance_configs:
            self.logger.log_warning(
                "⚠️ Tautulli config present but no valid instance dicts found."
            )
            return {}

        aggregated: dict[tuple, dict] = defaultdict(
            lambda: {
                "watch_count": 0,
                "last_watched_at": None,
                "percent_complete": 0,
                "per_user": {},  # username → latest ISO-8601 timestamp (or None if no date)
            }
        )
        total_raw_entries = 0

        for instance_name, instance_config in instance_configs.items():
            try:
                api = TautulliInstanceAPI(
                    logger=self.logger,
                    instance_config=instance_config,
                    cache=self.global_cache,
                )
                # Log the resolved base URL (never the API key) so mismatched
                # config entries (e.g. wrong base_url vs url/port) are easy to spot.
                self.logger.log_info(
                    f"🔌 Tautulli '{instance_name}' connecting to: {api.base_url}"
                )
                response = api.get_history(length=5000)
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Tautulli '{instance_name}' history request failed: {e}"
                )
                continue

            # Response structure: {"response": {"data": {"data": [...], "recordsTotal": N}}}
            entries = ((response or {}).get("response") or {}).get("data", {})
            if isinstance(entries, dict):
                entries = entries.get("data", [])
            if not isinstance(entries, list):
                self.logger.log_warning(
                    f"⚠️ Unexpected Tautulli response shape for '{instance_name}'"
                )
                continue

            self.logger.log_info(
                f"📺 Tautulli '{instance_name}': {len(entries)} history entries retrieved"
            )
            total_raw_entries += len(entries)

            for entry in entries:
                if entry.get("media_type") != "episode":
                    continue

                title   = entry.get("grandparent_title")
                season  = entry.get("parent_media_index")   # season number
                episode = entry.get("media_index")           # episode number
                played  = entry.get("date")                  # Unix timestamp
                pct     = entry.get("percent_complete", 0)

                if not title or season is None or episode is None:
                    continue

                key = (title, int(season), int(episode))
                rec = aggregated[key]
                rec["watch_count"] += 1
                rec["percent_complete"] = max(rec["percent_complete"], pct or 0)
                if played:
                    ts = datetime.fromtimestamp(int(played), tz=timezone.utc).isoformat()
                    if rec["last_watched_at"] is None or ts > rec["last_watched_at"]:
                        rec["last_watched_at"] = ts

                # Track per-user timestamps for household watch-state resolution
                _user = str(entry.get("user") or "")
                if _user:
                    if played:
                        _uts = datetime.fromtimestamp(int(played), tz=timezone.utc).isoformat()
                        _prev = rec["per_user"].get(_user)
                        if _prev is None or _uts > _prev:
                            rec["per_user"][_user] = _uts
                    elif _user not in rec["per_user"]:
                        rec["per_user"][_user] = None  # watched but no date recorded

        result = dict(aggregated)
        self.logger.log_info(
            f"📊 Tautulli aggregation complete: {len(result)} unique episode(s) "
            f"from {total_raw_entries} raw entries across "
            f"{len(instance_configs)} instance(s)"
        )
        return result

    def _build_jit_watchers(self, instance: str, history: dict) -> dict:
        """``series_id``(str) → recent household watcher(s), most-recent first — derived
        from the per-user Tautulli watch timestamps. Purely for the JIT grab grid's
        'For' column (who each next-up was acquired for). Best-effort: a one-pass
        title→series_id map (cheap O(1) lookups) + last-watch per user per series;
        never raises, ``{}`` when the series cache is unavailable."""
        series_mgr = getattr(self.sonarr_cache, "series", None)
        if not series_mgr or not history:
            return {}
        title_to_sid: dict[str, int] = {}
        try:
            for s in series_mgr.iter_all_series(instance):
                t = (s.get("title") or "").strip().lower()
                if t and "id" in s:
                    title_to_sid.setdefault(t, s["id"])
        except Exception:
            return {}
        by_sid: dict[int, dict] = {}
        for (series_title, _season, _episode), watch in history.items():
            sid = title_to_sid.get((series_title or "").strip().lower())
            if sid is None:
                continue
            agg = by_sid.setdefault(int(sid), {})
            for user, ts in ((watch or {}).get("per_user") or {}).items():
                if not user:
                    continue
                ts = ts or ""                       # None → "" so it sorts last
                if user not in agg or ts > agg[user]:   # add on first sight, keep latest
                    agg[user] = ts
        return {
            str(sid): [u for u, _ in sorted(users.items(), key=lambda kv: kv[1], reverse=True)]
            for sid, users in by_sid.items() if users   # drop series with no watchers
        }

    # ── Household watch-state helpers ────────────────────────────────────────────

    def _get_household_members(self) -> list[str]:
        """
        Return the list of household member usernames from config.

        Reads ``rating_groups.household.members`` — the same config key used
        by the Tautulli group-completion logic.  Returns an empty list when the
        key is absent (disables household-watch gating entirely).
        """
        return (
            (((self.config or {})
            .get("rating_groups") or {})
            .get("household") or {})
            .get("members", [])
        )

    @staticmethod
    def _resolve_household_watch_state(
        per_user: dict, household_members: list[str], *, quorum: "int | None" = None
    ) -> tuple[bool, str | None]:
        """
        Determine whether the household has watched an episode — delegates to the brain
        (lifecycle.household_watch.resolve_household_watch). ``quorum`` (default None =
        require every member, byte-identical) lets a per-member quorum count as
        household-watched. Returns ``(household_watched, household_last_watched_at)``.
        """
        return resolve_household_watch(per_user, household_members, quorum=quorum)

    # ── Public: pilot batch ─────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_pilot_batch")
    def run_pilot_batch(
        self,
        instance: str,
        all_series: list[dict],
        batch_size: int | None = PILOT_BATCH_SIZE,
    ) -> dict:
        """
        Fetch pilot episode-file metadata for series not yet in the Parquet.

        Priority order
        --------------
        1. **Watched series** (``is_watched=True`` in the Parquet) — always
           processed in full, no cap.  These carry the highest ML signal because
           we have real user behaviour data for them and need their codec /
           quality fingerprint immediately.
        2. **Unwatched series** — processed up to ``batch_size`` per run.
           Pass ``batch_size=None`` to process the entire library in one shot.

        The cache fills incrementally across runs.  Once a series has a pilot
        row it is excluded from future batches.

        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        df = self.load(instance)

        # ── Classify existing rows ────────────────────────────────────────────
        # Three mutually exclusive pilot categories:
        #
        #   real_pilot_ids  — is_pilot=True AND episode_file_id is not null.
        #                     Skip forever; file data is stable.
        #
        #   fresh_stub_ids  — is_pilot=True AND episode_file_id is null AND
        #                     date_added < CACHE_MAX_AGE ago.
        #                     Skip this run; recently checked, still no file.
        #
        #   stale_stub_ids  — is_pilot=True AND episode_file_id is null AND
        #                     date_added >= CACHE_MAX_AGE ago (or null).
        #                     Re-check; a pilot may have been downloaded since.
        #                     When a file is now found → upgrade stub to real row.
        #                     When still no file → refresh date_added timestamp.
        now = datetime.now(tz=timezone.utc)
        stale_cutoff = now - timedelta(seconds=self.CACHE_MAX_AGE)

        if not df.empty and "is_pilot" in df.columns:
            _is_pilot_mask = df["is_pilot"] == True
            _has_file_mask = df["episode_file_id"].notna()

            real_pilot_ids: set[int] = set(
                df.loc[_is_pilot_mask & _has_file_mask, "series_id"]
                .dropna().astype(int)
            )

            _stub_mask = _is_pilot_mask & ~_has_file_mask
            stub_rows  = df[_stub_mask].copy()

            if not stub_rows.empty and "date_added" in stub_rows.columns:
                _da = pd.to_datetime(stub_rows["date_added"], utc=True, errors="coerce")
                _fresh = _da >= stale_cutoff
                fresh_stub_ids: set[int] = set(
                    stub_rows.loc[_fresh, "series_id"].dropna().astype(int)
                )
                stale_stub_ids: set[int] = set(
                    stub_rows.loc[~_fresh, "series_id"].dropna().astype(int)
                )
            else:
                fresh_stub_ids = set()
                stale_stub_ids = set(
                    stub_rows["series_id"].dropna().astype(int)
                ) if not stub_rows.empty else set()
        else:
            real_pilot_ids = fresh_stub_ids = stale_stub_ids = set()
            stub_rows = pd.DataFrame(columns=self.SCHEMA_COLUMNS)

        # Build a fast sid→row_index map for stale stubs so we can update
        # them in-place without a full DataFrame scan per series.
        stale_stub_idx: dict[int, int] = {}
        if not stub_rows.empty:
            for idx_s, row_s in stub_rows.iterrows():
                sid_s = row_s.get("series_id")
                if pd.notna(sid_s) and int(sid_s) in stale_stub_ids:
                    stale_stub_idx[int(sid_s)] = idx_s

        _total_stubs  = len(fresh_stub_ids) + len(stale_stub_ids)
        self.logger.log_info(
            f"📂 Loaded episode_files.parquet for '{instance}': "
            f"{len(df)} rows — {len(real_pilot_ids)} real pilot(s), "
            f"{len(fresh_stub_ids)} fresh stub(s) (skipped), "
            f"{len(stale_stub_ids)} stale stub(s) (will re-check)"
        )

        # Series with Tautulli watch history — process first, no cap
        watched_ids: set[int] = (
            set(
                df.loc[
                    df["is_watched"].infer_objects(copy=False).fillna(False).astype(bool), "series_id"
                ].dropna().astype(int)
            )
            if not df.empty else set()
        )

        # skip_ids = real pilots + fresh stubs.  Stale stubs are NOT skipped
        # so they get re-queried and potentially upgraded to real pilot rows.
        skip_ids   = real_pilot_ids | fresh_stub_ids
        pending_all = [s for s in all_series if s.get("id") and int(s["id"]) not in skip_ids]
        pending_watched = [s for s in pending_all if int(s["id"]) in watched_ids]
        pending_other   = [s for s in pending_all if int(s["id"]) not in watched_ids]

        # Build the batch: all watched (no limit) + up to batch_size others
        if batch_size is None:
            other_slice = pending_other
        else:
            other_slots = max(0, batch_size - len(pending_watched))
            other_slice = pending_other[:other_slots]

        batch = pending_watched + other_slice

        stats = {
            "total_series":    len(all_series),
            "already_cached":  len(real_pilot_ids),
            "stubs_fresh":     len(fresh_stub_ids),
            "stubs_stale":     len(stale_stub_ids),
            "pending_watched": len(pending_watched),
            "pending_other":   len(pending_other),
            "fetched":         0,
            "rows_added":      0,
            "stubs_added":     0,
            "stubs_upgraded":  0,
            "stubs_refreshed": 0,
        }

        if not batch:
            self.logger.log_info(
                f"✅ Pilot cache complete for '{instance}' — "
                f"all {len(all_series)} series checked "
                f"({len(real_pilot_ids)} with files, {_total_stubs} without Sonarr files)."
            )
            return stats

        remaining_after = len(pending_all) - len(batch)
        limit_label = "unlimited" if batch_size is None else str(batch_size)
        self.logger.log_info(
            f"🎬 Pilot batch for '{instance}': {len(batch)} series to fetch "
            f"({len(pending_watched)} watched-priority + {len(other_slice)} other "
            f"[limit={limit_label}]), "
            f"{remaining_after} remaining after this run…"
        )

        new_rows:     list[dict] = []
        df_was_mutated = False  # True when stale-stub rows are updated in-place

        # One tqdm bar (stderr) instead of a per-20-series progress line — a cold rebuild
        # of 11k+ series otherwise floods the log. Errors still log (see the except below).
        from scripts.support.utilities.progress.tqdm_wrapper import tqdm
        _pbar = tqdm(batch, total=len(batch), desc=f"🧩 Pilot batch [{instance}]", unit="series")
        for idx, series in enumerate(_pbar, start=1):
            sid   = int(series["id"])
            title = series.get("title", "")
            is_stale_stub = sid in stale_stub_ids
            try:
                files = self._get_episode_files(instance, sid)
                rep   = self._pick_representative_file(files)
                if rep:
                    row = self._normalise(
                        raw=rep,
                        series_id=sid,
                        series_title=title,
                        season_number=rep.get("seasonNumber"),
                        episode_number=None,   # not resolved for pilots
                        is_pilot=True,
                    )
                    if is_stale_stub:
                        # Upgrade: drop the old stub row, write a real pilot row.
                        old_idx = stale_stub_idx.get(sid)
                        if old_idx is not None and old_idx in df.index:
                            # Before dropping, capture the successful profile ID
                            # from the stub row so we know which profile worked.
                            _succ_pid = df.at[old_idx, "pilot_last_profile_id"]                                 if "pilot_last_profile_id" in df.columns else None
                            df = df.drop(index=old_idx).reset_index(drop=True)
                            # Re-index stale_stub_idx after drop so later
                            # iterations still resolve correctly.
                            stale_stub_idx = {
                                k: (v - 1 if v > old_idx else v)
                                for k, v in stale_stub_idx.items()
                                if k != sid
                            }
                            df_was_mutated = True
                            # Write successful profile onto the new real pilot row
                            # so JIT restore never downgrades below this profile.
                            if _succ_pid is not None and pd.notna(_succ_pid):
                                row["pilot_successful_profile_id"] = int(_succ_pid)
                        stats["stubs_upgraded"] += 1
                        self.logger.log_info(
                            f"  ⬆️ Upgraded stub → real pilot: '{title}'"
                        )
                    new_rows.append(row)
                    stats["rows_added"] += 1

                else:
                    if is_stale_stub:
                        # Still no file — refresh the timestamp so this series
                        # is treated as a fresh stub for the next CACHE_MAX_AGE
                        # window rather than being re-queried every run.
                        old_idx = stale_stub_idx.get(sid)
                        if old_idx is not None and old_idx in df.index:
                            df.at[old_idx, "date_added"] = now.isoformat()
                            df_was_mutated = True
                        stats["stubs_refreshed"] += 1
                    else:
                        # Brand-new series with no files — create a stub.
                        stub: dict = {col: None for col in self.SCHEMA_COLUMNS}
                        stub.update({
                            "series_id":           sid,
                            "series_title":        title,
                            "is_pilot":            True,
                            "is_watched":          False,
                            "next_episode":        False,
                            "watch_count":         0,
                            "marked_for_deletion": False,
                            "hdr":                 False,
                            "date_added":          now.isoformat(),
                        })
                        new_rows.append(stub)
                        stats["stubs_added"] += 1

                stats["fetched"] += 1
                if idx % 50 == 0:    # cheap live counts on the bar, no log lines
                    _pbar.set_postfix(files=stats["rows_added"], stubs=stats["stubs_added"])
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Pilot fetch failed for series {sid} ('{title}'): {e}"
                )

        changed = bool(new_rows) or df_was_mutated
        if changed:
            if new_rows:
                df_new = pd.DataFrame(new_rows, columns=self.SCHEMA_COLUMNS)
                for col in self._NUMERIC_COLUMNS:
                    if col in df_new.columns:
                        df_new[col] = pd.to_numeric(df_new[col], errors="coerce")
                df = self._safe_concat(df, df_new)
            # The Parquet is a read-only mirror of the Sonarr library (built from
            # GET requests only), so it materialises even in dry_run — same as the
            # JSON data-pull caches. dry_run still gates the actual *arr writes
            # (search/delete), which are independently guarded inside their methods.
            self.save(instance, df)
            if self.dry_run:
                self.logger.log_debug(
                    f"[dry_run] Built episode_files cache for '{instance}' "
                    f"({len(df)} rows) — local write only, no Sonarr changes."
                )

        if remaining_after > 0:
            self.logger.log_table(
                ["Outcome", "Count"],
                [
                    ["with file data", stats["rows_added"]],
                    ["new stubs",      stats["stubs_added"]],
                    ["upgraded",       stats["stubs_upgraded"]],
                    ["refreshed",      stats["stubs_refreshed"]],
                ],
                title=f"📋 Pilot batch done '{instance}' ({remaining_after} series still pending)",
                caption="What this pilot-cache batch wrote to the Parquet before the next "
                        "run continues with the still-pending series.",
                descriptions=[
                    "series rows added with real episode file data",
                    "new pilot stub rows added (no Sonarr file yet)",
                    "existing stub rows upgraded to file rows",
                    "stale stub rows refreshed in place",
                ],
            )
        else:
            self.logger.log_table(
                ["Outcome", "Count"],
                [
                    ["with file data", stats["rows_added"]],
                    ["new stubs",      stats["stubs_added"]],
                    ["upgraded",       stats["stubs_upgraded"]],
                    ["refreshed",      stats["stubs_refreshed"]],
                ],
                title=f"✅ Pilot cache fully populated '{instance}'",
                caption="Final rollup once every series is cached: what this last pilot "
                        "batch wrote to the Parquet.",
                descriptions=[
                    "series rows added with real episode file data",
                    "new pilot stub rows added (no Sonarr file yet)",
                    "existing stub rows upgraded to file rows",
                    "stale stub rows refreshed in place",
                ],
            )

        return stats

    # ── Public: Tautulli watched sync ───────────────────────────────────────────

    # ── Public: Pilot search + profile step-down ──────────────────────────────

    # ── Pilot episode ID cache (single Parquet) ─────────────────────────────

    def _pilot_cache_key(self, instance: str) -> str:
        return f"sonarr/{instance}/episodes/pilots"

    def _load_pilot_episode_cache(self, instance: str) -> dict:
        """
        Load {series_id: sonarr_episode_id} from the pilots Parquet.
        Avoids per-series cache files and live API calls for S01E01 IDs.
        """
        import pandas as pd, pathlib
        key = self._pilot_cache_key(instance)
        raw = self.global_cache.get(key) if self.global_cache else None
        if isinstance(raw, dict):
            return raw
        if self.global_cache and hasattr(self.global_cache, "key_builder"):
            try:
                pq = pathlib.Path(
                    str(self.global_cache.key_builder.build_path(key)) + ".parquet"
                )
                if pq.exists():
                    df_p   = pd.read_parquet(pq)
                    mapping = dict(zip(
                        df_p["series_id"].astype(int),
                        df_p["sonarr_episode_id"].astype(int),
                    ))
                    self.global_cache.set(key, mapping)
                    return mapping
            except Exception:
                pass
        return {}

    def _save_pilot_episode_cache(self, instance: str, mapping: dict):
        """Persist {series_id: sonarr_episode_id} to pilots.parquet."""
        import pandas as pd, pathlib
        if not mapping:
            return
        key = self._pilot_cache_key(instance)
        if self.global_cache:
            self.global_cache.set(key, mapping)
        if self.global_cache and hasattr(self.global_cache, "key_builder"):
            try:
                pq = pathlib.Path(
                    str(self.global_cache.key_builder.build_path(key)) + ".parquet"
                )
                pq.parent.mkdir(parents=True, exist_ok=True)
                rows = [{"series_id": s, "sonarr_episode_id": e}
                        for s, e in mapping.items()]
                pd.DataFrame(rows).to_parquet(pq, index=False)
            except Exception:
                pass

    @LoggerManager().log_function_entry
    @timeit("run_pilot_search")
    def run_pilot_search(self, instance: str) -> dict:
        """
        For every series with a stub pilot (no episode file), trigger an
        EpisodeSearch for S01E01.

        Profile stepping — two strategies (config ``pilot_best_tier_first.enabled``)
        ----------------
        BEST-TIER-FIRST (default ON): the pilot targets the HIGHEST tier whose
        estimated grab still keeps the JIT space reserve (``choose_pilot_profile``,
        SPACE-gated, never watch-likelihood-gated) — "always the highest tier
        space allows". When that tier keeps coming up empty it DIVERTS DOWN one
        rung per run toward the floor (``next_pilot_profile_descend``), holding at
        the floor so a pilot is never abandoned. A pilot is NEVER deleted — when
        even the floor would breach the reserve it is either grabbed at the floor
        anyway (``pilot_best_tier_first.force_floor=true``) or skipped and
        re-probed when space frees (default), never removed.

        LEGACY (flag OFF, byte-identical to before): profiles ranked by max
        resolution (ascending). Attempt 1 sets the FLOOR (rank 0). Each later
        attempt that fails at the current profile climbs one tier UP on the next
        run; once at the widest ("Any") the series keeps being searched there.

        Search interval
        ---------------
        Only re-searches series whose last attempt was >= PILOT_SEARCH_INTERVAL_H
        hours ago (default 24 h) to avoid hammering indexers every run.
        """
        PILOT_SEARCH_INTERVAL_H = 24
        instance = self._resolve_instance(instance)
        stats = {
            "checked": 0, "searched": 0, "stepped_down": 0,
            "at_floor": 0, "skipped_recent": 0, "skipped_space": 0, "failed": 0,
        }

        df = self.load(instance)
        if df.empty:
            return stats

        for col in ("pilot_search_attempts", "pilot_last_searched_at", "pilot_last_profile_id"):
            if col not in df.columns:
                df[col] = None

        stub_mask = (
            df["is_pilot"].infer_objects(copy=False).fillna(False).astype(bool)
            & df["episode_file_id"].isna()
        )
        if not stub_mask.any():
            self.logger.log_info(
                f"[PilotSearch] No stub pilots for '{instance}' — nothing to search."
            )
            return stats

        # ── Quality profiles sorted ascending (lowest first) ──────────────────
        try:
            raw_profiles = self.sonarr_api._make_request(
                instance, "qualityprofile", fallback=[]
            ) or []
        except Exception as e:
            self.logger.log_warning(f"[PilotSearch] Could not fetch quality profiles: {e}")
            return stats

        ranked             = rank_pilot_profiles(raw_profiles)
        profile_id_to_rank = {p["id"]: i for i, p in enumerate(ranked)}

        # ── Within-run floor-first climb (default) ────────────────────────────
        # The pilot grabs at the LOWEST resolution actually available: a background worker flips the
        # series profile UP an ascending floor→widest ladder one tier at a time, searches S01E01, and
        # STOPS at the first tier that yields a release — leaving the series at that low tier so the
        # watch-based upgrade path (run_active_watcher_upgrades / JIT) raises it later. This supersedes
        # best-tier-first, which pinned every never-watched pilot to the highest tier space allowed.
        _climb_cfg  = (self.config or {}).get("pilot_floor_climb", {}) or {}
        pilot_climb = bool(_climb_cfg.get("enabled", True))
        # Ascending ladder: one rung per resolution tier (dedupe profiles sharing a max resolution),
        # dropping profiles that allow nothing. ladder[0] = floor (lowest res), ladder[-1] = widest.
        _climb_ladder: list = []
        _seen_res: set = set()
        for _p in ranked:
            _r = profile_max_resolution(_p)
            if _r <= 0 or _r in _seen_res or _p.get("id") is None:
                continue
            _seen_res.add(_r)
            _climb_ladder.append((int(_p["id"]), int(_r)))
        if not _climb_ladder:   # degenerate profile set → fall back to every usable ranked id, ascending
            _climb_ladder = [(int(p["id"]), int(profile_max_resolution(p)))
                             for p in ranked
                             if p.get("id") is not None and profile_max_resolution(p) > 0]
        _floor_pid = _climb_ladder[0][0] if _climb_ladder else None
        if pilot_climb and not _climb_ladder:
            self.logger.log_warning(
                f"[PilotSearch] No usable quality profiles for '{instance}' — cannot climb; "
                f"nothing searched."
            )
            return stats

        # ── Legacy strategies (escape hatch — only when pilot_floor_climb is OFF) ──
        # best-tier-first: target the highest tier the space reserve allows, divert DOWN across empty
        # runs. OFF that too: floor-first/step-up across runs. Both are superseded by the climb above.
        _pbtf = (self.config or {}).get("pilot_best_tier_first", {}) or {}
        pilot_best_tier   = (not pilot_climb) and bool(_pbtf.get("enabled", False))
        # force_floor: when even the floor breaches the reserve, grab at the floor anyway (always
        # seed the pilot) vs skip-and-re-probe. Default FALSE — never breach the configured floor;
        # a skipped pilot is re-probed when space frees (it is never deleted, just not grabbed yet).
        pilot_force_floor = bool(_pbtf.get("force_floor", False))
        best_first = list(reversed(ranked)) if pilot_best_tier else None   # highest-res first
        pilot_reserve_gb = None
        pilot_free_gb = None
        if pilot_best_tier:
            _total_gb = self._get_total_space_gb(instance)
            alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, _total_gb)
            _, _pilot_floor = space_targets(
                self.config, fallback_gb=self.MIN_FREE_SPACE_GB, total_gb=_total_gb,
            )
            pilot_reserve_gb = jit_reserve_gb(_total_gb, _pilot_floor, self.JIT_RESERVE_PCT)
            # STATIC current free space — every pilot is evaluated independently against the SAME
            # free space, NOT a running total. A pilot is a one-episode discovery probe; most stubs
            # never find a release, so cumulatively reserving each search's grab would defer the bulk
            # of the library even on a near-empty disk. The gate therefore searches all due stubs at
            # the highest tier that fits, and only skips/forces-floor when free space is GENUINELY
            # below the reserve. (Cross-run: the next run sees the reduced free space and re-gates.)
            pilot_free_gb = self._get_free_space_gb(instance)

        now_utc  = datetime.now(tz=timezone.utc)
        interval = timedelta(hours=PILOT_SEARCH_INTERVAL_H)
        # Optional exponential backoff + re-probeable exhausted cooldown: a stub that keeps
        # coming up empty is retried less often (and, past exhausted_after attempts, only on
        # a long re-probe cooldown). Default-off (pilot_backoff unset) → effective interval
        # is exactly `interval`, byte-identical.
        _pilot_backoff = ((self.config or {}).get("pilot_backoff") or {})
        changed  = False

        # Measured MiB/min per quality for the dry-run space estimate (JIT
        # fallback table covers qualities with no samples). Computed once.
        _pilot_measured = self._measured_mb_per_min(df)

        PROGRESS_BAR_THRESHOLD = 10
        stub_indices = list(df.index[stub_mask])
        use_tqdm     = len(stub_indices) > PROGRESS_BAR_THRESHOLD

        _tqdm_cls = None
        if use_tqdm:
            try:
                from tqdm import tqdm as _tqdm_cls
            except ImportError:
                # No tqdm available — leave _tqdm_cls = None so the iterator below
                # falls back to the plain index list (no progress bar). Do NOT
                # auto-install at runtime (unpinned/unhashed pip = supply risk).
                _tqdm_cls = None

        # On bulk runs (count > threshold) suppress per-series chatter entirely
        # and let the throttled progress bar be the only output. Small runs keep
        # full per-series logging.
        def _log(msg: str):
            if use_tqdm:
                return
            self.logger.log_info(msg)

        _iter = (
            _tqdm_cls(stub_indices, desc="PilotSearch", unit="series",
                      dynamic_ncols=True, leave=False, mininterval=0.5)
            if use_tqdm and _tqdm_cls is not None
            else stub_indices
        )

        # Searches are collected here and pushed in batches after the loop,
        # rather than one Sonarr command per series.
        queued        = []   # (idx, episode_id, new_pid, title) → batched EpisodeSearch  (legacy)
        series_queued = []   # (idx, series_id,  new_pid, title) → individual SeriesSearch (legacy)
        # Within-run climb collects (sid, s01e01_id) for the background worker; a stub whose S01E01
        # id can't be resolved (rare cache miss) falls back to a single floor SeriesSearch.
        climb_items: list     = []   # (sid, episode_id) → background floor-first climb
        series_fallback: list = []   # (idx, sid, title) → floor SeriesSearch fallback

        def _mark_searched(idx: int, pid) -> None:
            prev = df.at[idx, "pilot_search_attempts"]
            df.at[idx, "pilot_search_attempts"]  = (int(prev) + 1) if prev and pd.notna(prev) else 1
            df.at[idx, "pilot_last_searched_at"] = now_utc.isoformat()
            df.at[idx, "pilot_last_profile_id"]  = pid

        # ── Series source (bulk snapshot, BOTH modes) ─────────────────────────
        # The tier DECISION (current profile + runtime) is read from a single O(1) snapshot of
        # every series — taken once from the local letter-bucketed cache (populated by the
        # series-sync phase this run → fast, memoised I/O), or one bulk /series fetch on a cache
        # miss. The OLD code did a FRESH live GET series/{sid} PER STUB in live mode — thousands
        # of serial ~1 s round-trips (a multi-hour crawl on the first run, before the interval
        # guard kicks in). A profile change is rare, so _pilot_set_profile instead re-fetches
        # just the FEW changing series fresh right before the PUT — the write still lands against
        # current Sonarr state (a stale snapshot could revert a concurrent change) without paying
        # a per-stub GET for the 99% that don't change. A single live GET /series is the opposite
        # failure mode (one huge blocking response that freezes the bar at 0%), so the cache is
        # preferred and the bulk fetch is the fallback only.
        series_by_id: dict = {}
        _series_mgr = getattr(self.sonarr_cache, "series", None)
        _all_series = None
        if _series_mgr is not None:
            for _meth in ("get_all_series", "iter_all_series"):
                _fn = getattr(_series_mgr, _meth, None)
                if callable(_fn):
                    try:
                        _all_series = list(_fn(instance))
                        break
                    except Exception:
                        _all_series = None
        if not _all_series:
            # Cache miss → single live fetch (the bare "series" endpoint is
            # run-memoised, so the rest of the run reuses it).
            _all_series = self.sonarr_api._make_request(instance, "series", fallback=[]) or []
        series_by_id = {
            int(s["id"]): s for s in _all_series
            if isinstance(s, dict) and s.get("id") is not None
        }
        if not series_by_id:
            self.logger.log_warning(
                f"[PilotSearch] No series available for '{instance}' (letter cache "
                f"empty and live /series returned nothing) — stub searches this run "
                f"will be skipped (counted as failed)."
            )

        # Shared episode cache for _get_episode_id. In LIVE mode the episode id is
        # needed to queue a precise EpisodeSearch, so pre-warm the by_series cache
        # CONCURRENTLY (interval-eligible stubs not already resolvable) — otherwise
        # the walk pays a serial by_series GET per stub. In dry-run the id only
        # decorates a log label, so we skip the warm and run _get_episode_id
        # API-free (allow_live=False) in the loop below.
        _ep_cache: dict = {}
        if use_tqdm and not self.dry_run:
            _pilot_ep = self._load_pilot_episode_cache(instance)
            _warm_sids = []
            for _i in stub_indices:
                _s = df.at[_i, "series_id"]
                if pd.isna(_s):
                    continue
                _s = int(_s)
                if _s in _pilot_ep:
                    continue  # already resolvable without an API call
                _watt = df.at[_i, "pilot_search_attempts"]
                _wiv = pilot_backoff_interval(
                    interval, int(_watt) if _watt and pd.notna(_watt) else 0,
                    backoff=_pilot_backoff,
                )
                if not pilot_search_due(df.at[_i, "pilot_last_searched_at"], now_utc, _wiv):
                    continue  # interval-guarded out → won't be searched
                _warm_sids.append(_s)
            if _warm_sids:
                self._prewarm_by_series_episode_cache(
                    instance, _warm_sids,
                    season_ep_cache=_ep_cache, files_session_cache=None,
                    desc="PilotSearch warm",
                )

        for idx in _iter:
            stats["checked"] += 1
            sid   = df.at[idx, "series_id"]
            title = df.at[idx, "series_title"] or f"series {sid}"
            if pd.isna(sid):
                continue
            sid = int(sid)

            # ── Interval guard (with optional attempts-based backoff) ─────────
            _att_raw = df.at[idx, "pilot_search_attempts"]
            _eff_interval = pilot_backoff_interval(
                interval, int(_att_raw) if _att_raw and pd.notna(_att_raw) else 0,
                backoff=_pilot_backoff,
            )
            if not pilot_search_due(df.at[idx, "pilot_last_searched_at"], now_utc, _eff_interval):
                stats["skipped_recent"] += 1
                continue

            # ── Series object: read from the O(1) bulk snapshot in BOTH modes. The tier
            #    decision needs only the current profile + runtime; the fresh per-stub GET is
            #    deferred to _pilot_set_profile and made ONLY for a stub that actually changes
            #    profile, so its PUT still writes against current Sonarr state. ──
            series = series_by_id.get(sid)
            if not series or not isinstance(series, dict):
                stats["failed"] += 1
                continue

            # ── Within-run floor-first climb (default) ────────────────────────
            # Resolve S01E01 and hand it to the background climb worker; the worker walks the
            # ascending ladder and grabs at the lowest tier with a release. Mark the stub searched
            # (at the floor, for the 24 h interval guard) whether or not the id resolves.
            if pilot_climb:
                ep_id = self._get_episode_id(
                    instance, sid, 1, 1, series_ep_cache=_ep_cache,
                    log_cache_miss=not use_tqdm, log_expired=not use_tqdm,
                    allow_live=(not self.dry_run) and not use_tqdm,
                )
                _mark_searched(idx, _floor_pid)
                changed = True
                if ep_id:
                    climb_items.append((sid, int(ep_id)))
                else:
                    series_fallback.append((idx, sid, title))
                if self.dry_run:
                    _what = "S01E01" if ep_id else "SeriesSearch (S01E01 id n/a)"
                    _log(
                        f"  [dry_run] PilotSearch would climb '{title}' {_what} floor-first "
                        f"(≤{_climb_ladder[0][1]}p → ≤{_climb_ladder[-1][1]}p), grabbing at the "
                        f"lowest available tier | why: stub pilot, no file"
                    )
                continue

            current_pid  = series.get("qualityProfileId")
            current_rank = profile_id_to_rank.get(current_pid, 0)
            last_pid_raw = df.at[idx, "pilot_last_profile_id"]
            last_pid     = int(last_pid_raw) if last_pid_raw and pd.notna(last_pid_raw) else None
            new_pid      = current_pid

            # ── Tier decision: best-tier-first (default) or legacy floor/step-up ──
            if pilot_best_tier:
                # Highest tier whose estimated grab keeps the reserve (space-gated, NO likelihood
                # cap), diverting DOWN one rung per empty run. A pilot is never deleted: when even
                # the floor breaches the reserve it is forced-floor-grabbed or skipped+re-probed.
                runtime_min = float((series or {}).get("runtime") or 0) or 45.0
                _chosen = choose_pilot_profile(
                    best_first, projected_free=pilot_free_gb,
                    reserve_gb=pilot_reserve_gb, runtime_min=runtime_min, measured=_pilot_measured,
                )
                if _chosen is None and not pilot_force_floor:
                    stats["skipped_space"] += 1
                    _log(
                        f"  ⏭️ PilotSearch skip '{title}': no profile fits the "
                        f"{pilot_reserve_gb:.0f} GB reserve — re-probe next run (pilot never deleted)"
                    )
                    continue
                _forced = _chosen is None
                if _forced:
                    _chosen = ranked[0]          # forced floor: always seed the pilot
                _start_rank = profile_id_to_rank.get(_chosen["id"], 0)
                new_pid, _action = next_pilot_profile_descend(
                    start_rank=_start_rank, current_pid=current_pid,
                    current_rank=current_rank, last_pid=last_pid, ranked=ranked,
                )
                _tp    = next((p for p in ranked if p.get("id") == new_pid), None)
                _tname = (_tp or {}).get("name", str(new_pid))
                # No cumulative reservation: each pilot is gated against the static current free
                # space (above), so the running total is not decremented per search. Actual disk
                # safety on grab is owned by the space-pressure coordinator + the JIT reserve, and
                # the next run re-gates against the new free space.
                if new_pid != current_pid:
                    if self.dry_run:
                        _tr = self._profile_max_quality(_tp)[0] if _tp else 0
                        _log(
                            f"  [dry_run] Would set '{title}' → best-fit '{_tname}' "
                            f"(≤{_tr}p, {_action})"
                        )
                    else:
                        try:
                            if self._pilot_set_profile(instance, sid, new_pid):
                                _log(f"  🎯 '{title}' → best-fit '{_tname}' ({_action})")
                                stats["stepped_down"] += 1
                            else:
                                new_pid = current_pid
                        except Exception as e:
                            self.logger.log_warning(
                                f"  ⚠️ Best-tier profile set failed for '{title}': {e}"
                            )
                            new_pid = current_pid
                elif _action == "at_floor":
                    stats["at_floor"] += 1

            else:
                # ── Step-up: floor first, ceiling = "Any" ──────────────────────
                # Attempt 1 : profile set to floor (rank 0 = most permissive).
                # Attempt 2+: step up one tier each run until ranked[-1] ("Any").
                # "Any" accepts all resolutions — widest net on the final attempt.
                ceiling_profile = ranked[-1]
                ceiling_pid     = ceiling_profile["id"]
                ceiling_name    = ceiling_profile.get("name", str(ceiling_pid))

                attempts_raw  = df.at[idx, "pilot_search_attempts"]
                attempts_done = int(attempts_raw) if attempts_raw and pd.notna(attempts_raw) else 0

                # ── Optional likelihood cap on the climb: a stub nobody is likely to watch
                #    stops at the resolution its propensity earns instead of escalating all
                #    the way to the widest "Any". Default-off (pilot_likelihood_cap unset) →
                #    max_rank None → uncapped, byte-identical. ──
                _max_rank = None
                if ((self.config or {}).get("pilot_likelihood_cap") or {}).get("enabled"):
                    _ll = watch_likelihood(df.loc[idx], config=self.config)
                    _cap_res = resolution_cap_for_likelihood(_ll, config=self.config)
                    _max_rank = max(
                        (r for r, p in enumerate(ranked) if profile_max_resolution(p) <= _cap_res),
                        default=0,
                    )

                # ── Ladder step (pure): floor first, step UP one tier per failed run,
                #    then hold at the widest "Any" (rank 0 is the most-permissive floor) ──
                new_pid, _action = next_pilot_profile(
                    attempts_done=attempts_done, current_pid=current_pid,
                    current_rank=current_rank, last_pid=last_pid, ranked=ranked,
                    max_rank=_max_rank,
                )

                if _action == "floor":
                    floor_p    = ranked[0]
                    new_pid    = floor_p["id"]
                    floor_name = floor_p.get("name", str(new_pid))
                    if new_pid != current_pid:
                        if self.dry_run:
                            _fr = self._profile_max_quality(floor_p)[0]
                            _log(
                                f"  [dry_run] Would set '{title}' → floor '{floor_name}' "
                                f"(≤{_fr}p, attempt 1)"
                            )
                        else:
                            try:
                                if self._pilot_set_profile(instance, sid, new_pid):
                                    _log(
                                        f"  🔽 '{title}' → floor '{floor_name}' (attempt 1)"
                                    )
                                    stats["stepped_down"] += 1
                                else:
                                    new_pid = current_pid
                            except Exception as e:
                                self.logger.log_warning(
                                    f"  ⚠️ Floor profile set failed for '{title}': {e}"
                                )
                                new_pid = current_pid

                elif _action in ("step_up", "at_ceiling"):
                    new_rank = current_rank + 1
                    if new_rank < len(ranked):
                        higher      = ranked[new_rank]
                        new_pid     = higher["id"]
                        higher_name = higher.get("name", str(new_pid))
                        if self.dry_run:
                            _cur_name = next(
                                (p.get("name") for p in ranked if p.get("id") == current_pid),
                                str(current_pid),
                            )
                            _hr = self._profile_max_quality(higher)[0]
                            _log(
                                f"  [dry_run] Would step up '{title}': "
                                f"'{_cur_name}' → '{higher_name}' (≤{_hr}p, attempt {attempts_done + 1})"
                            )
                        else:
                            try:
                                if self._pilot_set_profile(instance, sid, new_pid):
                                    _log(
                                        f"  📈 Stepped up '{title}' → '{higher_name}' "
                                        f"(attempt {attempts_done + 1})"
                                    )
                                    stats["stepped_down"] += 1
                                else:
                                    new_pid = current_pid
                            except Exception as e:
                                self.logger.log_warning(
                                    f"  ⚠️ Step-up failed for '{title}': {e}"
                                )
                                new_pid = current_pid
                    else:
                        # At ceiling ("Any") — keep searching with all indexers
                        stats["at_floor"] += 1
                        _log(
                            f"  🔛 '{title}' at ceiling '{ceiling_name}' — re-searching"
                        )

            # ── Queue the search (pushed in batches after the loop) ───────────
            # log_cache_miss=False on bulk runs keeps the cache layer's per-item
            # "♻️ Cache miss" lines from cluttering the progress bar.
            #
            # CACHE-ONLY in the bulk path: when use_tqdm is set we already pre-warmed the
            # by-series episode cache CONCURRENTLY above, so the serial loop must resolve the
            # id from cache and NEVER fall back to a per-stub live episode?seriesId= GET — that
            # fallback is exactly the serial-round-trip crawl the warm exists to eliminate (it
            # was re-introducing ~1 s/stub even after the snapshot fix removed the series GET).
            # A genuine cache miss (rare) just yields no id → harmless SeriesSearch fallback.
            # Small batches (use_tqdm False) skip the warm, so they keep the live fallback for
            # their handful of stubs.
            ep_id = self._get_episode_id(
                instance, sid, 1, 1, series_ep_cache=_ep_cache,
                log_cache_miss=not use_tqdm, log_expired=not use_tqdm,
                allow_live=(not self.dry_run) and not use_tqdm,
            )
            if ep_id:
                queued.append((idx, ep_id, new_pid, title))
                desc = f"EpisodeSearch S01E01 for '{title}'"
            else:
                series_queued.append((idx, sid, new_pid, title))
                desc = f"SeriesSearch for '{title}' (S01E01 id unavailable)"

            if self.dry_run:
                _tp    = next((p for p in ranked if p.get("id") == new_pid), None)
                _tres  = self._profile_max_quality(_tp)[0] if _tp else 0
                _tname = (_tp or {}).get("name", str(new_pid))
                _rt    = float((series or {}).get("runtime") or 0) or 45.0
                _est   = self._estimate_grab_gb(_tp, _rt, 1, _pilot_measured)
                _what  = "S01E01" if ep_id else "SeriesSearch (S01E01 id n/a)"
                _log(
                    f"  [dry_run] PilotSearch '{title}' {_what} at '{_tname}' "
                    f"(≤{_tres}p, ~{_est:.2f} GB est) | why: stub pilot, no file"
                )

        # ── Dispatch: within-run floor-first climb (default) ──────────────────
        if pilot_climb:
            _unresolved = 0
            if self.dry_run:
                stats["searched"] = len(climb_items) + len(series_fallback)
            else:
                # Last-resort LIVE S01E01 id resolution for the few stubs the cache missed, so the
                # climb only ever searches S01E01. A SeriesSearch would over-grab the WHOLE monitored
                # series (every season) at the floor — the opposite of a single lowest-tier pilot probe.
                # Still unresolved after a live GET → skip this run (re-probed next run), never
                # whole-series searched.
                for _idx, _sid, _title in series_fallback:
                    try:
                        _ep = self._get_episode_id(
                            instance, _sid, 1, 1, allow_live=True,
                            log_cache_miss=False, log_expired=False,
                        )
                    except Exception:
                        _ep = None
                    if _ep:
                        climb_items.append((_sid, int(_ep)))
                    else:
                        _unresolved += 1
                        self.logger.log_info(
                            f"  ⏭️ PilotSearch '{_title}': S01E01 id unresolved — skipping this "
                            f"run (re-probe next run; never whole-series searched)"
                        )
                if climb_items:
                    self._spawn_pilot_climb_worker(instance, climb_items, _climb_ladder)
                stats["searched"] = len(climb_items)
            if changed and not self.dry_run:
                self.save(instance, df)
            _prefix = "[dry_run] " if self.dry_run else ""
            self.logger.log_table(
                ["Outcome", "Count"],
                [
                    ["climb searched",     stats["searched"]],
                    ["id unresolved",      _unresolved],
                    ["skipped recent",     stats["skipped_recent"]],
                    ["failed",             stats["failed"]],
                ],
                title=f"[PilotSearch] {_prefix}'{instance}' floor-first climb "
                      f"(≤{_climb_ladder[0][1]}p → ≤{_climb_ladder[-1][1]}p)",
                caption="Within-run floor-first pilot climb: each pilot is searched UP the resolution "
                        "ladder and grabbed at the LOWEST tier with a release, then left there for the "
                        "watch-based upgrade path. Stubs whose S01E01 id can't be resolved are deferred "
                        "(never whole-series searched).",
                descriptions=[
                    "pilots handed to the background climb worker (grab at lowest available tier)",
                    "stubs whose S01E01 id stayed unresolved (cache + live miss) → deferred to next run",
                    f"stubs skipped: searched within last {PILOT_SEARCH_INTERVAL_H}h",
                    "search or profile-set calls that errored",
                ],
            )
            return stats

        # ── Batched search push (legacy escape-hatch paths) ───────────────────
        # EpisodeSearch accepts a list of episode ids, so issue one command per
        # chunk instead of one per series; SeriesSearch only takes a single id,
        # so the (rare) id-unavailable fallbacks are pushed individually. df
        # tracking is updated only for series whose push actually succeeded, so a
        # failed push leaves them eligible for retry on the next run.
        EPISODE_SEARCH_CHUNK = 100

        if self.dry_run:
            stats["searched"] = len(queued) + len(series_queued)
            for idx, _ep, pid, _t in queued:
                _mark_searched(idx, pid)
                changed = True
            for idx, _sid, pid, _t in series_queued:
                _mark_searched(idx, pid)
                changed = True
        else:
            for i in range(0, len(queued), EPISODE_SEARCH_CHUNK):
                batch  = queued[i:i + EPISODE_SEARCH_CHUNK]
                ep_ids = [ep for _i, ep, _p, _t in batch]
                try:
                    self.sonarr_api._make_request(
                        instance, "command", method="POST",
                        payload={"name": "EpisodeSearch", "episodeIds": ep_ids},
                    )
                    for idx, _ep, pid, _t in batch:
                        _mark_searched(idx, pid)
                    stats["searched"] += len(batch)
                    changed = True
                except Exception as e:
                    self.logger.log_warning(
                        f"[PilotSearch] Batched EpisodeSearch failed for "
                        f"{len(batch)} episode(s): {e}"
                    )
                    stats["failed"] += len(batch)

            for idx, sid, pid, title in series_queued:
                try:
                    self.sonarr_api._make_request(
                        instance, "command", method="POST",
                        payload={"name": "SeriesSearch", "seriesId": sid},
                    )
                    _mark_searched(idx, pid)
                    stats["searched"] += 1
                    changed = True
                except Exception as e:
                    self.logger.log_warning(f"  ⚠️ SeriesSearch failed for '{title}': {e}")
                    stats["failed"] += 1

        if changed and not self.dry_run:
            self.save(instance, df)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["searched",        stats["searched"]],
                ["profile changes", stats["stepped_down"]],
                ["at ceiling",      stats["at_floor"]],
                ["skipped recent",  stats["skipped_recent"]],
                ["skipped no-space", stats["skipped_space"]],
                ["failed",          stats["failed"]],
            ],
            title=f"[PilotSearch] {prefix}'{instance}'",
            caption="Per-pass outcome of the pilot step-down search: how many pilot stubs "
                    "were searched, re-profiled, or skipped and why.",
            descriptions=[
                "pilot stubs a SeriesSearch was triggered for",
                "stubs whose quality profile was stepped down",
                "stubs already at the lowest profile (ceiling)",
                f"stubs skipped: searched within last {PILOT_SEARCH_INTERVAL_H}h",
                "stubs skipped: no disk space, will re-probe",
                "search or profile-set calls that errored",
            ],
        )
        return stats

    def _pilot_set_profile(self, instance: str, sid: int, new_pid) -> bool:
        """Re-fetch series ``sid`` FRESH and PUT only its qualityProfileId. The pilot tier
        DECISION is read off the bulk snapshot (fast, no per-stub GET), but the WRITE must land
        against CURRENT Sonarr state so it can't revert a concurrent change to another field —
        so the one series that actually changes profile is fetched fresh here, right before the
        PUT. Returns True on a PUT, False if the fresh fetch came back empty (caller keeps the
        existing profile; the stub re-probes next run)."""
        fresh = self.sonarr_api._make_request(instance, f"series/{sid}", fallback=None)
        if not fresh or not isinstance(fresh, dict):
            return False
        fresh = dict(fresh)
        fresh["qualityProfileId"] = new_pid
        self.sonarr_api._make_request(
            instance, f"series/{sid}", method="PUT", payload=fresh
        )
        return True

    def _spawn_pilot_climb_worker(self, instance: str, items: list, ladder: list) -> None:
        """Fire-and-forget background worker that grabs each stub pilot at its LOWEST available
        resolution. ``items`` is ``[(sid, s01e01_episode_id), ...]``; ``ladder`` is the ascending
        ``[(profile_id, max_resolution), ...]`` floor→widest tier list. Per series the worker flips
        the series profile UP the ladder one tier at a time, searches S01E01, and STOPS at the first
        tier that yields a grab — leaving the series at that low tier so the watch-based upgrade path
        (run_active_watcher_upgrades / JIT) can raise it later. It is the mirror of the JIT step-DOWN
        worker (:meth:`_jit_search_worker`).

        Runs as a NON-daemon thread: it never blocks the pipeline (we do not join it), but the
        interpreter waits for it on exit, so a half-climbed series is always left coherent (either at
        the grabbed tier or reverted to its pre-climb profile)."""
        import threading

        items  = [(int(s), int(e)) for s, e in items if s is not None and e is not None]
        ladder = [(int(p), int(r)) for p, r in ladder if p is not None]
        if not items or not ladder:
            return
        threading.Thread(
            target=self._pilot_climb_worker,
            args=(instance, items, ladder),
            name="pilot-climb-search",
            daemon=False,
        ).start()
        self.logger.log_info(
            f"[PilotSearch] Background floor-first climb started for {len(items)} pilot(s) "
            f"across up to {min(self.JIT_SEARCH_MAX_WORKERS, len(items))} parallel worker(s) "
            f"(ladder ≤{ladder[0][1]}p → ≤{ladder[-1][1]}p, {len(ladder)} tier(s))."
        )

    def _pilot_climb_worker(self, instance: str, items: list, ladder: list) -> None:
        """Per stub pilot: climb the ascending profile ``ladder`` (floor→widest), searching S01E01 at
        each tier and STOPPING at the first (lowest) tier that yields a grab — the series is LEFT at
        that tier (NOT reverted), so it sits at the lowest available quality until the watch-based
        upgrade path raises it. When no tier yields a release the series is reverted to its pre-climb
        profile and re-probed next run.

        Series climb CONCURRENTLY (each owns its own profile, so they are independent); each series'
        ladder is strictly SEQUENTIAL (the shared series profile means tier N must finish before the
        flip to tier N+1). Mirror of :meth:`_jit_search_worker` — see it for the concurrency model.

        Mechanism assumption: setting a series to a profile whose max resolution is ≤Np makes Sonarr's
        EpisodeSearch grab only releases ≤Np (profiles gate which qualities are valid for selection),
        so flipping floor→up and stopping at the first grab yields the LOWEST available resolution.

        Safe alongside the JIT step-down worker (also a background thread this run): the two never
        contend for the same series' profile in any HARMFUL way. A never-watched series — the case
        this feature targets — has NO ``next_episode`` flag (``_compute_next_episodes`` only walks
        forward from a watched episode), so it is never a JIT candidate; the climb owns it outright.
        The only overlap is a *watched* series that still has a missing S01E01 stub, and there JIT
        keeping the series at its watch-appropriate tier is the desired outcome anyway."""
        POLL_INTERVAL_S = 3.0
        CMD_TIMEOUT_S   = 180.0
        DONE_STATES     = ("completed", "failed", "aborted", "cancelled")

        def _label(sid, info=None):
            """Readable series id: ``sonarr/<instance> '<title>' (tvdb-<id>)``; ``info`` is any
            already-fetched series dict (carries title + tvdbId) so it costs no extra request."""
            if isinstance(info, dict):
                title = (info.get("title") or "").strip()
                if title:
                    tvdb = info.get("tvdbId")
                    tvdb_s = f"tvdb-{tvdb}" if tvdb else f"sid-{sid}"
                    return f"sonarr/{instance} '{title}' ({tvdb_s})"
            return f"sonarr/{instance} series {sid}"

        def _wait_command(cid):
            if not cid:
                return
            start = time.time()
            while time.time() - start < CMD_TIMEOUT_S:
                cmd = self.sonarr_api._make_request(instance, f"command/{cid}", fallback=None)
                if (cmd or {}).get("status") in DONE_STATES:
                    return
                time.sleep(POLL_INTERVAL_S)

        def _set_profile(sid, pid):
            """Flip series ``sid`` to ``pid`` against fresh state (only PUTs when it actually
            differs). Returns False if the fresh GET came back empty OR the PUT failed — so a
            caller never searches at a tier it could not actually set (a silently-failed floor
            flip would otherwise search at the series' original, possibly higher, profile and
            grab high: the exact over-grab this feature exists to prevent)."""
            s = self.sonarr_api._make_request(instance, f"series/{sid}", fallback=None)
            if not (s and isinstance(s, dict)):
                return False
            if s.get("qualityProfileId") != pid:
                s = dict(s)
                s["qualityProfileId"] = pid
                # _make_request returns the updated series on success, the fallback (None) on a
                # failed write — so a None result means the flip did not land.
                if self.sonarr_api._make_request(
                    instance, f"series/{sid}", method="PUT", payload=s
                ) is None:
                    return False
            return True

        def _process_pilot(sid, ep_id):
            original_pid = None
            revert_pid = None
            label = _label(sid)
            try:
                base = self.sonarr_api._make_request(instance, f"series/{sid}", fallback=None)
                if not (base and isinstance(base, dict)):
                    return
                label = _label(sid, base)
                # The pre-climb profile, captured ONCE, so a no-grab climb reverts to the TRUE
                # original (never an intermediate rung). ``current`` tracks the ACTUAL profile (may
                # be None if the series has none — then the first flip always PUTs); ``revert_pid``
                # is the safe restore target, falling back to the floor when there's no original.
                original_pid = base.get("qualityProfileId")
                current = original_pid
                revert_pid = original_pid if original_pid is not None else (
                    ladder[0][0] if ladder else None)

                # Already downloading (a prior run's grab still in the queue)? Leave it untouched —
                # re-climbing would see that queue item and falsely "grab" at the floor, downgrading
                # the series profile under an in-flight higher-tier download.
                if self._episodes_in_queue(instance, [ep_id]):
                    self.logger.log_info(
                        f"  ⏳ Pilot {label}: S01E01 already in the download queue — skipping climb"
                    )
                    return

                for pid, res in ladder:
                    if current != pid:
                        if not _set_profile(sid, pid):
                            break
                        current = pid
                    # EpisodeSearch carries ONLY S01E01, so the climb can never grab another episode.
                    _cmd = self.sonarr_api._make_request(
                        instance, "command", method="POST",
                        payload={"name": "EpisodeSearch", "episodeIds": [ep_id]},
                    )
                    _wait_command(_cmd.get("id") if isinstance(_cmd, dict) else None)
                    if self._episodes_in_queue(instance, [ep_id]):
                        self.logger.log_info(
                            f"  ✅ Pilot grab: {label} grabbed S01E01 at ≤{res}p (profile {pid}) — "
                            f"lowest available tier; left here for the watch-based upgrade path"
                        )
                        return   # SUCCESS — leave the series at this tier (do NOT revert)
                    self.logger.log_info(
                        f"  ⏫ Pilot climb: {label} found no S01E01 release at ≤{res}p "
                        f"(profile {pid}) — climbing one tier"
                    )

                # Exhausted every tier with no grab → restore the pre-climb profile, retry next run.
                self.logger.log_info(
                    f"  ∅ Pilot: {label} found no S01E01 release across {len(ladder)} tier(s); "
                    f"reverted, will re-probe next run"
                )
                if revert_pid is not None and current != revert_pid:
                    _set_profile(sid, revert_pid)
            except Exception as e:
                self.logger.log_warning(
                    f"[PilotSearch] Background climb failed for {label}: {e}"
                )
                try:  # best-effort revert — captured before any flip (floor if no original)
                    if revert_pid is not None:
                        _set_profile(sid, revert_pid)
                except Exception:
                    pass

        # Run the climbs CONCURRENTLY (each pilot owns its own profile); writes to the one Sonarr
        # instance still serialize on the per-instance write lock, but the long command/queue waits
        # overlap instead of one pilot blocking the next. A single pilot stays on the sequential path.
        if len(items) <= 1:
            for sid, ep_id in items:
                _process_pilot(sid, ep_id)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            max_workers = min(self.JIT_SEARCH_MAX_WORKERS, len(items))
            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="pilot-climb"
            ) as ex:
                futures = [ex.submit(_process_pilot, sid, ep_id) for sid, ep_id in items]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:  # _process_pilot shouldn't raise, but stay defensive
                        self.logger.log_warning(f"[PilotSearch] climb task crashed: {e}")

    def _get_total_space_gb(self, instance: str) -> float:
        """
        Total disk capacity in GB across the mounts that host Sonarr root
        folders (via /diskspace). Returns 0.0 on failure so callers can fall
        back to a fixed reserve.
        """
        try:
            disks = self.sonarr_api._make_request(instance, "diskspace", fallback=[]) or []
            roots = self.sonarr_api._make_request(instance, "rootfolder", fallback=[]) or []
            root_paths = [str(r.get("path", "")) for r in roots
                          if isinstance(r, dict) and r.get("path")]

            total, seen = 0, set()
            for d in disks:
                if not isinstance(d, dict):
                    continue
                path = str(d.get("path", ""))
                if path in seen:
                    continue
                # Only count a mount if it hosts a Sonarr root folder.
                if root_paths and not any(rp.startswith(path) for rp in root_paths if path):
                    continue
                seen.add(path)
                total += d.get("totalSpace", 0) or 0

            if total <= 0:  # no match — sum everything reported
                total = sum((d.get("totalSpace", 0) or 0)
                            for d in disks if isinstance(d, dict))
            return total / (1024 ** 3)
        except Exception:
            return 0.0

    def _measured_mb_per_min(self, df) -> dict:
        """
        Average MiB-per-minute per quality_name, measured from the library's own
        episode files (size_bytes / runtime_seconds). Delegates to the shared
        size_model so the Sonarr/Radarr measurement logic stays identical.
        """
        return measured_mb_per_min(df, runtime_unit="seconds")

    @staticmethod
    def _profile_max_quality(profile: dict):
        """
        Return ``(max_resolution, quality_name)`` of the highest-resolution
        allowed quality in a Sonarr quality profile. Delegates to the shared
        size_model.
        """
        return profile_max_quality(profile)

    def _estimate_grab_gb(self, profile, runtime_min, n_eps: int = 1,
                          measured: dict | None = None) -> float:
        """
        Estimated disk space (GiB) to grab ``n_eps`` episode(s) at the given
        quality profile's top *allowed* quality. Thin wrapper over the shared
        size_model: per-quality MiB/min resolved as measured → calibrated table
        → resolution default, then × runtime(min) × n_eps, MiB→GiB via /1024.
        """
        return estimate_gb_for_profile(profile, runtime_min, n_eps, measured)

    def _get_episode_id(self, instance: str, series_id: int,
                        season: int, episode: int,
                        series_ep_cache: dict | None = None,
                        log_cache_miss: bool = True,
                        log_expired: bool = True,
                        allow_live: bool = True) -> int | None:
        """
        Look up the Sonarr internal episode ID for a specific S/E.
        Uses the in-memory series_ep_cache (keyed by series_id → {season: [eps]})
        when available to avoid a live API call.

        ``allow_live=False`` makes the lookup completely API-free: it consults
        only the in-memory cache, the pilot Parquet cache, and any *already
        persisted* on-disk by_series cache (read-only — never regenerates or
        falls back to the network). Used by dry-run PilotSearch, where the id
        only decorates a log label and is not worth a per-stub round-trip.
        """
        # Try the in-memory episode cache first (populated by _get_all_episodes)
        if series_ep_cache and series_id in series_ep_cache:
            season_eps = series_ep_cache[series_id].get(season, [])
            for ep in season_eps:
                if ep.get("episodeNumber") == episode:
                    return ep.get("id")

        # For S01E01, check the pilot Parquet cache before hitting the API
        if season == 1 and episode == 1:
            _pc = self._load_pilot_episode_cache(instance)
            if series_id in _pc:
                return _pc[series_id]

        # API-free path: read any already-persisted by_series cache without
        # regenerating it, and never fall back to the live API.
        if not allow_live:
            if self.global_cache:
                try:
                    cache_key = f"sonarr/{instance}/episodes/by_series/{series_id}"
                    cached = self.global_cache.get(cache_key) or []
                    for ep in cached:
                        if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
                            return ep.get("id")
                except Exception:
                    pass
            return None

        # Try the on-disk episode cache
        if self.global_cache:
            try:
                cache_key = f"sonarr/{instance}/episodes/by_series/{series_id}"
                cached = self.global_cache.get_or_generate_cache(
                    key=cache_key,
                    generator_function=lambda: (
                        self.sonarr_api._make_request(
                            instance, f"episode?seriesId={series_id}", fallback=[]
                        ) or []
                    ),
                    expiration_time=self.EPISODES_CACHE_TTL_S,
                    log_miss=log_cache_miss, log_expired=log_expired,
                ) or []
                for ep in cached:
                    if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
                        return ep.get("id")
                return None
            except Exception:
                pass

        # Live API fallback
        try:
            eps = self.sonarr_api._make_request(
                instance,
                f"episode?seriesId={series_id}&seasonNumber={season}",
                fallback=[],
            ) or []
            for ep in eps:
                if ep.get("episodeNumber") == episode:
                    ep_id = ep.get("id")
                    if season == 1 and episode == 1 and ep_id:
                        try:
                            _pc = self._load_pilot_episode_cache(instance)
                            _pc[series_id] = ep_id
                            self._save_pilot_episode_cache(instance, _pc)
                        except Exception:
                            pass
                    return ep_id
        except Exception:
            pass
        return None

    @LoggerManager().log_function_entry
    @timeit("run_jit_quality_upgrades")
    def run_jit_quality_upgrades(self, instance: str) -> dict:
        """
        Unified just-in-time next-up GRAB pass — acquire AND re-quality together.

        For every episode flagged next_episode=True that is not yet watched and not
        already JIT-upgraded — whether MISSING (a fresh ACQUIRE, already monitored by
        _do_acquire_next_episodes) or ON DISK (a re-quality UPGRADE/DOWNGRADE):
          1. Picks the highest-resolution quality profile whose estimated grab
             still keeps JIT_RESERVE_PCT of the disk free ("best that fits"),
             within the episode's watch-likelihood resolution cap.
          2. Bumps the SERIES quality profile to that target (snapshotting the
             original) and fires EpisodeSearch so Sonarr grabs the best release.
          3. Snapshots current file quality to pre_upgrade_quality and sets
             upgraded_for_watching=True.

        A background worker then waits for each EpisodeSearch command to finish
        and sets the series profile back to its original value — so the bump
        only affects the targeted search and NOT every future grab for that
        series.

        PER-EPISODE TIERS (config ``jit_per_episode_tiers.enabled``, default ON).
        Each next-up episode earns its OWN best-that-fits tier against the LIVE
        (decrementing) reserve, so one series may mix tiers (e.g. one 2160p next
        to four 1080p). The work is bucketed by target tier and the background
        worker flips the series profile + EpisodeSearches ONE tier group at a
        time, so a lower-target episode is NEVER searched while the series
        profile sits at a higher tier — the group-by-tier invariant that keeps
        the mixed-target search free of over-grab. With the flag OFF the method
        decides ONE profile per series (legacy memo) and runs a single search
        group, byte-identical to the pre-per-episode behavior.

        INVARIANT — if you extend this to assign per-episode targets by any new
        signal, the targets MUST stay grouped by ``target_tier_key`` so the
        worker only ever searches a group while the series profile is at that
        group's tier. Do NOT collapse the groups back into one all-remaining
        EpisodeSearch ladder: that re-introduces the over-grab (a 1080p-target
        episode grabbing a 2160p release while the profile is flipped up).

        Respects dry_run (logs decisions, mutates nothing).
        Skips kids-cert and keep-tagged series.
        """
        import json

        KIDS_CERTS = {"g", "pg", "tv-g", "tv-y", "tv-y7"}
        stats = {
            "checked": 0, "acquired": 0, "upgraded": 0, "already_upgraded": 0,
            "skipped_kids": 0, "skipped_keep": 0, "skipped_space": 0, "failed": 0,
            "skipped_active_downgrade": 0,   # downgrades suppressed because the series is actively watched
        }

        # ── Space reserve: JIT upgrades must keep free space above the configured
        # floor (U = free_space_limit + headroom) AND a JIT_RESERVE_PCT fraction of
        # the disk. Upgrades consume space, so they only run when comfortably above U.
        free_gb    = self._get_free_space_gb(instance)
        total_gb   = self._get_total_space_gb(instance)
        # U = free_space_limit + headroom, or 25% of the total drive when unset; the
        # MIN_FREE_SPACE_GB constant is the last resort only when total is also unknown.
        alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, total_gb)
        _, _upgrade_floor = space_targets(
            self.config, fallback_gb=self.MIN_FREE_SPACE_GB, total_gb=total_gb,
        )
        reserve_gb = jit_reserve_gb(total_gb, _upgrade_floor, self.JIT_RESERVE_PCT)

        # Clear the playlist JIT signal UP FRONT so EVERY exit path leaves a fresh set — the
        # four early returns below (space-pressure / empty df / no candidates / no profiles)
        # would otherwise leave a stale jit_grabbed boosting a series the user already finished
        # (or, under steady-state space pressure, forever). The full-execution path overwrites
        # this with the real planned_sids at the end.
        if self.global_cache is not None:
            try:
                self.global_cache.set(f"sonarr/{instance}/jit_grabbed", [])
            except Exception:
                pass

        if free_gb <= reserve_gb:
            self.logger.log_info(
                f"[JIT] Skipping JIT upgrades — {free_gb:.0f} GB free "
                f"<= reserve {reserve_gb:.0f} GB "
                f"({self.JIT_RESERVE_PCT * 100:.0f}% of {total_gb:.0f} GB total)"
            )
            return stats

        df = self.load(instance)
        if df.empty:
            return stats

        if "pre_upgrade_quality" not in df.columns:
            df["pre_upgrade_quality"] = None
        if "upgraded_for_watching" not in df.columns:
            df["upgraded_for_watching"] = False

        # Re-enable episodes whose background step-down search grabbed nothing
        # last run, so this pass retries them. The worker can't safely write the
        # parquet (concurrent with the main pipeline), so it records failures to
        # a side cache that we consume here.
        reconcile_changed = (
            self._reconcile_failed_jit(instance, df) if not self.dry_run else False
        )

        # Unified candidate selection (brain: space.jit_planner.next_up_grab_candidates) —
        # next-up unwatched not-already-upgraded episodes, BOTH missing (ACQUIRE) and on-disk
        # (UPGRADE/DOWNGRADE). Missing rows are kept in full (fresh "just-in-time" grabs, already
        # bounded by the prefetch budget); on-disk rows are capped at JIT_MAX_EPISODES per series
        # so one run never re-qualifies a whole season. Both are routed through the SAME
        # reserve-aware tier/size calibration below — a missing next-up episode is acquired at the
        # JIT tier, not the raw series profile (_do_acquire_next_episodes already MONITORED it, so
        # the shared step-down worker can search it).
        candidates = next_up_grab_candidates(df, upgrade_cap=self.JIT_MAX_EPISODES)
        if candidates.empty:
            if reconcile_changed and not self.dry_run:
                self.save(instance, df)  # persist the flag resets from reconcile
            return stats

        # Series with a watch inside the active-watch window — their next-up episodes must NEVER be
        # DOWNGRADED. Each upcoming episode is itself unwatched, so its per-episode watch_likelihood
        # is affinity-only (no engagement floor) and a low-affinity show would otherwise have its
        # owned 1080p torn down to the affinity tier mid-binge. The recency signal lives on the
        # series' WATCHED rows (the next-up stubs have no last_watched_at), so derive it series-wide
        # from the full df — the same last_watched_at the prefetch uses for 'upgrade-eligible'.
        # UPGRADES and ACQUIRES are unaffected; only the tear-down is suppressed.
        active_watch_sids: set = set()
        if "last_watched_at" in df.columns and "series_id" in df.columns:
            _lw = pd.to_datetime(df["last_watched_at"], utc=True, errors="coerce")
            _cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.JIT_ACTIVE_WATCH_DAYS)
            _recent_sids = df.loc[_lw >= _cutoff, "series_id"].dropna()
            active_watch_sids = {int(s) for s in _recent_sids.unique()}

        # ── Quality model ──────────────────────────────────────────────────────
        # Profiles ranked ascending by max resolution; we try best-first so each
        # series gets the highest-quality profile whose estimated grab still
        # leaves the reserve intact ("step down to best that fits"). Size is
        # estimated from the library's own measured MiB/min per quality, with a
        # static per-quality fallback.
        raw_profiles = self.sonarr_api._make_request(
            instance, "qualityprofile", fallback=[]
        ) or []
        if not raw_profiles:
            self.logger.log_warning(
                "[JIT] No quality profiles available — cannot target best quality."
            )
            return stats
        ranked     = sorted(raw_profiles, key=lambda p: self._profile_max_quality(p)[0])
        best_first = list(reversed(ranked))
        measured   = self._measured_mb_per_min(df)

        def _est_gb(profile: dict, runtime_min: float) -> float:
            return self._estimate_grab_gb(profile, runtime_min, 1, measured)

        projected_free      = free_gb
        # Optional space-band ceiling: when the drive sits in the lower part of the
        # pressure band (free within headroom_gb of the reserve), cap JIT grabs to a
        # lower resolution so a near-floor disk doesn't pull 4K even for a hot series.
        # Default-off → None → the bare likelihood cap (byte-identical).
        _jit_band = (self.config or {}).get("jit_space_band", {}) or {}
        jit_pressure_cap = None
        if _jit_band.get("enabled") and (free_gb - reserve_gb) < float(_jit_band.get("headroom_gb", 0) or 0):
            try:
                jit_pressure_cap = int(_jit_band.get("cap_resolution", 1080))
            except (TypeError, ValueError):
                jit_pressure_cap = 1080
        # Per-episode tiering (deliverable B). ON (default): each episode earns its own tier
        # against the live projected_free, so a series can mix tiers; work is bucketed by tier so
        # the worker flips the QP one group at a time (no over-grab). OFF (escape hatch): legacy
        # one-profile-per-series memo + single search group, byte-identical to before.
        per_episode_tiers = bool(
            ((self.config or {}).get("jit_per_episode_tiers") or {}).get("enabled", True)
        )
        series_choice: dict = {}      # legacy memo (per_episode_tiers OFF): series_id → chosen
        # series_id → {tier_res(int): {"eps": [...], "step_pids": [...], "chosen": profile}}
        series_work: dict = {}
        changed = False
        # Ledger columns so we can stamp the 'upgrade' plan (consumed space) below.
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        # pre_upgrade_quality holds a json.dumps snapshot (a STRING) stamped on the live grab
        # path below. A parquet loaded with that column all-null comes back as float64, and a
        # strict-dtype pandas rejects assigning a string into it ("Invalid value '{...}' for
        # dtype 'float64'") — which crashed the whole JIT pass in LIVE mode (the stamp is in the
        # not-dry_run branch, so dry-runs never hit it). Coerce it to object alongside the ledger.
        for _c in ("planned_action", "plan_reason", "pre_upgrade_quality"):
            if _c in df.columns and df[_c].dtype != object:
                df[_c] = df[_c].astype(object)

        # series_id(str) → recent household watcher(s); built by sync_from_tautulli from the
        # per-user Tautulli history. Annotates the grab grid's 'For' column (who each
        # next-up was acquired for). Best-effort: {} when unavailable → 'For' shows '-'.
        jit_watchers = (self.global_cache.get(f"sonarr/{instance}/jit_watchers")
                        if self.global_cache else None) or {}

        table_rows: list[list] = []      # unified grab breakdown → printed once as a fixed grid below
        acquire_monitor_ids: list = []   # ACQUIRE ep ids → monitored right before the worker searches
        planned_sids: set = set()        # series with a planned JIT grab THIS pass — collected
                                         # UNCONDITIONALLY (the live `eps`/`queued` are dry_run-gated,
                                         # so deriving the playlist JIT signal from them would be empty
                                         # in dry_run, the default mode — silently inert)

        for idx, row in candidates.iterrows():
            stats["checked"] += 1
            fid    = row.get("episode_file_id")
            sid    = row.get("series_id")
            sn     = int(row.get("season_number") or 0)
            en     = int(row.get("episode_number") or 0)
            title  = row.get("series_title") or f"series {sid}"
            policy = row.get("keep_policy")
            cert   = str(row.get("certification") or "").lower()

            _skip = jit_row_skip(policy, cert, fid, sid, KIDS_CERTS)
            if _skip == "keep":
                stats["skipped_keep"] += 1
                continue
            if _skip == "kids":
                stats["skipped_kids"] += 1
                continue
            if pd.isna(sid):   # no usable series id (jit_row_skip 'no_sid')
                continue
            sid = int(sid)
            # A missing file (jit_row_skip → 'no_file') is NO LONGER skipped — it's an ACQUIRE:
            # a fresh "just-in-time" grab routed through the SAME reserve-aware tier/size
            # calibration as an on-disk re-quality, searched by the shared step-down worker.
            is_acquire = (_skip == "no_file")

            rt_s = row.get("runtime_seconds")
            runtime_min = (float(rt_s) / 60.0) if rt_s and pd.notna(rt_s) and float(rt_s) > 0 else 45.0

            # Decide the target profile: the best profile that fits the reserve AND is within the
            # resolution this episode's watch-likelihood earns. A next-up episode is unwatched, so
            # the likelihood is the series' affinity-driven propensity (capped below 4K) —
            # actively-watched series reach 1080p, stale ones stay 720p, none grab 4K here.
            # PER-EPISODE (default): recompute per row against the LIVE projected_free, so later
            # episodes of a series may earn a lower tier as the reserve shrinks. LEGACY (flag OFF):
            # decide ONCE per series and reuse it (byte-identical to the pre-per-episode behavior).
            if per_episode_tiers:
                _cap = resolution_cap_for_likelihood(
                    watch_likelihood(row, config=self.config), config=self.config
                )
                chosen = choose_jit_profile(
                    best_first, cap=_cap, projected_free=projected_free,
                    reserve_gb=reserve_gb, runtime_min=runtime_min, measured=measured,
                    pressure_cap=jit_pressure_cap,
                )
            else:
                if sid not in series_choice:
                    _cap = resolution_cap_for_likelihood(
                        watch_likelihood(row, config=self.config), config=self.config
                    )
                    # Best profile that fits the reserve within the earned tier (brain).
                    series_choice[sid] = choose_jit_profile(
                        best_first, cap=_cap, projected_free=projected_free,
                        reserve_gb=reserve_gb, runtime_min=runtime_min, measured=measured,
                        pressure_cap=jit_pressure_cap,
                    )
                chosen = series_choice[sid]

            if chosen is None:
                stats["skipped_space"] += 1
                self.logger.log_info(
                    f"  ⏭️  JIT skip '{title}' S{sn:02d}E{en:02d}: even the lowest "
                    f"profile would drop below the {reserve_gb:.0f} GB reserve"
                )
                continue

            est_gb = _est_gb(chosen, runtime_min)
            if projected_free - est_gb < reserve_gb:
                stats["skipped_space"] += 1
                continue

            ep_id = self._get_episode_id(instance, sid, sn, en)
            if not ep_id:
                stats["failed"] += 1
                continue

            target_res, target_q = self._profile_max_quality(chosen)
            cur_q = row.get("quality_name") or f"{row.get('resolution') or '?'}p"

            # ACQUIRE (no file yet) vs on-disk re-quality (UPGRADE / DOWNGRADE by resolution).
            if is_acquire:
                action = "ACQUIRE"
            else:
                _cr = row.get("resolution")
                try:
                    _cr = int(_cr) if (_cr is not None and pd.notna(_cr)) else None
                except (TypeError, ValueError):
                    _cr = None
                action = "DOWNGRADE" if (_cr is not None and _cr > target_res) else "UPGRADE"

            # ACTIVE-WATCH GUARD: never tear down an owned file of a series the household is
            # currently watching. The episode is unwatched so its affinity-only tier is low, but
            # the series is being binged now (watched within JIT_ACTIVE_WATCH_DAYS), so leave the
            # existing higher-quality file alone. UPGRADE/ACQUIRE still proceed; only the proactive
            # DOWNGRADE is skipped (downgrades under genuine pressure are the coordinator's job).
            if action == "DOWNGRADE" and sid in active_watch_sids:
                stats["skipped_active_downgrade"] += 1
                self.logger.log_debug(
                    f"  🛡️  JIT keep '{title}' S{sn:02d}E{en:02d}: actively watched "
                    f"(within {self.JIT_ACTIVE_WATCH_DAYS}d) — not downgrading {cur_q}."
                )
                continue

            # Bucket the episode under its (series, target-tier) group. The first episode in a
            # group fixes the group's step-down ladder + representative profile; later same-tier
            # episodes reuse it (same tier ⇒ same top resolution ⇒ identical ladder). ACQUIRE and
            # re-quality episodes that earn the SAME tier share ONE group ⇒ one QP flip + one
            # EpisodeSearch covers both. With per_episode_tiers OFF every episode of a series
            # shares one tier ⇒ exactly one group ⇒ the single-group search as before.
            tier = target_tier_key(chosen)
            _series_buckets = series_work.setdefault(sid, {})
            bucket = _series_buckets.get(tier)
            if bucket is None:   # only the first episode of a group builds the ladder (avoid recompute)
                bucket = _series_buckets[tier] = {
                    "eps": [], "step_pids": jit_step_down_pids(best_first, chosen), "chosen": chosen,
                }

            # One table row per grab (acquire + re-quality) — collected here, printed once below.
            table_rows.append([
                title, f"S{sn:02d}E{en:02d}", action,
                ("-" if is_acquire else cur_q), f"<={target_res}p",
                f"{est_gb:.2f}", f"{projected_free - est_gb:.0f}",
                # who this next-up was grabbed FOR — the recent household watcher(s) of
                # this series, most-recent first (blank when no per-user history).
                ", ".join((jit_watchers.get(str(sid)) or [])[:2]) or "-",
            ])
            planned_sids.add(int(sid))   # active-series JIT signal (dry_run-independent)

            if not self.dry_run:
                bucket["eps"].append((ep_id, sn, en))
                if is_acquire:
                    # Monitor this fresh grab right before the worker searches it (below) — closes
                    # the window where _do_acquire's separately space-gated monitor pass could have
                    # been skipped while this pass still runs.
                    acquire_monitor_ids.append(ep_id)
                else:
                    # Re-quality of an EXISTING file: snapshot the original so the JIT restore
                    # pass can revert it post-watch, and mark it bumped. Acquire has no prior file.
                    df.at[idx, "pre_upgrade_quality"] = json.dumps({
                        "quality_name":   row.get("quality_name"),
                        "quality_source": row.get("quality_source"),
                        "resolution":     row.get("resolution"),
                        "video_codec":    row.get("video_codec"),
                    })
                    df.at[idx, "upgraded_for_watching"] = True

            # Ledger: on-disk re-quality CONSUMES the delta (negative = space used), stamped here.
            # ACQUIRE rows are stamped 'acquire' by sync_from_tautulli's mask-based ledger and we
            # deliberately do NOT re-stamp them here — that keeps the dry-run plan-summary oracle
            # exactly where it was (acquire owned by sync, upgrade owned by this pass).
            if not is_acquire:
                _cur_gb = (float(row.get("size_bytes")) / (1024 ** 3)) if (row.get("size_bytes") is not None and pd.notna(row.get("size_bytes"))) else 0.0
                df.at[idx, "planned_action"]  = "upgrade"
                df.at[idx, "plan_reason"]     = "JIT quality upgrade (next unwatched ep)"
                df.at[idx, "plan_reclaim_gb"] = -round(max(0.0, est_gb - _cur_gb), 2)
                changed = True
                stats["upgraded"] += 1
            else:
                stats["acquired"] += 1

            projected_free -= est_gb

        if (changed or reconcile_changed) and not self.dry_run:
            self.save(instance, df)
        elif self.dry_run and changed:
            # Persist the JIT 'upgrade' ledger stamps as a plan-only preview — the real
            # upgrade columns (pre_upgrade_quality, upgraded_for_watching) are written
            # ONLY in the non-dry_run branch, so this saves annotations, not changes.
            self.save(instance, df)

        # Hand each bumped series to a background worker: it bumps the QP, runs
        # EpisodeSearch, and if nothing is grabbed steps DOWN one profile at a
        # time until something grabs — then restores the original profile. Runs
        # off the main pipeline so it never blocks the rest of the run.
        # Drop empty tier-groups (all eps skipped/dry_run) and then empty series, so the worker
        # only ever receives groups that actually have episodes to search.
        queued: dict = {}
        for sid, tiers in series_work.items():
            nonempty = {t: g for t, g in tiers.items() if g.get("eps")}
            if nonempty:
                queued[sid] = nonempty

        # Persist the JIT 'grabbed' series set so the per-user playlist builder can lift an
        # actively-watched series ABOVE household-popular content (precedence: user affinity
        # > JIT > household). Sourced from planned_sids (collected unconditionally) NOT queued
        # (whose `eps` are live-only) so the signal is populated in dry_run too — the default
        # mode. Always overwrites (incl. empty) so a stale set from a prior run can't linger;
        # the builder intersects it with jit_watchers so it boosts only the member watching it.
        if self.global_cache is not None:
            try:
                self.global_cache.set(f"sonarr/{instance}/jit_grabbed", sorted(planned_sids))
            except Exception as e:
                self.logger.log_warning(f"[JIT] jit_grabbed persist failed: {e}")

        if queued and not self.dry_run:
            if acquire_monitor_ids:
                # Ensure freshly-acquired (missing) episodes are MONITORED immediately before the
                # worker's EpisodeSearch — guarantees monitor-before-search in this pass regardless
                # of whether _do_acquire's earlier (separately space-gated) monitor pass ran.
                try:
                    self.sonarr_api._make_request(
                        instance, "episode/monitor", method="PUT",
                        payload={"episodeIds": acquire_monitor_ids, "monitored": True},
                    )
                except Exception as e:
                    self.logger.log_warning(f"[JIT] acquire monitor PUT failed: {e}")
            self._spawn_jit_search_worker(instance, queued)

        _group_count = sum(len(tiers) for tiers in queued.values())

        # One aligned breakdown of every grab this pass (acquire + re-quality), printed all at
        # once as a single fixed-width grid (every column the same width) — not per-episode lines.
        if table_rows:
            _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
            if _rs is not None:
                _rs.add_rows("sonarr", "JIT next-up grab plan", instance,
                             ["Series", "Ep", "Action", "From", "Target", "~GB", "ProjFree", "For"],
                             table_rows, order=12)
            else:
                self.logger.log_grid(
                    ["Series", "Ep", "Action", "From", "Target", "~GB", "ProjFree", "For"],
                    table_rows,
                    title=(
                        f"JIT next-up grab plan - '{instance}'"
                        f"{' [dry_run]' if self.dry_run else ''}  "
                        f"(reserve {reserve_gb:.0f} GB, free {free_gb:.0f} GB)"
                    ),
                    cap=24,   # per-column widths → lets Series + For show fuller without bloating the rest
                )

        # Vertical 2-column table (label → count) instead of one very wide pipe-delimited line,
        # so the JIT outcome fits a screen without horizontal scrolling.
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["acquired",               stats["acquired"]],
                ["re-quality",             stats["upgraded"]],
                ["active-watch protected", stats["skipped_active_downgrade"]],
                ["no-space",               stats["skipped_space"]],
                ["kids",                   stats["skipped_kids"]],
                ["keep-tagged",            stats["skipped_keep"]],
                ["failed",                 stats["failed"]],
                ["series queued",          len(queued)],
                ["tier-groups",            _group_count],
            ],
            title=f"[JIT] grab pass '{instance}' (reserve {reserve_gb:.0f} GB)",
            caption="Per-pass outcome of the just-in-time next-up grab: how many upcoming "
                    "episodes were acquired or re-qualitied, what was skipped and why, and how "
                    "much was queued for the background step-down search.",
            descriptions=[
                "missing next-up episodes grabbed fresh",
                "owned next-up episodes re-grabbed at the calibrated tier",
                "downgrades skipped: series watched within the active window",
                "skipped: grab would breach the disk reserve",
                "skipped: kids-cert series",
                "skipped: keep_series / keep_season tagged",
                "search or profile-set call errored",
                "series handed to the background step-down search worker",
                "distinct (series, target-tier) search groups queued",
            ],
        )
        return stats

    def _reconcile_failed_jit(self, instance: str, df) -> bool:
        """
        Re-enable episodes that a prior run's background step-down search failed
        to grab: reset their JIT flags so this run's pass re-attempts them.
        Consumes (deletes) the side cache. Returns True if any row changed.
        """
        if not self.global_cache:
            return False
        key = f"sonarr/{instance}/jit/failed_upgrades"
        try:
            failed = self.global_cache.get(key) or []
        except Exception:
            failed = []
        if not failed:
            return False

        reset = 0
        for f in failed:
            if not isinstance(f, dict):
                continue
            try:
                sid = int(f.get("series_id"))
                sn  = int(f.get("season"))
                en  = int(f.get("episode"))
            except (TypeError, ValueError):
                continue
            mask = (
                (df["series_id"] == sid)
                & (df["season_number"] == sn)
                & (df["episode_number"] == en)
            )
            if mask.any():
                df.loc[mask, "upgraded_for_watching"] = False
                df.loc[mask, "pre_upgrade_quality"]   = None
                reset += int(mask.sum())

        try:
            self.global_cache.delete(key)
        except Exception:
            pass
        if reset:
            self.logger.log_info(
                f"[JIT] Re-enabled {reset} episode(s) for retry "
                f"(no release grabbed last run)."
            )
        return reset > 0

    def _spawn_jit_search_worker(self, instance: str, work: dict) -> None:
        """
        Fire-and-forget background worker that, per series and per target-tier
        group: bumps the quality profile to that group's tier, runs EpisodeSearch
        for ONLY that group's episodes, and for episodes that grab nothing steps
        DOWN one profile at a time until they grab or the group ladder is
        exhausted — then restores the series' original profile ONCE.
        Episodes that never grab are recorded for retry on the next run.

        ``work`` is shaped ``{sid: {tier_res: {"eps": [...], "step_pids": [...]}}}``.
        It is flattened to ONE entry per series carrying that series' tier-groups
        (shape A), so the worker captures the series' original profile exactly
        once before any flip and reverts exactly once after the last group — a
        flat per-group list could otherwise capture an already-bumped profile as
        the "original" and revert to the wrong tier.

        Runs as a NON-daemon thread: it never blocks the main pipeline (we do
        not join it), but the interpreter will not exit until it finishes, so
        the QP is always restored. Every search poll is timeout-bounded so the
        thread can never hang the process.
        """
        import threading

        items = []
        for sid, tiers in work.items():
            groups = []
            for tier_res, g in tiers.items():
                eps = list(g.get("eps") or [])
                step_pids = list(g.get("step_pids") or [])
                if eps and step_pids:
                    groups.append((int(tier_res), eps, step_pids))
            if groups:
                items.append((int(sid), groups))
        if not items:
            return
        _group_count = sum(len(groups) for _sid, groups in items)
        threading.Thread(
            target=self._jit_search_worker,
            args=(instance, items),
            name="jit-qp-search",
            daemon=False,
        ).start()
        self.logger.log_info(
            f"[JIT] Background step-down search worker started for {len(items)} series "
            f"({_group_count} tier-group(s)) across up to "
            f"{min(self.JIT_SEARCH_MAX_WORKERS, len(items))} parallel worker(s)."
        )

    def _jit_search_worker(self, instance: str, items: list) -> None:
        """
        Per series, per target-tier GROUP: search that group's step-down ladder
        (best→worst), re-searching only the not-yet-grabbed episodes OF THAT
        GROUP at each lower tier, until they grab or the group ladder is
        exhausted. Episodes that never grab are recorded for next-run retry.

        ``items`` is shaped ``[(sid, [(tier_res, eps, step_pids), ...]), ...]``.
        The series' original profile is captured ONCE before any flip and the QP
        is reverted ONCE after the last group (and on any error). Because each
        group's EpisodeSearch carries only that group's episodeIds and the
        group's ladder never rises above the group's tier, a lower-target episode
        is NEVER searched while the series profile is flipped to a higher tier —
        the group-by-tier invariant that prevents over-grab.
        """
        POLL_INTERVAL_S = 3.0
        CMD_TIMEOUT_S   = 180.0
        DONE_STATES     = ("completed", "failed", "aborted", "cancelled")

        def _label(sid, info=None):
            """Readable series id for the log: ``sonarr/<instance> '<title>' (tvdb-<id>)``.
            ``info`` is any series dict already fetched (it carries title + tvdbId), so this
            costs no extra request; falls back to the raw id when the title is unknown."""
            if isinstance(info, dict):
                title = (info.get("title") or "").strip()
                if title:
                    tvdb = info.get("tvdbId")
                    tvdb_s = f"tvdb-{tvdb}" if tvdb else f"sid-{sid}"
                    return f"sonarr/{instance} '{title}' ({tvdb_s})"
            return f"sonarr/{instance} series {sid}"

        def _wait_command(cid):
            if not cid:
                return
            start = time.time()
            while time.time() - start < CMD_TIMEOUT_S:
                cmd = self.sonarr_api._make_request(instance, f"command/{cid}", fallback=None)
                if (cmd or {}).get("status") in DONE_STATES:
                    return
                time.sleep(POLL_INTERVAL_S)

        def _revert(sid, original_pid):
            if original_pid is None:
                return
            fresh = self.sonarr_api._make_request(instance, f"series/{sid}", fallback=None)
            if (fresh and isinstance(fresh, dict)
                    and fresh.get("qualityProfileId") != original_pid):
                fresh["qualityProfileId"] = original_pid
                self.sonarr_api._make_request(
                    instance, f"series/{sid}", method="PUT", payload=fresh
                )
                self.logger.log_info(
                    f"  ↩️ JIT QP revert: {_label(sid, fresh)} → profile {original_pid}"
                )

        def _process_series(sid, groups) -> list:
            """Run ONE series' step-down ladder and return its not-grabbed episodes.

            Every series owns its own profile (flip + revert) and its own
            episodeIds, so series are independent and run concurrently. The
            ladder WITHIN a series stays strictly sequential — that is what
            preserves the group-by-tier invariant (a lower-target episode is
            never searched while the profile is flipped to a higher tier).
            """
            failed: list = []
            original_pid = None
            label = _label(sid)
            try:
                base = self.sonarr_api._make_request(instance, f"series/{sid}", fallback=None)
                if not (base and isinstance(base, dict)):
                    return failed
                label = _label(sid, base)
                # Capture the pre-flip profile ONCE, before any group bumps the QP, so the
                # end-of-series revert always restores the true original (never an intermediate
                # group's tier).
                original_pid = base.get("qualityProfileId")

                for tier_res, eps, step_pids in groups:
                    ep_meta   = {int(e[0]): (int(e[1]), int(e[2])) for e in eps if e and e[0]}
                    remaining = set(ep_meta.keys())

                    for pid in step_pids:
                        if not remaining:
                            break
                        s = self.sonarr_api._make_request(instance, f"series/{sid}", fallback=None)
                        if not (s and isinstance(s, dict)):
                            break
                        if s.get("qualityProfileId") != pid:
                            s["qualityProfileId"] = pid
                            self.sonarr_api._make_request(
                                instance, f"series/{sid}", method="PUT", payload=s
                            )
                        # EpisodeSearch carries ONLY this group's remaining episodeIds, so it can
                        # never grab a higher tier for a lower-target episode of another group.
                        _cmd = self.sonarr_api._make_request(
                            instance, "command", method="POST",
                            payload={"name": "EpisodeSearch", "episodeIds": list(remaining)},
                        )
                        _wait_command(_cmd.get("id") if isinstance(_cmd, dict) else None)
                        grabbed_now = self._episodes_in_queue(instance, list(remaining))
                        if grabbed_now:
                            remaining -= grabbed_now
                            self.logger.log_info(
                                f"  ✅ JIT grab: {label} grabbed {len(grabbed_now)} ep(s) "
                                f"at profile {pid} (≤{tier_res}p tier, {len(remaining)} still searching)"
                            )
                        else:
                            self.logger.log_info(
                                f"  ⏬ JIT step-down: {label} found nothing at profile {pid}"
                            )

                    if remaining:
                        self.logger.log_info(
                            f"  ∅ JIT: {label} — {len(remaining)} ep(s) in the ≤{tier_res}p "
                            f"tier found no release across {len(step_pids)} profile(s); "
                            f"queued for retry next run"
                        )
                        for _eid in remaining:
                            _sn, _en = ep_meta[_eid]
                            failed.append({"series_id": sid, "season": _sn, "episode": _en})

                # Revert ONCE per series, after the last group, to the captured pre-flip profile.
                _revert(sid, original_pid)
            except Exception as e:
                self.logger.log_warning(
                    f"[JIT] Background step-down search failed for {label}: {e}"
                )
                try:  # best-effort revert on error — original_pid was captured before any flip
                    _revert(sid, original_pid)
                except Exception:
                    pass
            return failed

        # Run the series ladders CONCURRENTLY. Each ladder is independent (its own
        # profile + episodeIds); writes to the one Sonarr instance still serialize on
        # the per-instance write lock, but the long command/queue waits now overlap
        # instead of one series blocking the next. Each _process_series swallows its
        # own errors and returns its failed list, so a single bad series can't sink
        # the pool, and the QP is reverted on every path.
        failed_all: list = []
        if len(items) <= 1:
            for sid, groups in items:
                failed_all.extend(_process_series(sid, groups))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            max_workers = min(self.JIT_SEARCH_MAX_WORKERS, len(items))
            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="jit-search"
            ) as ex:
                futures = [ex.submit(_process_series, sid, groups) for sid, groups in items]
                for fut in as_completed(futures):
                    try:
                        failed_all.extend(fut.result() or [])
                    except Exception as e:  # _process_series shouldn't raise, but stay defensive
                        self.logger.log_warning(f"[JIT] step-down series task crashed: {e}")

        # Persist not-grabbed episodes so the next run re-enables them.
        if failed_all and self.global_cache:
            try:
                key = f"sonarr/{instance}/jit/failed_upgrades"
                existing = self.global_cache.get(key) or []
                self.global_cache.set(key, list(existing) + failed_all)
            except Exception as e:
                self.logger.log_warning(
                    f"[JIT] Could not persist failed upgrades for retry: {e}"
                )

    def _episodes_in_queue(self, instance: str, ep_ids: list,
                           attempts: int = 3, delay_s: float = 2.0) -> set:
        """
        Return the subset of ep_ids that currently have a download-queue item
        (i.e. a release was just grabbed). Retries briefly because the queue can
        lag the EpisodeSearch command completing.
        """
        wanted = {int(e) for e in ep_ids if e}
        if not wanted:
            return set()
        # Sonarr's /queue/details wants REPEATED episodeIds params (?episodeIds=1&episodeIds=2),
        # NOT a comma-joined value — 'id1,id2,...' 400s ("The value '...' is not valid"). That made
        # this poll always fail, so the step-down worker never saw its grab land in the queue and
        # churned the profile DOWN the ladder (the 7->6->4 false "found nothing" stepping).
        _q = "&".join(f"episodeIds={e}" for e in wanted)
        for i in range(max(1, attempts)):
            found = set()
            try:
                resp = self.sonarr_api._make_request(
                    instance, f"queue/details?{_q}", fallback=[]
                ) or []
                for rec in resp:
                    if not isinstance(rec, dict):
                        continue
                    eid = rec.get("episodeId")
                    if eid is None:
                        eid = (rec.get("episode") or {}).get("id")
                    if eid is not None and int(eid) in wanted:
                        found.add(int(eid))
            except Exception:
                found = set()
            if found:
                return found
            if i < attempts - 1:
                time.sleep(delay_s)
        return set()

    @LoggerManager().log_function_entry
    @timeit("run_jit_quality_restores")
    def run_jit_quality_restores(self, instance: str) -> dict:
        """
        Restore episode files to their pre-upgrade quality after watching.

        For every episode where upgraded_for_watching=True AND is_watched=True:
          1. Fetches the current episodefile from Sonarr.
          2. PUTs the quality object back to the pre_upgrade_quality snapshot.
          3. Clears the JIT flags.

        This keeps the high-quality slot free while leaving the episode
        accessible at its original quality for out-of-order viewing.
        """
        import json

        stats = {"checked": 0, "restored": 0, "failed": 0, "no_snapshot": 0}

        df = self.load(instance)
        if df.empty or "upgraded_for_watching" not in df.columns:
            return stats

        # Use completion threshold: only restore after episode is substantially
        # watched (>= 80%). Avoids restoring when someone starts then stops.
        RESTORE_PCT_THRESHOLD = 80.0
        pct_col = "percent_complete" if "percent_complete" in df.columns else None
        if pct_col:
            restore_mask = (
                (df["upgraded_for_watching"] == True) &
                (df["is_watched"] == True) &
                (df[pct_col].fillna(0) >= RESTORE_PCT_THRESHOLD)
            )
        else:
            restore_mask = (
                (df["upgraded_for_watching"] == True) &
                (df["is_watched"] == True)
            )
        candidates = df[restore_mask]
        changed = False

        for idx, row in candidates.iterrows():
            stats["checked"] += 1
            fid   = row.get("episode_file_id")
            sn    = int(row.get("season_number") or 0)
            en    = int(row.get("episode_number") or 0)
            title = row.get("series_title") or ""
            snap  = row.get("pre_upgrade_quality")

            if pd.isna(fid):
                stats["failed"] += 1
                continue

            if not snap or pd.isna(snap):
                df.at[idx, "upgraded_for_watching"] = False
                stats["no_snapshot"] += 1
                changed = True
                continue

            try:
                original = json.loads(snap)
            except (json.JSONDecodeError, TypeError):
                df.at[idx, "upgraded_for_watching"] = False
                stats["no_snapshot"] += 1
                changed = True
                continue

            try:
                current = self.sonarr_api._make_request(
                    instance, f"episodefile/{int(fid)}", fallback=None
                )
                if not current or not isinstance(current, dict):
                    stats["failed"] += 1
                    continue

                # Patch the quality sub-object back; preserve revision
                q_block = current.get("quality") or {}
                q_inner = q_block.get("quality") or {}
                q_inner["name"]       = original.get("quality_name")   or q_inner.get("name")
                q_inner["source"]     = original.get("quality_source") or q_inner.get("source")
                q_inner["resolution"] = original.get("resolution")     or q_inner.get("resolution")
                q_block["quality"]    = q_inner
                current["quality"]    = q_block

                # Guard: never restore below the pilot_successful_profile_id
                # (the profile that first successfully downloaded this series).
                # Check by resolution: if the original quality resolution is
                # lower than what the successful profile supports, skip restore.
                _succ_pid = row.get("pilot_successful_profile_id") if hasattr(row, "get")                     else df.at[idx, "pilot_successful_profile_id"]                     if "pilot_successful_profile_id" in df.columns else None
                if _succ_pid and pd.notna(_succ_pid):
                    try:
                        _succ_pid = int(_succ_pid)
                        _profiles = self.sonarr_api._make_request(
                            instance, "qualityprofile", fallback=[]
                        ) or []
                        _succ_profile = next(
                            (p for p in _profiles if p.get("id") == _succ_pid), None
                        )
                        if _succ_profile:
                            def _min_res(p):
                                best = 9999
                                for item in (p.get("items") or []):
                                    if item.get("allowed"):
                                        res = (item.get("quality") or {}).get("resolution", 9999)
                                        if isinstance(res, (int, float)):
                                            best = min(best, int(res))
                                return best if best < 9999 else 0
                            succ_min = _min_res(_succ_profile)
                            orig_res = original.get("resolution") or 0
                            if orig_res and int(orig_res) < succ_min:
                                self.logger.log_info(
                                    f"  🔒 JIT restore skipped for '{title}' "
                                    f"S{sn:02d}E{en:02d}: original resolution "
                                    f"{orig_res}p < successful floor {succ_min}p"
                                )
                                # Still clear the JIT flag — episode was watched
                                df.at[idx, "upgraded_for_watching"] = False
                                df.at[idx, "pre_upgrade_quality"]   = None
                                changed = True
                                stats["restored"] += 1
                                continue
                    except Exception:
                        pass  # on any error, proceed with normal restore

                self.sonarr_api._make_request(
                    instance, f"episodefile/{int(fid)}",
                    method="PUT", payload=current,
                )
                df.at[idx, "upgraded_for_watching"]  = False
                df.at[idx, "pre_upgrade_quality"]    = None
                df.at[idx, "quality_name"]   = original.get("quality_name")
                df.at[idx, "quality_source"] = original.get("quality_source")
                df.at[idx, "resolution"]     = original.get("resolution")
                df.at[idx, "video_codec"]    = original.get("video_codec")
                changed = True
                stats["restored"] += 1
                self.logger.log_info(
                    f"  ⬇️ JIT restore: '{title}' S{sn:02d}E{en:02d} → "
                    f"{original.get('quality_name', '?')}"
                )
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ JIT restore failed for '{title}' S{sn:02d}E{en:02d}: {e}"
                )
                stats["failed"] += 1

        # NOTE: series quality-profile reverts are no longer done here — the JIT
        # upgrade pass bumps the profile only for the duration of its
        # EpisodeSearch and a background worker restores the original profile as
        # soon as that search completes. This restore pass handles file quality
        # only.

        if changed:
            self.save(instance, df)

        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["restored",    stats["restored"]],
                ["no-snapshot", stats["no_snapshot"]],
                ["failed",      stats["failed"]],
            ],
            title=f"[JIT] Restore pass '{instance}'",
            caption="Per-pass outcome of the JIT file-quality restore: how many upgraded "
                    "episodes were rolled back to their pre-upgrade file.",
            descriptions=[
                "episodes restored to their pre-upgrade file",
                "episodes skipped: no pre-upgrade snapshot recorded",
                "restore search calls that errored",
            ],
        )
        return stats

    @LoggerManager().log_function_entry
    @timeit("sync_from_tautulli")
    def sync_from_tautulli(self, instance: str) -> dict:
        """
        Synchronise the episode-file Parquet with Tautulli watch history.

        For each (series, season, episode) tuple in history:
        - If a Parquet row already exists for that episode → update watch stats.
        - If no row exists → resolve the episode file via Sonarr and add a row.

        Series name matching uses ``sonarr_cache.series.get_series_by_title``
        (case-insensitive).  Episodes unresolvable in Sonarr are skipped with
        a debug log.

        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        stats = {
            "history_entries": 0,
            "series_matched":  0,
            "updated":         0,
            "added":           0,
            "skipped":         0,
        }

        history = self._collect_tautulli_episode_history()
        if not history:
            self.logger.log_info(
                "📭 No Tautulli episode history found — skipping watched sync."
            )
            return stats

        stats["history_entries"] = len(history)
        self.logger.log_info(
            f"📺 Syncing {len(history)} watched episode(s) from Tautulli → '{instance}'…"
        )

        # Cache per-series recent watcher(s) so the later JIT grab grid can show who each
        # next-up was acquired FOR (display-only; never affects grab decisions).
        try:
            if self.global_cache:
                self.global_cache.set(f"sonarr/{instance}/jit_watchers",
                                      self._build_jit_watchers(instance, history))
        except Exception as e:
            self.logger.log_debug(f"[JIT] watcher attribution skipped: {e}")

        # Household members from config — used to gate grace-period countdown.
        household_members = self._get_household_members()
        if household_members:
            self.logger.log_info(
                f"👨‍👩‍👧‍👦 Household watch tracking active: {household_members}"
            )
        # Optional per-member quorum: count a title as household-watched once a fraction of
        # members have watched it, rather than requiring every single member. Default-off
        # (household_watch_quorum unset / fraction>=1.0) → quorum None → require all,
        # byte-identical. fraction is clamped to [0,1] and rounded UP to a member count.
        _hh_quorum: "int | None" = None
        _hq_cfg = ((self.config or {}).get("household_watch_quorum") or {})
        if _hq_cfg.get("enabled") and household_members:
            try:
                _frac = max(0.0, min(1.0, float(_hq_cfg.get("fraction", 1.0))))
            except (TypeError, ValueError):
                _frac = 1.0
            _raw = _frac * len(household_members)
            _need = max(1, int(_raw) + (1 if _raw > int(_raw) else 0))   # ceil, no math import
            if _need < len(household_members):   # ==len → require all → leave None (identical)
                _hh_quorum = _need

        df = self.load(instance)

        # Ensure household columns exist for backward-compat with pre-schema Parquets.
        for _hcol in ("all_household_watched", "household_last_watched_at"):
            if _hcol not in df.columns:
                df[_hcol] = pd.Series([None] * len(df), dtype=object, index=df.index)

        # Build a fast lookup: (series_id, season, episode) → row index.
        # Use pd.notna rather than `is not None` — NaN is a float, not None,
        # so `is not None` would pass NaN through and int(NaN) would crash.
        # Pilot rows intentionally have episode_number=NaN; they are correctly
        # excluded by this guard and never appear in existing_key.
        existing_key: dict[tuple, int] = {}
        if not df.empty:
            for idx, row in df.iterrows():
                sid = row.get("series_id")
                sn  = row.get("season_number")
                en  = row.get("episode_number")
                if pd.notna(sid) and pd.notna(sn) and pd.notna(en):
                    existing_key[(int(sid), int(sn), int(en))] = idx

        # Sonarr series title lookup (case-insensitive)
        series_mgr = getattr(self.sonarr_cache, "series", None)

        new_rows:     list[dict] = []
        updated_idxs: list[int]  = []
        files_session_cache: dict[int, list] = {}
        season_ep_cache:     dict[tuple, list] = {}  # (series_id, season) → episodes; shared across loop + pipeline
        _loop_start = time.time()
        _total = len(history)

        for _loop_i, ((series_title, season, episode), watch) in enumerate(history.items(), start=1):
            # Resolve Sonarr series ID from title
            sonarr_series: dict | None = None
            if series_mgr:
                try:
                    sonarr_series = series_mgr.get_series_by_title(instance, series_title)
                except Exception:
                    pass

            if not sonarr_series:
                self.logger.log_debug(
                    f"  ⤵ No Sonarr match for Tautulli title '{series_title}' — skipping"
                )
                stats["skipped"] += 1
                continue

            stats["series_matched"] += 1
            sid = sonarr_series["id"]
            key = (sid, season, episode)

            if key in existing_key:
                # Update watch stats on the existing row
                row_idx = existing_key[key]
                df.at[row_idx, "watch_count"]    = watch["watch_count"]
                df.at[row_idx, "last_watched_at"] = watch["last_watched_at"]
                df.at[row_idx, "percent_complete"] = watch["percent_complete"]
                df.at[row_idx, "is_watched"]      = True
                # Recompute household watch state on every sync so a newly-added
                # watcher is reflected immediately rather than waiting for TTL.
                _all_hh, _hh_ts = self._resolve_household_watch_state(
                    watch.get("per_user", {}), household_members, quorum=_hh_quorum
                )
                df.at[row_idx, "all_household_watched"]     = _all_hh
                df.at[row_idx, "household_last_watched_at"] = _hh_ts
                df.at[row_idx, "last_synced_at"]           = datetime.now(tz=timezone.utc).isoformat()
                updated_idxs.append(row_idx)
                stats["updated"] += 1
            else:
                # Fetch episode file metadata and add a new row
                file_rec, _, air_date_utc = self._resolve_episode_file(
                    instance, sid, season, episode, files_session_cache, season_ep_cache
                )
                if file_rec:
                    _all_hh, _hh_ts = self._resolve_household_watch_state(
                        watch.get("per_user", {}), household_members, quorum=_hh_quorum
                    )
                    row = self._normalise(
                        raw=file_rec,
                        series_id=sid,
                        series_title=series_title,
                        season_number=season,
                        episode_number=episode,
                        is_pilot=False,
                        watch_count=watch["watch_count"],
                        last_watched_at=watch["last_watched_at"],
                        percent_complete=watch["percent_complete"],
                        air_date_utc=air_date_utc,
                        all_household_watched=_all_hh,
                        household_last_watched_at=_hh_ts,
                    )
                    new_rows.append(row)
                    stats["added"] += 1
                else:
                    self.logger.log_debug(
                        f"  ⤵ No episode file in Sonarr for "
                        f"'{series_title}' S{season:02d}E{episode:02d} — skipping"
                    )
                    stats["skipped"] += 1

            # Progress checkpoint every 25% of total entries
            _checkpoint = max(1, _total // 4)
            if _loop_i % _checkpoint == 0 or _loop_i == _total:
                _elapsed = time.time() - _loop_start
                _rate    = _loop_i / _elapsed if _elapsed > 0 else 0
                _eta     = (_total - _loop_i) / _rate if _rate > 0 else 0
                self.logger.log_info(
                    f"  ⏳ [{_loop_i}/{_total}] — "
                    f"{stats['updated']} updated, {stats['added']} added, {stats['skipped']} skipped — "
                    f"{_elapsed:.0f}s elapsed, ETA ~{_eta:.0f}s — "
                    f"last: '{series_title}' S{season:02d}E{episode:02d}"
                )

        if new_rows:
            df_new = pd.DataFrame(new_rows, columns=self.SCHEMA_COLUMNS)
            for col in self._NUMERIC_COLUMNS:
                if col in df_new.columns:
                    df_new[col] = pd.to_numeric(df_new[col], errors="coerce")
            df = self._safe_concat(df, df_new)

        # ── Lifecycle pipeline ────────────────────────────────────────────────
        # 0. Resolve keep-policy from Sonarr tags → stamp keep_policy column.
        #    Must run before _apply_grace_period so the policy is readable when
        #    deciding whether to mark rows for deletion.
        _ps = time.time()
        self.logger.log_info(f"[⏱️] Pipeline start — {len(df)} rows")

        df = self._sync_keep_policies(df, instance)
        self.logger.log_info(f"[⏱️] keep_policies — {time.time()-_ps:.1f}s")

        df = self._compute_next_episodes(df, instance, files_session_cache, season_ep_cache=season_ep_cache)
        self.logger.log_info(f"[⏱️] compute_next_episodes — {time.time()-_ps:.1f}s")

        acquire_stats = self._do_acquire_next_episodes(instance, df)
        self.logger.log_info(f"[⏱️] acquire_next_episodes — {time.time()-_ps:.1f}s")

        df = self._apply_grace_period(df)
        self.logger.log_info(f"[⏱️] apply_grace_period — {time.time()-_ps:.1f}s")

        # When the cross-service space coordinator owns deletion, keep MARKING
        # (above) so it has candidates, but defer the actual episode deletion +
        # purge to the coordinator's unified, space-driven, lowest-watchability pool.
        if coordinator_owns_deletion(self.config):
            delete_stats = {"deleted": 0, "bytes_freed": 0.0}
            purge_stats  = {"purged": 0}
            self.logger.log_info(
                "[EpisodeFiles] deletion delegated to the space-pressure coordinator "
                "(grace marks applied)."
            )
        else:
            df, delete_stats = self._do_delete_marked_files(instance, df)
            self.logger.log_info(f"[⏱️] delete_marked_files — {time.time()-_ps:.1f}s")

            df, purge_stats = self._do_purge_sonarr_deleted(instance, df)
            self.logger.log_info(f"[⏱️] purge_sonarr_deleted — {time.time()-_ps:.1f}s")

        df, cleanup_count = self._do_cleanup_non_essential(df)
        self.logger.log_info(f"[⏱️] cleanup_non_essential — {time.time()-_ps:.1f}s")

        stats["acquired"]    = acquire_stats["triggered"]
        stats["deleted"]     = delete_stats["deleted"]
        stats["bytes_freed"] = delete_stats.get("bytes_freed", 0.0)
        stats["purged"]      = purge_stats["purged"]
        stats["cleaned_up"]  = cleanup_count

        # ── Decision ledger: stamp delete + acquire plans from the final state ──
        # Persisted in dry_run via the save below so the Parquet is a queryable
        # preview. Only the delete/acquire rows are (re)written — a JIT 'upgrade'
        # plan stamped elsewhere is preserved.
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        # Reloaded all-null Parquet columns come back as float64; force the
        # string-plan columns to object so the str assignments below don't trip
        # pandas' incompatible-dtype FutureWarning.
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)
        if not df.empty:
            _marked = (
                df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
                if "marked_for_deletion" in df.columns else pd.Series(False, index=df.index)
            )
            _nextep = (
                df["next_episode"].infer_objects(copy=False).fillna(False).astype(bool)
                if "next_episode" in df.columns else pd.Series(False, index=df.index)
            )
            _nofile = (
                df["episode_file_id"].isna()
                if "episode_file_id" in df.columns else pd.Series(True, index=df.index)
            )
            _gb = (
                (pd.to_numeric(df["size_bytes"], errors="coerce") / (1024 ** 3)).round(2)
                if "size_bytes" in df.columns else pd.Series(0.0, index=df.index)
            )
            # Clear the delete/acquire/downgrade/upgrade plans. Each pass re-stamps
            # its own this run AFTER this sync: run_space_pressure_downgrades re-stamps
            # 'downgrade' under pressure; run_jit_quality_upgrades re-stamps 'upgrade'.
            # Clearing here means stale plans from a prior run disappear once they no
            # longer apply (space recovered, episode now best quality, etc.).
            _owned = df["planned_action"].isin(["delete", "acquire", "downgrade", "upgrade"])
            df.loc[_owned, "planned_action"]  = None
            df.loc[_owned, "plan_reason"]     = None
            df.loc[_owned, "plan_reclaim_gb"] = None
            # acquire: an upcoming flagged episode that has no file yet. Reclaim is
            # NEGATIVE (space CONSUMED). Estimate each upcoming episode at the median
            # size of its series' EXISTING files (what that series actually grabs),
            # falling back to the library-wide median — an API-free preview.
            _acq = _nextep & _nofile
            df.loc[_acq, "planned_action"] = "acquire"
            df.loc[_acq, "plan_reason"]    = "upcoming episode in watch window"
            if _acq.any() and "size_bytes" in df.columns and "series_id" in df.columns:
                _filed = df["episode_file_id"].notna()
                _szb = pd.to_numeric(df["size_bytes"], errors="coerce")
                _series_med = _szb[_filed].groupby(df.loc[_filed, "series_id"]).median() if _filed.any() else pd.Series(dtype=float)
                _lib_med = float(_szb[_filed].median()) if (_filed.any() and _szb[_filed].notna().any()) else 0.0
                for _i in df.index[_acq]:
                    _b = _series_med.get(df.at[_i, "series_id"], float("nan"))
                    if pd.isna(_b):
                        _b = _lib_med
                    df.at[_i, "plan_reclaim_gb"] = (-round(float(_b) / (1024 ** 3), 2)) if (_b and _b == _b) else 0.0
            # delete: grace-expired marked rows. Only stamp here when the per-service
            # delete path owns deletion — Sonarr's _do_delete_marked_files removes ALL
            # marked files this run, so the grace marks ARE what gets deleted. When the
            # cross-service coordinator owns deletion, it deletes only a space-driven
            # SUBSET and stamps its own selection (delete_selected_episode_files), so
            # stamping every marked row here would over-report. The _owned clear above
            # already reset any prior 'delete' plan, so skipping the stamp leaves the
            # coordinator to re-stamp.
            if not coordinator_owns_deletion(self.config):
                df.loc[_marked, "planned_action"]  = "delete"
                df.loc[_marked, "plan_reason"]     = "watched; grace period expired"
                df.loc[_marked, "plan_reclaim_gb"] = _gb[_marked]
                # De-dupe reclaim across multi-episode files: N marked rows can share
                # ONE episode_file_id, but deleting it frees the file once. plan_summary
                # sums plan_reclaim_gb per row, so stamp the size on the FIRST marked
                # row per file id and null the rest — else the dry-run "would free X GB"
                # preview (the operator's go/no-go signal) is inflated ×n_eps. (Mirrors
                # the one-row stamping the Phase-3 downgrade pass already uses.)
                if "episode_file_id" in df.columns:
                    _seen_fids: set = set()
                    for _i in df.index[_marked]:
                        _fid = df.at[_i, "episode_file_id"]
                        if pd.isna(_fid):
                            continue
                        _fid = int(_fid)
                        if _fid in _seen_fids:
                            df.at[_i, "plan_reclaim_gb"] = None
                        else:
                            _seen_fids.add(_fid)

        # Persist the lifecycle result — the Parquet is a local read-only mirror,
        # so it is written even in dry_run (the deletion/acquire steps above already
        # no-op their *arr writes under dry_run and leave the rows intact, so the
        # marks persisted here are a faithful preview, not phantom changes).
        if not df.empty:
            self.save(instance, df)
            if self.dry_run:
                self.logger.log_debug(
                    f"[dry_run] Persisted episode_files cache for '{instance}' "
                    f"({len(df)} rows) — local write only, no Sonarr changes."
                )

        verb = "would free" if self.dry_run else "freed"
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["added",      stats["added"]],
                ["updated",    stats["updated"]],
                ["skipped",    stats["skipped"]],
                ["acquired",   stats["acquired"]],
                ["deleted",    stats["deleted"]],
                ["purged",     stats["purged"]],
                ["cleaned up", stats["cleaned_up"]],
            ],
            title=f"✅ Tautulli sync complete '{instance}' ({verb} {self._fmt_bytes(stats['bytes_freed'])})",
            caption="End-of-sync rollup of how each episode row changed while reconciling "
                    "the Parquet cache against Tautulli watch history.",
            descriptions=[
                "new episode rows added from watch history",
                "existing rows updated with fresh watch stats",
                "history entries skipped: unresolvable in Sonarr",
                "missing episodes a fresh grab was triggered for",
                "episode files deleted under keep policy",
                "rows purged: episode file gone from Sonarr",
                "non-essential rows cleaned out of the cache",
            ],
        )
        return stats

    # ── Reporting / ML helpers ──────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_episode_file_summary")
    def get_summary(self, instance: str) -> dict:
        """Quick stats on what's in the Parquet — useful for diagnostics."""
        df = self.load(instance)
        if df.empty:
            return {
                "total_rows": 0,
                "pilot_rows": 0,
                "watched_rows": 0,
                "series_covered": 0,
            }

        return {
            "total_rows":     len(df),
            "pilot_rows":     int(df["is_pilot"].sum()) if "is_pilot" in df.columns else 0,
            "watched_rows":   int(df["is_watched"].sum()) if "is_watched" in df.columns else 0,
            "series_covered": int(df["series_id"].nunique()),
            "total_size_gb":  round(df["size_bytes"].sum() / 1e9, 2)
                              if "size_bytes" in df.columns else 0.0,
            "codec_dist":     df["video_codec"].value_counts().to_dict()
                              if "video_codec" in df.columns else {},
            "resolution_dist": df["resolution"].value_counts().to_dict()
                               if "resolution" in df.columns else {},
            "hdr_count":      int(df["hdr"].sum()) if "hdr" in df.columns else 0,
        }
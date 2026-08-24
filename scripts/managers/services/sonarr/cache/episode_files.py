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

import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.acquisition.next_episode_planner import (
    DEFAULT_BUDGET_RAMP,
    DEFAULT_GRADUATED_CAP,
    DEFAULT_RECENCY_GATE,
    build_runtime_lookup,
    episode_cap,
    group_key_for_series,
    group_members,
    is_cold_series,
    last_watched_per_series,
    order_groups_by_recency,
    order_series_by_recency,
    series_budget_multiplier,
)
from scripts.managers.machine_learning.acquisition.pilot_stepping import (
    choose_pilot_profile,
    indexer_fingerprint,
    next_pilot_profile,
    next_pilot_profile_descend,
    pilot_backoff_interval,
    pilot_recheck_due,
    pilot_search_due,
    pilot_watchability_keep,
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
from scripts.managers.machine_learning.lifecycle.restore_policy import (
    RELEASE_FIELDS,
    episode_key,
    history_release_record,
    ledger_releases,
    match_release,
    merge_ledger_entry,
    merge_release_records,
    push_descriptor,
    push_descriptors_by_episode,
    release_record,
)
from scripts.managers.machine_learning.space.deletion_log import (
    coverage,
    deletion_record,
    drift_after_rebuild,
    intersection_drift,
    merge_upgrade_intents,
    new_run_id,
    reconcile_upgrades,
    split_by_kind,
    to_jsonl,
    upgrade_intent,
)
from scripts.managers.machine_learning.lifecycle.stale_prune_policy import (
    restore_cooldown_active,
)
from scripts.managers.machine_learning.lifecycle.viewer_retention import (
    account_facts,
    episode_ordinal,
    merge_state,
    resolve_retention_config,
    series_protected_from_states,
    watched_by_tautulli,
)
from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    series_universe_credits,
)
from scripts.managers.machine_learning.sizing.size_model import (
    CALIBRATED_MB_PER_MIN,
    estimate_gb,
    estimate_gb_for_profile,
    measured_mb_per_min,
    profile_max_quality,
)
from scripts.managers.machine_learning.space import jit_backoff
from scripts.managers.machine_learning.space.downgrade_planner import (
    DEFAULT_FLOOR_RESOLUTION as DOWNGRADE_FLOOR_RESOLUTION,
)
from scripts.managers.machine_learning.space.jit_planner import (
    choose_jit_profile,
    jit_reserve_gb,
    jit_row_skip,
    jit_step_down_pids,
    next_up_grab_candidates,
    pilot_floor_hold,
    target_tier_key,
)
from scripts.managers.machine_learning.thresholds.registry import get_threshold
from scripts.managers.services.plex.playlists.universe_order import (
    tv_group_maps_from_series,
)
from scripts.support.utilities.backup_gate import effective_dry_run
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import (
    coordinator_owns_deletion,
    deletions_disabled_reason,
    deletions_enabled,
    exhaustive_downgrade,
    space_targets,
)
from scripts.support.utilities.watch_likelihood import (
    resolution_cap_for_likelihood,
    watch_likelihood,
)


class SonarrCacheEpisodeFilesManager(BaseManager, ComponentManagerMixin):

    PILOT_BATCH_SIZE  = None   # max *unwatched* series per enrichment run (None = unlimited)
    CACHE_MAX_AGE     = 172_800  # 48 hours
    # ── stale-stub herd control ───────────────────────────────────────────────
    # Stubs created together (cold start) expire together, and re-checking one
    # refreshes its timestamp — so the whole cohort re-expires together forever:
    # a ~7.7k-series, ~77s serial API spike every 48h. Two dampers:
    #   * JITTER spreads each series' effective TTL deterministically over
    #     [TTL, TTL*(1+PCT)] by series id, so the cohort fans out instead of
    #     firing as one block. Deterministic (not random) so a series' due-time
    #     is stable across runs rather than re-rolled every pass.
    #   * CAP bounds how many stale stubs are re-checked per run, OLDEST FIRST.
    #     Deferred stubs keep their old timestamp, so they stay due and are
    #     picked up next run — FIFO, nothing starves.
    PILOT_STALE_TTL_JITTER_PCT = 0.25   # 48h → up to 60h, spread by series id
    PILOT_STALE_RECHECK_CAP    = 2000   # per run; config: pilot_stale_recheck_cap (0 = unlimited)
    GRACE_HOURS       = 3        # keep a watched file available this long before deletion
    RECENT_AIR_DAYS   = 30       # never delete an episode that aired within this many days
    PREFETCH_HOURS    = 3.0      # target runtime budget of upcoming episodes to pre-acquire per series
    # ── per-viewer retention (lifecycle.viewer_retention) ─────────────────────
    # Its knobs are REAL CONFIG KEYS ("episode_retention"), unlike the three class
    # constants above, for two reasons. (1) They are household preferences, not
    # physics: GRACE_HOURS/RECENT_AIR_DAYS/PREFETCH_HOURS are "how long is a
    # sensible buffer" defaults nobody has ever needed to tune, while the backward
    # buffer and the horizon encode how a specific household watches TV — Robert
    # named 2 and 14 as *his* numbers and asked for them to be tunable. (2) They
    # gate DELETION, so they must be auditable from config.json and settable
    # headlessly (.env / unraid template) without editing source. The two parquet
    # columns below carry the per-run verdict so the three duplicated guard layers
    # (grace marking, whole-file protected set, delete-time defence-in-depth) all
    # read the SAME decision instead of each re-deriving it from history.
    _RETENTION_COLS = ("retention_hold", "retention_hold_by")

    # Durable per-(account, series) resume state. The Tautulli history it is derived
    # from is VOLATILE (tautulli/history/all is overwritten hourly and Tautulli
    # prunes its own DB), so the positions are merged forward here — see
    # _apply_viewer_retention.
    _VIEWER_POSITIONS_KEY = "sonarr/{inst}/viewer_positions"
    PILOT_MIN_WATCHABILITY = 15.0  # interactive-search a stub pilot only when its (affinity-driven)
                                   # watchability_score is >= this OR not yet graded — so the daemon
                                   # samples the shows the household is plausibly interested in, not
                                   # every empty series. DELIBERATELY BELOW the net-new/cold 20-tier
                                   # (operator ruling 2026-08-07): pilots are EXPLORATION — "by nature
                                   # we don't know if we'll like a pilot or not" — so new media gets
                                   # slightly easier access through the pilot door than through a
                                   # committed add. 0 disables the gate (search every stub). The
                                   # row + score are never gated, so a held-back stub keeps being
                                   # re-graded and returns once its affinity climbs back over the floor.
    # Cache key the Plex playlist builder writes the fetched mdblist universe lists under; the
    # per-group prefetch walk reads the SAME source so acquisition + playlists agree on grouping.
    _UNIVERSE_SRC_KEY = "plex/playlists/universe_source"
    MIN_FREE_SPACE_GB = 50.0     # last-resort ACQUIRE/UPGRADE floor (free_space_limit unset AND
                                   # total drive unreadable); normally space_targets uses 25%-of-total.
                                   #
                                   # DELIBERATELY NOT ZEROED when the delete-side fallbacks were.
                                   # PRESSURE_FALLBACK_GB / PRESSURE_THRESHOLD_GB went to 0.0 because
                                   # a floor of 0 makes `free < T` never true -> NOTHING IS DELETED,
                                   # which is the safe direction for a reclaim gate with no basis for
                                   # its threshold. This constant has the OPPOSITE POLARITY: it gates
                                   # "may we CONSUME space?" (prefetch / pilot search / JIT upgrade),
                                   # so a floor of 0 would mean `free < 0` never true -> ACQUIRE
                                   # WITHOUT LIMIT, and the run would happily fill a disk it cannot
                                   # measure. Same missing information, opposite safe answer.
                                   #
                                   # All three call sites also raise alert_unconfigured_floor, so the
                                   # operator is told the floor is unconfigured rather than it passing
                                   # silently.
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
        "row_origin",          # None (watch/pilot/stub machinery) | "cold_scan" (GLD-ACQ-24
                               # cold-TV reclaim inventory — unwatched, cold-score, ingested
                               # deliberately; NEVER confuse with a watch-derived row)
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

    # ══════════════════════════════════════════════════════════════════════════════
    # SECTION INDEX — §1..§20, in FILE ORDER
    # ══════════════════════════════════════════════════════════════════════════════
    # Numbered by POSITION, not by subsystem, so a banner can never disagree with
    # where the code actually is. Several subsystems are split across two ranges
    # (the file accreted); the "→" note on each names the submanager it would become,
    # so sections sharing a target are the ones that merge on extraction.
    #
    #   §1  Construction, instance plumbing, parquet I/O ...... → stays (owns the frame)
    #   §2  Score refresh, shields, universe/saga credits ..... → SonarrEpisodeScoringManager
    #   §3  Size-anomaly report + remediation ................. → SonarrEpisodeSizeAnomalyManager
    #   §4  Codec routing reports + legacy re-grab ............ → SonarrEpisodeCodecManager
    #   §5  Enrichment + show score map ...................... → SonarrEpisodeScoringManager (with §2)
    #   §6  Delete candidates, deletion, restore ............. → SonarrEpisodeRetentionManager
    #   §7  Free space + TTL config .......................... → stays (shared helpers)
    #   §8  Sonarr episode/file fetch + cache ................ → stays (owns the frame)
    #   §9  Next-episode computation ......................... → SonarrEpisodeNextUpManager
    #   §10 Grace period, purge, inventory ingestion ......... → SonarrEpisodeRetentionManager (with §6)
    #   §11 Acquisition + recycle-to-fund .................... → SonarrEpisodeAcquireManager
    #   §12 Delete execution + public delete entrypoints ..... → SonarrEpisodeRetentionManager (with §6)
    #   §13 Watch history ingestion + household resolution ... → SonarrEpisodeHistoryManager
    #   §14 Viewer retention ................................. → SonarrEpisodeRetentionManager (with §6)
    #   §15 Pilot search, batch, workers, offload ............ → SonarrEpisodePilotManager
    #   §16 Sizing + episode-id helpers ...................... → stays (shared helpers)
    #   §17 JIT quality upgrades + workers ................... → SonarrEpisodeJitManager
    #   §18 JIT quality restore .............................. → SonarrEpisodeJitManager (with §17)
    #   §19 Tautulli sync .................................... → SonarrEpisodeHistoryManager (with §13)
    #   §20 Run summary ...................................... → stays
    #
    # EXTRACTION ORDER, smallest-risk first (each is a MOVE, tests green either side):
    #   §4 legacy re-grab (already has legacy_regrab.py) → §3 size anomaly → §15 pilot
    #   (already has pilot_720_upgrade.py / pilot_interactive.py) → §17+§18 JIT (already
    #   has jit_search.py + space/jit_planner.py) → §13+§19 history → §6+§10+§12+§14
    #   retention LAST: it is the delete path and the most cross-cutting.
    #
    # WHAT MUST NOT BE SPLIT: every section below reads and writes the SAME parquet
    # (marked_for_deletion, is_watched, upgraded_for_watching, pre_upgrade_quality,
    # quality_action, plan_reason). That is a shared mutable SCHEMA, not incidental
    # coupling. Radarr's answer is the one to copy — ONE manager owns the frame and
    # the others borrow it via a getter (RadarrSpacePressureManager calls
    # _get_movie_files_manager()). Peer managers each loading their own copy would
    # either duplicate the read or lose the ordering that matters (grace → delete,
    # JIT upgrade → JIT restore), and that ordering is currently visible only because
    # these live in one file.
    # ══════════════════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════════════════
    # §1  FOUNDATION — construction, instance plumbing, parquet I/O
    #     Owns the DataFrame every other section reads and writes. STAYS on extraction.
    # ══════════════════════════════════════════════════════════════════════════════

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
    def _safe_concat(df: pd.DataFrame, df_new: pd.DataFrame) -> pd.DataFrame:
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

    # ── §1.2  Protected / pilot file-id sets ──────────────────────────────────────
    # Guard sets consulted before anything deletes or re-grabs. Built once per pass.

    @staticmethod
    @timeit("_build_pilot_file_ids")
    def _build_pilot_file_ids(df: pd.DataFrame) -> frozenset:
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
        df: pd.DataFrame,
        now: datetime,
        pilot_file_ids: frozenset | None = None,
    ) -> frozenset:
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
        * **household** — file ids on rows where ``all_household_watched`` is False
          AND an active watcher is still approaching the episode (row
          ``retention_hold``; GLD-ACQ-18 — active watchers only).

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

    def _build_protected_file_reasons(
        self,
        df: pd.DataFrame,
        now: datetime,
        pilot_file_ids: frozenset | None = None,
    ) -> dict[str, frozenset]:
        """GLD-ACQ-22 — per-guard breakdown of :meth:`_build_protected_file_ids`:
        ``{guard: fids}`` from the SAME mask source, so attribution can never disagree
        with the guard. The union of the values equals the flat protected set."""
        from scripts.managers.machine_learning.classification.guards import (
            build_protected_file_reasons,
        )
        if pilot_file_ids is None:
            pilot_file_ids = self._build_pilot_file_ids(df)
        return build_protected_file_reasons(
            df, now, pilot_file_ids, recent_air_days=self.RECENT_AIR_DAYS
        )

    # ── Formatting helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _fmt_bytes(n: float | None) -> str:
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

    # Columns that must always be OBJECT dtype in memory — the mirror of
    # _NUMERIC_COLUMNS, and for the opposite failure. Parquet round-trips an ALL-NULL
    # column as float64, and strict-dtype pandas then REJECTS assigning a string into
    # it: "Invalid value '2026-08-06T02:04:43+00:00' for dtype 'float64'". That is not a
    # warning — it raises, and it escapes whatever pass was stamping, which is how a
    # single ISO timestamp took out run_pilot_search, run_episode_file_enrichment and
    # run_full_series_enrichment together (all three reporting the identical timestamp).
    #
    # It had been fixed FOUR times at individual write sites (the JIT ledger's
    # planned_action / plan_reason / pre_upgrade_quality, and the deletion-plan pair)
    # and missed at three more (pilot_last_searched_at, pilot_last_planned_at,
    # date_added / last_synced_at / household_last_watched_at). Casting on LOAD, beside
    # the numeric cast, means a writer no longer has to remember: the column is object
    # before any pass touches it.
    _STRING_COLUMNS = (
        # ISO timestamps
        "date_added", "last_synced_at", "household_last_watched_at",
        "row_origin",
        "last_watched_at", "air_date_utc",
        "pilot_last_searched_at", "pilot_last_planned_at",
        # plan / ledger strings
        "planned_action", "plan_reason", "pre_upgrade_quality",
    )

    # ── §1.3  Parquet load / save ─────────────────────────────────────────────────

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

        String columns get the OPPOSITE cast for the opposite reason: an all-null
        column returns as float64, and assigning a string into it RAISES. See
        :data:`_STRING_COLUMNS`.
        """
        path = self._parquet_path(instance)
        if path.exists():
            try:
                df = pd.read_parquet(path)
                for col in self._NUMERIC_COLUMNS:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                for col in self._STRING_COLUMNS:
                    if col in df.columns and df[col].dtype != object:
                        df[col] = df[col].astype(object)
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
            # debug: save() fires 5-7× per run from different pipeline stages —
            # identical bookkeeping lines that told the operator nothing new.
            self.logger.log_debug(
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

    # ══════════════════════════════════════════════════════════════════════════════
    # §2  SCORING — score refresh, watchlist shield, universe & saga credits
    #     → SonarrEpisodeScoringManager (merges with §5)
    # ══════════════════════════════════════════════════════════════════════════════

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
        # Franchise/universe credit: a hot saga lends borrowed effective-watch-count to its members,
        # so the likelihood-gated upgrade AND downgrade passes elevate a single-watch sibling (and let
        # it fall again as the saga's last watch recedes). Non-destructive annotation; 0 when no heat.
        try:
            self._apply_universe_credit(instance, df)
        except Exception as e:
            self.logger.log_debug(f"[Universe] credit pass skipped for '{instance}': {e}")
        # Group-A5 DELETE SHIELD, broadcast per-series onto every episode row exactly like
        # the score above — so ``guards.build_protected_file_ids`` (which is pure pandas
        # over this frame) and the delete-time defence-in-depth both read the same verdict.
        # Non-destructive; all-False when the term is disabled or nothing is watchlisted.
        _tvdb_by_series = self._tvdb_by_series(instance)
        try:
            self._apply_watchlist_shield(df, _sid_num, _tvdb_by_series)
        except Exception as e:
            self.logger.log_debug(f"[Intent] watchlist shield skipped for '{instance}': {e}")
        self.save(instance, df)
        vals = list(score_by_series.values())
        self.logger.log_info(
            f"[ShowScore] Scored {len(score_by_series)} series for '{instance}' "
            f"(range: {min(vals)}-{max(vals)})"
        )
        # ── ML snapshot append (Stage 1 — pure logging; ml.snapshots.enabled,
        #    DEFAULT ON). One row per SERIES from the score/breakdown just saved.
        #    Fully wrapped: a snapshot failure can never affect the run.
        try:
            from scripts.managers.machine_learning.labels.snapshots import (
                maybe_snapshot_shows,
            )
            maybe_snapshot_shows(self.config, self.global_cache, self.logger,
                                 instance, df, tvdb_by_series=_tvdb_by_series)
        except Exception as e:
            self.logger.log_debug(f"[MLSnapshot] sonarr/{instance} snapshot hook failed: {e}")
        try:
            _rows = self.report_size_anomalies(instance, df)
            self.remediate_size_anomalies(instance, _rows)
        except Exception as e:
            self.logger.log_debug(f"[SizeAnomaly] report/remediate failed for '{instance}': {e}")
        try:
            self.report_codec_routing(instance, df)   # read-only codec preview; changes nothing
        except Exception as e:
            self.logger.log_debug(f"[CodecRoute] report failed for '{instance}': {e}")
        try:
            self.report_pilots_off_720(instance, df)  # read-only 720-floor audit; changes nothing
        except Exception as e:
            self.logger.log_debug(f"[Pilot720] report failed for '{instance}': {e}")
        return len(score_by_series)

    def _tvdb_by_series(self, instance: str) -> dict:
        """``{series_id: tvdb_id}`` from the cached Sonarr series list.

        Hoisted out of the ML-snapshot hook so the Group-A5 watchlist shield (which keys on
        TVDb id, the id every intent feed carries) and the snapshot share ONE map built once
        per refresh. Empty dict on any failure — both callers degrade to "no tvdb known"."""
        try:
            _series_cache = getattr(self.sonarr_cache, "series", None)
            if not _series_cache:
                return {}
            return {int(s["id"]): s.get("tvdbId")
                    for s in _series_cache.iter_all_series(instance)
                    if s.get("id") is not None}
        except Exception:
            return {}

    def _apply_watchlist_shield(self, df, sid_num, tvdb_by_series) -> int:
        """Broadcast ``watchlist_hold`` / ``watchlist_hold_by`` per SERIES onto every episode row.

        The TV twin of ``radarr/quality/space_pressure._apply_watchlist_shield`` — see that
        docstring for why the shield exists and why it expires on the watchlister's own
        dormancy rather than on the age of the listing. Broadcast per-series (like the score
        and the universe credit) because the intent feeds speak SERIES, while every delete
        guard on this side speaks EPISODE FILE: ``guards.build_protected_file_ids`` collapses
        the column to file ids so a multi-episode file backing a held series survives whole.

        Returns the number of held series. All-False when the shield is disabled, nothing is
        watchlisted, or no watchlister resolves to an active account."""
        from scripts.managers.machine_learning.scoring._shared import intent_hold_active
        from scripts.managers.services._intent_index import gather_intent_index

        df["watchlist_hold"] = False
        df["watchlist_hold_by"] = None
        wl = ((self.config or {}).get("scoring", {}) or {}).get("watchlist_intent", {}) or {}
        shield = (wl.get("shield") or {}) if isinstance(wl, dict) else {}
        if not (bool(wl.get("enabled", True)) and bool(shield.get("enabled", True))):
            return 0
        try:
            dormancy = float(shield.get("dormancy_window_days", 90))
        except (TypeError, ValueError):
            dormancy = 90.0
        index = (gather_intent_index(self.global_cache, self.config,
                                     logger=getattr(self, "logger", None)) or {}).get("shows") or {}
        if not index or not tvdb_by_series:
            return 0
        now = datetime.now(tz=timezone.utc)
        held_by: dict = {}
        for sid, tvdb in tvdb_by_series.items():
            if tvdb is None:
                continue
            try:
                entry = index.get(int(tvdb))
            except (TypeError, ValueError):
                continue
            if not entry or not intent_hold_active(entry, now, dormancy_days=dormancy):
                continue
            held_by[int(sid)] = ", ".join(entry.get("members") or ()) or "watchlist"
        if not held_by:
            return 0
        _mask = sid_num.map(lambda s: (int(s) in held_by) if pd.notna(s) else False).astype(bool)
        df.loc[_mask, "watchlist_hold"] = True
        df["watchlist_hold_by"] = sid_num.map(
            lambda s: held_by.get(int(s)) if pd.notna(s) else None).astype(object)
        self.logger.log_info(
            f"[Intent] watchlist shield: {len(held_by)} series held from deletion "
            f"({int(_mask.sum())} episode row(s); released after {dormancy:.0f}d of "
            f"watchlister dormancy).")
        return len(held_by)

    def _apply_universe_credit(self, instance: str, df) -> None:
        """Broadcast a per-series ``universe_credit`` column onto every episode row: borrowed
        effective-watch-count from a HOT TV franchise/universe (rewatched siblings), recency-decayed.
        Read by ``watch_likelihood`` so the upgrade + downgrade passes elevate a single-watch member of
        a hot saga. Sonarr-only; 0 everywhere when there's no franchise heat (byte-identical effect)."""
        series_cache = getattr(self.sonarr_cache, "series", None)
        if series_cache is None or "series_id" not in df.columns:
            df["universe_credit"] = 0.0
            df["saga_credit"] = 0.0
            return
        rows = [s for s in series_cache.iter_all_series(instance) if isinstance(s, dict)]
        source = self.global_cache.get(self._UNIVERSE_SRC_KEY) if self.global_cache else None
        fran, _timeline = tv_group_maps_from_series(rows, source or {})
        _sid = pd.to_numeric(df["series_id"], errors="coerce")
        stats: dict = {}
        if "watch_count" in df.columns:
            _wc = df.assign(_s=_sid).dropna(subset=["_s"]).groupby("_s")["watch_count"].max()
            for sid, v in _wc.items():
                stats.setdefault(int(sid), {})["watch_count"] = float(v) if pd.notna(v) else 0.0
        if "last_watched_at" in df.columns:
            now = pd.Timestamp.now(tz="UTC")
            _lw = pd.to_datetime(df["last_watched_at"], utc=True, errors="coerce")
            _last = pd.DataFrame({"_s": _sid, "_lw": _lw}).dropna(subset=["_s"]).groupby("_s")["_lw"].max()
            for sid, ts in _last.items():
                stats.setdefault(int(sid), {})["days_since"] = (now - ts).days if pd.notna(ts) else 1e9
        credits = series_universe_credits(fran or {}, stats, config=self.config)
        # Saga CAUGHT-UP / DEPTH credit (household, cross-media, release-grace-decayed) — combined via
        # max with the rewatched-fraction credit above. Default-off: {} (no change) when
        # scoring.saga_credit.enabled is unset, so this is byte-identical inert until opted in.
        saga_cr = self._saga_quality_credits(df, rows, _sid, instance)
        # SPLIT, not blended. ``universe_credit`` carries ONLY the rewatched-sibling signal -
        # "the household rewatches this family" - which is a legitimate reason to resist
        # DELETION. ``saga_credit`` carries the caught-up/depth signal, whose own contract is
        # forward-looking ("a caught-up household gets a grace window to watch a NEW entry in
        # Remux"): it justifies ACQUIRING and UPGRADING the next entry, not keeping the old
        # ones. Being caught up is precisely when S01E02 becomes dead weight, so folding it
        # into the delete guard inverted its meaning - and it got WORSE as watch data
        # improved, since better history raises caught_up_frac across the library.
        # Quality consumers take max(universe_credit, saga_credit); the delete guards read
        # universe_credit alone.
        df["universe_credit"] = _sid.map(
            lambda s: credits.get(int(s), 0.0) if pd.notna(s) else 0.0)
        df["saga_credit"] = _sid.map(
            lambda s: saga_cr.get(int(s), 0.0) if pd.notna(s) else 0.0)
        if credits or saga_cr:
            _allc = dict(credits)
            for _k, _v in saga_cr.items():
                _allc[_k] = max(_allc.get(_k, 0.0), _v)
            _saga_note = f"; {len(saga_cr)} via saga caught-up/depth" if saga_cr else ""
            self.logger.log_info(
                f"[Universe] '{instance}': lent franchise credit to {len(_allc)} series "
                f"(max {max(_allc.values()):.2f} watch-counts{_saga_note}).")

    def _saga_quality_credits(self, df, rows, _sid, instance) -> dict:
        """``{series_id: saga caught-up/depth credit}`` for the TV QUALITY pre-pass — household,
        cross-media, release-grace-decayed (:func:`watch_likelihood.saga_credit`). Empty when
        ``scoring.saga_credit.enabled`` is off, the universe source is absent, or the series tvdb /
        ``date_added`` can't be resolved, so the caller's ``max`` is a no-op. ``rows`` are the series
        cache dicts (for series_id→tvdb + title); ``_sid`` is the numeric series_id column. Availability
        is the series' FRESHEST episode ``date_added`` (latest content arrival → the grace-window
        anchor). Also surfaces the per-title detail to the run log + a global_cache snapshot (the GUI
        feed) via :func:`emit_saga_credit_preview`. Best-effort: logs + returns ``{}`` on any failure."""
        out: dict = {}
        try:
            from scripts.managers.machine_learning.likelihood.saga_engagement import (
                emit_saga_credit_preview,
                gather_saga_engagement,
                household_member_count,
            )
            from scripts.managers.machine_learning.likelihood.watch_likelihood import (
                saga_credit,
            )
            from scripts.managers.services.plex.playlists.universe_order import (
                saga_display_name,
            )
            eng = gather_saga_engagement(self.global_cache, self.config)
            if not eng or "date_added" not in df.columns:
                return out
            sid_to_tvdb: dict = {}
            sid_to_title: dict = {}
            for s in rows or []:
                sid_i, tv = s.get("id"), s.get("tvdbId")
                try:
                    if sid_i is not None and tv is not None:
                        sid_to_tvdb[int(sid_i)] = int(tv)
                        sid_to_title[int(sid_i)] = s.get("title")
                except (TypeError, ValueError):
                    continue
            if not sid_to_tvdb:
                return out
            members = household_member_count(self.config, self.global_cache) or None
            now = pd.Timestamp.now(tz="UTC")
            _da = pd.to_datetime(df["date_added"], utc=True, errors="coerce")
            _added = pd.DataFrame({"_s": _sid, "_da": _da}).dropna(subset=["_s"]).groupby("_s")["_da"].max()
            items: list = []
            for sid, ts in _added.items():
                sid = int(sid)
                tvdb = sid_to_tvdb.get(sid)
                if tvdb is None or pd.isna(ts):
                    continue
                e = eng.get(("show", tvdb))
                if not e:
                    continue
                days = max(0.0, float((now - ts).days))
                cr = saga_credit(caught_up_frac=e["caught_up_frac"],
                                 saga_watched_frac=e["saga_watched_frac"],
                                 days_since_available=days, household_members=members,
                                 config=self.config)
                if cr > 0:
                    out[sid] = cr
                    items.append({"id": sid, "title": sid_to_title.get(sid) or f"series {sid}",
                                  "saga": saga_display_name(e.get("saga", "")),
                                  "caught_up": e["caught_up_frac"], "depth": e["saga_watched_frac"],
                                  "days": days, "credit": cr})
            emit_saga_credit_preview(self.global_cache, self.logger, self.config, "sonarr", instance, items)
        except Exception as e:
            self.logger.log_debug(f"[Universe] saga quality credit skipped: {e}")
        return out

    # ══════════════════════════════════════════════════════════════════════════════
    # §3  SIZE ANOMALY — report + remediation (rescan vs search routing)
    #     Policy lives in machine_learning/sizing/anomaly.py; this is the service half.
    #     → SonarrEpisodeSizeAnomalyManager
    # ══════════════════════════════════════════════════════════════════════════════

    @timeit("report_size_anomalies")
    def report_size_anomalies(self, instance: str, df=None) -> list:
        """Flag episode files WILDLY out of size profile for their graded quality (the Sonarr
        twin of the Radarr check — e.g. a 45-minute episode graded 1080p at 30 GiB). Read-only:
        logs a count and records a detail table in the end-of-run summary. Returns the anomaly
        rows. Off via size_anomaly.enabled=false."""
        from scripts.managers.machine_learning.sizing import anomaly as size_anomaly

        cfg = size_anomaly.config_for(self.config)
        if not cfg.get("enabled", True):
            return []
        instance = self._resolve_instance(instance)
        if df is None:
            df = self.load(instance)
        if df is None or getattr(df, "empty", True):
            return []

        rows = size_anomaly.find_size_anomalies(
            df, id_cols=("series_title", "season_number", "episode_number",
                         "series_id", "episode_file_id"),
            size_col="size_bytes", runtime_col="runtime_seconds", runtime_unit="seconds",
            quality_col="quality_name", resolution_col="resolution",
            over_ratio=cfg["over_ratio"], under_ratio=cfg["under_ratio"],
            min_samples=cfg["min_samples"],
        )
        if not rows:
            return []
        over = [r for r in rows if r["verdict"] == "oversized"]
        reclaim = sum(r["reclaim_gb"] for r in over)
        self.logger.log_info(
            f"[SizeAnomaly] '{instance}': {len(rows)} episode file(s) wildly out of size "
            f"profile — {len(over)} oversized (~{reclaim:.0f} GB reclaimable), "
            f"{len(rows) - len(over)} undersized."
        )
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            table = [[self._fmt_episode_label(r), r["quality_name"], r["looks_like"],
                      f"{r['size_gb']:.1f} GB", f"{r['expected_gb']:.1f} GB", f"x{r['ratio']:.1f}",
                      f"{r['reclaim_gb']:.1f} GB", r["verdict"]] for r in rows[:cfg["report_limit"]]]
            _rs.add_rows(
                "sonarr", "Size anomalies", instance,
                ["Episode", "Graded", "Looks like", "Size", "Expected", "Ratio", "Reclaim", "Verdict"],
                table, order=35,
            )
        return rows

    @staticmethod
    def _fmt_episode_label(r: dict) -> str:
        """'Series SxxExx' for the size-anomaly table; degrades to the title when s/e absent."""
        title = str(r.get("series_title") or "?")[:24]
        try:
            sn, en = int(r.get("season_number")), int(r.get("episode_number"))
            return f"{title} S{sn:02d}E{en:02d}"
        except (TypeError, ValueError):
            return title

    @timeit("remediate_size_anomalies")
    def remediate_size_anomalies(self, instance: str, rows: list | None) -> dict:
        """ACT on the size anomalies (opt-in: ``size_anomaly.remediate=true``) — the TV twin of
        Radarr's remediation.

          * MIS-GRADED (junk/SD grade, really HD) and UNDERSIZED (suspiciously small) → ``RefreshSeries``
            re-reads the series' mediainfo to fix a wrong grade / re-verify the file. Non-destructive.
          * BLOATED (oversized at a real HD/UHD grade) → re-grab at the profile target: DELETE the
            oversized episode file + ``EpisodeSearch`` so Sonarr re-acquires a properly-sized release.
            DESTRUCTIVE — only for MONITORED episodes (else the delete orphans the episode), only when
            the file isn't whole-file-guarded (pilot / keep / recent-air / household), and only when
            the backup gate is armed (``effective_dry_run`` False); otherwise logged as 'would …'.
            Honours the run's dry_run AND the degrade-to-dry-run backup gate."""
        from scripts.managers.machine_learning.sizing import anomaly as size_anomaly

        cfg = size_anomaly.config_for(self.config)
        if not cfg.get("remediate", False) or not rows:
            return {}
        if self.sonarr_api is None:
            return {}
        instance = self._resolve_instance(instance)
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        stats = {"rescanned": 0, "regrabbed": 0, "skipped_unmonitored": 0,
                 "skipped_guard": 0, "failed": 0}

        # ── rescan mis-graded + undersized (non-destructive: RefreshSeries) ──────
        rescan_sids = sorted({int(r["series_id"]) for r in rows
                              if r.get("action") == "rescan" and r.get("series_id") is not None})
        if rescan_sids:
            if eff_dry:
                self.logger.log_info(f"[SizeAnomaly] [dry_run] would RefreshSeries (rescan) "
                                     f"{len(rescan_sids)} series on '{instance}'.")
            else:
                for sid in rescan_sids:
                    try:
                        self.sonarr_api._make_request(instance, "command", method="POST",
                                                      payload={"name": "RefreshSeries", "seriesId": sid})
                        stats["rescanned"] += 1
                    except Exception as e:
                        stats["failed"] += 1
                        self.logger.log_warning(f"[SizeAnomaly] RefreshSeries failed for series "
                                                f"{sid} on '{instance}': {e}")
                if stats["rescanned"]:
                    self.logger.log_info(f"[SizeAnomaly] rescanned {stats['rescanned']} series on "
                                         f"'{instance}' to fix mis-graded / verify undersized files.")

        # ── re-grab bloated (destructive: delete + search) ───────────────────────
        regrab = [r for r in rows if r.get("action") == "regrab"
                  and r.get("series_id") is not None and r.get("episode_file_id") is not None]
        if not regrab:
            return stats

        # Whole-file guard set (pilot / keep / recent-air / household): NEVER re-grab a protected
        # episode file — a failed search would leave it gone. Fail safe: if the data or guard set
        # can't be built, skip every re-grab this cycle (mirrors delete_selected_episode_files).
        df = self.load(instance)
        if df is None or getattr(df, "empty", True):
            stats["skipped_guard"] += len(regrab)
            self.logger.log_warning(f"[SizeAnomaly] no episode-file data to verify guards on "
                                    f"'{instance}'; skipping {len(regrab)} re-grab(s) (fail-safe).")
            return stats
        now = datetime.now(tz=timezone.utc)
        try:
            protected = self._build_protected_file_ids(df, now)
        except Exception as e:
            stats["skipped_guard"] += len(regrab)
            self.logger.log_error(
                f"[SizeAnomaly] protected-file-id build failed for '{instance}'; skipping ALL "
                f"{len(regrab)} re-grab(s) this cycle (fail-safe): {e}"
            )
            return stats

        # Map every physical file id → ALL the episode coords it backs. A multi-episode file (one
        # episodeFileId backing e.g. S01E02-E07) may be deleted ONLY when EVERY episode it backs is
        # monitored + resolvable, else the delete would orphan an unmonitored sibling with nothing to
        # re-grab it. Built from the FULL df (monitored-agnostic) so a sibling outside the anomaly set
        # still protects the file.
        fid_coords: dict[int, set] = {}
        _cols_raw = getattr(df, "columns", None)
        _cols = set(_cols_raw) if _cols_raw is not None else set()
        if {"episode_file_id", "season_number", "episode_number"}.issubset(_cols):
            _ff = pd.to_numeric(df["episode_file_id"], errors="coerce")
            _sn = pd.to_numeric(df["season_number"], errors="coerce")
            _en = pd.to_numeric(df["episode_number"], errors="coerce")
            for pos in range(len(df)):
                f, s, e = _ff.iat[pos], _sn.iat[pos], _en.iat[pos]
                if pd.notna(f) and pd.notna(s) and pd.notna(e):
                    fid_coords.setdefault(int(f), set()).add((int(s), int(e)))

        # ONE decision per UNIQUE file id (a multi-episode omnibus yields several anomaly rows sharing
        # one fid — dedup so we DELETE + search it once), grouped by series for the episode lookup.
        by_series: dict[int, dict] = {}
        for r in regrab:
            by_series.setdefault(int(r["series_id"]), {}).setdefault(int(r["episode_file_id"]), r)

        # Attempt ledger: a bloat re-grab is a SEARCH, and Sonarr grabs only on UPGRADE, so a
        # smaller replacement is never one. A file that is mis-SIZED rather than mis-GRADED is
        # therefore searched every run with no effect and, until now, no detector (measured: the
        # same 38 episodes searched on two consecutive runs, 496 anomalies unchanged). Bound the
        # loop. Read is best-effort; a cache that cannot answer must not block remediation, but it
        # is logged rather than swallowed silently so a permanently-unreadable ledger is visible.
        _now_ts = now.timestamp()
        _max_attempts = cfg.get("max_regrab_attempts", 3)
        _retry_days = cfg.get("regrab_retry_days", 7)
        _akey = size_anomaly.attempts_key(instance)
        try:
            _ledger = (self.global_cache.get(_akey) if self.global_cache else None) or {}
            if not isinstance(_ledger, dict):
                _ledger = {}
        except Exception as e:
            _ledger = {}
            self.logger.log_warning(f"[SizeAnomaly] could not read the re-grab attempt ledger for "
                                    f"'{instance}' ({e}); this cycle is unbounded.")
        _ledger_dirty = False

        for sid, fid_rows in by_series.items():
            eps = self.sonarr_api._make_request(instance, f"episode?seriesId={sid}", fallback=[]) or []
            ep_by_coord = {(e.get("seasonNumber"), e.get("episodeNumber")): e
                           for e in eps if isinstance(e, dict)}
            for fid, r in fid_rows.items():
                label = self._fmt_episode_label(r)
                if fid in protected:
                    stats["skipped_guard"] += 1
                    self.logger.log_info(f"[SizeAnomaly] skip re-grab '{label}' — whole-file guarded.")
                    continue
                # Retry budget. Checked BEFORE the monitored/resolvable work so an abandoned file
                # costs nothing, and before the dry-run branch so a preview reports what an armed
                # run would really do.
                _ok, _why_attempt = size_anomaly.should_attempt(
                    _ledger.get(str(fid)), r.get("size_bytes"), _now_ts,
                    max_attempts=_max_attempts, retry_days=_retry_days)
                if not _ok:
                    stats[_why_attempt] = stats.get(_why_attempt, 0) + 1
                    self.logger.log_debug(
                        f"[SizeAnomaly] skip re-grab '{label}' — {_why_attempt} "
                        f"({_ledger.get(str(fid), {}).get('attempts', 0)} prior attempt(s), "
                        f"size unchanged).")
                    continue
                # EVERY episode this file backs (not just the anomaly row's) must be monitored AND
                # resolvable, or deleting the file would orphan a sibling. Fall back to the row's own
                # coord when the df lacks the columns to enumerate siblings.
                coords = set(fid_coords.get(fid) or set())
                if not coords:
                    try:
                        coords = {(int(r.get("season_number")), int(r.get("episode_number")))}
                    except (TypeError, ValueError):
                        coords = set()
                backing = [ep_by_coord.get(c) for c in coords]
                eids = [b["id"] for b in backing if isinstance(b, dict) and b.get("id")]
                # GLD-SON-19 - UNMONITORED and UNRESOLVED are different problems and
                # used to share one counter and one message, so 38 skips could be 38
                # policy states, 38 broken mappings, or any mix, and the log could not
                # say which.
                #
                #   unresolved  the file backs a coord Sonarr does not know. Searching
                #               cannot help - there is nothing to search FOR. Report it.
                #   unmonitored the episode exists and is simply not being tracked.
                #               Under GLD-SON-18 this is now SEARCHABLE: the search is
                #               inert rather than destructive, so it is attempted.
                resolved = bool(coords) and all(isinstance(b, dict) and b.get("id")
                                                for b in backing)
                if not resolved:
                    stats["skipped_unresolved"] = stats.get("skipped_unresolved", 0) + 1
                    self.logger.log_info(
                        f"[SizeAnomaly] skip re-grab '{label}' - file backs an episode Sonarr "
                        f"cannot resolve; a search has nothing to look for (GLD-SON-19).")
                    continue
                monitored = all(b.get("monitored") for b in backing)
                if not monitored:
                    # GLD-SON-18 - NO LONGER A SKIP. The orphan risk was created by
                    # DELETE-then-search: Sonarr will not search for an unmonitored
                    # episode, so the command was accepted, did nothing, and left the
                    # file deleted with no replacement. Searching in place cannot
                    # orphan anything - at worst the search finds nothing and the
                    # household keeps exactly what it had.
                    #
                    # It is still recorded separately, because an unmonitored episode
                    # is the one case where the search is EXPECTED to be inert: it
                    # tells the operator this file needs `monitored: true` before the
                    # bloat can actually be corrected.
                    stats["searched_unmonitored"] = stats.get("searched_unmonitored", 0) + 1
                if eff_dry:
                    self.logger.log_info(
                        f"[SizeAnomaly] [dry_run] would re-grab '{label}' "
                        f"({r.get('size_gb')} GB {r.get('quality_name')} -> profile target, "
                        f"~{r.get('reclaim_gb')} GB reclaim): EpisodeSearch {len(eids)} ep(s), "
                        f"file {fid} left in place"
                        + (" [UNMONITORED - the search will be inert until it is monitored]"
                           if not monitored else "") + ".")
                    continue
                try:
                    # SEARCH ONLY - the file stays. Sonarr replaces it on import if and
                    # only if it finds a release matching the profile, so there is never
                    # a window where the episode has no file. The previous armed path
                    # was `DELETE episodefile/{fid}` THEN EpisodeSearch, which is what
                    # made the orphan guard necessary in the first place; removing the
                    # delete removes the hazard rather than guarding it.
                    #
                    # The bloated file is NOT reclaimed at this moment. It is reclaimed
                    # when the replacement imports and Sonarr retires the old one into
                    # the recycle bin - which is the same path every upgrade takes, and
                    # the one `bin_forecast` already accounts for.
                    self.sonarr_api._make_request(instance, "command", method="POST",
                                                  payload={"name": "EpisodeSearch",
                                                           "episodeIds": eids})
                    stats["regrabbed"] += 1
                    _ledger[str(fid)] = size_anomaly.record_attempt(
                        _ledger.get(str(fid)), r.get("size_bytes"), _now_ts)
                    _ledger_dirty = True
                    self.logger.log_info(
                        f"[SizeAnomaly] searching {len(eids)} ep(s) at profile target for "
                        f"'{label}' - bloated file kept until a replacement imports"
                        + (" [UNMONITORED - search is inert until monitored]"
                           if not monitored else "") + ".")
                except Exception as e:
                    stats["failed"] += 1
                    self.logger.log_warning(f"[SizeAnomaly] re-grab failed for '{label}': {e}")

        # Persist the ledger, pruned to files still anomalous THIS run — a file that dropped out
        # of the anomaly set was fixed (or removed), so its history should not survive to penalise
        # a future, unrelated size problem on the same id.
        if _ledger_dirty and self.global_cache is not None and not eff_dry:
            try:
                self.global_cache.set(_akey, size_anomaly.prune_attempts(
                    _ledger, {r.get("episode_file_id") for r in regrab}))
            except Exception as e:
                self.logger.log_warning(f"[SizeAnomaly] could not persist the re-grab attempt "
                                        f"ledger for '{instance}' ({e}); retries stay unbounded.")

        acted = stats["rescanned"] + stats["regrabbed"]
        if acted or stats["skipped_unmonitored"] or stats["skipped_guard"] \
                or stats.get("skipped_unresolved") or stats.get("abandoned") \
                or stats.get("cooling"):
            self.logger.log_info(
                f"[SizeAnomaly] '{instance}' remediation: {stats['rescanned']} rescanned, "
                f"{stats['regrabbed']} searched"
                + (f" ({stats['searched_unmonitored']} of them UNMONITORED - inert until "
                   f"monitored)" if stats.get("searched_unmonitored") else "")
                + f", {stats.get('skipped_unresolved', 0)} skipped (unresolvable), "
                f"{stats['skipped_guard']} skipped (guarded), "
                f"{stats.get('cooling', 0)} cooling, "
                f"{stats.get('abandoned', 0)} abandoned (searched "
                f"{_max_attempts}x, size never moved), {stats['failed']} failed."
            )
        return stats

    # ══════════════════════════════════════════════════════════════════════════════
    # §4  CODEC ROUTING & LEGACY RE-GRAB — reports + the legacy-codec replacement pass
    #     Pure core already extracted to sonarr/cache/legacy_regrab.py; these are the
    #     wrappers. SMALLEST extraction target — start here.
    #     → SonarrEpisodeCodecManager
    # ══════════════════════════════════════════════════════════════════════════════

    @timeit("report_codec_routing")
    def report_codec_routing(self, instance: str, df=None) -> list:
        """READ-ONLY codec-routing preview for SERIES — the TV twin of
        ``RadarrSpacePressureManager.report_codec_routing``. For each owned, WATCHED series at a
        resolution tier that has >= 2 codec-variant quality profiles, show the codec the
        transcode-minimising policy WOULD pick for its actual viewers (profile_selector.
        choose_codec_profile over the per-user device->transcode matrix) vs the files' current
        (dominant) codec. Changes NOTHING — logs a count and records a 'Codec routing preview'
        table in the end-of-run summary. Per-episode-file rows are reduced to one (series,
        resolution) row at the dominant codec, so a series with mixed-codec tiers surfaces each
        tier. Off via ``scoring.codec_profiles.report=false``."""
        if not (((self.config or {}).get("scoring") or {}).get("codec_profiles") or {}).get("report", True):
            return []
        instance = self._resolve_instance(instance)
        if df is None:
            df = self.load(instance)
        if df is None or getattr(df, "empty", True):
            return []
        history = (self.global_cache.get("tautulli/history/all") if self.global_cache else None) or []
        if not history:
            self.logger.log_info(
                f"[CodecRoute] '{instance}': no Tautulli watch history cached yet — nothing to evaluate.")
            return []
        try:
            profiles = self.sonarr_api._make_request(instance, "qualityProfile", fallback=[]) or []
        except Exception:
            profiles = []
        if not profiles:
            self.logger.log_info(f"[CodecRoute] '{instance}': no quality profiles available — skipped.")
            return []
        series_df = self._series_codec_frame(df)
        if series_df is None or getattr(series_df, "empty", True):
            return []
        from scripts.managers.machine_learning.quality_analytics.codec_report import (
            build_per_title_watchers,
            codec_report_rows,
            normalize_title,
            per_user_platform_usage_from_history,
        )
        from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
            per_user_source_fingerprint_matrix,
            per_user_transcode_fingerprint_matrix,
        )
        # Source-codec matrix (keyed by the FILE's codec via the metadata index, NOT Plex's streamed /
        # transcode-target codec) so the prediction is codec-aware; falls back to the streamed matrix
        # only when no metadata index is cached yet. Identical to the Radarr movie preview.
        metadata = (self.global_cache.get("tautulli/metadata/index") if self.global_cache else None) or {}
        matrix = (per_user_source_fingerprint_matrix(history, metadata) if metadata
                  else per_user_transcode_fingerprint_matrix(history))
        watchers = build_per_title_watchers(history)
        rows = codec_report_rows(
            series_df, profiles, matrix,
            per_user_platform_usage_from_history(history),
            watchers, title_col="series_title",
        )
        # TRANSPARENCY: always report what the pass evaluated. ``n_watched`` is owned series that appear
        # in the watch history; ``len(rows)`` is the subset at a multi-codec tier (the only place a codec
        # CAN be re-picked). Mirrors the Radarr movie preview's transparency line.
        n_watched = sum(1 for t in series_df["series_title"] if normalize_title(t) in watchers)
        n_changed = sum(1 for r in rows if r["change"])
        self.logger.log_info(
            f"[CodecRoute] '{instance}': evaluated {n_watched} watched series "
            f"({len(rows)} at a multi-codec tier); {n_changed} would change codec to reduce "
            f"transcoding (read-only preview; nothing applied)."
        )
        if rows:
            headers = ["Series", "Viewers", "Current", "Recommend", "CurCost", "RecCost", "Change"]
            table = [[str(r["title"])[:28], ",".join(r["watchers"])[:16],
                      r["current_codec"], r["recommended_codec"],
                      f"{r['current_cost']:.2f}", f"{r['recommended_cost']:.2f}",
                      "YES" if r["change"] else "-"] for r in rows[:25]]
            self.logger.log_grid(
                headers, table,
                title=f"Codec routing preview (TV) - '{instance}' (read-only; cost = P(transcode))", cap=24,
            )
            _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
            if _rs is not None:
                _rs.add_rows("sonarr", "Codec routing preview", instance, headers, table, order=37)
        return rows

    def report_pilots_off_720(self, instance: str, df=None) -> list:
        """READ-ONLY audit: TV pilots (S01E01) whose ON-DISK file is still BELOW 720p. The policy is
        every pilot at 720 until the series earns a watchability score; this surfaces the pilots that
        remain sub-720 and splits genuine upgrade candidates from HELD ones (full series that merely
        owns a 480p pilot / watched / scored / keep-tagged). Logs a count + a 'Pilots below 720' table
        in the end-of-run summary and changes NOTHING — ``scripts/support/tools/sonarr_upgrade_pilots_720``
        actuates. Off via ``pilot_interactive.report=false``."""
        from collections import Counter

        if not (((self.config or {}).get("pilot_interactive")) or {}).get("report", True):
            return []
        instance = self._resolve_instance(instance)
        if df is None:
            df = self.load(instance)
        if df is None or getattr(df, "empty", True):
            return []
        cols = set(getattr(df, "columns", []))
        if not {"is_pilot", "episode_file_id", "resolution", "series_id", "series_title"} <= cols:
            return []

        is_pilot = df["is_pilot"].fillna(False).astype(bool)
        has_file = df["episode_file_id"].notna()
        res = pd.to_numeric(df["resolution"], errors="coerce")
        sub = df[is_pilot & has_file & res.notna() & (res >= 0) & (res < 720)].copy()
        if sub.empty:
            self.logger.log_info(f"[Pilot720] '{instance}': no on-disk pilots below 720p.")
            return []
        sub["_res"] = res[sub.index].astype(int)

        # df view of episodeFileCount per series (a genuine stub owns just the pilot) + series-level
        # watched, so a full library or a series being sampled is never counted a naive upgrade target.
        owned_by_series = df[has_file].groupby("series_id").size().to_dict()
        watched_sids = (set(df.loc[df["is_watched"].fillna(False).astype(bool), "series_id"])
                        if "is_watched" in cols else set())

        rows, reasons = [], Counter()
        for _, r in sub.iterrows():
            sid = r.get("series_id")
            owned = int(owned_by_series.get(sid, 1))
            score = int(r.get("watchability_score") or 0) if "watchability_score" in cols else 0
            keep = str(r.get("keep_policy") or "") in ("keep_series", "keep_season")
            if owned > 1:
                status = "full-series"          # real library that merely owns a 480p pilot — never cap
            elif sid in watched_sids:
                status = "watched"              # being sampled → normal scoring lifts it
            elif score >= 75:
                status = "scored"               # earned upgrades already
            elif keep:
                status = "keep"
            else:
                status = "upgradable"
            reasons[status] += 1
            rows.append({"series_title": r.get("series_title"), "resolution": int(r["_res"]),
                         "owned_files": owned, "score": score, "status": status})

        held = {k: v for k, v in reasons.items() if k != "upgradable"}
        self.logger.log_info(
            f"[Pilot720] '{instance}': {len(rows)} on-disk pilot(s) below 720 — "
            f"{reasons.get('upgradable', 0)} upgradable to 720, {sum(held.values())} held "
            f"{held or {}}. Read-only audit; run sonarr_upgrade_pilots_720 to raise the upgradable ones."
        )
        _rank = {"upgradable": 0, "full-series": 1, "watched": 2, "scored": 3, "keep": 4}
        rows.sort(key=lambda x: (_rank.get(x["status"], 9), x["resolution"], str(x["series_title"])))
        headers = ["Series", "Res", "Files", "Score", "Status"]
        table = [[str(x["series_title"])[:32], f"{x['resolution']}p", str(x["owned_files"]),
                  str(x["score"]), x["status"]] for x in rows[:25]]
        self.logger.log_grid(headers, table, title=f"Pilots below 720 - '{instance}' (read-only)", cap=24)
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            _rs.add_rows("sonarr", "Pilots below 720", instance, headers, table, order=38)
        return rows

    @staticmethod
    def _series_codec_frame(df):
        """Reduce per-episode-file rows to one row per (series_title, resolution) at the DOMINANT
        (most common) ``video_codec`` — the movie codec preview is 1 file = 1 title, but a series has
        many files at possibly mixed codecs/tiers, so each (series, tier) is summarised to the codec
        that dominates it (a series mostly h264@720 + some XviD@480 surfaces BOTH tiers). Returns a
        DataFrame with series_title/resolution/video_codec; empty frame when those columns are absent.
        Pure DataFrame reshape."""
        need = ["series_title", "resolution", "video_codec"]
        if df is None or not set(need) <= set(getattr(df, "columns", [])):
            return pd.DataFrame(columns=need)
        sub = df[need].dropna(subset=["series_title", "resolution"])
        if sub.empty:
            return pd.DataFrame(columns=need)

        def _dominant(s):
            vc = s.dropna().value_counts()
            return vc.index[0] if len(vc) else None

        return (sub.groupby(["series_title", "resolution"], dropna=True)["video_codec"]
                .agg(_dominant).reset_index())

    @timeit("regrab_legacy_codecs")
    def regrab_legacy_codecs(self, instance: str) -> dict:
        """ACTUATION (gated, default-OFF): replace owned legacy-codec episode files (XviD / DivX /
        MPEG-2 / WMV) with a modern-codec release so modern Plex clients stop transcoding them — the
        curative twin of the AVC/x264 profile preference. Runs AFTER the transcode-decision reports
        (report_codec_routing). The WHOLE eligible backlog (cooldown-filtered + round-robined ACROSS
        series so coverage spreads instead of finishing one binged show first) is processed: a LARGE
        batch SPILLS to the standalone pilot-search daemon (mode 'legacy_regrab') so the run never
        blocks on the slow per-file interactive searches; a small batch (or a dry-run preview) runs
        inline, capped so it can't stall the run. Each grab is the specific modern release — Sonarr
        replaces the file on IMPORT, nothing is deleted, so a file with no modern release is left
        untouched. Honours dry_run (previews, grabs nothing). Off via
        ``scoring.codec_profiles.legacy_regrab=false`` — the default, so the pass is fully inert."""
        from datetime import datetime, timedelta, timezone

        cp = (((self.config or {}).get("scoring") or {}).get("codec_profiles") or {})
        if not cp.get("legacy_regrab", False):
            return {}
        instance = self._resolve_instance(instance)
        df = self.load(instance)
        if df is None or getattr(df, "empty", True):
            return {}
        from scripts.managers.machine_learning.quality_analytics.legacy_codec import (
            interleave_by_series,
            legacy_files_from_df,
        )
        from scripts.managers.services.sonarr.cache.legacy_regrab import (
            ledger_key,
            run_legacy_regrab,
        )
        legacy = legacy_files_from_df(df)
        if not legacy:
            return {}
        # Cooldown: skip files attempted within the window (esp. 'no_release'), so the daemon doesn't
        # re-search the same files every run. The ledger doubles as the resume checkpoint.
        cooldown = timedelta(days=max(1, int(cp.get("legacy_regrab_cooldown_days", 14) or 14)))
        now = datetime.now(tz=timezone.utc)
        ledger = dict((self.global_cache.get(ledger_key(instance)) if self.global_cache else None) or {})

        def _recent(fid) -> bool:
            ent = ledger.get(str(fid))
            if not ent:
                return False
            try:
                return (now - datetime.fromisoformat(ent.get("at"))) < cooldown
            except Exception:
                return False

        # GLD-ACQ-29 - COUNT the cooldown suppressions. `eligible` silently discarded
        # every file inside the re-try window, so the summary reported `881 legacy-codec
        # file(s), 67 eligible` and left 814 unexplained. The entry recorded it as
        # "822 -> 2+752 (68 unaccounted)": a residue that looks like a leak and is
        # actually the cooldown working exactly as designed. An uncounted suppression
        # is indistinguishable from a lost file *(P-D)*.
        _cooled = [r for r in legacy if _recent(r["episode_file_id"])]
        eligible = interleave_by_series([r for r in legacy if not _recent(r["episode_file_id"])])
        # `no_release` is the sub-population worth separating: those files were searched
        # and NOTHING modern exists for them, so they will re-suppress every run until
        # the indexers change. That is a permanent-ish state, not a transient backoff,
        # and it is the reason the same handful re-spill run after run.
        _no_release = sum(1 for r in _cooled
                          if (ledger.get(str(r["episode_file_id"])) or {}).get("status")
                          == "no_release")
        if _cooled:
            self.logger.log_info(
                f"[LegacyRegrab] '{instance}': {len(_cooled)} file(s) suppressed by the "
                f"{cooldown.days}-day re-try cooldown"
                + (f" ({_no_release} of them because no modern release was found last time "
                   f"- these will keep re-suppressing until the indexers change)"
                   if _no_release else "")
                + f"; {len(eligible)} of {len(legacy)} eligible this run.")
        if not eligible:
            self.logger.log_info(
                f"[LegacyRegrab] '{instance}': {len(legacy)} legacy-codec file(s), all within the "
                f"re-try cooldown — nothing to do this run.")
            return {"legacy": len(legacy), "checked": 0, "grabbed": 0}

        # LARGE backlog → spill the WHOLE set to the background daemon so the run never blocks on the
        # slow per-file interactive searches (mirrors the pilot/JIT offload). Returns early.
        if self._maybe_offload_legacy_regrab(instance, eligible):
            return {"legacy": len(legacy), "queued": len(eligible), "offloaded": True}

        # Dry-run: the inline path exists to PREVIEW, but every check is a slow
        # interactive release search (~2s of blocked wall each — ~21s/run at the
        # default cap of 10, all of it on the main pipeline; profiler showed
        # cpu≈0.2s of 21.4s wall). Default dry-run budget 0 skips the searches
        # and previews the QUEUE instead (what would be checked, from data we
        # already have); set scoring.codec_profiles.legacy_regrab_dry_run_budget
        # > 0 to sample real availability in dry runs. Live behavior unchanged.
        if self.dry_run:
            cap = int(cp.get("legacy_regrab_dry_run_budget", 0) or 0)
            if cap <= 0:
                _rows = []
                for r in eligible[:24]:
                    try:
                        _ep = (f"{r.get('series_title') or '?'} "
                               f"S{int(r.get('season_number') or 0):02d}"
                               f"E{int(r.get('episode_number') or 0):02d}")
                    except (TypeError, ValueError):
                        _ep = str(r.get("series_title") or "?")
                    _rows.append([_ep, f"{r.get('video_codec') or '?'}/"
                                       f"{int(r.get('resolution') or 0)}p"])
                if _rows:
                    self.logger.log_grid(
                        ["Episode", "Current"], _rows,
                        title=f"Legacy-codec re-grab queue - '{instance}' "
                              f"(dry-run; release checks deferred to live/daemon)",
                        cap=44)
                self.logger.log_info(
                    f"[LegacyRegrab] [dry_run] '{instance}': {len(legacy)} legacy-codec "
                    f"file(s), {len(eligible)} eligible after cooldown — release "
                    f"availability checks deferred (live run offloads to the daemon; set "
                    f"scoring.codec_profiles.legacy_regrab_dry_run_budget>0 to sample inline).")
                return {"legacy": len(legacy), "checked": 0, "grabbed": 0,
                        "previewed": 0, "no_release": 0, "failed": 0,
                        "deferred": len(eligible)}
        else:
            cap = max(1, int(cp.get("legacy_regrab_budget", 10) or 10))
        batch = eligible[:cap]
        # GLD-ACQ-30 - the space floor this lane never had. Read the SAME figures
        # the pressure passes and the other acquire lanes use, so all four agree
        # on what "below the floor" means rather than each deciding separately.
        # Legacy re-grab issues a DIRECT `POST release` by guid; on 2026-08-07 it
        # was one of the lanes still grabbing into a 98% array while the pressure
        # pass was trying to reclaim, which is how SAB reached
        # `complete_dir not writable`.
        _lr_free = self._get_free_space_gb(instance)
        _lr_total = self._get_total_space_gb(instance)
        _, _lr_floor = space_targets(
            self.config, fallback_gb=self.MIN_FREE_SPACE_GB, total_gb=_lr_total,
        )
        result = run_legacy_regrab(
            make_request=self.sonarr_api._make_request, logger=self.logger,
            global_cache=self.global_cache, instance=instance, items=batch,
            max_workers=1, dry_run=self.dry_run,
            free_gb=_lr_free, acquire_floor_gb=_lr_floor,
        )
        if result.get("skipped_space"):
            return {"legacy": len(legacy), "checked": 0, "grabbed": 0, "previewed": 0,
                    "no_release": 0, "empty_search": 0, "failed": 0,
                    "skipped_space": result["skipped_space"]}
        prefix = "[dry_run] " if self.dry_run else ""
        # empty_search is its OWN outcome, not a miss. `legacy_regrab` deliberately
        # refuses to record a zero-release search as `no_release` because that costs a
        # 14-day cooldown (GLD-SON-02: 814 of 881 files were once benched that way on
        # evidence never gathered). It was counted and named per title, but omitted
        # HERE -- so a run where every search came back empty printed
        # "0 grabbed, 0 no modern release, 0 failed (of 69 checked)" and read as though
        # nothing had happened, when in fact all 69 landed in a bucket the summary did
        # not show. Same shape as GLD-ACQS-20's "0 refused" while 122 were capped.
        _empty = int(result.get("empty_search") or 0)
        # GLD-SON-25 - `checked` counts LOOP ITERATIONS, not searches, so these three
        # outcomes have to be printed or the line lies by omission. On 2026-08-19/20 all
        # 69 eligible rows were stale pointers: every one returned before searching, the
        # summary printed "0 grabbed, 0 no modern release, 0 failed (of 69 checked)", and
        # the run took ONE SECOND - a single interactive search takes about four.
        _sup  = int(result.get("superseded") or 0)
        _nof  = int(result.get("episode_no_file") or 0)
        _unr  = int(result.get("unresolved") or 0)
        _searched = int(result["checked"]) - _sup - _nof - _unr
        self.logger.log_info(
            f"[LegacyRegrab] {prefix}'{instance}': {len(legacy)} legacy-codec file(s); checked "
            f"{result['checked']} (inline cap {cap}), {_searched} actually searched — "
            f"{result['grabbed']} grabbed, "
            f"{result['previewed']} would-grab, {result['no_release']} no modern release, "
            + (f"{_empty} empty search (indexer returned nothing — NOT recorded as a miss, "
               f"retried next run), " if _empty else "")
            + (f"{_sup} superseded (file already replaced; stale pointer retired), "
               if _sup else "")
            + (f"{_nof} episode has no file, " if _nof else "")
            + (f"{_unr} unresolved (no episode matched), " if _unr else "")
            + f"{result['failed']} failed"
            + (f"; {len(eligible) - len(batch)} more need the daemon" if len(eligible) > len(batch) else "")
            + ".")
        if _empty and _empty == result.get("checked"):
            # EVERY search came back empty. That is an indexer-side signal, not a
            # library one -- a disabled/unconfigured/rate-limited provider looks
            # identical to "no release exists", and concluding the latter from a
            # whole-batch zero is how a backlog gets written off wholesale.
            self.logger.log_warning(
                f"[LegacyRegrab] '{instance}': ALL {_empty} search(es) returned zero "
                f"releases. That points at indexer health (disabled, unconfigured or "
                f"rate-limited), not at the library — check Prowlarr before treating "
                f"these as having no modern release.")
        if result["preview"]:
            self.logger.log_grid(
                ["Episode", "Current", "-> Modern release", "Res", "State"],
                [[*row, "would-grab"] for row in result["preview"]],
                title=f"Legacy-codec re-grab - '{instance}' (dry-run preview)", cap=24)
        return {"legacy": len(legacy), "checked": result["checked"], "grabbed": result["grabbed"],
                "previewed": result["previewed"], "no_release": result["no_release"],
                "empty_search": _empty,
                # GLD-SON-25 - carried out so a caller can tell a pass that SEARCHED and
                # found nothing from one that never searched at all. `checked` alone
                # cannot: it counts loop iterations.
                "searched": _searched, "superseded": _sup,
                "episode_no_file": _nof, "unresolved": _unr,
                "failed": result["failed"]}

    def _maybe_offload_legacy_regrab(self, instance: str, items: list) -> bool:
        """Spill a LARGE legacy re-grab backlog to the standalone pilot-search daemon (mode
        'legacy_regrab') so the run process exits immediately instead of waiting out a long per-file
        interactive-search spree. Returns True when enqueued AND the daemon is running (caller skips
        the inline path). False — caller falls back to the capped inline path — when dry-run, the
        daemon is disabled, the batch is at/below the spill threshold, or anything fails (re-grabs are
        never silently dropped; the inline cap + cooldown make the next run pick up the rest)."""
        if self.dry_run:
            return False
        try:
            import os

            from scripts.managers.factories.daemons.daemon_paths import (
                PILOT_SPILL_THRESHOLD,
            )
            dcfg = ((self.config or {}).get("daemons", {}) or {}).get("pilot_search", {}) or {}
            if not dcfg.get("enabled", True):
                return False
            try:
                threshold = int(dcfg.get("threshold", PILOT_SPILL_THRESHOLD))
            except (TypeError, ValueError):
                threshold = PILOT_SPILL_THRESHOLD
            if len(items) <= max(0, threshold):
                return False
            from scripts.managers.factories.daemons import pilot_jobs
            from scripts.managers.factories.daemons.supervisor import (
                PilotSearchDaemonSupervisor,
            )
            cp = (((self.config or {}).get("scoring") or {}).get("codec_profiles") or {})
            job = {
                "version":       1,
                "mode":          "legacy_regrab",
                "instance":      instance,
                "items":         [{
                    "series_id": int(i["series_id"]), "episode_file_id": int(i["episode_file_id"]),
                    "resolution": int(i.get("resolution") or 0),
                    "series_title": i.get("series_title"), "video_codec": i.get("video_codec"),
                    "season_number": i.get("season_number"), "episode_number": i.get("episode_number"),
                } for i in items],
                "cooldown_days": int(cp.get("legacy_regrab_cooldown_days", 14) or 14),
                "run_pid":       os.getpid(),
            }
            path = pilot_jobs.enqueue(instance, job)
            try:
                PilotSearchDaemonSupervisor(logger=self.logger).ensure_running()
            except Exception:
                pilot_jobs.remove(path)   # don't orphan a job AND run inline (a double-search)
                raise
            self.logger.log_info(
                f"[LegacyRegrab] 🛰️ Spilled {len(items)} legacy-codec file(s) to the background "
                f"search daemon (batch > {threshold}); job {path.name}, log: pilot_search_daemon.log.")
            return True
        except Exception as e:
            self.logger.log_warning(
                f"[LegacyRegrab] Could not offload to the search daemon ({e}); "
                f"falling back to the capped inline path for this run.")
            return False

    # ══════════════════════════════════════════════════════════════════════════════
    # §5  ENRICHMENT & SHOW SCORING — metadata enrichment + the show score map
    #     _build_show_score_map (402 lines) is pure computation over the frame and is
    #     a candidate for machine_learning/ rather than a submanager.
    #     → SonarrEpisodeScoringManager (merges with §2)
    # ══════════════════════════════════════════════════════════════════════════════

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
        from scripts.managers.factories.daemons.bucket_merge import (
            show_enrichment_columns,
        )

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
    def _build_show_score_map(self, df: pd.DataFrame, instance: str,
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
            build_show_feature_row,
            score_show_features,
        )

        # Group-D device matrix: the shipped cold-start prior plus whatever the operator
        # added under scoring.device_capabilities. Resolved ONCE for the whole pass.
        from scripts.managers.machine_learning.scoring._shared import (
            resolve_device_capabilities,
        )
        device_capabilities = resolve_device_capabilities(self.config)

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

        # GROUP C4 — cast/crew taste overlap (config.scoring.person_affinity), the TV twin
        # of the movie path. Same shared resolver, so cap=0.0 (byte-identical) whenever the
        # term is disabled or the people-matrix affinity is empty. Loaded once per pass.
        from scripts.managers.machine_learning.scoring._shared import (
            resolve_person_affinity_inputs,
        )
        _aff_raw = self.global_cache.get("people_matrix/affinity") if self.global_cache else None
        person_weights, person_affinity_cap = resolve_person_affinity_inputs(self.config, _aff_raw)

        # GROUP A5 — explicit watchlist intent (config.scoring.watchlist_intent), the TV
        # twin of the movie path and the same shared gather, so movies and shows can never
        # disagree about what the household asked for. Shows are where the DATED half bites:
        # Plex's union is undated, but Trakt's show watchlist carries a real ``listed_at``.
        # cap forced to 0.0 (byte-identical) when disabled or nothing is watchlisted.
        from scripts.managers.machine_learning.scoring._shared import (
            resolve_intent_inputs,
        )
        from scripts.managers.services._intent_index import (
            gather_intent_index,
            intent_memo_fingerprint,
        )
        _intent_all = gather_intent_index(self.global_cache, self.config,
                                          logger=getattr(self, "logger", None))
        (intent_index, intent_cap,
         intent_half_life, intent_floor) = resolve_intent_inputs(self.config,
                                                                 _intent_all.get("shows"))
        intent_now = datetime.now(tz=timezone.utc)

        # GROUP D v2 (config.scoring.device_fit_v2, DEFAULT ON) — the household transcode
        # profile, built ONCE for the pass and shared by every series. None → the legacy
        # D1/D2/D3 bonus terms, byte-identical. The TV twin of the Radarr path; both read
        # the same two Tautulli buckets, so movies and shows can never disagree about
        # what this household transcodes for.
        transcode_profile = self._build_transcode_profile(platform_usage)

        watched_tvdb_ids: set[int] = set()
        if related_enabled and "is_watched" in df.columns:
            for _wsid, _wrows in df.groupby("series_id", sort=False):
                try:
                    if int((_wrows["is_watched"] == True).sum()) <= 0:
                        continue
                    _wso = series_by_id.get(str(int(_wsid))) or {}
                    _wtv = _wso.get("tvdbId")
                    if _wtv:
                        watched_tvdb_ids.add(int(_wtv))
                except Exception:
                    continue

        # ── P10 score memo: rescore ONLY series whose inputs changed ──────────
        # The per-series feature-build + score is ~1.8ms of pandas/Python × ~12k
        # series (~22s/run) even when NOTHING changed since the last run. Scores
        # are pure functions of (household context, episode rows, series object,
        # cached credits/ratings) — so memo them: a context hash guards the
        # household-wide inputs, a per-series key guards the rest. Any mismatch,
        # missing memo, or error falls through to the normal scoring path, so
        # scores are BYTE-IDENTICAL to an unmemoized run by construction (the
        # memo only ever skips recomputing an identical result).
        import hashlib as _hl
        import json as _json

        def _h(obj) -> str:
            try:
                return _hl.sha1(_json.dumps(obj, sort_keys=True, default=str)
                                .encode("utf-8", "replace")).hexdigest()
            except Exception:
                return ""

        # SCORER_REVISION: see the twin comment in radarr/quality/space_pressure. The
        # memo is keyed on inputs, which do not change when the scoring CODE does; the
        # revision token turns a scoring change into exactly one full rescore instead
        # of an indefinitely stale memo.
        from scripts.managers.machine_learning.scoring._shared import SCORER_REVISION
        _ctx_hash = _h([genre_affinity, platform_usage, transcode_stats,
                        per_user_affinity, sorted(kids_users or []), sorted(adult_users or []),
                        sorted(watched_tvdb_ids), related_graph_cap, person_weights,
                        person_affinity_cap, language_consumability, ur_kwargs,
                        sorted((k, v) for k, v in (user_show_ratings or {}).items()),
                        # The operator's Group-D device overrides. The SHIPPED matrix
                        # moves only with SCORER_REVISION, but an edit to
                        # scoring.device_capabilities changes D1/D2/D3 without changing
                        # any other input — so it has to be part of the key or the memo
                        # keeps serving scores from the old device table.
                        ((self.config or {}).get("scoring", {}) or {}).get("device_capabilities"),
                        # Group-D v2: a per-PASS household input that lives outside the
                        # per-series data, so it belongs in the CONTEXT hash — otherwise
                        # a shift in the observed cause mix keeps serving old scores.
                        (transcode_profile.memo_key() if transcode_profile is not None else None),
                        # Group-A5: the watchlist index is a per-PASS household input that
                        # ``_SCORE_COLS`` below cannot see — it lists episode-row columns,
                        # and the watchlist lives outside the parquet entirely. Without this
                        # line, adding a series to your watchlist would never invalidate its
                        # memoized score and A5 would silently do nothing. Only the
                        # score-relevant fields are digested (intent_memo_fingerprint), so a
                        # cosmetic union change does not force a needless full rescore.
                        # The DECAY knobs ride with the cap for the same reason: they are
                        # config, not row data, and they move every DATED title's A5
                        # without moving one column ``_SCORE_COLS`` can see. TV is where
                        # this bites hardest — Trakt's show watchlist is the dated feed.
                        intent_cap, intent_half_life, intent_floor,
                        intent_memo_fingerprint(_intent_all),
                        SCORER_REVISION])
        _MEMO_KEY = f"sonarr/{instance}/show_score_memo"
        _memo_prev: dict = {}
        if self.global_cache and _ctx_hash:
            try:
                _blob = self.global_cache.get(_MEMO_KEY) or {}
                if _blob.get("ctx") == _ctx_hash and isinstance(_blob.get("series"), dict):
                    _memo_prev = _blob["series"]
            except Exception:
                _memo_prev = {}
        _memo_next: dict = {}
        _memo_hits = 0
        # The ONLY episode-row columns build_show_feature_row reads (mirror of
        # show_features.py's rows[...] accesses). Hashing the whole row instead
        # made the memo useless in practice: the pilot batch rewrites
        # `date_added` on ~7.7k stale stubs every 48h and sync/plan passes touch
        # more bookkeeping columns, so most series looked "changed" while their
        # score inputs were identical (one run: 8675 needless rescores at ~18ms
        # each). Drift in show_features' column set is caught by the sampled
        # parity audit below.
        # Group-D v2 added six more columns to build_show_feature_row's reads
        # (audio_codec/audio_channels/video_bitrate/size_bytes/runtime_seconds/
        # relative_path). They MUST be here: the memo would otherwise miss a codec or
        # bitrate change and keep serving a stale transcode-risk penalty. Drift in
        # show_features' column set is still caught by the sampled parity audit below.
        _SCORE_COLS = ("air_date_utc", "audio_channels", "audio_codec", "audio_languages",
                       "is_watched", "keep_policy", "last_watched_at", "relative_path",
                       "resolution", "runtime_seconds", "size_bytes", "subtitles",
                       "video_bitrate", "video_codec", "watch_count")
        _score_cols = [c for c in _SCORE_COLS if c in df.columns]
        # Batch both halves of the key up front — at 12k series the per-series
        # versions were the whole cost of a 99%-hit pass (46s of stat() syscalls
        # and pandas hashing to conclude "nothing changed"):
        #   * ONE scandir per Trakt bucket instead of ~36k stat() calls
        #   * ONE vectorized row hash instead of 12k hash_pandas_object calls
        # Both degrade to the per-series path if anything goes wrong.
        _fp_index = None
        if show_cache:
            try:
                _fp_index = show_cache.fingerprint_index()
            except Exception:
                _fp_index = None
        _fp_buckets = getattr(show_cache, "SCORER_BUCKETS", ("people", "ratings", "related"))
        _row_hash = None
        if _score_cols:
            try:
                _row_hash = pd.util.hash_pandas_object(df[_score_cols], index=False)
            except Exception:
                _row_hash = None
        # Sampled parity audit: on a small fraction of memo hits, rescore anyway
        # and compare — the tripwire for the one failure mode a memo can hide
        # (an input missing from the key going stale silently). Mismatch → loud
        # warning + the fresh score wins. ~1% of hits ≈ 0.2s/run.
        import random as _rnd
        try:
            _audit_pct = float(((self.config or {}).get("scoring", {}) or {})
                               .get("show_score_memo_audit_pct", 0.01) or 0.0)
        except (TypeError, ValueError):
            _audit_pct = 0.01
        _audited = _audit_mismatches = 0

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

                user_rating = user_show_ratings.get(int(tvdb_id)) if tvdb_id else None

                # ── per-series cache FINGERPRINT (stat only, no decompression) ──
                # The credits/ratings/related payloads are one gzip+JSON file EACH
                # per series. Reading them to build the memo key meant ~36k
                # decompressions per run even at a 98% hit rate — the memo could
                # never save the I/O it was keyed on. Stat-based fingerprints
                # (size+mtime+freshness) identify the same payloads at ~µs, so the
                # reads below happen ONLY when a series actually needs rescoring.
                _fp = None
                if tvdb_id and _fp_index is not None:
                    _t = int(tvdb_id)
                    _fp = tuple(_fp_index.get(b, {}).get(_t) for b in _fp_buckets)
                elif tvdb_id and show_cache:
                    try:
                        _fp = show_cache.fingerprint(int(tvdb_id))
                    except Exception:
                        _fp = None

                # Per-series memo key: vectorized row hash (C-speed) + the series
                # object + every cache-derived input the scorer sees. 'now' is
                # deliberately excluded — recency terms decay with wall-clock, so
                # the memo also embeds the DAY: a date roll invalidates everything
                # once per day (recency drift is bounded to <24h, matching the
                # 24h cadence of the caches feeding it).
                # Key cost matters at 12k series: json.dumps of the FULL series
                # object + credits blob was ~5ms/series — the key construction
                # outweighed the scoring it skipped (profiler: refresh_scores
                # 77s at 98% memo hit rate). Compact fingerprints instead: the
                # scalar fields the scorer actually consumes + head-of-list
                # digests for credits. A deep tail edit the digest misses is
                # exactly what the 1% parity audit exists to catch (0 mismatches
                # in 120 audits so far).
                _st = series_obj.get("statistics") or {}
                _skey = _h([
                    # NOTE: list(...values) keeps numpy scalars so _h()'s
                    # default=str renders them exactly as the per-group path did —
                    # .tolist() would emit bare JSON ints and silently invalidate
                    # every existing memo entry.
                    (list(_row_hash.loc[rows.index].values) if _row_hash is not None
                     else list(pd.util.hash_pandas_object(
                         rows[_score_cols] if _score_cols else rows, index=False).values)),
                    [series_obj.get("title"), series_obj.get("tvdbId"),
                     series_obj.get("status"), series_obj.get("network"),
                     series_obj.get("certification"), series_obj.get("genres"),
                     series_obj.get("seriesType"), series_obj.get("monitored"),
                     series_obj.get("qualityProfileId"),
                     (series_obj.get("ratings") or {}).get("value"),
                     _st.get("episodeFileCount"), _st.get("sizeOnDisk"),
                     _st.get("episodeCount")],
                    _fp, user_rating,
                    now.date().isoformat(), bool(with_breakdown),
                ])
                _hit = _memo_prev.get(str(sid)) if _skey else None
                _audit_expect = None
                if _hit and _hit.get("k") == _skey:
                    if _audit_pct > 0 and _rnd.random() < _audit_pct:
                        _audit_expect = _hit.get("v")   # fall through: rescore + compare
                    else:
                        _v = _hit.get("v")
                        out[sid] = (tuple(_v) if isinstance(_v, list) else _v) if not with_breakdown \
                            else (_v[0], _v[1]) if isinstance(_v, list) else _v
                        _memo_next[str(sid)] = _hit
                        _memo_hits += 1
                        continue

                # ── MISS: only now pay the per-series cache reads ──────────────
                # credits + Trakt ratings + the related-neighbour set (all
                # gzip+JSON, one file each). Deferred past the memo check above so
                # an unchanged series costs a stat(), not three decompressions.
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
                    device_capabilities=device_capabilities,
                    transcode_profile=transcode_profile,
                    per_user_affinity=per_user_affinity,
                    kids_users=kids_users,
                    adult_users=adult_users,
                    watched_tvdb_ids=watched_tvdb_ids,
                    related_graph_cap=related_graph_cap,
                    person_weights=person_weights,
                    person_affinity_cap=person_affinity_cap,
                    intent_index=intent_index,
                    intent_cap=intent_cap,
                    intent_now=intent_now,
                    intent_half_life_days=intent_half_life,
                    intent_stale_floor=intent_floor,
                    language_consumability=language_consumability,
                    return_breakdown=with_breakdown,
                    **ur_kwargs,
                )
                if _skey:
                    _sv = out[sid]
                    _memo_next[str(sid)] = {"k": _skey,
                                            "v": list(_sv) if isinstance(_sv, tuple) else _sv}
                    if _audit_expect is not None:
                        _audited += 1
                        _fresh = list(_sv) if isinstance(_sv, tuple) else _sv
                        if _fresh != _audit_expect:
                            _audit_mismatches += 1
                            self.logger.log_warning(
                                f"[ShowScore] memo parity MISMATCH for series {sid}: "
                                f"memo={_audit_expect!r} fresh={_fresh!r} — fresh wins; "
                                f"an input is missing from the memo key (report this).")
            except Exception as e:
                out[sid] = (30, {}) if with_breakdown else 30   # neutral fallback, mirrors Radarr _score_row
                fallbacks += 1
                self.logger.log_debug(f"[ShowScore] series {sid} fell back to 30: {e}")
        if fallbacks:
            self.logger.log_warning(
                f"[ShowScore] {fallbacks}/{len(out)} series fell back to neutral 30 "
                f"— see debug log for causes."
            )
        if self.global_cache and _ctx_hash and _memo_next:
            try:
                self.global_cache.set(_MEMO_KEY, {"ctx": _ctx_hash, "series": _memo_next},
                                      pretty=False)
            except Exception:
                pass
        if _memo_hits or _audited:
            self.logger.log_info(
                f"[ShowScore] memo: {_memo_hits}/{len(out)} series unchanged — reused "
                f"prior scores (rescored {len(out) - _memo_hits}; parity-audited "
                f"{_audited}, {_audit_mismatches} mismatch(es)).")
        if with_breakdown:
            self._report_group_d(
                [v[1] for v in out.values() if isinstance(v, tuple) and len(v) == 2],
                instance, transcode_profile)
        return out

    def _build_transcode_profile(self, platform_usage: dict | None):
        """The household Group-D-v2 transcode profile, or None (→ the legacy D1/D2/D3
        terms). The exact twin of the Radarr helper — same buckets, same settings, so
        the movie and show paths cannot disagree about what this household transcodes
        for. Any failure, or a household with no evidence at all, returns None."""
        try:
            from scripts.managers.machine_learning.scoring._shared import (
                resolve_device_capabilities,
            )
            from scripts.managers.machine_learning.scoring.device_fit import (
                build_transcode_profile,
                resolve_device_fit,
            )
            settings = resolve_device_fit(self.config)
            if not settings.enabled:
                return None
            gc = self.global_cache
            return build_transcode_profile(
                platform_usage=platform_usage,
                stream_decisions=(gc.get("tautulli/stream_decisions") if gc else None),
                transcode_fingerprint=(gc.get("tautulli/transcode_fingerprint") if gc else None),
                capabilities=resolve_device_capabilities(self.config),
                settings=settings,
                preferred_languages=((self.config or {}).get("preferred_languages") or ["en"]),
            )
        except Exception as e:
            self.logger.log_debug(f"[ShowScore] device_fit_v2 profile unavailable: {e}")
            return None

    def _report_group_d(self, breakdowns, instance: str, profile) -> None:
        """Log the pass's Group-D distribution — the regression guard. See the twin in
        radarr/quality/space_pressure.py for why this exists (v1 collapsed to a
        near-constant in silence and invalidated every absolute threshold).

        TV SPLITS THE REPORT: 7,600 of this library's 11,973 series own no episode file,
        and a file-less stub is neutral 0.0 BY DESIGN. Reporting only the pooled figure
        would show a 65%-at-mode 'constant' that is nothing of the sort, so the
        file-owning subset — the population the ladder and the delete floor are anchored
        on — is reported separately and is the one the guard fires on."""
        try:
            from scripts.managers.machine_learning.scoring.device_fit import (
                summarise_group_d,
            )
            vals = []
            for bd in breakdowns:
                if not isinstance(bd, dict):
                    continue
                vals.append(bd.get("D4_transcode_risk", 0.0)
                            + bd.get("D1_device_capability", 0.0)
                            + bd.get("D2_transcode_avoidance", 0.0)
                            + bd.get("D3_platform_ceiling", 0.0))
            allv = summarise_group_d(vals)
            if not allv["n"]:
                return
            # A stub scores exactly 0.0 under v2; under v1 it scored a flat +2.0.
            neutral = 0.0 if profile is not None else 2.0
            owners = summarise_group_d([v for v in vals if round(v, 2) != neutral])
            mode = "v2 risk-penalty" if profile is not None else "v1 legacy bonus"
            self.logger.log_info(
                f"[ShowScore] '{instance}' Group D ({mode}): all {allv['n']} series mean "
                f"{allv['mean']:+.2f} sd {allv['sd']:.2f}, {allv['distinct']} distinct; "
                f"file-owning {owners['n']} mean {owners['mean']:+.2f} sd {owners['sd']:.2f} "
                f"mode {owners['mode']:+.2f} @ {owners['share_at_mode']:.1%}, "
                f"{owners['distinct']} distinct.")
            if owners["n"] >= 50 and (owners["share_at_mode"] >= 0.75 or owners["distinct"] <= 2):
                self.logger.log_warning(
                    f"[ShowScore] '{instance}' Group D is behaving as a CONSTANT on the "
                    f"file-owning population ({owners['share_at_mode']:.0%} share one value, "
                    f"{owners['distinct']} distinct) — no ranking information, and every "
                    f"absolute threshold anchored on the score is drifting. This is the "
                    f"exact regression scoring/device_fit.py was written to prevent.")
        except Exception:
            pass

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
                from scripts.managers.services.trakt.shows.cache import (
                    TraktShowCacheManager,
                )
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

    # ══════════════════════════════════════════════════════════════════════════════
    # §6  DELETE CANDIDATES & RESTORE — selection, the selected-file delete, recovery
    #     THE DELETE PATH. Extract LAST, and only with tests green either side.
    #     → SonarrEpisodeRetentionManager (merges with §10, §12, §14)
    # ══════════════════════════════════════════════════════════════════════════════

    @timeit("build_episode_delete_candidates")
    def build_delete_candidates(self, instance: str, df=None) -> list[dict]:
        """Return EPISODE delete-candidates (one per episode_file_id) for the
        coordinator's pool: rows already marked_for_deletion (watched + grace
        expired) whose file isn't whole-file-guarded. Each dict carries the
        per-series watchability_score, size, file id, series id + episode coords.

        DELETE-ELIGIBILITY INVARIANT (``space_exhaustive_downgrade``, DEFAULT ON): a file may
        enter the pool ONLY when its resolution is AT or BELOW the 720p floor — nothing left to
        shrink. Files above the floor are excluded and counted as ``skipped_downgradable`` (also
        published on ``self.last_skipped_downgradable`` for the coordinator's log), so the TV
        step-down pass must shrink them to the floor before they can ever be deleted. An unknown
        resolution counts as at-floor (the downgrade planner treats it as nothing-to-step, so it
        would otherwise be undeletable forever)."""
        out: list[dict] = []
        self.last_skipped_downgradable = 0
        self.last_skipped_unscored = 0
        if df is None:
            df = self.load(instance)
        if df is None or df.empty or "episode_file_id" not in df.columns or "marked_for_deletion" not in df.columns:
            return out
        marked = (df["marked_for_deletion"] == True)
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
        # "Deletion is the true last resort": only at/below-720p files may be deleted.
        _floor_gate = exhaustive_downgrade(self.config)
        _held_downgradable = 0
        _held_unscored = 0    # rows refresh_scores hasn't reached yet -> deferred, not deleted
        seen: set[int] = set()
        for idx in df.index[marked]:
            fid = df.at[idx, "episode_file_id"]
            if pd.isna(fid):
                continue
            fid = int(fid)
            if fid in protected or fid in seen:
                continue
            _res = df.at[idx, "resolution"] if "resolution" in df.columns else None
            if _floor_gate:
                try:
                    if _res is not None and pd.notna(_res) and int(_res) > DOWNGRADE_FLOOR_RESOLUTION:
                        _held_downgradable += 1
                        seen.add(fid)   # one count per FILE, not per backing episode row
                        continue        # still shrinkable → NOT delete-eligible
                except (TypeError, ValueError):
                    pass                # unreadable resolution → treated as at-floor
            # UNSCORED -> DEFERRED. The old ``... else 5`` handed a row with no score a
            # literal point on the score axis, which silently changed meaning when Group D
            # v2 translated that axis. This now matches the whole-column guard above
            # ("won't delete on fallback scores") for the partial case. Counted + logged.
            # Full reasoning: machine_learning/space/downgrade_planner.row_score.
            sc = df.at[idx, "watchability_score"] if "watchability_score" in df.columns else None
            if sc is None or not pd.notna(sc):
                _held_unscored += 1
                seen.add(fid)   # one count per FILE, not per backing episode row
                continue
            seen.add(fid)
            size = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0
            score = int(sc)
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
                "resolution": int(_res) if (_res is not None and pd.notna(_res)) else None,
                "title": f"{title} S{int(sn):02d}E{int(en):02d}" if (pd.notna(sn) and pd.notna(en)) else str(title),
            })
        self.last_skipped_unscored = _held_unscored
        if _held_unscored:
            self.logger.log_warning(
                f"[EpisodeFiles] '{instance}': {_held_unscored} episode file(s) DEFERRED from the delete "
                f"pool — no watchability_score yet, and a file is never deleted on a guessed score. They "
                f"re-qualify as soon as refresh_scores reaches them; a count that persists across runs "
                f"means the scorer is skipping those rows."
            )
        self.last_skipped_downgradable = _held_downgradable
        if _held_downgradable:
            self.logger.log_info(
                f"[EpisodeFiles] '{instance}': {_held_downgradable} episode file(s) EXCLUDED from the "
                f"delete pool — still above the {DOWNGRADE_FLOOR_RESOLUTION}p floor, so there is quality "
                f"left to shrink (skipped_downgradable). Deletion is the true last resort."
                + ("" if out else " The pool is EMPTY for exactly this reason — nothing is at the "
                                  "floor yet, so nothing is deletable.")
            )
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
                f"[EpisodeFiles] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
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
        # GLD-RST-08 — permanent record; flushed once after the loop.
        _archive: list = []
        if not getattr(self, "_deletion_run_id", None):
            self._deletion_run_id = new_run_id()
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
                # RELEASE IDENTITY (ledger v2): record WHICH release we are removing
                # so the restore can ask for that one back instead of firing a blind
                # search. Everything here is already on this row — no indexer lookup,
                # and no guid (it would be stale long before the restore fires).
                _rr = release_record({f: df.at[idx, f] for f in RELEASE_FIELDS
                                      if f in df.columns})
                _ek = episode_key(sn, en)
                if _rr and _ek:
                    ent.setdefault("releases", {})[_ek] = _rr
            if fid in done:   # multi-ep file already handled
                continue
            done.add(fid)
            size = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0
            title = df.at[idx, "series_title"] if "series_title" in df.columns else f"series {sid}"
            _why = None
            if "planned_reason" in df.columns and pd.notna(df.at[idx, "planned_reason"]):
                _why = str(df.at[idx, "planned_reason"])
            _why = _why or "coordinator pool"
            if effective_dry_run(self.dry_run, self.global_cache):    # also dry when backup gate disarmed
                self.logger.log_info(f"  🗑️ [dry_run] Would delete episode file: '{title}' (fid={fid}, {self._fmt_bytes(size)})")
                _del_rows.append([str(title), _se, str(fid), self._fmt_bytes(size), "would delete"])
                stats["deleted"] += 1
                stats["bytes_freed"] += size
                deleted_fids.add(fid)
                self._archive_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    season=sn, episode=en, disposition="would-delete",
                    reason=_why, size_bytes=size, file_id=fid, source="coordinator")
                continue
            _del_ok = False
            try:
                # CHECKED (GLD-RST-20) — see `_do_delete_marked_files`. `_make_request`
                # swallows HTTP failures and returns the fallback, so the old bare
                # try/except could not catch a 500 and every failed DELETE counted as
                # freed bytes AND wrote a restore-ledger entry for a file still on disk.
                _del_ok = bool(self.sonarr_api._make_request(
                    instance, f"episodefile/{fid}", method="DELETE"))
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Episode-file delete raised for '{title}' (fid={fid}): {e}")
            if not _del_ok:
                self.logger.log_warning(
                    f"  ⚠️ Episode-file delete FAILED for '{title}' (fid={fid}) — "
                    f"file KEPT; retries next run.")
                _del_rows.append([str(title), _se, str(fid), self._fmt_bytes(size), "FAILED"])
                self._archive_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    season=sn, episode=en, disposition="failed",
                    reason=f"{_why} | DELETE returned falsy", size_bytes=size,
                    file_id=fid, source="coordinator", fetch_descriptor=False)
                stats["failed"] += 1
                continue
            stats["deleted"] += 1
            stats["bytes_freed"] += size
            deleted_fids.add(fid)
            self.logger.log_info(f"  🗑️ Deleted episode file: '{title}' (fid={fid}, {self._fmt_bytes(size)})")
            _del_rows.append([str(title), _se, str(fid), self._fmt_bytes(size), "deleted"])
            self._archive_deletion(
                _archive, instance=instance, row=df.loc[idx], title=title,
                season=sn, episode=en, disposition="deleted",
                reason=_why, size_bytes=size, file_id=fid, source="coordinator")

        _archived = self._flush_deletion_archive(_archive)
        if _archived:
            self.logger.log_info(
                f"  \U0001f5c3\ufe0f  archived {_archived} deletion row(s) → "
                f"logs/deletions/deletions.jsonl (run {self._deletion_run_id})")

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
                    # merge_ledger_entry tolerates BOTH schema versions on either
                    # side: a v1 record already on disk (episodes + ts, no releases)
                    # keeps working and simply gains a releases map from here on.
                    dset[sk] = merge_ledger_entry(dset.get(sk), ent)
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
        watchability_score has recovered above ``tv_restore_score_threshold`` (17):
        re-monitor the episodes + trigger EpisodeSearch. Mirrors Radarr's
        restore_recovered_deletions — but NOT its threshold: that twin scores on a
        different axis and keeps ``owned_restore_score_threshold`` (20). Tracked in
        ``sonarr/{inst}/deleted_episodes``."""
        instance = self._resolve_instance(instance)
        stats = {"tracked": 0, "restored": 0, "still_low": 0, "cooling": 0,
                 "dropped": 0, "deferred": 0, "failed": 0, "targeted": 0}
        if self.sonarr_api is None or self.global_cache is None:
            return stats
        now = datetime.now(tz=timezone.utc)
        dkey = self._DELETED_EPISODES_KEY.format(inst=instance)
        dset = self.global_cache.get(dkey)
        dset = dset if isinstance(dset, dict) else {}
        if not dset:
            return stats
        # ``tv_restore_score_threshold``, NOT Radarr's ``owned_restore_score_threshold``.
        # This leg reads the PERSISTED ``watchability_score`` column (axis V2 — Group D v2's
        # transcode-risk penalty is live in it), while the Radarr twin
        # (repair/anomaly.py::restore_recovered_deletions) re-scores raw Radarr dicts on an
        # axis Group D v2 never reached. One key could not be correct for both, so it was
        # split; the key here is also welded to a different hysteresis partner — these
        # episodes were deleted by the COORDINATOR under ``tv_space_pressure_score_ceiling``
        # (17), not by anomaly.py's demote floor (20). Deletion is ``score < ceiling`` and
        # restore is ``score > floor``, so floor == ceiling == 17 is thrash-free (a series at
        # exactly 17 is neither deleted nor restored). Full rationale + the measured
        # two-axis gap: machine_learning/thresholds/registry.py's delete block.
        try:
            restore_floor = int(self.config.get("tv_restore_score_threshold", 17) if self.config else 17)
        except (TypeError, ValueError):
            restore_floor = 17
        restore_floor = get_threshold("series_restore", self.config, restore_floor,
                                      logger=getattr(self, "logger", None))
        try:
            restore_min_age = int(self.config.get("owned_restore_min_age_days", 0) if self.config else 0)
        except (TypeError, ValueError):
            restore_min_age = 0

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
            if restore_cooldown_active(ent.get("ts"), now, restore_min_age):
                # Score recovered but the re-grab cooldown hasn't elapsed since
                # deletion — hold off (no API call) so a series hovering at the floor
                # can't be deleted one run and restored the next, repeatedly.
                keep[sk] = ent
                stats["cooling"] += 1
                continue
            # Resolve episode ids for the recovered coords.
            eps = self.sonarr_api._make_request(instance, f"episode?seriesId={sid}", fallback=[]) or []
            want = set(coords)
            ep_ids = [e.get("id") for e in eps
                      if (e.get("seasonNumber"), e.get("episodeNumber")) in want and e.get("id")]
            # episode id -> the release identity recorded when this episode was
            # deleted (ledger v2). Empty for a v1 record → every episode below takes
            # the blind-search path, exactly as before.
            recorded_by_eid: dict = {}
            _rel_map = ledger_releases(ent)
            if _rel_map:
                for e in eps:
                    _ek = episode_key(e.get("seasonNumber"), e.get("episodeNumber"))
                    if _ek and e.get("id") and _ek in _rel_map:
                        recorded_by_eid[e["id"]] = _rel_map[_ek]
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
                    f"re-monitor + search {len(ep_ids)} ep(s)"
                    + (f" ({len(recorded_by_eid)} with a recorded release to target)"
                       if recorded_by_eid else " (blind search — no recorded release)")
                )
                stats["restored"] += 1
                keep[sk] = ent   # still deleted in reality → keep tracking
                continue
            try:
                self.sonarr_api._make_request(instance, "episode/monitor", method="PUT",
                                              payload={"episodeIds": ep_ids, "monitored": True})
                # TARGETED first, blind for the remainder. Every episode we recorded a
                # release identity for gets ONE interactive search; if that reveals the
                # recorded release (or a close-enough sibling — see
                # restore_policy.match_release) we grab exactly it. Anything that
                # doesn't match falls through to the original blind EpisodeSearch, so
                # a missing or stale scene_name can never block a restore.
                targeted = self._targeted_restore(instance, sid, recorded_by_eid)
                stats["targeted"] += len(targeted)
                blind_ids = [e for e in ep_ids if e not in targeted]
                if blind_ids:
                    self.sonarr_api._make_request(
                        instance, "command", method="POST",
                        payload={"name": "EpisodeSearch", "episodeIds": blind_ids})
                stats["restored"] += 1
                self.logger.log_info(
                    f"  Restored series {sid}: re-monitored {len(ep_ids)} ep(s) — "
                    f"{len(targeted)} grabbed on the recorded release, "
                    f"{len(blind_ids)} blind-searched"
                )
            except Exception as e:
                self.logger.log_warning(f"  Episode restore failed for series {sid}: {e}")
                stats["failed"] += 1
                keep[sk] = ent
        try:
            self.global_cache.set(dkey, keep)
        except Exception:
            pass
        return stats

    def _targeted_restore(self, instance: str, sid: int, recorded_by_eid: dict) -> set:
        """Re-grab the SPECIFIC releases recorded at delete time. Returns the set of
        episode ids actually grabbed; every id NOT in that set must be blind-searched
        by the caller.

        ONE interactive ``GET /release?episodeId=`` per episode that has a recorded
        identity (the same primitive ``legacy_regrab`` / ``pilot_interactive`` /
        ``space_pressure`` already use), then ``POST /release`` with the chosen
        release's guid + indexerId. The MATCH decision is pure
        (``restore_policy.match_release``); this is only the I/O.

        FAIL-OPEN EVERYWHERE. A search that errors, returns nothing, or reveals no
        release matching what we recorded leaves that episode out of the returned
        set → the caller blind-searches it. So a stale scene_name, a dead indexer,
        or a rewritten ledger degrades the restore to exactly the old behaviour and
        never blocks it."""
        grabbed: set = set()
        if not recorded_by_eid or self.sonarr_api is None:
            return grabbed
        for eid, recorded in recorded_by_eid.items():
            # GLD-RST-03 — STRENGTHEN THE KEY BEFORE SEARCHING WITH IT.
            # The ledger identity is lifted off the parquet row at delete time, where
            # `scene_name` is absent on every entry in this library. That leaves
            # match_release scoring at most 3 (group + quality + resolution) when the
            # release TITLE alone is worth +3 exact / +2 substring. Sonarr's own grab
            # history knows that title, so one cheap read here decides whether the
            # search below can tell two same-quality encodes apart or has to guess on
            # size. This is an IDENTITY source, not a grab source — the guid is
            # deliberately not carried (see restore_policy: dead in hours, and a dead
            # guid is worse than none because it looks actionable).
            recorded = self._enrich_recorded_from_history(instance, eid, recorded)
            try:
                releases = self.sonarr_api._make_request(
                    instance, f"release?episodeId={int(eid)}", fallback=None)
            except Exception as e:
                self.logger.log_debug(f"  [Restore] interactive search failed for ep {eid}: {e}")
                continue
            best = match_release(releases if isinstance(releases, list) else [], recorded)
            if not best or not best.get("guid"):
                self.logger.log_debug(
                    f"  [Restore] no release matching the recorded "
                    f"'{recorded.get('scene_name') or recorded.get('release_group') or '?'}' "
                    f"for ep {eid} — falling back to a blind search.")
                continue
            try:
                res = self.sonarr_api._make_request(
                    instance, "release", method="POST", fallback=None,
                    payload={"guid": best.get("guid"), "indexerId": best.get("indexerId")})
            except Exception as e:
                self.logger.log_debug(f"  [Restore] targeted grab errored for ep {eid}: {e}")
                continue
            if res is not None:
                grabbed.add(eid)
                self.logger.log_info(
                    f"  🎯 Restored ep {eid} on its recorded release: "
                    f"{str(best.get('title'))[:64]}")
        return grabbed

    def _enrich_recorded_from_history(self, instance: str, eid, recorded) -> dict:
        """*recorded*, with any gaps filled from this episode's Sonarr grab history — GLD-RST-03.

        BEST-EFFORT BY CONSTRUCTION. History is an optimisation on the match KEY, never a
        precondition for the restore: an unreachable endpoint, an empty history, or a
        series whose grabs predate the retention window all return *recorded* unchanged,
        and the caller proceeds exactly as it did before. Enrichment must never be able to
        cost a restore it was added to improve.

        The ledger stays authoritative — ``merge_release_records`` gives it every field it
        actually holds — because it describes the FILE that was on disk while history
        describes what was GRABBED, and a repack, manual import or external replacement
        makes those differ. In practice history contributes ``scene_name``, plus
        ``video_codec`` on entries written before GLD-RST-01.
        """
        try:
            raw = self.sonarr_api._make_request(
                instance, f"history?episodeId={int(eid)}&pageSize=50", fallback=None)
        except Exception:
            return recorded
        # Sonarr returns a bare list on some routes and a paged {records:[...]} envelope on
        # others; tolerate both rather than betting on one.
        rows = raw.get("records") if isinstance(raw, dict) else raw
        from_history = history_release_record(rows if isinstance(rows, list) else [])
        if not from_history:
            return recorded
        merged = merge_release_records(recorded, from_history) or recorded
        if merged.get("scene_name") and not (recorded or {}).get("scene_name"):
            self.logger.log_debug(
                f"  [Restore] ep {eid}: identity strengthened from grab history — "
                f"'{str(merged['scene_name'])[:64]}'")
        return merged

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
            # ``watch_count`` counts WATCHES, not plays (the caller's aggregate already
            # applied lifecycle.watched_definition), so this stays the derivation it
            # always was and now means what Plex/Tautulli mean.
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

    # Columns whose ONLY source is the Sonarr ``/episodefile`` record — GLD-EPF-14.
    # Everything NOT listed here is accumulated state the sync must never overwrite:
    # watch stats, grace marks (`marked_for_deletion`, `available_until`), plan stamps,
    # `keep_policy`, `watchability_score`, `row_origin`, `next_episode`, `is_pilot`.
    # The re-point below copies THIS LIST ONLY, so a widened schema can never quietly
    # start clobbering a lifecycle field — a new column is opted in deliberately or
    # not at all.
    _FILE_DERIVED_COLUMNS = (
        "episode_file_id", "relative_path", "path", "size_bytes", "date_added",
        "quality_name", "quality_source", "resolution", "video_codec", "video_bitrate",
        "video_fps", "video_bit_depth", "width", "height", "runtime_seconds",
        "scan_type", "hdr", "hdr_type", "audio_codec", "audio_channels",
        "audio_languages", "subtitles", "release_group", "scene_name",
        "quality_cutoff_not_met",
    )

    def _repoint_file_fields(self, df, row_idx, file_rec, fid) -> bool:
        """Re-point an EXISTING row at the file Sonarr holds for it now — GLD-EPF-14.

        Returns True when the row was actually re-pointed.

        THE BUG THIS CLOSES. ``sync_from_tautulli`` resolved the episode file only on
        the branch that CREATES a row. A row that already existed had its watch stats
        refreshed and ``last_synced_at`` stamped, and its file columns — resolution,
        size_bytes, quality_name, video_codec, episode_file_id — were never read again
        for the life of the row. So file facts froze at first insertion: every upgrade,
        manual import or external replacement after that point was invisible, and the
        row kept describing a file that had been deleted.

        Observed live on Loki 2026-08-13: Sonarr held 12 files, all WEBDL-2160p,
        67.4 GB, imported 08-12. The parquet held 6 rows at 480p/720p/1080p totalling
        6.7 GB whose ids (35843, 35845, 35848, 41044, 41052, 51457) had ALL been
        deleted — zero overlap with reality, a 10x understatement, wearing a
        four-minute-old ``last_synced_at``. Both caches were correct at the time; the
        row simply was not re-read. Textbook P-C: the row's PRESENCE was treated as
        equivalent to its CONTENTS being current.

        Why it hid so well: every symptom pointed at the cache. The timestamp was
        fresh, the API payloads were fresh, and rows created AFTER an upgrade (See,
        Silo, Yellowstone) showed 2160p correctly — so the library looked partially
        right rather than uniformly wrong, which reads as a data gap rather than a bug.
        """
        try:
            stored = df.at[row_idx, "episode_file_id"]
        except Exception:
            return False
        try:
            same = stored is not None and stored == stored and int(stored) == int(fid)
        except (TypeError, ValueError):
            same = False
        if same:
            return False
        fresh = self._normalise(
            raw=file_rec, series_id=int(df.at[row_idx, "series_id"]),
            series_title=df.at[row_idx, "series_title"],
            season_number=df.at[row_idx, "season_number"],
            episode_number=df.at[row_idx, "episode_number"],
        )
        for col in self._FILE_DERIVED_COLUMNS:
            if col in df.columns and col in fresh:
                df.at[row_idx, col] = fresh[col]
        return True

    # ── Sonarr API helpers ──────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════════════════════════
    # §7  SPACE & TTL — free-space probe and the episode/file cache TTL knobs
    #     Shared by §9, §11, §15, §17. STAYS on extraction.
    # ══════════════════════════════════════════════════════════════════════════════

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

    # ── per-series Sonarr cache freshness (GLD-CACHE-14) ─────────────────────
    # Two payloads, ONE clock, because the join between them is what the parquet is
    # built from and a join is only as fresh as its stalest half:
    #
    #   EPISODES  (episode?seriesId=)     the season/episode list — and, critically,
    #                                     each episode's ``episodeFileId``. That POINTER
    #                                     changes on every grab, upgrade and manual
    #                                     import, so this payload is NOT slow-moving
    #                                     reference data however much the name suggests it.
    #   EPISODEFILES (episodefile?seriesId=)  the FILE FACTS — resolution, size_bytes,
    #                                     quality_name, video_codec, video_bitrate, hdr.
    #                                     Every space decision reads these cells
    #                                     (_build_row -> the parquet -> the step-down floor
    #                                     check, the size-anomaly pass, plan_reclaim_gb),
    #                                     so a stale one is a wrong plan, not a slow one.
    #                                     Mirrors Radarr's ``radarr_movie_library_max_age_s``
    #                                     (900 s); Radarr re-pulls its library every run.
    #
    # WHY THEY MUST MATCH — THIS SHIPPED WRONG ONCE. These were first split 86400/900 on
    # the reasoning that a season list "moves when a season airs, a day is generous". It
    # does not: _resolve_episode_file reads episodeFileId from the EPISODE record and
    # looks it up in the FILE list, so a stale episode half yields RETIRED file ids that
    # match nothing in the fresh half. The resolve then returns (None, None, None) and the
    # caller leaves the old row in place — while still stamping ``last_synced_at`` with
    # now. Observed live on Loki 2026-08-13: Sonarr held 12 files, all WEBDL-2160p, 67.4 GB,
    # imported 08-12; the parquet held 6 rows at 480p/720p/1080p totalling 6.7 GB whose ids
    # (35843, 35845, 35848, 41044, 41052, 51457) had ALL been deleted — zero overlap, a 10x
    # understatement, carrying a four-minute-old sync timestamp. A wrong number wearing a
    # fresh timestamp defeats the obvious staleness check, so the invariant is enforced in
    # _episodes_ttl_s rather than left to whoever edits the config next.
    #
    # BOTH are passed ``regenerate_on_expiry=True`` at the call sites. Without that opt-in
    # ``get_or_generate_cache`` logs the expiry and serves the stale copy ANYWAY, which
    # froze these keys at first write — observed live: every by_series payload stamped
    # 2026-08-08 17:33 and still being served 2026-08-13, read daily, rewritten never.
    EPISODES_CACHE_TTL_S: int = 900        # sonarr_episode_list_max_age_s
    EPISODE_FILES_CACHE_TTL_S: int = 900      # sonarr_episode_files_max_age_s

    def _episodes_ttl_s(self) -> int:
        """Episode-LIST cache max age (seconds). Config: ``sonarr_episode_list_max_age_s``.

        CLAMPED to never exceed the episode-FILE age. The two payloads are joined on
        episodeFileId, so a longer clock here silently reintroduces the retired-id
        mismatch above no matter what the file half is set to. Two independent knobs for
        one matched pair is a footgun; taking the min makes the pairing structural instead
        of conventional, and a config that violates it is corrected rather than obeyed.
        """
        return min(
            self._ttl_cfg("sonarr_episode_list_max_age_s", self.EPISODES_CACHE_TTL_S),
            self._episode_files_ttl_s(),
        )

    def _episode_files_ttl_s(self) -> int:
        """Episode-FILE cache max age (seconds). Config: ``sonarr_episode_files_max_age_s``."""
        return self._ttl_cfg("sonarr_episode_files_max_age_s", self.EPISODE_FILES_CACHE_TTL_S)

    def _ttl_cfg(self, key: str, default: int) -> int:
        """Positive int from config, else *default*. A 0 / negative / unparseable value means
        'not configured' rather than 'never cache' — an accidental 0 would turn every walk
        into a per-series API storm, so it falls back instead of being taken literally."""
        try:
            raw = self.config.get(key, default) if getattr(self, "config", None) else default
            val = int(raw)
            return val if val > 0 else int(default)
        except (TypeError, ValueError, AttributeError):
            return int(default)

    # ══════════════════════════════════════════════════════════════════════════════
    # §8  SONARR FETCH & CACHE — episode/file retrieval, prewarm, resolution
    #     The read side of the frame. STAYS on extraction — every section depends on it.
    #     ⚠️ `_resolve_episode_file` is where GLD-SON-25's stale-pointer class lives:
    #        an episode_file_id is a HANDLE any replacement invalidates, not an identity.
    # ══════════════════════════════════════════════════════════════════════════════

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
                # fallback=None (NOT []) so a FAILED fetch is distinguishable from a
                # genuinely-empty series — the house idiom (see base_instance_manager
                # ~line 542). This matters ONLY now that regenerate_on_expiry is on: the
                # generator actually runs on expiry, and get_or_generate_cache treats
                # ``None`` as "serve the last-good copy" but caches ``[]`` as real data.
                # With the old ``or []`` a single Sonarr blip would have overwritten a good
                # payload with an empty list — P-C, absent conflated with empty.
                all_eps = self.global_cache.get_or_generate_cache(
                    key=cache_key,
                    generator_function=lambda: self.sonarr_api._make_request(
                        instance, f"episode?seriesId={series_id}", fallback=None
                    ),
                    expiration_time=self._episodes_ttl_s(),
                    regenerate_on_expiry=True,
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
                    # Short TTL + real regeneration: these are the FILE FACTS every space
                    # decision reads. fallback=None for the same reason as the episode list
                    # above — never overwrite a good payload with an empty list on a blip.
                    cached_files = self.global_cache.get_or_generate_cache(
                        key=files_cache_key,
                        generator_function=lambda: self.sonarr_api._make_request(
                            instance, f"episodefile?seriesId={series_id}", fallback=None
                        ),
                        expiration_time=self._episode_files_ttl_s(),
                        regenerate_on_expiry=True,
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
                           retries: int = 2, retry_delay_s: float = 2.0) -> "list[dict] | None":
        """All episodefile records for a series, or **None** when the fetch FAILED.

        ⚠️ None and [] MEAN DIFFERENT THINGS (GLD-DEL-09, **P-C**). ``[]`` is "Sonarr
        holds no files for this series"; ``None`` is "we could not find out". Callers
        that PRUNE on absence must treat them differently, or a failed read deletes
        every row for the series.

        This returned ``[]`` for both. ``_make_request(fallback=[])`` SWALLOWS HTTP
        failures and returns the fallback rather than raising — the same P-A shape as
        `GLD-SON-23` — so a 500 never reached the ``except``, the retry loop never ran,
        and the caller saw an empty list indistinguishable from an empty series. That
        was survivable while `_do_purge_sonarr_deleted` only scanned `marked_for_deletion`
        rows; widening it to every file-owning row (10,225 of them) would have turned
        one Sonarr blip into a mass row deletion.

        ``fallback=None`` makes the failure legible, and the falsy check below now
        distinguishes it from a genuine empty list.
        """
        last_exc = None
        for attempt in range(max(1, retries)):
            try:
                res = self.sonarr_api._make_request(
                    instance, f"episodefile?seriesId={series_id}", fallback=None)
                if res is not None:
                    return list(res)
                last_exc = "request returned no payload"
            except Exception as e:
                last_exc = e
            if attempt < retries - 1:
                self.logger.log_debug(
                    f"  ↺ _get_episode_files series={series_id} "
                    f"attempt {attempt + 1}/{retries} failed — retrying in {retry_delay_s:.1f}s: {last_exc}"
                )
                time.sleep(retry_delay_s)
        self.logger.log_warning(
            f"  ⚠️ _get_episode_files series={series_id} failed after {retries} "
            f"attempt(s): {last_exc} — treating as UNKNOWN, not empty."
        )
        return None

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

        # GLD-DEL-09: `_get_episode_files` now returns None on a FAILED fetch, so
        # that pruning callers can tell it from an empty series. Read-only callers
        # like this one only need it to be iterable.
        all_files = files_cache[series_id] or []
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

    def _universe_group_maps(self, instance):
        """``({series_id: group_token}, {series_id: timeline_index})`` for the per-group prefetch
        walk — the SAME grouping + order the Plex playlist builder uses
        (``universe_order.tv_group_maps``), sourced Sonarr-side from the series cache (id / title /
        tvdbId) + the cached universe source. Returns ``({}, {})`` — i.e. every series becomes its
        own singleton group and the walk reduces BYTE-IDENTICALLY to the legacy per-series
        behaviour — when ``acquisition.enabled`` OR ``acquisition.universe.enabled`` is off, or the
        series cache / source is unavailable. (The mdblist source may be empty without a key; the
        curated TV franchises — One Chicago, Law & Order, Doctor Who — still group from the bundled
        map.)

        ``acquisition.universe`` is a CHILD of ``acquisition``: disabling the parent disables the
        whole acquisition subtree, including this universe reordering/injection — it never runs
        behind a master switch the operator believes is off."""
        acq = ((self.config or {}).get("acquisition", {}) or {})
        uni = (acq.get("universe", {}) or {})
        if not (acq.get("enabled") and uni.get("enabled")):
            return {}, {}
        series_cache = getattr(self.sonarr_cache, "series", None)
        if series_cache is None:
            return {}, {}
        try:
            rows = [s for s in series_cache.iter_all_series(instance) if isinstance(s, dict)]
        except Exception as e:
            self.logger.log_warning(f"[UniverseAcquire] series list unavailable for '{instance}': {e}")
            return {}, {}
        source = self.global_cache.get(self._UNIVERSE_SRC_KEY) if self.global_cache else None
        fran, timeline = tv_group_maps_from_series(rows, source or {})
        if fran:
            self.logger.log_info(
                f"[UniverseAcquire] {len(set(fran.values()))} saga group(s) → {len(fran)} owned "
                f"series for the per-group prefetch walk ({len(timeline)} timeline-ordered).")
        return fran, timeline

    def _plan_group_walk(self, last_by_series, group_of, group_timeline, instance):
        """Order the prefetch walk by GROUP. Returns a list of resume-row DICTS, each tagged with
        ``_group`` (its saga key). Groups run in recency order; within a group, members in saga
        order (``timeline_index``, else stable). For an ENGAGED group (≥1 already-watched member)
        the UNSTARTED owned in-Sonarr members are appended as synthetic resume rows (S1E0 → the
        walk begins at S1E1), so the saga's NEXT show prefetches once the current is exhausted
        ("finish Loki → Daredevil"). No cold-start: a saga nobody has watched is never injected.

        With ``group_of`` empty (feature off) every series is its own singleton group and the order
        equals ``last_by_series`` — so the downstream walk reduces BYTE-IDENTICALLY to the legacy
        per-series behaviour."""
        rows = [dict(r) for _, r in last_by_series.iterrows()]
        if not group_of:
            for r in rows:
                r["_group"] = f"series:{int(r['series_id'])}"
            return rows

        def _key(sid):
            return group_key_for_series(sid, group_of, group_timeline)[0]

        started = {int(r["series_id"]) for r in rows}
        engaged = {_key(s) for s in started}
        resume_by_sid = {int(r["series_id"]): r for r in rows}
        # Titles for the synthetic unstarted rows — from the series cache (id → title).
        titles: dict = {}
        series_cache = getattr(self.sonarr_cache, "series", None)
        if series_cache is not None:
            try:
                titles = {int(s["id"]): s.get("title", "")
                          for s in series_cache.iter_all_series(instance)
                          if isinstance(s, dict) and s.get("id") is not None}
            except Exception:
                titles = {}
        extra_sids = [sid for sid in group_of if sid not in started and _key(sid) in engaged]
        all_sids = list(dict.fromkeys(list(started) + extra_sids))
        groups = group_members(all_sids, group_of, group_timeline)
        order = order_groups_by_recency(last_by_series, {sid: _key(sid) for sid in all_sids})
        walk: list = []
        for key in order:
            for sid in groups.get(key, {}).get("members", []):
                r = resume_by_sid.get(sid)
                if r is None:                              # unstarted member → synthetic resume @ S1E0
                    r = {"series_id": sid, "series_title": titles.get(sid, ""),
                         "season_number": 1, "episode_number": 0,
                         "watchability_percentile": None, "last_watched_at": None,
                         "keep_policy": None, "certification": None}
                r["_group"] = key
                walk.append(r)
        return walk

    # ══════════════════════════════════════════════════════════════════════════════
    # §9  NEXT-EPISODE COMPUTATION — which episode each series owes the household next
    #     507 lines of pure computation over the frame. Brain primitives already live in
    #     machine_learning/acquisition/next_episode_planner.py (12 symbols imported).
    #     → SonarrEpisodeNextUpManager — or push the computation itself into the brain.
    # ══════════════════════════════════════════════════════════════════════════════

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

        # Group-aware ordering (acquisition.universe.enabled). The walk-plan lists resume rows by
        # SAGA group (members in saga order, + unstarted members of engaged sagas), so a universe/
        # franchise shares ONE budget walked frontier-first. Feature off → group_of empty → every
        # series is its own singleton group → the plan == last_by_series → byte-identical walk.
        group_of, group_timeline = self._universe_group_maps(instance)
        _walk_plan = self._plan_group_walk(last_by_series, group_of, group_timeline, instance)
        _prev_group = None

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
        series_ids = list(dict.fromkeys(int(r["series_id"]) for r in _walk_plan))
        PROGRESS_BAR_THRESHOLD = 10
        use_tqdm = len(series_ids) > PROGRESS_BAR_THRESHOLD
        if use_tqdm and season_ep_cache is not None:
            self._prewarm_by_series_episode_cache(
                instance, series_ids,
                season_ep_cache=season_ep_cache,
                files_session_cache=files_session_cache,
                desc="Episode cache",
            )

        for _cne_i, row in enumerate(_walk_plan, start=1):
            sid          = int(row["series_id"])
            series_title = str(row.get("series_title") or "")
            # Who this next-up is queued FOR — recent household watcher(s), most-recent first
            # (same attribution the JIT grab grid shows). Display-only; never affects the walk.
            _for_cell = ", ".join((_jit_watchers.get(str(sid)) or [])[:2]) or "-"
            _total_series = len(_walk_plan)
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
            MAX_TIME_PER_SERIES = 25.0   # wall-clock seconds before bailing out (per member)
            _series_start = time.time()
            # Group boundary — detected HERE (after the cold-skip `continue` above) so a cold
            # frontier member doesn't consume the saga's lead and strand the next member with an
            # unset budget. The first NON-skipped member of a saga is its frontier.
            _group_lead = row.get("_group") != _prev_group
            _prev_group = row.get("_group")
            # GROUP-level budget + cap + accumulators: computed/reset ONLY on a saga's frontier
            # member (`_group_lead`) and SHARED across the rest of its members, so a universe/
            # franchise prefetches one budget walked frontier-first (finish the current show's
            # next-up, then the next show). A singleton group (feature off) is its own frontier
            # every iteration → this reduces EXACTLY to the legacy per-series budget/cap/reset.
            if _group_lead:
                _ep_cap = episode_cap(
                    series_runtime_s, short_episode_s=SHORT_EPISODE_S, max_ep=MAX_EP_PER_SERIES,
                    graduated=_graduated_cap,
                )
                # Runtime budget (opt-in percentile ramp; multiplier is exactly 1.0 when
                # unconfigured, so series_budget == budget_seconds → byte-identical).
                series_budget = budget_seconds * series_budget_multiplier(
                    row.get("watchability_percentile"), _budget_ramp
                )
                accumulated_s = 0.0
                ep_count = 0
            cur_season, cur_ep = last_season, last_ep   # per-member resume point
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
                                    (f for f in (files_session_cache[sid] or [])
                                     if f.get("id") == ep_fid),
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

    # ══════════════════════════════════════════════════════════════════════════════
    # §10  RETENTION: GRACE, PURGE, INVENTORY — what becomes deletable, and when
    #      Grace expiry is what MARKS a row; §12 is what acts on the mark. The ordering
    #      grace → delete is only visible because these share a file.
    #      → SonarrEpisodeRetentionManager (merges with §6, §12, §14)
    # ══════════════════════════════════════════════════════════════════════════════

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

            # Per-viewer retention: this episode sits inside some account's
            # [position − backward_buffer, position + pace × horizon] interval.
            # Stamped by _apply_viewer_retention (which owns the history + the
            # position sidecar); absent column = legacy frame → not held.
            viewer_protected = False
            if "retention_hold" in df.columns:
                _vh = df.at[idx, "retention_hold"]
                viewer_protected = bool(pd.notna(_vh) and bool(_vh))

            # Household — ACTIVE WATCHERS ONLY (GLD-ACQ-18, decision 2026-08-06):
            # "not all watched" blocks marking only while a member actively watching
            # this series will come upon the episode reasonably soon — which is
            # exactly viewer_protected above (retention's window; dormant accounts
            # have no forward reach). One definition of "approaching", not two. The
            # all-members mandate alone froze near-everything (571 all-watched of
            # 12,637 rows). NaN = legacy/no-household → not blocked.
            household_blocked = False
            if "all_household_watched" in df.columns:
                _ahw = df.at[idx, "all_household_watched"]
                household_blocked = bool(
                    pd.notna(_ahw) and not bool(_ahw) and viewer_protected
                )

            decision = episode_grace_decision(
                is_pilot=is_pilot, is_next=is_next, is_watched=df.at[idx, "is_watched"],
                has_last_watched=bool(lw), fid_protected=fid_protected,
                keep_series=keep_series, keep_season_current=keep_season_current,
                recent_aired=recent_aired, household_blocked=household_blocked,
                viewer_protected=viewer_protected,
            )
            if decision == "clear":     # pilot/next/protected/keep/recent/household — never mark
                df.at[idx, "marked_for_deletion"] = False
                continue
            if decision == "skip":      # watched but no last-watched stamp — leave as-is
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
        Drop cache rows whose Sonarr episode file NO LONGER EXISTS.

        ⚠️ THIS PASS DELETES NOTHING. It is bookkeeping: for every row marked for
        deletion it asks Sonarr whether the file is still there, and drops the row when
        it is not. The file was removed by something ELSE — an operator, a Sonarr
        upgrade replacing it, another tool — and this is glidearr noticing.

        The log wording was previously *"🗑️ Purged … confirmed deleted from Sonarr"*,
        which reads as "we deleted this" — and it prints directly above the
        DELETIONS DISABLED banner, so an operator seeing six of their episodes named
        there reasonably concludes glidearr removed them. It did not, and cannot: the
        actual delete pass (`_do_delete_marked_files`) refuses without consent and says
        so on its own line.

        One API call per unique series (fetches all episode files at once).

        Returns ``(updated_df, stats)``.
        """
        stats = {"checked": 0, "purged": 0, "still_pending": 0}

        if "marked_for_deletion" not in df.columns:
            return df, stats

        pending_mask = df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
        # GLD-DEL-09 — WIDENED beyond `marked_for_deletion`. That scope was the BBT S3
        # `files=6` gap: a file deleted outside glidearr on an actively-watched series
        # was never marked, so nothing ever asked Sonarr about it and the row stayed
        # "owned" forever, counting bytes that were already gone. The drift detector
        # measured the backlog at 194 orphans on 2026-08-24 (gate 102), converging to
        # 102 after two re-syncs — a tail that re-syncing alone will never clear,
        # because a fresh cache confirms the file is gone without removing the row.
        #
        # Only rows that CLAIM a file are added: a row with no `episode_file_id` is a
        # watched stub, not an orphan, and the marked-row branch below still handles
        # those under its own (narrower) semantics.
        if "episode_file_id" in df.columns:
            owns_file = df["episode_file_id"].notna()
            pending_mask = pending_mask | owns_file
        if not pending_mask.any():
            return df, stats

        # One episodefile API call per series
        series_ids = df.loc[pending_mask, "series_id"].dropna().unique()
        # GLD-DEL-09 — BOUND THE FIRST RUNS. Widening the mask turns this from a
        # handful of marked rows into every file-owning series (5,258 on the measured
        # library), and each is an API call. The cap makes the backlog drain over
        # several nights instead of hammering Sonarr once, and it means a mistake in
        # this pass costs a bounded number of rows rather than the whole cache.
        # Series are taken in a stable order so the drain is deterministic rather
        # than re-checking the same head every night.
        _cap = int((self.config or {}).get("purge_max_series_per_run", 250) or 250)
        if len(series_ids) > _cap:
            series_ids = sorted(int(s) for s in series_ids)[:_cap]
            self.logger.log_debug(
                f"  purge scan capped at {_cap} series this run — the rest drain next run.")
        live_file_ids: dict[int, set | None] = {}
        for sid in series_ids:
            try:
                files = self._get_episode_files(instance, int(sid))
                # None means the fetch FAILED — leave every row for this series
                # alone. An empty list means Sonarr genuinely holds no files and
                # the rows really are stale. Conflating them (GLD-DEL-09, P-C) is
                # how one 500 becomes a mass row deletion.
                live_file_ids[int(sid)] = (
                    None if files is None
                    else {f.get("id") for f in files if f.get("id")})
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
            if live is None:  # fetch FAILED or series not scanned this run — leave
                stats["still_pending"] += 1
                continue
            _marked = bool(df.at[idx, "marked_for_deletion"]) \
                if "marked_for_deletion" in df.columns else False
            title  = df.at[idx, "series_title"] or f"series {sid}"
            # NaN-safe. `int(x or 0)` does NOT work here: NaN is TRUTHY, so `NaN or 0`
            # returns NaN and int(NaN) raises. Latent while this pass only saw
            # `marked_for_deletion` rows (which always carry season/episode); widening
            # the mask to every file-owning row surfaced it immediately, and an
            # uncaught ValueError here aborts the whole Tautulli sync.
            _s_raw, _e_raw = df.at[idx, "season_number"], df.at[idx, "episode_number"]
            s_num  = int(_s_raw) if pd.notna(_s_raw) else 0
            e_num  = int(_e_raw) if pd.notna(_e_raw) else 0
            if pd.isna(fid):
                # Orphan: marked for deletion but never had a Sonarr file id
                # (e.g. a watched stub). Nothing to confirm — drop as cleanup.
                # ONLY for marked rows: an unmarked row without a file id is a
                # legitimate stub (pilot placeholder, next-up), not an orphan.
                if not _marked:
                    stats["still_pending"] += 1
                    continue
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
                    f"  🧹 Dropped stale row: '{title}' S{s_num:02d}E{e_num:02d} "
                    f"(file {int(fid)} no longer exists in Sonarr - removed elsewhere, "
                    f"not by glidearr)"
                )
            else:
                stats["still_pending"] += 1

        if drop_indices:
            df = df.drop(index=drop_indices).reset_index(drop=True)
            self.logger.log_info(
                f"🧹 Stale-row cleanup: {stats['purged']} row(s) dropped from the cache "
                f"(their files are already gone from Sonarr), "
                f"{stats['still_pending']} still present and awaiting removal. "
                f"NO FILES WERE DELETED BY THIS PASS."
            )

        return df, stats

    def _cold_cfg(self) -> dict:
        """``cold_tv_reclaim`` — GLD-ACQ-24, DEFAULT OFF. Feeds cold unwatched TV bulk
        into the SAME marked → guards → coordinator flow the watched lifecycle uses —
        the coordinator stays the sole delete decider (no second delete path). Keys:
        ``enabled`` (False), ``score_floor`` (20 — matches the pilot-search gate),
        ``min_owned_days`` (90), ``grace_days`` (30 — the two-stage visibility window,
        ported from the movie stale-prune as a grace window instead of a parallel
        path), ``max_series_per_run`` (25 — bounds first-enable fetch cost)."""
        return ((self.config or {}).get("cold_tv_reclaim", {}) or {})

    @timeit("_ingest_cold_inventory")
    def _ingest_cold_inventory(
        self, df: pd.DataFrame, instance: str,
        *, season_ep_cache: dict | None = None,
        files_session_cache: dict | None = None,
    ) -> pd.DataFrame:
        """GLD-ACQ-24 — make cold unwatched TV reachable by the delete flow.

        Unwatched non-pilot episodes have NO parquet rows (rows come from the watch
        sync, the pilot batch, and next-up stubs), so a never-watched 100-episode
        pile is invisible to grace marking, to ``build_delete_candidates``, and to
        the coordinator — unreclaimable by every pass. This ingests them
        DELIBERATELY, flagged ``row_origin='cold_scan'`` so no consumer ever
        mistakes them for watch-derived rows, and runs a self-contained two-stage
        lifecycle on them:

          1. INGEST — for series whose pilot-row ``watchability_score`` sits below
             ``score_floor`` and that own files: every owned, unwatched, non-pilot
             episode file older than ``min_owned_days`` becomes a flagged row via
             the SAME ``_resolve_episode_file`` + ``_normalise`` path the watch sync
             uses (one row builder, no duplicate), stamped with the series score
             (so ``build_delete_candidates`` never defers it as unscored) and
             ``available_until = now + grace_days`` — the visibility window.
          2. MARK — a cold row whose window has expired AND whose series is still
             below the floor flips ``marked_for_deletion=True``; the existing
             guard cascade (pilot / keep / recent-air / retention / watchlist /
             universe) and the coordinator's one ranked pool decide from there.
          3. RELEASE — if the series' score has climbed to/above the floor, cold
             rows are unmarked and their window cleared: the show earned its keep
             and, if it cools again later, the window restarts from scratch. This
             is the re-acquirability property the operator chose this design for.

        Deliberately NOT threaded through ``episode_grace_decision`` — that policy
        is watched-anchored, and bending it around unwatched rows is the
        guard-narrower-than-it-appears shape (§8 P-B). The lifecycle here is three
        explicit steps with its own log line instead.

        Byte-identical when ``cold_tv_reclaim.enabled`` is unset/false.
        """
        cfg = self._cold_cfg()
        if not cfg.get("enabled"):
            return df
        try:
            floor = float(cfg.get("score_floor", 20))
            min_days = int(cfg.get("min_owned_days", 90))
            grace_days = int(cfg.get("grace_days", 30))
            cap = int(cfg.get("max_series_per_run", 25))
        except (TypeError, ValueError):
            floor, min_days, grace_days, cap = 20.0, 90, 30, 25

        now = datetime.now(tz=timezone.utc)
        now_iso = now.isoformat()
        stats = {"ingested": 0, "marked": 0, "released": 0, "rewindowed": 0, "series": 0}

        # ── Per-series score + keep from the PILOT rows (every series has one) ────
        _sid = pd.to_numeric(df.get("series_id"), errors="coerce")
        _score = pd.to_numeric(df.get("watchability_score"), errors="coerce")
        _pilot = (df["is_pilot"].infer_objects(copy=False).fillna(False).astype(bool)
                  if "is_pilot" in df.columns else pd.Series(False, index=df.index))
        series_score: dict = {}
        for i in df.index[_pilot]:
            s = _sid.at[i]
            if pd.notna(s) and pd.notna(_score.at[i]):
                series_score[int(s)] = float(_score.at[i])

        # ── 2+3. MARK expired / RELEASE recovered on EXISTING cold rows ─────────
        if "row_origin" in df.columns:
            cold_idx = df.index[df["row_origin"] == "cold_scan"]
            for i in cold_idx:
                s = _sid.at[i]
                sc = series_score.get(int(s)) if pd.notna(s) else None
                if sc is not None and sc >= floor:
                    if bool(df.at[i, "marked_for_deletion"]) or pd.notna(df.at[i, "available_until"]):
                        df.at[i, "marked_for_deletion"] = False
                        df.at[i, "available_until"] = None
                        stats["released"] += 1
                    continue
                _au = df.at[i, "available_until"] if "available_until" in df.columns else None
                if not bool(df.at[i, "marked_for_deletion"]):
                    if pd.notna(_au) and _au:
                        try:
                            if pd.to_datetime(_au, utc=True) <= now:
                                df.at[i, "marked_for_deletion"] = True
                                stats["marked"] += 1
                        except Exception:
                            # A CORRUPT available_until leaves the row unmarked, which is
                            # the SAFE direction (the file is kept). But silently: the row
                            # can never become deletable, on any future run, and nothing
                            # says so -- space that will never be reclaimed for a reason
                            # nobody can see. Count it so the grace summary can name it.
                            # (P-D: failure with no detector -- the shape that hid
                            # GLD-SON-25's 69 dead pointers for weeks.)
                            stats["bad_available_until"] = stats.get("bad_available_until", 0) + 1
                    else:
                        # WINDOW RESTART: cold again after a score-recovery release
                        # (available_until was cleared) — stamp a FRESH visibility
                        # window rather than exempting the row forever. This is the
                        # "restarts from scratch" half of the release contract; a
                        # release without it would be a permanent exemption.
                        df.at[i, "available_until"] = (
                            now + timedelta(days=grace_days)).isoformat()
                        stats["rewindowed"] += 1

        # ── 1. INGEST new cold rows (capped per run; idempotent via (sid,sn,en)) ──
        existing: set = set()
        _sn_all = pd.to_numeric(df.get("season_number"), errors="coerce")
        _en_all = pd.to_numeric(df.get("episode_number"), errors="coerce")
        for i in df.index:
            if pd.notna(_sid.at[i]) and pd.notna(_sn_all.at[i]) and pd.notna(_en_all.at[i]):
                existing.add((int(_sid.at[i]), int(_sn_all.at[i]), int(_en_all.at[i])))

        keep_sids: set = set()
        if "keep_policy" in df.columns:
            _kp = df["keep_policy"]
            keep_sids = {int(s) for s, k in zip(_sid, _kp)
                         if pd.notna(s) and k in ("keep_series", "keep_season")}

        cold_sids = [sid for sid, sc in series_score.items()
                     if sc < floor and sid not in keep_sids]
        # FILE-BULK SELECTION (first-run lesson: the pilot-fid "has a file" proxy
        # spent the whole per-run cap on PILOT-ONLY series — cold sub-floor series
        # are exactly the ones the pilot sampler grabbed one file for, so 25 slots
        # yielded 0 rows). Use the series cache's episodeFileCount instead: require
        # ≥2 files (pilot + something reclaimable) and walk FATTEST-FIRST so the cap
        # goes to DBZ-shaped piles — the operator's stated target — not one-file
        # samples. Falls back to the old pilot-fid proxy when statistics are absent.
        _fc: dict = {}
        try:
            _sc_mgr = getattr(self.sonarr_cache, "series", None)
            for s in (_sc_mgr.iter_all_series(instance) if _sc_mgr else []):
                if isinstance(s, dict) and s.get("id") is not None:
                    _n = ((s.get("statistics") or {}).get("episodeFileCount"))
                    if _n is not None:
                        _fc[int(s["id"])] = int(_n)
        except Exception:
            _fc = {}
        if _fc:
            cold_sids = sorted(
                (s for s in cold_sids if _fc.get(s, 0) >= 2),
                key=lambda s: -_fc.get(s, 0))[:max(0, cap)]
        else:
            _has_file_sids = {int(s) for i, s in _sid.items()
                              if pd.notna(s) and _pilot.at[i]
                              and pd.notna(df.at[i, "episode_file_id"])}
            cold_sids = [s for s in cold_sids if s in _has_file_sids][:max(0, cap)]

        new_rows: list = []
        title_by_sid: dict = {}
        if "series_title" in df.columns:
            for i in df.index[_pilot]:
                s = _sid.at[i]
                if pd.notna(s):
                    title_by_sid[int(s)] = df.at[i, "series_title"]
        for sid in cold_sids:
            try:
                seasons = self._get_all_episodes(
                    instance, sid, season_ep_cache, files_session_cache,
                    log_miss=False, log_expired=False)
            except Exception:
                continue
            stats["series"] += 1
            title = title_by_sid.get(sid, f"series {sid}")
            for sn, eps in (seasons or {}).items():
                for ep in eps or []:
                    en = ep.get("episodeNumber")
                    if sn in (None, 0) or en is None:
                        continue          # skip specials + malformed
                    if not ep.get("hasFile") or not ep.get("episodeFileId"):
                        continue
                    if (sid, int(sn), int(en)) in existing:
                        continue          # watched / pilot / stub / already ingested
                    file_rec, _, air_utc = self._resolve_episode_file(
                        instance, sid, int(sn), int(en),
                        files_session_cache, season_ep_cache)
                    if not file_rec:
                        continue
                    _da = file_rec.get("dateAdded")
                    try:
                        if _da and (now - pd.to_datetime(_da, utc=True)).days < min_days:
                            continue      # too fresh to call cold
                    except Exception:
                        pass
                    row = self._normalise(
                        raw=file_rec, series_id=sid, series_title=title,
                        season_number=int(sn), episode_number=int(en),
                        is_pilot=False, watch_count=0, last_watched_at=None,
                        percent_complete=0, air_date_utc=air_utc,
                        all_household_watched=False,
                        household_last_watched_at=None,
                    )
                    row["row_origin"] = "cold_scan"
                    row["is_watched"] = False
                    row["watchability_score"] = series_score.get(sid)
                    row["available_until"] = (now + timedelta(days=grace_days)).isoformat()
                    row["marked_for_deletion"] = False
                    row["last_synced_at"] = now_iso
                    new_rows.append(row)
                    existing.add((sid, int(sn), int(en)))
                    stats["ingested"] += 1

        if new_rows:
            df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)

        if any(stats.values()):
            self.logger.log_info(
                f"❄️ [ColdTV] '{instance}': {stats['ingested']} cold row(s) ingested "
                f"across {stats['series']} series (score<{floor:g}, owned>{min_days}d), "
                f"{stats['marked']} window-expired row(s) marked, {stats['released']} "
                f"released (score recovered), {stats['rewindowed']} re-windowed "
                f"(cooled again after release). Window {grace_days}d; cap {cap} series/run; "
                f"deletion still decided by the coordinator's ranked pool.")
        # Surfaced SEPARATELY and as a WARNING, not folded into the line above: these
        # rows are stuck. An unparseable available_until means the row can never reach
        # its window end, on this run or any future one, so the space it holds is
        # unreclaimable until someone looks. Folding it into the info line would make a
        # permanent condition read like a per-run statistic.
        if stats.get("bad_available_until"):
            self.logger.log_warning(
                f"❄️ [ColdTV] '{instance}': {stats['bad_available_until']} row(s) have an "
                f"unparseable available_until — they were left UNMARKED (the safe direction, "
                f"the file is kept) but they can never expire, so their space is "
                f"unreclaimable until the value is repaired or cleared.")
        return df

    # ---- GLD-INV-01: inventory is not the same question as reclaim -------------
    def _inventory_cfg(self) -> dict:
        """``inventory_scan`` config with safe defaults (disabled unless asked for)."""
        raw = {}
        try:
            got = (self.config or {}).get("inventory_scan")
            if isinstance(got, dict):
                raw = got
        except (AttributeError, TypeError):
            raw = {}
        def _i(k, d):
            try:
                v = int(raw.get(k, d))
                return v if v > 0 else int(d)
            except (TypeError, ValueError):
                return int(d)
        return {"enabled": bool(raw.get("enabled", False)),
                "max_series_per_run": _i("max_series_per_run", 25),
                "min_episodes": _i("min_episodes", 2),
                "rescan_days": _i("rescan_days", 30)}

    @timeit("_ingest_inventory_tv")
    def _ingest_inventory_tv(self, df, instance, season_ep_cache, files_session_cache):
        """Enumerate owned episodes for series nothing else enumerates - GLD-INV-01.

        THE GAP. Three passes write rows: the watch sync (Tautulli-driven), the pilot
        batch, and next-up stubs. A series NOBODY HAS WATCHED gets only a pilot row --
        one representative file from ``_pick_representative_file``, ``episode_number=None``,
        intended as a codec/quality FINGERPRINT. The space planner then reads that row's
        ``size_bytes`` and ``resolution`` as if they described the series. Observed live:
        Marvel's Daredevil, 39 episodes of 4K HDR on disk per Plex, represented by one
        0.4 GB 480p row; across the library 12,075 pilot rows carry 5,160 GB of
        single-file sizes standing in for whole series.

        WHY ``_ingest_cold_tv`` DOESN'T COVER IT, AND WHY WIDENING IT WOULD BE WRONG.
        That pass already enumerates exactly the right thing, but it is gated on
        ``score_floor`` (20) because its purpose is DELETE reachability -- it ingests only
        series it might want to remove. The five Netflix Marvel shows score 24-43, so they
        are deliberately skipped. Lowering that floor would make good series delete-eligible
        to fix a SIZE-VISIBILITY problem: enumeration and delete-eligibility are different
        questions, and coupling them is what hid this.

        So this pass ingests regardless of score and is structurally incapable of causing a
        deletion: rows are stamped ``row_origin='inventory_scan'`` with
        ``marked_for_deletion=False`` and ``available_until=None``, and unlike cold rows
        they carry no window that could ever expire into a mark. They exist to be SEEN --
        by the step-down ladder, the size-anomaly pass, and plan_reclaim_gb.

        Reuses ``_resolve_episode_file`` + ``_normalise`` (one row builder, no duplicate --
        P-E) and skips any (series, season, episode) that already has a row, so it can never
        double-count against the watch sync or the cold scan. Capped per run like its twin;
        byte-identical when ``inventory_scan.enabled`` is unset.
        """
        cfg = self._inventory_cfg()
        if not cfg["enabled"] or df is None or df.empty or "is_pilot" not in df.columns:
            return df
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        _sid = pd.to_numeric(df.get("series_id"), errors="coerce")
        _pilot = df["is_pilot"].infer_objects(copy=False).fillna(False).astype(bool)
        _epno = pd.to_numeric(df.get("episode_number"), errors="coerce")

        # Series that have a pilot row but NO real episode rows: exactly the population
        # nothing enumerates. A series the watch sync or cold scan already reached has
        # real rows and is left alone.
        have_real, title_by_sid, score_by_sid, keep_by_sid, owns_by_sid = set(), {}, {}, {}, {}
        for i in df.index:
            s = _sid.at[i]
            if pd.isna(s):
                continue
            s = int(s)
            if _pilot.at[i]:
                title_by_sid[s] = df.at[i, "series_title"]
                score_by_sid[s] = df.at[i, "watchability_score"]
                if "keep_policy" in df.columns:
                    keep_by_sid[s] = df.at[i, "keep_policy"]
                # A pilot row with no file facts is a METADATA-ONLY pilot — the series
                # owns nothing, so enumerating it can never yield a row. 7,098 of the
                # 12,075 pilot series are in this bucket; without this filter they sit
                # at the FRONT of the target order and the capped walk re-examines them
                # every run before reaching anything productive.
                try:
                    _sz = df.at[i, "size_bytes"] if "size_bytes" in df.columns else None
                    owns_by_sid[s] = bool(pd.notna(_sz) and float(_sz) > 0)
                except (TypeError, ValueError):
                    owns_by_sid[s] = False
            elif pd.notna(_epno.at[i]):
                have_real.add(s)
        # THE WALK MUST REMEMBER WHERE IT HAS BEEN. A productive series drops out of the
        # pool naturally (it gains real rows), but an examined series that yielded fewer
        # than min_episodes writes NOTHING — so without a marker it is re-examined every
        # run, and the capped walk advances only by last run's productive count. Observed
        # live 2026-08-14: the second pass re-walked the same ~1,000 thin targets, moved
        # ~59 positions deeper, ingested 0, and logged nothing. The marker is scan
        # PROGRESS, not action state, so it is written even in dry_run, and it expires
        # (rescan_days) so a series that acquires files later is not hidden forever.
        scan_key = f"sonarr/{instance}/inventory_scanned"
        try:
            scanned = dict(self.global_cache.get(scan_key) or {})
        except Exception:
            scanned = {}
        _cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=cfg["rescan_days"])).isoformat()

        eligible = [s for s in title_by_sid if s not in have_real and owns_by_sid.get(s)]
        skipped_recent = sum(1 for s in eligible if str(scanned.get(str(s), "")) >= _cutoff)
        targets = [s for s in eligible
                   if not str(scanned.get(str(s), "")) >= _cutoff][: cfg["max_series_per_run"]]
        if not targets:
            self.logger.log_info(
                f"\U0001f4e6 [InventoryTV] '{instance}': target pool exhausted — every "
                f"file-owning pilot series is enumerated or was scanned within "
                f"{cfg['rescan_days']}d ({skipped_recent} deferred). Nothing to do.")
            return df

        existing = set()
        _sn = pd.to_numeric(df.get("season_number"), errors="coerce")
        for i in df.index:
            s, sn, en = _sid.at[i], _sn.at[i], _epno.at[i]
            if pd.notna(s) and pd.notna(sn) and pd.notna(en):
                existing.add((int(s), int(sn), int(en)))

        new_rows, stats = [], {"ingested": 0, "series": 0, "gb": 0.0}
        examined = 0
        for sid in targets:
            examined += 1
            scanned[str(sid)] = now_iso
            try:
                seasons = self._get_all_episodes(
                    instance, sid, season_ep_cache, files_session_cache,
                    log_miss=False, log_expired=False)
            except Exception:
                continue
            found, series_gb = 0, 0.0
            for sn, eps in (seasons or {}).items():
                for ep in eps or []:
                    en = ep.get("episodeNumber")
                    if sn in (None, 0) or en is None:
                        continue                       # specials + malformed
                    if not ep.get("hasFile") or not ep.get("episodeFileId"):
                        continue
                    if (sid, int(sn), int(en)) in existing:
                        continue
                    file_rec, _, air_utc = self._resolve_episode_file(
                        instance, sid, int(sn), int(en), files_session_cache, season_ep_cache)
                    if not file_rec:
                        continue
                    row = self._normalise(
                        raw=file_rec, series_id=sid, series_title=title_by_sid.get(sid, f"series {sid}"),
                        season_number=int(sn), episode_number=int(en),
                        is_pilot=False, watch_count=0, last_watched_at=None,
                        percent_complete=0, air_date_utc=air_utc,
                        all_household_watched=False, household_last_watched_at=None)
                    row["row_origin"] = "inventory_scan"
                    row["is_watched"] = False
                    row["watchability_score"] = score_by_sid.get(sid)
                    row["keep_policy"] = keep_by_sid.get(sid)
                    # NO available_until: a cold row's window expires into a mark, and this
                    # pass must never be able to reach that state.
                    row["available_until"] = None
                    row["marked_for_deletion"] = False
                    row["last_synced_at"] = now_iso
                    new_rows.append(row)
                    existing.add((sid, int(sn), int(en)))
                    series_gb += float(file_rec.get("size") or 0) / (1024 ** 3)
                    found += 1
            if found >= cfg["min_episodes"]:
                stats["series"] += 1
                stats["ingested"] += found
                stats["gb"] += series_gb   # KEPT rows only — the per-row accumulator once
                                           # counted trimmed rows too, so the first live
                                           # log claimed 970.8 GB while 591.5 GB landed.
            elif found:
                # Below min_episodes: drop them again rather than leave a second partial
                # fingerprint alongside the pilot row.
                new_rows = new_rows[:-found]

        if new_rows:
            df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
        try:
            self.global_cache.set(scan_key, scanned)
        except Exception:
            pass
        # Logged UNCONDITIONALLY while enabled: a silent pass is ambiguous between
        # "disabled", "pool empty", and "examined plenty, kept nothing" — and that
        # ambiguity cost a diagnosis round when the second pass ingested 0 silently.
        self.logger.log_info(
            f"\U0001f4e6 [InventoryTV] '{instance}': {stats['ingested']} owned episode(s) "
            f"across {stats['series']} series made visible ({stats['gb']:.1f} GB kept); "
            f"examined {examined} of {len(eligible)} eligible (cap "
            f"{cfg['max_series_per_run']}, {skipped_recent} deferred as recently scanned); "
            f"~{max(0, len(eligible) - skipped_recent - examined)} eligible series remain. "
            f"Never delete-eligible.")
        return df

    # Row origins that are FEATURE STATE, not orphans. Every deliberately-ingested,
    # unwatched origin must join this set or _do_cleanup_non_essential culls it in the
    # same run its feature creates it — observed live 2026-08-13 with 'inventory_scan'
    # (GLD-INV-01): 989 rows / 970.8 GB ingested, gone before the parquet write, while
    # the InventoryTV log reported success. A string-equality test against ONE origin is
    # how that slipped through; the set makes the next origin a one-line opt-in.
    _PROTECTED_ROW_ORIGINS = frozenset({"cold_scan", "inventory_scan"})

    @timeit("_do_cleanup_non_essential")
    def _do_cleanup_non_essential(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """
        Drop genuine orphan rows that carry no actionable value.

        Keep:
        * Pilots (``is_pilot=True``) — codec / quality fingerprint.
        * Next-episode rows (``next_episode=True``) — ingestion target.
        * Deliberately-ingested origins (``row_origin`` in ``_PROTECTED_ROW_ORIGINS``):
          ``'cold_scan'`` (GLD-ACQ-24) carries the cold-reclaim lifecycle;
          ``'inventory_scan'`` (GLD-INV-01) IS the owned inventory for never-watched
          series. Inventory rows match the orphan profile on every watch-derived axis
          (unwatched, no window, never marked) — that is exactly why the exemption is
          by ORIGIN, not by lifecycle state. Removing either silently disables its
          feature every run.
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
        # Deliberately-ingested rows are unwatched BY DEFINITION — without this
        # exemption the cleanup deletes each feature's own state every run.
        is_protected = (df["row_origin"].isin(self._PROTECTED_ROW_ORIGINS)
                        if "row_origin" in df.columns
                        else pd.Series(False, index=df.index))

        keep_mask = is_pilot | is_next | is_watched | is_protected
        removed   = int((~keep_mask).sum())

        if removed:
            df = df[keep_mask].reset_index(drop=True)
            self.logger.log_info(
                f"🧹 Non-essential cleanup: {removed} orphaned row(s) removed "
                "(not pilot, not next-episode, never watched, no protected origin)."
            )

        return df, removed

    @timeit("_resolve_keep_policy_map")
    def _resolve_keep_policy_map(
        self, instance: str, df: pd.DataFrame
    ) -> dict[int, str | None]:
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

    # ══════════════════════════════════════════════════════════════════════════════
    # §11  ACQUISITION & RECYCLE — acquire the next episode, recycle watched to fund it
    #      `_recycle_to_fund_acquisition` is a DELETE path, but a different act from §12:
    #      "I finished this episode, spend it on the next one" — own consent, own gate.
    #      → SonarrEpisodeAcquireManager
    # ══════════════════════════════════════════════════════════════════════════════

    @timeit("_do_acquire_next_episodes")
    def _do_acquire_next_episodes(
        self, instance: str, df: pd.DataFrame, *, season_ep_cache: dict | None = None
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
            # SELF-FUNDING ("leapfrog") RECYCLE, before giving up. A blocked prefetch is why
            # a stay-ahead library never gets ahead: these queued episodes are the NEXT ones
            # for series the household is actively watching (recency_gate walks hottest
            # first), and refusing them leaves "Up Next" offering pilots of shows nobody has
            # started. Recycling an episode the household has ALREADY WATCHED to fund the
            # next one is net <= 0 on space, so the floor this gate protects is never
            # breached. Returns the (sid, sn, en) coords now funded; empty = skip stands.
            _funded = self._recycle_to_fund_acquisition(
                instance, df, pending_mask, free_gb, acquire_floor, stats)
            if not _funded:
                self.logger.log_info(
                    f"📦 Acquisition skipped for '{instance}': {free_gb:.1f} GB free "
                    f"< {acquire_floor:.0f} GB band top (in the space-pressure band). "
                    f"{int(pending_mask.sum())} episode(s) remain queued."
                )
                stats["checked"] = int(pending_mask.sum())
                return stats
            # Narrow the pending set to exactly what the recycle paid for. Everything else
            # stays queued for a later run -- a funded acquisition is the ONLY reason this
            # pass proceeds past the floor at all.
            _coord = pd.Series(
                [(df.at[i, "series_id"], df.at[i, "season_number"], df.at[i, "episode_number"])
                 for i in df.index], index=df.index)
            pending_mask = pending_mask & _coord.map(lambda c: c in _funded)
            if not pending_mask.any():
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

            # One season fetch per unique season for this series — read through the
            # run-scoped session cache when the pipeline hands one over, so seasons
            # already fetched by _compute_next_episodes seconds earlier are free.
            ep_map: dict[tuple[int, int], dict] = {}  # (sn, en) → sonarr ep obj
            for sn in seasons_needed:
                try:
                    for ep_obj in self._get_episodes_for_season(instance, sid, sn, season_ep_cache):
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

    def _recycle_cfg(self) -> dict:
        """``acquisition.next_episode.recycle_watched`` — DEFAULT OFF, and gated on its OWN
        consent, deliberately NOT on ``deletions_consent``.

        These are different acts. General deletion is *"the disk is full, give something
        up"*; a leapfrog recycle is *"I finished this episode, spend it on the next one"* —
        no title is lost, nothing the household still wants disappears, and the library does
        not shrink. An operator can reasonably want rolling-window recycling with general
        deletion firmly off, and folding them together makes that impossible.
        ``relocation_consent`` is separate from ``deletions_consent`` for the same reason.
        """
        return (((self.config or {}).get("acquisition", {}) or {})
                .get("next_episode", {}) or {}).get("recycle_watched", {}) or {}

    # Source type assumed for a tier the series owns no sample of. WEBDL is the most
    # common real grab source in this library, and it sits mid-table rather than at either
    # extreme (WEBDL-1080p 55.7 MiB/min, against Bluray-1080p 65.3 and Remux-1080p 235.2) --
    # so an unknown tier is neither wildly over- nor under-funded.
    _TIER_FALLBACK_QUALITY = {2160: "WEBDL-2160p", 1080: "WEBDL-1080p",
                              720: "WEBDL-720p", 480: "WEBDL-480p"}

    def _series_tier_estimates(self, rows, measured: dict) -> dict:
        """``{resolution: estimated GB}`` for this series' NEXT episode, from the
        library-calibrated size model rather than a per-series median.

        WHY NOT THE SERIES' OWN MEDIAN FILE SIZE (the first version of this): a series with
        only a pilot has one sample, and a series mid-upgrade has samples from two different
        tiers averaged together. The size model already answers this properly, from ~6,900
        measured files, and it is ALREADY IMPORTED at the top of this module.

        THREE INPUTS, and each one separates a case a flat table cannot:

          * RUNTIME (median of the series' own files) -- a 24-minute anime episode and a
            60-minute drama at the same quality differ by 2.5x. This is the single biggest
            term and it is per-series by construction.
          * CODEC (this series' modal ``video_codec``) -- ``mb_per_min`` prefers a
            codec-qualified rate (``"Bluray-1080p@h265"``) when the library has measured
            one. HEVC/VP9/AV1 run 30-50% smaller than H.264 at the same resolution, which
            is exactly the anime-vs-live-action gap: x265 10-bit fansub against an x264
            drama. Without this, every anime tier is over-funded and every live-action one
            under-funded.
          * SOURCE TYPE -- the tier name is derived from the series' OWN dominant quality by
            swapping the resolution token (``Bluray-720p`` -> ``Bluray-1080p``), so a
            Bluray-sourced series is not priced as if it will arrive as a WEB rip.

        ``measured`` is the caller's per-instance ``measured_mb_per_min`` map (computed
        ONCE per instance, not per series) and wins over the calibrated table wherever the
        library has real samples for that quality (and codec).
        """
        out: dict = {}
        if rows is None or getattr(rows, "empty", True):
            return out

        # Runtime: this series' own median, in minutes. Falls back to 45 -- the same
        # DEFAULT_RUNTIME_S the prefetch walk uses, so the two agree about an unknown show.
        _rt = pd.to_numeric(rows.get("runtime_seconds"), errors="coerce").dropna()
        rt_min = float(_rt.median()) / 60.0 if len(_rt) and _rt.median() > 0 else 45.0

        # Modal codec + modal quality name across this series' owned files.
        def _modal(col):
            if col not in rows.columns:
                return None
            vc = rows[col].dropna().value_counts()
            return str(vc.index[0]) if len(vc) else None

        codec = _modal("video_codec")
        base_q = _modal("quality_name")

        for res, fallback_q in self._TIER_FALLBACK_QUALITY.items():
            qname = fallback_q
            if base_q:
                # Swap the resolution token in the series' own quality name, so source type
                # (Bluray / WEBDL / HDTV / Remux) is preserved across the tier change.
                for _r in ("2160p", "1080p", "720p", "576p", "480p"):
                    if _r in base_q:
                        qname = base_q.replace(_r, f"{res}p")
                        break
            gb = estimate_gb(qname, rt_min, 1, measured, resolution=res, codec=codec)
            if gb > 0:
                out[res] = gb
        return out

    @timeit("_recycle_to_fund_acquisition")
    def _recycle_to_fund_acquisition(self, instance, df, pending_mask,
                                     free_gb, acquire_floor, stats) -> set:
        """Delete already-watched episodes to fund the blocked next-up acquisitions.

        Returns the set of ``(series_id, season_number, episode_number)`` coords that are
        now paid for. Empty set = nothing was recycled and the caller's skip stands.

        The DECISION is
        :func:`machine_learning.acquisition.recycle_planner.plan_recycle` (pure, 19 tests);
        this method only resolves its inputs and applies the result. Two passes there:
        price the advance against the whole eligible pool, then delete only what that
        advance costs.

        GUARDS, beyond the planner's own five:
          * ``_build_protected_file_ids`` — the SAME whole-file guard set the delete pass
            uses (pilot / keep_series / keep_season / recent-air / household — active
            watchers approaching only, GLD-ACQ-18 / retention / watchlist). A recycle can never remove something the delete path
            itself would refuse to touch.
          * ``all_household_watched`` — REMOVED. It required every configured member to
            have watched, which with six members is almost never true and never will be:
            Mom is not going to watch Blue Bloods. ``retention_hold`` (already inside
            ``_build_protected_file_ids``) answers the real question per VIEWER — is anyone
            still walking toward this episode? — so a series two people are mid-way through
            stays protected while the five only one person watches become recyclable.
            See GLD-ACQ-18.
          * ``effective_dry_run`` — honours both the run's dry_run AND the backup gate.

        DELETE FIRST, THEN ACQUIRE. The inverse of the 4K path's make-before-break, and
        correct here for the opposite reason: the episode is already CONSUMED, so losing it
        costs nothing — while acquiring first would breach the very floor this gate exists
        to protect.
        """
        from scripts.managers.machine_learning.acquisition.recycle_planner import (
            DEFAULT_REWATCH_BUFFER,
            DEFAULT_SIZE_TOLERANCE,
            plan_recycle,
        )

        cfg = self._recycle_cfg()
        # DIAGNOSTIC SCAFFOLD. Four inferences about why this returns empty have now been
        # wrong (household mandate, a never-populated column, keep-tag ordering, tier
        # pricing). Every one of them was a plausible reading of the code that the data then
        # refuted. So: make the method SAY where it exits instead of leaving the caller to
        # infer it from a missing log line. One run of this costs less than another round of
        # reasoning -- the same lesson GLD-TRT-01 taught.
        def _bail(why: str) -> set:
            self.logger.log_info(f"[Recycle] '{instance}': no plan - {why}")
            return set()

        if not cfg.get("enabled"):
            return _bail("recycle_watched.enabled is false")
        if not cfg.get("consent"):
            return _bail("recycle_watched.consent is false")
        if self.sonarr_api is None:
            return _bail("no sonarr_api")
        if "episode_file_id" not in df.columns:
            return _bail("frame has no episode_file_id column")
        try:
            buffer_n = int(cfg.get("rewatch_buffer", DEFAULT_REWATCH_BUFFER))
            tol = float(cfg.get("size_tolerance", DEFAULT_SIZE_TOLERANCE))
        except (TypeError, ValueError):
            buffer_n, tol = DEFAULT_REWATCH_BUFFER, DEFAULT_SIZE_TOLERANCE

        now = datetime.now(tz=timezone.utc)
        try:
            # GLD-ACQ-22: build the per-guard breakdown ONCE and derive the flat set
            # from it — one mask source, so the reason line can never disagree with
            # the guard that refused.
            _prot_reasons = self._build_protected_file_reasons(df, now)
            protected = (frozenset().union(*_prot_reasons.values())
                         if _prot_reasons else frozenset())
        except Exception as e:
            # FAIL SAFE, exactly as the delete pass does: no guard set -> recycle nothing.
            self.logger.log_error(
                f"[Recycle] protected-file-id build failed for '{instance}'; recycling "
                f"NOTHING this cycle (fail-safe): {e}")
            return set()

        def _series_guard_counts(rows, reasons) -> dict:
            """GLD-ACQ-22 — ``{guard: n}`` of THIS series' owned fids per protecting
            guard, sorted desc; a fid may count under several guards (overlap is real).
            The number that finally names the blocker instead of lumping seven guards
            into one 'protected' count."""
            _sf = ({int(f) for f in rows["episode_file_id"].dropna()}
                   if "episode_file_id" in rows.columns else set())
            _c = {g: len(_sf & s) for g, s in (reasons or {}).items() if _sf & s}
            return dict(sorted(_c.items(), key=lambda kv: -kv[1]))

        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        # Library-measured MiB/min per quality AND per quality@codec, computed ONCE for the
        # instance. Wins over the calibrated cold-start table wherever this library has real
        # samples -- which for the common tiers is over a thousand files each.
        _measured = measured_mb_per_min(
            df, size_col="size_bytes", runtime_col="runtime_seconds",
            runtime_unit="seconds", quality_col="quality_name", codec_col="video_codec")
        funded: set = set()
        tot_freed = tot_cost = 0.0
        n_recycled = 0
        _rows_out: list = []
        _why: list = []          # per-series refusal reasons, for the scaffold summary

        for sid, want_rows in df[pending_mask].groupby("series_id", sort=False):
            try:
                sid_i = int(sid)
            except (TypeError, ValueError):
                continue
            series_rows = df[pd.to_numeric(df["series_id"], errors="coerce") == sid_i]
            title = str(want_rows.iloc[0].get("series_title") or f"series {sid_i}")
            keep_tagged = str(series_rows["keep_policy"].dropna().iloc[0]) in (
                "keep_series", "keep_season") if (
                "keep_policy" in series_rows.columns
                and series_rows["keep_policy"].notna().any()) else False

            # ELIGIBLE POOL: owned + watched + not whole-file guarded.
            #
            # NO WHOLE-HOUSEHOLD MANDATE (GLD-ACQ-18). Requiring every configured member to
            # have watched is unsatisfiable with six of them -- Mom will not watch Blue
            # Bloods -- and ``retention_hold`` inside the guard set above already answers the
            # real question PER VIEWER: is anyone still walking toward this episode?
            #
            # WATCH ANCHOR: ``last_watched_at``, with the household stamp preferred only when
            # present. ``household_last_watched_at`` is declared in SCHEMA_COLUMNS and
            # populated on ZERO of 12,637 rows, so requiring it emptied the pool for every
            # series and the recycle silently never fired.
            _drop = {"no_fid": 0, "protected": 0, "unwatched": 0, "no_date": 0, "no_size": 0}
            watched_owned: list = []
            for i in series_rows.index:
                fid = series_rows.at[i, "episode_file_id"]
                if pd.isna(fid):
                    _drop["no_fid"] += 1
                    continue
                if int(fid) in protected:
                    _drop["protected"] += 1
                    continue
                if "is_watched" in series_rows.columns:
                    _iw = series_rows.at[i, "is_watched"]
                    if not (pd.notna(_iw) and bool(_iw)):
                        _drop["unwatched"] += 1
                        continue
                _hw = (series_rows.at[i, "household_last_watched_at"]
                       if "household_last_watched_at" in series_rows.columns else None)
                if not (pd.notna(_hw) and _hw):
                    _hw = (series_rows.at[i, "last_watched_at"]
                           if "last_watched_at" in series_rows.columns else None)
                _sz = series_rows.at[i, "size_bytes"] if "size_bytes" in series_rows.columns else None
                if not (pd.notna(_hw) and _hw):
                    _drop["no_date"] += 1
                    continue
                if not pd.notna(_sz):
                    _drop["no_size"] += 1
                    continue
                watched_owned.append({
                    "season": series_rows.at[i, "season_number"],
                    "episode": series_rows.at[i, "episode_number"],
                    "size_gb": float(_sz) / (1024 ** 3),
                    "watched_at": str(_hw),
                    "episode_file_id": int(fid),
                })
            if not watched_owned:
                # Break the 0 down by stage so the failing filter names itself. The standalone
                # diagnostic reads the parquet as WRITTEN AT END OF RUN; this sees the frame
                # MID-PIPELINE, and the two have disagreed (10 vs 0 for the same series), so
                # the counts must come from here rather than from a post-hoc read.
                _own = series_rows[series_rows["episode_file_id"].notna()]
                _w = _own[_own["is_watched"].fillna(False).astype(bool)] if "is_watched" in _own.columns else _own
                _ung = _w[~_w["episode_file_id"].astype("Int64").isin(list(protected))] if len(_w) else _w
                _keep_n = int((series_rows.get("keep_policy").notna()).sum()) if "keep_policy" in series_rows.columns else -1
                # The guards passed (unguarded > 0) but nothing survived, so the loss is in
                # the DATE or SIZE step. Report both directly rather than inferring which.
                _hw_n = int(_ung["household_last_watched_at"].notna().sum()) if (
                    len(_ung) and "household_last_watched_at" in _ung.columns) else 0
                _lw_n = int(_ung["last_watched_at"].notna().sum()) if (
                    len(_ung) and "last_watched_at" in _ung.columns) else 0
                _sz_n = int(_ung["size_bytes"].notna().sum()) if (
                    len(_ung) and "size_bytes" in _ung.columns) else 0
                _why.append(
                    f"{title}: rows={len(series_rows)} owned={len(_own)} watched={len(_w)} "
                    f"unguarded={len(_ung)} | of those unguarded: household_ts={_hw_n} "
                    f"last_watched_at={_lw_n} size_bytes={_sz_n} | LOOP DROPS: {_drop} | "
                    f"keep_policy on {_keep_n}, keep_tagged={keep_tagged}, "
                    f"protected-set={len(protected)}"
                    f" | protected-by: {_series_guard_counts(series_rows, _prot_reasons)}")
                continue

            tiers = self._series_tier_estimates(series_rows, _measured)
            wanted = [{"season": want_rows.at[i, "season_number"],
                       "episode": want_rows.at[i, "episode_number"],
                       "est_gb_by_tier": dict(tiers)} for i in want_rows.index]

            plan = plan_recycle(
                watched_owned=watched_owned, wanted=wanted,
                free_gb=float(free_gb), floor_gb=float(acquire_floor),
                keep_tagged=keep_tagged, rewatch_buffer=buffer_n, size_tolerance=tol)
            if not plan["acquire"]:
                # The planner's OWN reason, verbatim -- it always sets one.
                _why.append(f"{title}: {len(watched_owned)} eligible, {len(wanted)} wanted, "
                            f"tiers={ {k: round(v, 2) for k, v in (tiers or {}).items()} } "
                            f"-> planner: {plan.get('reason') or 'no reason given'}")
                continue

            # ── APPLY: delete first ────────────────────────────────────────────────
            _ok = True
            for ep in plan["recycle"]:
                if eff_dry:
                    continue
                try:
                    # CHECKED: DELETE success now returns True (base contract fix). The
                    # except below was DEAD protection — _make_request swallows HTTP
                    # errors and returns the fallback, so on the 2026-08-07 apply all 6
                    # Curious George deletes 500'd, _ok stayed True, and the acquisition
                    # was funded against space that was never freed. The return check is
                    # the guard that actually fires.
                    if not bool(self.sonarr_api._make_request(
                            instance, f"episodefile/{ep['episode_file_id']}",
                            method="DELETE")):
                        self.logger.log_warning(
                            f"[Recycle] delete FAILED for '{title}' file "
                            f"{ep['episode_file_id']} (see instance-manager error above) "
                            f"- abandoning this series' recycle (nothing acquired).")
                        _ok = False
                        break
                except Exception as e:
                    # A failed delete means the acquisition is NOT funded. Stop this series
                    # rather than acquiring against space that was never freed.
                    self.logger.log_warning(
                        f"[Recycle] delete failed for '{title}' file {ep['episode_file_id']}: "
                        f"{e} - abandoning this series' recycle (nothing acquired).")
                    _ok = False
                    break
            if not _ok:
                continue

            for a in plan["acquire"]:
                funded.add((sid, a["season"], a["episode"]))
            n_recycled += len(plan["recycle"])
            tot_freed += plan["freed_gb"]
            tot_cost += plan["cost_gb"]
            _rows_out.append([
                title[:32], str(len(plan["recycle"])), f"{plan['freed_gb']:.1f} GB",
                str(len(plan["acquire"])), f"{plan['cost_gb']:.1f} GB",
                "/".join(f"{a['tier']}p" for a in plan["acquire"] if a.get("tier")) or "-",
                f"{plan['held_gb']:.1f} GB",
            ])

        # SAY WHY, per refused series — UNCONDITIONALLY (GLD-ACQ-21). This block used to
        # sit under ``if not funded:``, so a PARTIAL fund (one series funded, five refused)
        # computed every refusal reason and then dropped them all — P-A, inside the very
        # scaffold GLD-ACQ-19 added to end the guessing. The question a partial fund
        # raises — "why did series X not fund while Y did?" — is exactly the one the
        # swallowed lines answered. A silent decline is indistinguishable from a decline
        # that never ran; now a refused series says so whether or not a sibling succeeded.
        for w in _why:
            self.logger.log_info(f"[Recycle] '{instance}': {w}")

        if not funded:
            if not _why:
                self.logger.log_info(
                    f"[Recycle] '{instance}': no series reached the planner at all — "
                    f"{int(pending_mask.sum())} pending row(s) grouped into 0 series.")
            return set()

        stats["recycled"] = n_recycled
        stats["recycled_gb"] = round(tot_freed, 1)
        prefix = "[dry_run] " if eff_dry else ""
        self.logger.log_info(
            f"♻️ {prefix}Self-funded acquisition on '{instance}': recycled {n_recycled} "
            f"watched episode(s) ({tot_freed:.1f} GB) to fund {len(funded)} next-up "
            f"episode(s) ({tot_cost:.1f} GB) across {len(_rows_out)} series - net "
            f"{tot_cost - tot_freed:+.1f} GB, so the {acquire_floor:.0f} GB floor holds.")
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        _hdr = ["Series", "Recycled", "Freed", "Acquiring", "Cost", "Tier", "Held"]
        if _rs is not None:
            _rs.add_rows("sonarr", "Self-funded acquisition", instance, _hdr, _rows_out, order=9)
        else:
            self.logger.log_grid(_hdr, _rows_out,
                                 title=f"Self-funded acquisition - '{instance}'{prefix}", cap=24)
        return funded

    # ══════════════════════════════════════════════════════════════════════════════
    # §12  DELETE EXECUTION — the marked-file delete plus the public entrypoints
    #      Gated by deletions_consent AND the backup gate. Nothing here runs unless both
    #      are armed. → SonarrEpisodeRetentionManager (merges with §6, §10, §14)
    # ══════════════════════════════════════════════════════════════════════════════

    def _series_grab_index(self, instance, series_id):
        """``{"S01E02": descriptor}`` for one series, fetched once per run.

        Sonarr's grab history is FINITE, so the descriptor is captured at the moment
        of destruction rather than hoped for later. Memoised per (instance, series)
        because a prune deletes many episodes of the same show and the history call
        answers all of them at once.

        Best-effort by construction: every failure path returns ``{}`` and the caller
        proceeds, because losing the descriptor must never cost the deletion it
        documents. A failed fetch is cached as empty so one bad series does not get
        re-requested once per episode.

        RUNS UNDER ``dry_run``. This is a read-only ``GET`` — it destroys nothing and
        mutates nothing — and skipping it made the descriptor path the ONE thing a
        dry_run could not prove, which is backwards: it is also the path most likely to
        be wrong, since it depends on Sonarr honouring ``includeEpisode=true``. A
        disarmed run must not WRITE; reading is how the operator verifies the archive
        before arming consent.
        """
        if series_id is None:
            return {}
        try:
            sid = int(series_id)
        except (TypeError, ValueError):
            return {}
        cache = getattr(self, "_grab_index_cache", None)
        if cache is None:
            cache = self._grab_index_cache = {}
        ck = (instance, sid)
        if ck in cache:
            return cache[ck]
        idx = {}
        try:
            raw = self.sonarr_api._make_request(
                instance,
                f"history?seriesId={sid}&eventType=1&includeEpisode=true&pageSize=500",
                fallback=None)
            idx = push_descriptors_by_episode(raw) or {}
        except Exception:
            idx = {}
        cache[ck] = idx
        return idx

    def _grab_descriptor(self, instance, series_id, season, episode):
        """The redacted ``release/push`` descriptor for one episode, or ``{}``."""
        try:
            key = episode_key(season, episode)
        except Exception:
            return {}
        if not key:
            return {}
        return self._series_grab_index(instance, series_id).get(key) or {}

    _TVDB_IN_PATH = re.compile(r"\{tvdb-(\d+)\}")

    @classmethod
    def _tvdb_from_path(cls, path):
        """The tvdb id out of a TRaSH-style folder name, or None.

        ``.../Ted Lasso (2020) {tvdb-383203}/Season 01/...`` -> ``383203``. Zero
        dependencies and present on every path in this library, which is why it is the
        FIRST source rather than a fallback: the series-record join it replaces turned
        out to depend on a broken accessor (see :meth:`_series_meta`)."""
        m = cls._TVDB_IN_PATH.search(str(path or ""))
        if not m:
            return None
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None

    _UPGRADE_PENDING_KEY = "sonarr/{inst}/upgrade_pending"

    def _persist_upgrade_intents(self, instance, intents):
        """Merge this pass's upgrade intents into the pending worklist — GLD-DEL-10.

        Never raises: failing to record an intent costs a reconciliation, not a file.
        """
        live = [i for i in (intents or []) if i]
        if not live or not self.global_cache:
            return 0
        try:
            key = self._UPGRADE_PENDING_KEY.format(inst=instance)
            led = self.global_cache.get(key)
            led = led if isinstance(led, dict) else {}
            self.global_cache.set(key, merge_upgrade_intents(led, live))
            self.logger.log_debug(
                f"  ↑ tracked {len(live)} upgrade intent(s) for reconciliation next pass")
            return len(live)
        except Exception as e:
            self.logger.log_debug(f"  upgrade intents not recorded ({e})")
            return 0

    def _observe_upgrade_targets(self, instance, ledger):
        """``{key: current_episode_file_id_or_None}`` for the pending worklist.

        ⚠️ Reads FRESH, bypassing the per-series episode cache. Reconciliation asks
        "has this changed since we triggered it?", and a cache written before the
        trigger answers the wrong question — it would report the OLD id and every
        landed upgrade would read as still-pending forever.

        A series whose fetch fails is simply ABSENT from the result. That is not the
        same as "no file" (**P-C**): `reconcile_upgrades` leaves absent keys pending
        rather than declaring them orphaned, so one bad fetch cannot manufacture
        phantom orphans across a whole series.
        """
        obs = {}
        if not isinstance(ledger, dict) or not ledger:
            return obs
        by_series = {}
        for key, rec in ledger.items():
            if isinstance(rec, dict) and rec.get("series_id") is not None:
                by_series.setdefault(int(rec["series_id"]), []).append(rec)
        for sid, recs in by_series.items():
            try:
                eps = self.sonarr_api._make_request(
                    instance, f"episode?seriesId={int(sid)}", fallback=None)
            except Exception:
                eps = None
            if eps is None:                      # unknown — leave every key absent
                continue
            live = {}
            for e in eps:
                if not isinstance(e, dict):
                    continue
                try:
                    live[(int(e.get("seasonNumber")), int(e.get("episodeNumber")))] = (
                        int(e.get("episodeFileId") or 0) or None)
                except (TypeError, ValueError):
                    continue
            for rec in recs:
                try:
                    obs[rec["key"]] = live.get(
                        (int(rec["season"]), int(rec["episode"])))
                except (TypeError, ValueError):
                    continue
        return obs

    def _reconcile_upgrade_intents(self, instance, df):
        """Did the upgrades we triggered actually land? — GLD-DEL-10.

        RUNS AT THE START of the pass, deliberately. The parquet drives every decision
        the pass then makes, so reconciling at the end would leave the whole run
        reasoning over rows already known to be wrong.

        Upgrades are the FACTORY for orphans: Sonarr deletes the old file and imports
        a new one with a new `episodeFileId`, so an upgrade that is not re-pointed
        leaves a dead pointer behind. `GLD-DEL-09`'s purge sweeps those up eventually;
        this stops them being created. Only rows WE triggered are checked, which keeps
        it to a handful of API calls — Sonarr's own scheduled upgrades still produce
        orphans that only the end-of-pass sweep catches.

        A fulfilled intent is RE-POINTED via `_repoint_file_fields` rather than
        dropped: the row keeps its watch history and scores and simply starts
        describing the file that is actually there.

        Returns ``(df, stats)``. Never raises.
        """
        stats = {"checked": 0, "repointed": 0, "orphaned": 0,
                 "abandoned": 0, "pending": 0}
        if df is None or df.empty or not self.global_cache:
            return df, stats
        try:
            key = self._UPGRADE_PENDING_KEY.format(inst=instance)
            led = self.global_cache.get(key)
            led = led if isinstance(led, dict) else {}
            if not led:
                return df, stats
            stats["checked"] = len(led)

            res = reconcile_upgrades(led, self._observe_upgrade_targets(instance, led))
            stats["pending"] = len(res["pending"])
            stats["orphaned"] = len(res["orphaned"])
            stats["abandoned"] = len(res["abandoned"])

            files_cache = {}
            for hit in res["fulfilled"]:
                try:
                    sid = int(hit["series_id"])
                    if sid not in files_cache:
                        files_cache[sid] = self._get_episode_files(instance, sid) or []
                    rec = next((f for f in files_cache[sid]
                                if int(f.get("id") or 0) == int(hit["observed_file_id"])), None)
                    if not rec:
                        continue
                    mask = ((df["series_id"] == sid)
                            & (df["season_number"] == hit["season"])
                            & (df["episode_number"] == hit["episode"]))
                    for idx in df.index[mask]:
                        if self._repoint_file_fields(df, idx, rec, int(hit["observed_file_id"])):
                            stats["repointed"] += 1
                except Exception:
                    continue

            self.global_cache.set(key, res["pending"])
            if stats["repointed"] or stats["orphaned"] or stats["abandoned"]:
                self.logger.log_info(
                    f"  ↑ upgrade reconcile '{instance}': {stats['repointed']} re-pointed, "
                    f"{stats['orphaned']} now file-less, {stats['abandoned']} abandoned "
                    f"(>48h), {stats['pending']} still queued.")
            else:
                self.logger.log_debug(
                    f"  ↑ upgrade reconcile '{instance}': {stats['pending']} still queued.")
        except Exception as e:
            self.logger.log_debug(f"  upgrade reconcile skipped for '{instance}': {e}")
        return df, stats

    def _arr_file_index(self, instance, series_ids):
        """``{episode_file_id: size_bytes}`` straight from Sonarr, for *series_ids*.

        Reads the per-series `episodefiles/by_series/<sid>` caches the sync already
        populated, so this costs NO extra API call. `get` rather than
        `get_or_generate_cache` on purpose: a miss must read as "unknown", not
        trigger a fetch — a detector that repopulates what it is auditing cannot
        detect anything.
        """
        out = {}
        if not self.global_cache:
            return out
        for sid in series_ids:
            try:
                rows = self.global_cache.get(
                    f"sonarr/{instance}/episodefiles/by_series/{int(sid)}")
            except Exception:
                continue
            for f in (rows or []):
                if not isinstance(f, dict):
                    continue
                fid, sz = f.get("id"), f.get("size")
                if fid is None:
                    continue
                try:
                    out[int(fid)] = int(sz or 0)
                except (TypeError, ValueError):
                    continue
        return out

    def _check_parquet_drift(self, instance, df):
        """Audit the parquet against Sonarr for the files it CLAIMS — GLD-DEL-08.

        ⚠️ INTERSECTION ONLY. The parquet is a WORKING SET, not a mirror: 97.6% of
        series hold exactly one row because an unwatched series gets only a pilot.
        Comparing totals would breach every night on a 35% gap that is DESIGN, and
        the rebuild it triggered could never close it. What this checks is whether
        the parquet is WRONG about a file it does track — orphaned rows and stale
        byte counts — both of which corrupt every space decision that reads them.

        On breach: re-sync the affected series' file caches and measure AGAIN.
        `drift_after_rebuild` decides whether it was staleness or something that
        keeps re-breaking, and a `persistent` verdict deliberately does not retry.

        Never raises. An audit that can abort the pass it audits is worse than none.
        """
        try:
            if df is None or df.empty or "episode_file_id" not in df.columns:
                return None
            owned = df[df["episode_file_id"].notna() & df["size_bytes"].notna()]
            if owned.empty:
                return None
            pq = {}
            for fid, sz in zip(owned["episode_file_id"], owned["size_bytes"]):
                try:
                    pq[int(fid)] = int(sz)
                except (TypeError, ValueError):
                    continue
            sids = {int(s) for s in owned["series_id"].dropna().unique()}
            arr = self._arr_file_index(instance, sids)

            first = intersection_drift(pq, arr)
            cov = coverage(len(pq), len(arr))
            if cov.get("ratio") is not None:
                self.logger.log_debug(
                    f"  \U0001f4d0 parquet coverage '{instance}': {cov['parquet']:,} of "
                    f"{cov['arr']:,} tracked files ({cov['ratio'] * 100:.0f}%) — gauge only")
            if not first.get("breach"):
                if first.get("reasons"):
                    self.logger.log_debug(f"  parquet audit '{instance}': {first['reasons'][0]}")
                return first

            self.logger.log_warning(
                f"  ⚠\ufe0f parquet drift on '{instance}' — " + "; ".join(first["reasons"]))

            # Re-sync EVERY series that diverged, then measure again. Driving this
            # off a display-truncated id list was the 2026-08-24 defect: 194 orphans
            # yielded only 9 series to repair, cleared 28%, and reported PERSISTENT.
            bad = {int(fid) for fid in (first["orphaned_ids"] + first["mismatch_ids"])}
            resync = {int(r["series_id"]) for _, r in owned.iterrows()
                      if int(r["episode_file_id"]) in bad and pd.notna(r["series_id"])}
            self.logger.log_debug(
                f"  repairing {len(bad)} diverged file(s) across {len(resync)} series "
                f"(sample ids: {sorted(bad)[:20]})")
            for sid in resync:
                try:
                    self.global_cache.delete(
                        f"sonarr/{instance}/episodefiles/by_series/{sid}")
                    self.global_cache.get_or_generate_cache(
                        key=f"sonarr/{instance}/episodefiles/by_series/{sid}",
                        generator_function=lambda s=sid: self.sonarr_api._make_request(
                            instance, f"episodefile?seriesId={s}", fallback=None),
                        expiration_time=self._episode_files_ttl_s(),
                        regenerate_on_expiry=True)
                except Exception:
                    continue

            second = intersection_drift(pq, self._arr_file_index(instance, sids))
            verdict = drift_after_rebuild(first, second)
            self.logger.log_warning(
                f"  ⚠\ufe0f parquet drift '{instance}' after re-syncing {len(resync)} series: "
                f"{verdict.upper()} — orphaned {first['orphaned']}→{second['orphaned']}, "
                f"stale-size {first['size_mismatch']}→{second['size_mismatch']} "
                f"(gate {first['tolerance']}). See GLD-DEL-08.")
            second["verdict"] = verdict
            return second
        except Exception as e:
            try:
                self.logger.log_debug(f"  parquet audit skipped for '{instance}': {e}")
            except Exception:
                pass
            return None

    def _series_meta(self, instance, series_id):
        """``{pid, path, tvdb_id, title}`` for one series, or ``{}`` — GLD-RST-08.

        WHY THIS EXISTS. ``episode_files`` carries no ``quality_profile_id``: on the TV
        side the quality profile belongs to the SERIES, so the deletion record has to
        join it in. The series library is already cached, so this costs no API call —
        which matters because it is the only way pid reaches the ``dry_run`` and
        ``marked-not-consented`` paths, neither of which may touch the network.

        ⚠️ TWO SOURCES, BECAUSE THE OBVIOUS ONE IS BROKEN.
        ``SonarrStorageLibraryManager.get_series_cache`` calls
        ``global_cache.load_cache(...)``, and **no such method exists** on
        ``GlobalCacheManager`` or ``BaseManager`` — its surface is
        ``get``/``get_json``/``get_or_generate_cache``. It therefore raises
        ``AttributeError`` on every call (``GLD-SON-27``). The 2026-08-23 22:00 run
        wrote 271 rows with ``pid`` and ``tvdb_id`` absent from every one, and said
        nothing, because this method swallowed it. The direct ``global_cache.get`` read
        is tried FIRST and the manager kept only as a fallback for when that defect is
        fixed.

        Failures are now LOGGED ONCE per instance rather than swallowed. A silent
        degradation is what cost the previous run.
        """
        if series_id is None:
            return {}
        try:
            sid = int(series_id)
        except (TypeError, ValueError):
            return {}
        idx = getattr(self, "_series_meta_index", None)
        if idx is None or idx.get("__inst__") != instance:
            idx = {"__inst__": instance}
            records, how, err = [], None, None
            try:
                raw = self.global_cache.get(f"sonarr/{instance}/library") if self.global_cache else None
                if isinstance(raw, dict):
                    records, how = list(raw.values()), "global_cache"
                elif isinstance(raw, list):
                    records, how = raw, "global_cache"
            except Exception as e:
                err = e
            if not records:
                try:
                    lib = self.registry.get("manager", "SonarrStorageLibraryManager") if self.registry else None
                    cache = lib.get_series_cache(instance) if lib else {}
                    if isinstance(cache, dict):
                        records, how = list(cache.values()), "library_manager"
                    elif isinstance(cache, list):
                        records, how = cache, "library_manager"
                except Exception as e:
                    err = err or e
            for s in records:
                if not isinstance(s, dict):
                    continue
                try:
                    idx[int(s.get("id"))] = {
                        "pid": s.get("qualityProfileId"),
                        "path": s.get("path"),
                        "tvdb_id": s.get("tvdbId"),
                        "title": s.get("title"),
                    }
                except (TypeError, ValueError):
                    continue
            self._series_meta_index = idx
            if len(idx) <= 1:
                self.logger.log_warning(
                    f"  ⚠\ufe0f series metadata unavailable for '{instance}' "
                    f"({type(err).__name__ + ': ' + str(err) if err else 'cache empty'}) — "
                    f"deletion rows will carry no quality-profile id. See GLD-SON-27.")
            else:
                self.logger.log_debug(
                    f"  series metadata for '{instance}': {len(idx) - 1} series via {how}")
        return idx.get(sid) or {}

    def _episode_index(self, instance, series_id):
        """``{episodeFileId: rec}`` and ``{(season, episode): rec}`` for one series.

        Sonarr's own ``episodeId`` is NOT on the parquet — ``episode_files`` carries
        ``episode_file_id`` only — but the episode records are already cached per series
        at ``sonarr/<instance>/episodes/by_series/<sid>``, as plain (uncompressed) JSON.
        Unlike the series LIBRARY, that key resolves cleanly through
        ``global_cache.get`` (``build_cache_path`` maps it to ``by_series/<sid>.json``),
        so this costs no API call and works on the ``dry_run`` and
        ``marked-not-consented`` paths.

        Two indexes because they fail in different places. ``episodeFileId`` is the
        DIRECT link — it is the same id the delete path is about to destroy — but it is
        ``0`` on every episode with no file, so it cannot resolve a row whose file is
        already gone. ``(season, episode)`` always resolves but is only as good as the
        parquet's indices. Measured on Ted Lasso: both hit 33/33 marked rows with zero
        disagreement, so file_id is preferred and (s,e) is the fallback.

        Memoised per (instance, series). Returns ``({}, {})`` on any failure.
        """
        try:
            sid = int(series_id)
        except (TypeError, ValueError):
            return {}, {}
        cache = getattr(self, "_episode_index_cache", None)
        if cache is None:
            cache = self._episode_index_cache = {}
        ck = (instance, sid)
        if ck in cache:
            return cache[ck]
        by_fid, by_se = {}, {}
        try:
            raw = self.global_cache.get(
                f"sonarr/{instance}/episodes/by_series/{sid}") if self.global_cache else None
            records = raw.values() if isinstance(raw, dict) else (raw or [])
            for e in records:
                if not isinstance(e, dict) or e.get("id") is None:
                    continue
                try:
                    fid = int(e.get("episodeFileId") or 0)
                except (TypeError, ValueError):
                    fid = 0
                if fid:
                    by_fid[fid] = e
                try:
                    by_se[(int(e.get("seasonNumber")), int(e.get("episodeNumber")))] = e
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            self.logger.log_debug(f"  episode index unavailable for series {sid} ({e})")
        cache[ck] = (by_fid, by_se)
        return cache[ck]

    def _episode_facts(self, instance, series_id, season, episode, file_id):
        """``{episode_id, episode_title, air_date}`` for one episode, or ``{}``."""
        by_fid, by_se = self._episode_index(instance, series_id)
        rec = None
        try:
            if file_id is not None:
                rec = by_fid.get(int(file_id))
        except (TypeError, ValueError):
            rec = None
        if rec is None:
            try:
                rec = by_se.get((int(season), int(episode)))
            except (TypeError, ValueError):
                rec = None
        if not isinstance(rec, dict):
            return {}
        out = {"episode_id": rec.get("id"),
               "episode_title": rec.get("title"),
               "air_date": rec.get("airDateUtc") or rec.get("airDate")}
        return {k: v for k, v in out.items() if v is not None}

    def _profile_names(self, instance):
        """``{profile_id: profile_name}`` for one instance, memoised. ``{}`` on failure.

        A bare ``pid: 3`` is not something an operator can act on months later; the
        cached profile list turns it into ``HD-720p``. Plain JSON at
        ``sonarr/<instance>/profiles``, so it resolves through ``global_cache.get`` and
        costs no API call — unlike the gzip-sharded series library.
        """
        cache = getattr(self, "_profile_name_cache", None)
        if cache is None:
            cache = self._profile_name_cache = {}
        if instance in cache:
            return cache[instance]
        names = {}
        try:
            raw = self.global_cache.get(f"sonarr/{instance}/profiles") if self.global_cache else None
            records = raw.values() if isinstance(raw, dict) else (raw or [])
            for p in records:
                if isinstance(p, dict) and p.get("id") is not None and p.get("name"):
                    try:
                        names[int(p["id"])] = str(p["name"])
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            self.logger.log_debug(f"  profile names unavailable for '{instance}' ({e})")
        cache[instance] = names
        return names

    def _archive_deletion(self, sink, *, instance, row, title, season, episode,
                          disposition, reason=None, size_bytes=None, file_id=None,
                          source=None, fetch_descriptor=True):
        """Append one deletion row to *sink* — GLD-RST-08.

        Records WHAT was destroyed (title/season/episode), WHY (``reason``), FROM
        WHERE (``path``, and the library ``class`` derived from it) and HOW TO GET IT
        BACK (the parquet-side release identity plus the redacted grab descriptor).

        Never raises. A diagnostic that can abort the pass it documents is worse than
        no diagnostic — but a swallowed failure here means a permanently
        unrecoverable file, so the miss is logged rather than silently dropped.
        """
        try:
            get = (lambda k: row.get(k)) if hasattr(row, "get") else (lambda k: None)
            rel = release_record(row) if row is not None else None
            meta = self._series_meta(instance, get("series_id"))
            _pid = get("quality_profile_id") or meta.get("pid")
            _pname = get("quality_profile_name")
            if not _pname and _pid is not None:
                try:
                    _pname = self._profile_names(instance).get(int(_pid))
                except (TypeError, ValueError):
                    _pname = None
            facts = self._episode_facts(instance, get("series_id"), season, episode, file_id)
            push = (self._grab_descriptor(instance, get("series_id"), season, episode)
                    if fetch_descriptor else {})
            sink.append(deletion_record(
                run_id=self._deletion_run_id,
                media="episode",
                instance=instance,
                title=title,
                season=season,
                episode=episode,
                disposition=disposition,
                reason=reason,
                path=get("path") or meta.get("path"),
                file_id=file_id,
                series_id=get("series_id"),
                tvdb_id=(get("tvdb_id") or meta.get("tvdb_id")
                         or self._tvdb_from_path(get("path"))),
                quality_profile_id=_pid,
                quality_profile_name=_pname,
                quality_name=get("quality_name"),
                resolution=get("resolution"),
                size_bytes=size_bytes,
                score=get("watchability_score"),
                release=rel,
                push=push or None,
                source=source,
                **facts,
            ))
        except Exception as e:
            try:
                self.logger.log_warning(
                    f"  \u26a0\ufe0f deletion archive row FAILED for '{title}' ({e}) — "
                    f"the file is still being deleted, but this row will not be recorded.")
            except Exception:
                pass

    def _archive_marked_not_consented(self, instance, df=None):
        """Record the rows this run WOULD have deleted, when consent is withheld.

        Deliberately cheap and side-effect free: no mutation of any kind, because a
        disabled pass must stay disabled. It DOES fetch grab descriptors — that is a
        read-only ``GET`` costing roughly one request per SERIES (memoised), and it is
        what makes the pending snapshot actionable: you can see what is re-acquirable
        BEFORE consenting, rather than discovering after 271 files are gone. Reading is
        not the thing consent gates.

        ``df`` is passed in by the choke point inside ``_do_delete_marked_files``,
        which already holds the frame it just evaluated — re-loading there would both
        cost a parquet read and risk archiving a DIFFERENT set of rows than the ones
        the gate actually saw. It falls back to ``self.load`` for the standalone
        wrapper, which gates before loading anything.

        Idempotent per (instance, run). Two gates gate the same rows — the choke
        point and the standalone wrapper — and although only one can fire in a given
        call chain, both can be reached in one RUN by different callers. Without this
        guard that would append the same 271 rows twice to an append-only file that is
        never rewritten.

        Never raises: this is a courtesy record on a path whose whole point is that it
        does nothing.
        """
        try:
            seen = getattr(self, "_notconsent_archived", None)
            if seen is None:
                seen = self._notconsent_archived = set()
            if instance in seen:
                return
            if df is None:
                df = self.load(instance)
            if df is None or "marked_for_deletion" not in df.columns:
                return
            marked = df[df["marked_for_deletion"].infer_objects(copy=False)
                        .fillna(False).astype(bool)]
            if marked.empty:
                return
            seen.add(instance)
            if not getattr(self, "_deletion_run_id", None):
                self._deletion_run_id = new_run_id()
            sink: list = []
            for idx in marked.index:
                row = df.loc[idx]
                sz = row.get("size_bytes")
                self._archive_deletion(
                    sink, instance=instance, row=row,
                    title=row.get("series_title") or f"series {row.get('series_id')}",
                    season=row.get("season_number"), episode=row.get("episode_number"),
                    disposition="marked-not-consented",
                    reason=deletions_disabled_reason(self.config),
                    size_bytes=float(sz) if pd.notna(sz) else None,
                    file_id=row.get("episode_file_id"),
                    source="grace_expiry", fetch_descriptor=True)
            n = self._flush_deletion_archive(sink)
            if n:
                self.logger.log_info(
                    f"  \U0001f5c3\ufe0f  archived {n} marked-but-not-consented row(s) → "
                    f"logs/deletions/pending.jsonl (run {self._deletion_run_id})")
            else:
                self.logger.log_warning(
                    f"  ⚠\ufe0f  {len(marked)} marked row(s) were NOT archived — the deletion "
                    f"record for '{instance}' is missing for this run.")
        except Exception as e:
            try:
                self.logger.log_warning(
                    f"  ⚠\ufe0f marked-not-consented archive skipped ({e}) — nothing was deleted, "
                    f"but this run has no record of what was queued.")
            except Exception:
                pass

    @staticmethod
    def _flush_deletion_archive(sink):
        """Persist this pass's rows, routed by KIND. Returns rows written.

        Events (``deleted`` / ``failed``) append to the permanent archive; state rows
        (``marked-not-consented`` / ``would-delete``) replace the pending snapshot.
        Mixing them was the accumulation bug — see ``deletion_log.STATE_DISPOSITIONS``.

        ONE write per kind per pass rather than per row: an append per deletion would
        fsync 271 times on a heavy prune, and ``parse_jsonl`` already tolerates a torn
        final line.
        """
        if not sink:
            return 0
        try:
            from scripts.support.utilities.logger.logger import (
                append_deletions, write_pending_deletions,
            )
            events, states = split_by_kind(sink)
            n = append_deletions(to_jsonl(events)) if events else 0
            # Called even when empty: a run that clears the queue must not leave a
            # stale snapshot claiming rows are still pending.
            n += write_pending_deletions(to_jsonl(states))
            return n
        except Exception:
            return 0

    def _record_restore_entry(self, restore_add, df, idx, sn, en, ts_iso):
        """Add one deleted episode to the restore set — GLD-RST-15.

        Mirrors `delete_selected_episode_files` exactly, including ledger v2 release
        identity, so `restore_recovered_episode_deletions` and `_targeted_restore`
        treat entries from both paths identically.

        The release record is what makes restore TARGETED rather than blind: the
        operator's chosen strategy is search-based recovery (`match_release` on
        scene_name + group + quality + resolution), because Sonarr MASKS indexer
        api keys — `GET /indexer` returns `apiKey` as ``********`` — so an archived
        download URL can never be refilled from Sonarr's own config. That makes this
        block the entire restore capability, not a nicety.

        Never raises: a bookkeeping failure must not abort a delete pass mid-flight.
        """
        try:
            sid = df.at[idx, "series_id"] if "series_id" in df.columns else None
            if pd.isna(sid) or pd.isna(sn) or pd.isna(en):
                return
            ent = restore_add.setdefault(str(int(sid)), {"episodes": [], "ts": ts_iso})
            pair = [int(sn), int(en)]
            if pair not in ent["episodes"]:
                ent["episodes"].append(pair)
            _rr = release_record({f: df.at[idx, f] for f in RELEASE_FIELDS
                                  if f in df.columns})
            _ek = episode_key(sn, en)
            if _rr and _ek:
                ent.setdefault("releases", {})[_ek] = _rr
        except Exception as e:
            try:
                self.logger.log_warning(
                    f"  ⚠\ufe0f restore-set entry FAILED for season {sn} episode {en} ({e}) — "
                    f"the file is deleted and will NOT be automatically restorable.")
            except Exception:
                pass

    def _persist_restore_set(self, instance, restore_add):
        """Merge this pass's restore entries into `deleted_episodes` — GLD-RST-15.

        Skipped under dry_run: tracking files that were never removed would make the
        recovery pass re-grab things still on disk.

        `merge_ledger_entry` tolerates BOTH schema versions on either side, so a v1
        record already written by the coordinator path keeps working and simply gains
        a releases map from here on.
        """
        if not restore_add or not self.global_cache or self.dry_run:
            return 0
        dkey = self._DELETED_EPISODES_KEY.format(inst=instance)
        try:
            dset = self.global_cache.get(dkey)
            dset = dset if isinstance(dset, dict) else {}
            for sk, ent in restore_add.items():
                dset[sk] = merge_ledger_entry(dset.get(sk), ent)
            self.global_cache.set(dkey, dset)
            return len(restore_add)
        except Exception as e:
            self.logger.log_error(
                f"[EpisodeFiles] ⚠\ufe0f Failed to persist episode restore-set ({dkey}): {e} — "
                f"{len(restore_add)} series' deletions are NOT restorable.")
            return 0

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
            "skipped_retention":    0,     # rows inside a viewer's retention interval
            "skipped_watchlist":    0,     # rows whose SERIES a still-active member watchlisted
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
                f"[EpisodeFiles] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
                f"leaving {int(marked_mask.sum())} marked row(s) untouched."
            )
            # GLD-RST-08. THIS is the live gate — `sync_from_tautulli` calls
            # `_do_delete_marked_files` directly, so the standalone wrapper's gate is
            # never reached on the real path. Archiving only there produced a run with
            # 271 rows held back and no deletions.jsonl at all. `df` is passed in
            # because this gate already holds the frame it evaluated.
            self._archive_marked_not_consented(instance, df=df)
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

        # GLD-RST-08 — the permanent record of what this pass destroys. Accumulated
        # here and flushed ONCE at the end rather than appended per row.
        _archive: list = []
        if not getattr(self, "_deletion_run_id", None):
            self._deletion_run_id = new_run_id()

        # GLD-RST-15 — THE RESTORE LEDGER, which this pass did not write.
        # `restore_recovered_episode_deletions` reads `deleted_episodes`, NOT the
        # deletion archive: the archive is a forensic record, the ledger is the
        # mechanism. Only `delete_selected_episode_files` fed it, so every file this
        # path removed was permanently outside automated restore — 273 of them queued
        # on the 2026-08-24 run. Same `restore_add` shape and same `merge_ledger_entry`
        # merge the coordinator path uses, so one ledger serves both.
        restore_add: dict[str, dict] = {}
        _now_iso = datetime.now(tz=timezone.utc).isoformat()

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
            # Mirrors the check in _apply_grace_period — ACTIVE WATCHERS ONLY
            # (GLD-ACQ-18, decision 2026-08-06): fires only when a member actively
            # watching this series is still approaching the episode (row
            # retention_hold; the one definition of "reasonably soon"). All-members
            # incompleteness alone no longer blocks — with six members it froze
            # near-everything. Catches rows marked in a previous run before
            # household tracking was active, or when a member's watch was logged
            # after the grace-period pass already ran.
            if "all_household_watched" in df.columns:
                _ahw = df.at[idx, "all_household_watched"]
                _hh_rh = (df.at[idx, "retention_hold"]
                          if "retention_hold" in df.columns else None)
                if (pd.notna(_ahw) and not bool(_ahw)
                        and pd.notna(_hh_rh) and bool(_hh_rh)):
                    self.logger.log_warning(
                        f"  🛡️ HOUSEHOLD GUARD: '{title}' {sn_str}{en_str} — "
                        "not watched by every member and an active watcher is "
                        "still approaching it — clearing deletion flag, skipping."
                    )
                    df.at[idx, "marked_for_deletion"] = False
                    stats["skipped_household"] += 1
                    continue

            # ── PER-VIEWER RETENTION GUARD (secondary / defence-in-depth) ───────
            # Mirrors the clear-guard in _apply_grace_period. Catches rows marked in
            # a previous run (before the rule existed, or before a viewer's position
            # moved back over them) and any row whose hold was recomputed after the
            # grace pass. ``retention_hold_by`` names the viewer so the hold is
            # auditable rather than anonymous.
            if "retention_hold" in df.columns:
                _vh = df.at[idx, "retention_hold"]
                if pd.notna(_vh) and bool(_vh):
                    _by = df.at[idx, "retention_hold_by"] if "retention_hold_by" in df.columns else None
                    _by = str(_by) if (_by is not None and pd.notna(_by)) else "a viewer"
                    self.logger.log_warning(
                        f"  🛡️ RETENTION GUARD: '{title}' {sn_str}{en_str} is inside "
                        f"{_by}'s watch interval — clearing deletion flag, skipping."
                    )
                    df.at[idx, "marked_for_deletion"] = False
                    stats["skipped_retention"] += 1
                    continue

            # ── WATCHLIST INTENT GUARD (secondary / defence-in-depth) ──────────
            # The Group-A5 twin of the retention guard above. Catches rows marked in a
            # previous run (before the shield existed, or before somebody added the series
            # to a watchlist) — the whole-file set is rebuilt per run, but a row already
            # carrying ``marked_for_deletion`` from an earlier pass never re-enters the
            # grace decision. ``watchlist_hold_by`` names the member so the hold is
            # auditable rather than anonymous.
            if "watchlist_hold" in df.columns:
                _wh = df.at[idx, "watchlist_hold"]
                if pd.notna(_wh) and bool(_wh):
                    _by = df.at[idx, "watchlist_hold_by"] if "watchlist_hold_by" in df.columns else None
                    _by = str(_by) if (_by is not None and pd.notna(_by)) else "the household"
                    self.logger.log_warning(
                        f"  🛡️ WATCHLIST GUARD: '{title}' {sn_str}{en_str} is on "
                        f"{_by}'s watchlist — clearing deletion flag, skipping."
                    )
                    df.at[idx, "marked_for_deletion"] = False
                    stats["skipped_watchlist"] += 1
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
                self._archive_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    season=sn, episode=en, disposition="would-delete",
                    reason=reason, size_bytes=_sz_f, file_id=fid,
                    source="grace_expiry")
                stats["deleted"] += 1
                continue

            _del_ok = False
            try:
                # CHECKED (GLD-RST-20). `_make_request` SWALLOWS HTTP failures: it logs
                # a warning and returns the fallback rather than raising, so the old
                # bare `try/except` around this call was DEAD protection for every 500
                # — the identical P-A shape `GLD-SON-23` fixed on the step-down path.
                # Under the 2026-08-24 Sonarr SQLite outage ('unable to open database
                # file') every DELETE would have returned the fallback and this pass
                # would have booked 273 PHANTOM deletions: archive rows and
                # restore-ledger entries for files still on disk, and a recovery pass
                # that re-grabs 273 episodes it already has. A successful DELETE
                # returns True under the base contract.
                _del_ok = bool(self.sonarr_api._make_request(
                    instance,
                    f"episodefile/{fid}",
                    method="DELETE",
                ))
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Delete raised for '{title}' {sn_str}{en_str} "
                    f"(episodeFileId={fid}): {e}"
                )
            if not _del_ok:
                self.logger.log_warning(
                    f"  ⚠️ Delete FAILED for '{title}' {sn_str}{en_str} "
                    f"(episodeFileId={fid}) — file KEPT, mark kept, retries next run."
                )
                self._archive_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    season=sn, episode=en, disposition="failed",
                    reason=f"{reason} | DELETE returned falsy", size_bytes=_sz_f,
                    file_id=fid, source="grace_expiry",
                    fetch_descriptor=False)
                stats["failed"] += 1
                continue
            stats["bytes_freed"] += _sz_f
            self.logger.log_info(
                f"  🗑️ Deleted: '{title}' {sn_str}{en_str} "
                f"({self._fmt_bytes(_sz_f)}) — {reason}"
            )
            self._archive_deletion(
                _archive, instance=instance, row=df.loc[idx], title=title,
                season=sn, episode=en, disposition="deleted",
                reason=reason, size_bytes=_sz_f, file_id=fid,
                source="grace_expiry")
            # Restore ledger (GLD-RST-15). Only reached once the DELETE is CONFIRMED —
            # a file still on disk must never enter the restore set, or the next
            # recovery pass re-grabs something already present.
            self._record_restore_entry(restore_add, df, idx, sn, en, _now_iso)
            stats["deleted"] += 1

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
                    ["retention guard",    stats["skipped_retention"]],
                    ["watchlist guard",    stats["skipped_watchlist"]],
                    ["shared-file guard",  stats["skipped_shared_file"]],
                    ["no file id",         stats["skipped_no_file"]],
                    ["multi-ep coalesced", stats["coalesced_multiep"]],
                    ["archived rows",      self._flush_deletion_archive(_archive)],
                    ["restore-set series", self._persist_restore_set(instance, restore_add)],
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
                    "files kept: inside a viewer's position/pace retention interval",
                    "files kept: a still-active member has this series watchlisted",
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
                f"[EpisodeFiles] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
                "skipping the grace-marked episode delete pass."
            )
            # GLD-RST-08. The gate used to return here having recorded NOTHING, so
            # "266 rows were marked and consent was withheld" existed only as a count
            # in one log line that rotates away in six days — nothing anywhere said
            # WHICH 266. They are archived as `marked-not-consented`: no API call, no
            # descriptor fetch, no mutation. Purely a record of what was queued.
            self._archive_marked_not_consented(instance)
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

    # ── Watch-history sweep tunables ────────────────────────────────────────────
    # A single get_history returns the most RECENT N rows across every media type.
    # At length=5000 this surfaced 433 unique episodes against a 12384-row library,
    # so ~97% of episode rows could never be marked watched - and an episode is only
    # ever delete-eligible once it IS watched. Mirrors the Radarr movie fix.
    _EPISODE_HISTORY_PAGE      = 1000
    _EPISODE_HISTORY_MAX_PAGES = 250

    # ══════════════════════════════════════════════════════════════════════════════
    # §13  WATCH HISTORY & HOUSEHOLD — Trakt + Tautulli ingestion, who watched what
    #      Feeds is_watched, the JIT watcher map, and viewer positions. §19 is the same
    #      subsystem at the far end of the file.
    #      → SonarrEpisodeHistoryManager (merges with §19)
    # ══════════════════════════════════════════════════════════════════════════════

    def _get_episode_history_pages(self, api, inst_name: str) -> list:
        """Every EPISODE history row for one Tautulli instance, paginated."""
        rows: list = []
        start = 0
        for _ in range(self._EPISODE_HISTORY_MAX_PAGES):
            response = api.get_history(
                length=self._EPISODE_HISTORY_PAGE, start=start, media_type="episode"
            )
            entries = ((response or {}).get("response") or {}).get("data", {})
            if isinstance(entries, dict):
                entries = entries.get("data", [])
            if not isinstance(entries, list) or not entries:
                break
            rows.extend(entries)
            if len(entries) < self._EPISODE_HISTORY_PAGE:
                break                       # short page -> end of history
            start += self._EPISODE_HISTORY_PAGE
        else:
            self.logger.log_warning(
                f"⚠️ Tautulli '{inst_name}': episode history sweep hit the "
                f"{self._EPISODE_HISTORY_MAX_PAGES}-page safety stop; counts may be partial."
            )
        return rows

    def _merge_trakt_episode_history(self, aggregated: dict) -> None:
        """Fold the household Trakt episode feed into the Tautulli aggregate, in place.

        WHY: Tautulli serves only what its own history retains - 433 unique episodes
        here - while Trakt keeps years (1913 plays across 155 series from 2020). An
        episode is only delete-eligible once ``is_watched`` is True, and that is gated
        on ``watch_count``, so a truncated history makes most of the library
        permanently invisible to the space coordinator rather than merely unscored.

        MERGE RULE - ``max()``, never ``+=``. Plex scrobbles to Trakt, so one watch
        appears in BOTH feeds. ``max()`` also keeps the merge MONOTONIC: it can only
        RAISE a count, so a bad Trakt fetch can make an episode look more watched -
        never less - and the direction of that error is toward MORE deletion pressure,
        so the guards below (universe / watchlist / retention / household) remain the
        thing standing between a raised count and an actual delete.

        PER-USER state is deliberately NOT touched. Trakt carries no user attribution,
        so it cannot say who watched an episode. ``per_user`` / ``per_user_watch`` drive
        ``all_household_watched`` and per-viewer retention; letting an unattributed feed
        write there would fabricate a viewer. A Trakt-only episode therefore looks
        exactly like one with no per-user history at all - unchanged behaviour.

        Disable with ``scoring.trakt_history_merge.enabled = false`` (shared with the
        movie-side merge).
        """
        _cfg = ((self.config or {}).get("scoring", {}) or {}).get("trakt_history_merge", {}) or {}
        if not bool(_cfg.get("enabled", True)):
            return
        try:
            hm = self.registry.get("manager", "TraktHistoryManager") if self.registry else None
        except Exception:
            hm = None
        if hm is None or not hasattr(hm, "get_full_watch_history_cached"):
            return
        # CACHED form. This called the uncached get_full_watch_history(), which
        # re-paginates the whole ~1,900-row episode history live -- a third full Trakt
        # sweep per run, on top of TraktManager.run()'s and the Plex playlist builder's.
        # Same data, served from trakt/history/episodes (24h).
        #
        # `or []` then `if not rows: return` is the right degradation here: a failed fetch
        # with no last-good copy means no Trakt layer this run, and the Tautulli aggregate
        # this merges INTO is untouched. The merge is max()-based and monotonic, so a
        # missing Trakt feed can only leave counts lower -- never delete or downgrade
        # something that would otherwise have survived.
        rows = hm.get_full_watch_history_cached() or []
        if not rows:
            return

        # Tautulli keys carry the RAW grandparent_title. Trakt and Plex can disagree on
        # case/spacing, so match case-insensitively against the keys already present and
        # reuse the existing key when one is found - otherwise the same show would land
        # twice under two spellings and neither would carry the full count.
        _by_norm = {}
        for (t, s, e) in aggregated:
            _by_norm[(str(t).strip().lower(), s, e)] = (t, s, e)

        added = raised = 0
        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "episode":
                continue
            st = (row.get("show") or {}).get("title")
            ep = row.get("episode") or {}
            season, number = ep.get("season"), ep.get("number")
            if not st or season is None or number is None:
                continue
            try:
                nk = (str(st).strip().lower(), int(season), int(number))
            except (TypeError, ValueError):
                continue
            key = _by_norm.get(nk)
            if key is None:
                key = (st, int(season), int(number))
                _by_norm[nk] = key
                added += 1
            rec = aggregated[key]
            _before = rec["watch_count"]
            rec["watch_count"] = max(rec["watch_count"], 1)
            rec["plays"] = max(rec["plays"], 1)
            _ts = row.get("watched_at")
            if _ts:
                iso = str(_ts).strip().replace("Z", "+00:00")
                if rec["last_watched_at"] is None or iso > rec["last_watched_at"]:
                    rec["last_watched_at"] = iso
            if rec["watch_count"] > _before:
                raised += 1

        if added or raised:
            self.logger.log_info(
                f"\U0001f4ca Trakt episode merge: +{added} episode(s) Tautulli never saw, "
                f"{raised} raised to watched; aggregate now {len(aggregated)} episode(s)."
            )

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
        from scripts.managers.services.tautulli.instances.api import (
            TautulliAPI as TautulliInstanceAPI,
        )

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
                # WATCHES, not plays: only rows that clear the bar
                # (lifecycle.watched_definition.play_is_watched — Tautulli's own
                # ``watched_status``, else ``percent_complete >= watched_threshold.percent``)
                # count here, so ``is_watched = watch_count > 0`` means what Plex and
                # Tautulli show the household and a 30-second sample no longer starts
                # the grace clock or buys an engagement floor.
                "watch_count": 0,
                # RAW play tally (every history row). Never persisted — it exists so the
                # sync log can say "N plays, M watches" and the drop is visible rather
                # than silent.
                "plays": 0,
                # Threshold-FREE playback facts. ``percent_complete`` is the partial-view
                # signal watch_likelihood grades started/abandoned off; ``last_watched_at``
                # is "when was this last played" and also carries the "was this tried at
                # all?" bit for a play Tautulli reported at 0%. Thresholding either would
                # drop a sub-threshold play onto the UNTOUCHED branch, where abandoning a
                # show would RAISE its quality target. See watched_definition's docstring.
                "last_watched_at": None,
                "percent_complete": 0,
                "per_user": {},  # username → latest ISO-8601 timestamp (or None if no date)
                # username → {"at": latest ISO, "watched": bool} where ``watched`` is the
                # same per-row verdict ``watch_count`` is built from. Feeds the per-viewer
                # retention rule (lifecycle.viewer_retention) and the household gate. NOT a
                # second application of the bar: this is a per-(account, series) aggregation
                # of the same admitted plays, and it never reads ``is_watched`` back.
                "per_user_watch": {},
            }
        )
        _watch_pct = self._retention_cfg()["watched_percent"]
        total_raw_entries = 0
        _sub_threshold = 0

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
                entries = self._get_episode_history_pages(api, instance_name)
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Tautulli '{instance_name}' history request failed: {e}"
                )
                continue

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
                # THE BAR, applied ONCE per history row and reused for the per-user
                # verdict below so the two can never disagree.
                _is_watch = watched_by_tautulli(
                    entry.get("watched_status"), pct, threshold_pct=_watch_pct)
                rec["plays"] += 1
                if _is_watch:
                    rec["watch_count"] += 1
                else:
                    _sub_threshold += 1
                rec["percent_complete"] = max(rec["percent_complete"], pct or 0)
                if played:
                    ts = datetime.fromtimestamp(int(played), tz=timezone.utc).isoformat()
                    if rec["last_watched_at"] is None or ts > rec["last_watched_at"]:
                        rec["last_watched_at"] = ts

                # Track per-user timestamps for household watch-state resolution
                _user = str(entry.get("user") or "")
                if _user:
                    _uts = (datetime.fromtimestamp(int(played), tz=timezone.utc).isoformat()
                            if played else None)
                    if _uts:
                        _prev = rec["per_user"].get(_user)
                        if _prev is None or _uts > _prev:
                            rec["per_user"][_user] = _uts
                    elif _user not in rec["per_user"]:
                        rec["per_user"][_user] = None  # watched but no date recorded

                    # Per-viewer retention + the household gate: the SAME per-row
                    # verdict ``watch_count`` is built from (computed once above).
                    _pw = rec["per_user_watch"].setdefault(
                        _user, {"at": None, "watched": False})
                    if _uts and (_pw["at"] is None or _uts > _pw["at"]):
                        _pw["at"] = _uts
                    if _is_watch:
                        _pw["watched"] = True

        # Trakt keeps years where Tautulli keeps only what it retained. Fold it in
        # BEFORE the aggregate is frozen so the watched counts below include it.
        # Fully wrapped: a Trakt failure must never cost us the Tautulli aggregate
        # already built.
        try:
            self._merge_trakt_episode_history(aggregated)
        except Exception as e:
            self.logger.log_debug(f"[EpisodeFiles] Trakt episode merge skipped: {e}")

        result = dict(aggregated)
        _watched_eps = sum(1 for v in result.values() if v["watch_count"] > 0)
        self.logger.log_info(
            f"📊 Tautulli aggregation complete: {len(result)} unique episode(s) "
            f"from {total_raw_entries} raw entries across "
            f"{len(instance_configs)} instance(s)"
        )
        # The bar's effect, stated out loud every run: an operator who tightens or
        # loosens watched_threshold.percent can see exactly what it cost.
        self.logger.log_info(
            f"   ▸ watched bar ≥{_watch_pct:g}% (or Tautulli watched_status=1): "
            f"{_watched_eps}/{len(result)} episode(s) count as WATCHED; "
            f"{_sub_threshold} sub-threshold play(s) recorded as sampled only"
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

        SUPERSEDED — LEAVE THIS EMPTY. ``rating_groups`` is absent from the live
        config, so this returns ``[]`` and the household gate is inert. That is
        deliberate: :meth:`_apply_viewer_retention` (per-account position + pace,
        ``machine_learning/lifecycle/viewer_retention.py``) now answers the same
        question — "does someone still need this?" — without a hand-maintained
        roster and with a bounded, self-expiring hold. Populating
        ``rating_groups.household.members`` would DOUBLE-GUARD: this gate holds
        every episode any member hasn't finished, FOREVER, on top of the retention
        interval. Tune ``episode_retention`` instead. See the note at the top of
        ``lifecycle/household_watch.py``.
        """
        return (
            (((self.config or {})
            .get("rating_groups") or {})
            .get("household") or {})
            .get("members", [])
        )

    @staticmethod
    def _watched_per_user(watch: dict) -> dict:
        """``{account: latest ISO}`` restricted to accounts that actually WATCHED the
        episode — the ``per_user`` shape the household resolver expects, filtered by
        the same bar ``watch_count`` is built from.

        ``per_user`` itself stays threshold-free (it answers "who has TOUCHED this",
        which is what the JIT grab grid's 'For' column wants); the household gate
        answers "has the household WATCHED this", so it reads this view instead.
        Falls back to raw ``per_user`` when ``per_user_watch`` is absent — a history
        dict built by an older code path, or a hand-rolled test fixture, must not
        silently resolve to "nobody watched it"."""
        pw = (watch or {}).get("per_user_watch")
        if not isinstance(pw, dict) or not pw:
            return (watch or {}).get("per_user", {}) or {}
        return {u: st.get("at") for u, st in pw.items() if (st or {}).get("watched")}

    @staticmethod
    def _resolve_household_watch_state(
        per_user: dict, household_members: list[str], *, quorum: int | None = None
    ) -> tuple[bool, str | None]:
        """
        Determine whether the household has watched an episode — delegates to the brain
        (lifecycle.household_watch.resolve_household_watch). ``quorum`` (default None =
        require every member, byte-identical) lets a per-member quorum count as
        household-watched. Returns ``(household_watched, household_last_watched_at)``.
        """
        return resolve_household_watch(per_user, household_members, quorum=quorum)

    # ── Per-viewer retention helpers ────────────────────────────────────────────
    # The rule that supersedes the (inert) household gate: protect each ACCOUNT's
    # [position − backward_buffer, position + pace × horizon] window on every series
    # it watches. Decision + arithmetic live in the brain
    # (machine_learning.lifecycle.viewer_retention); this side owns the Tautulli
    # history, the title→series_id resolution, the parquet stamp and the durable
    # position sidecar.

    def _retention_cfg(self) -> dict:
        """The normalised ``episode_retention`` knobs. ON by default — the current
        behaviour (mark 3 h after ANYONE watches, no backward cushion, no
        second-viewer protection) is the bug this rule exists to fix, so an opt-in
        default would leave every install broken."""
        return resolve_retention_config(self.config)

    def _retention_ignored_users(self) -> set:
        """Lower-cased ``ignored_users`` — the same roster exclusion every other
        per-user pass honours (an ignored account must not pin disk)."""
        return {str(u).strip().lower() for u in ((self.config or {}).get("ignored_users") or [])}

    def _load_viewer_positions(self, instance: str) -> dict:
        """The durable ``{account: {series_id: state}}`` sidecar (best-effort {})."""
        if not self.global_cache:
            return {}
        try:
            got = self.global_cache.get(self._VIEWER_POSITIONS_KEY.format(inst=instance))
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}

    # ══════════════════════════════════════════════════════════════════════════════
    # §14  VIEWER RETENTION — hold episodes a household member is mid-way through
    #      A protection, not a reclaim: it KEEPS files §12 would otherwise take.
    #      → SonarrEpisodeRetentionManager (merges with §6, §10, §12)
    # ══════════════════════════════════════════════════════════════════════════════

    @timeit("_apply_viewer_retention")
    def _apply_viewer_retention(
        self, df: pd.DataFrame, history: dict, instance: str
    ) -> tuple[pd.DataFrame, dict]:
        """Stamp the per-viewer retention verdict onto ``retention_hold`` /
        ``retention_hold_by`` and persist each account's resume state.

        WHY A COLUMN AND NOT A LIVE PREDICATE
        -------------------------------------
        This codebase deliberately duplicates every delete guard across three
        layers (grace marking, the whole-file protected set, the delete-time
        defence-in-depth block). Only the first has the Tautulli history in scope —
        the coordinator's ``build_delete_candidates`` / ``delete_selected_episode_files``
        run from a bare parquet load. Materialising the verdict as a column is how
        ``all_household_watched`` already solves exactly this, and it keeps the
        three layers reading ONE decision instead of three re-derivations that can
        drift.

        WHY A DURABLE SIDECAR
        ---------------------
        ``tautulli/history/all`` is regenerated hourly and Tautulli prunes its own
        database; the live ``get_history`` pull is likewise a window, not an
        archive. If a viewer's plays age out, recomputing from scratch would drop
        their position to None and silently release everything the rule was
        holding — a mass delete caused by an upstream retention setting. The
        sidecar merges positions forward monotonically
        (``viewer_retention.merge_state``) so the guard degrades to "hold the last
        known resume point" instead of "hold nothing".

        WHAT "TWO EPISODES BACK" MEANS
        ------------------------------
        The interval is an INDEX span into the series' sorted ordinal list, and that
        list is built from THIS FRAME. The frame tracks pilots + watched +
        next_episode rows (``_do_cleanup_non_essential`` drops the rest), so the
        buffer walks the episodes the cache can actually delete — "two deletable
        episodes back", not "two episodes back in the show". On a sparsely-owned
        series those can be far apart in ordinal terms; the COUNT of files held is
        what bounds disk, and that stays exactly ``backward_buffer``.

        Returns ``(df, stats)``; never raises (a guard that crashes must not take
        the sync down, and the columns default to "not held" only when the rule is
        explicitly disabled — see the except clause)."""
        stats = {"enabled": False, "accounts": 0, "series": 0, "held_rows": 0,
                 "intervals": 0, "dormant": 0}
        for col in self._RETENTION_COLS:
            if col not in df.columns:
                df[col] = None
            if df[col].dtype != object:
                df[col] = df[col].astype(object)
        # Recomputed from scratch every run: a stale hold from a previous run must
        # never outlive the history that justified it.
        df["retention_hold"] = False
        df["retention_hold_by"] = None

        cfg = self._retention_cfg()
        if not cfg["enabled"]:
            self.logger.log_info(
                "[Retention] episode_retention disabled — per-viewer holds OFF "
                "(legacy grace behaviour: delete 3h after any watch)."
            )
            return df, stats
        stats["enabled"] = True
        if df.empty:
            return df, stats

        try:
            return self._apply_viewer_retention_inner(df, history, instance, cfg, stats)
        except Exception as e:
            # FAIL-SAFE: an exception leaves every row un-held, which would EXPAND
            # the delete pool. Refuse that: mark every WATCHED row as held for this
            # run so the pass can only be more conservative than before, and shout.
            try:
                _w = df["is_watched"].infer_objects(copy=False).fillna(False).astype(bool)
                df.loc[_w, "retention_hold"] = True
                df.loc[_w, "retention_hold_by"] = "fail-safe (retention build failed)"
                stats["held_rows"] = int(_w.sum())
            except Exception:
                pass
            self.logger.log_error(
                f"[Retention] per-viewer retention build FAILED for '{instance}': {e} "
                f"— holding every watched row this run (fail-safe); nothing new will "
                f"be marked for deletion until the build succeeds."
            )
            return df, stats

    def _apply_viewer_retention_inner(self, df, history, instance, cfg, stats):
        now = datetime.now(tz=timezone.utc)

        # ── title → series_id, from the parquet itself (zero API calls) ──────────
        # The watched rows were written with the TAUTULLI title, so this map is the
        # exact inverse of how history reached the frame; the Sonarr series cache is
        # only consulted for titles the parquet has never seen.
        title_to_sid: dict[str, int] = {}
        _sid_num = pd.to_numeric(df["series_id"], errors="coerce")
        for _i in df.index:
            _t = df.at[_i, "series_title"]
            _s = _sid_num.at[_i]
            if pd.notna(_t) and pd.notna(_s):
                title_to_sid.setdefault(str(_t).strip().lower(), int(_s))

        # ── per (account, series) plays from this run's Tautulli history ─────────
        ignored = self._retention_ignored_users()
        plays: dict[tuple, list] = defaultdict(list)
        for (title, season, episode), watch in (history or {}).items():
            sid = title_to_sid.get(str(title or "").strip().lower())
            if sid is None:
                continue
            ordinal = episode_ordinal(season, episode)
            if ordinal is None:
                continue
            for account, rec in (watch.get("per_user_watch") or {}).items():
                if not account or str(account).strip().lower() in ignored:
                    continue
                plays[(str(account), sid)].append({
                    "ordinal": ordinal,
                    "at": (rec or {}).get("at"),
                    "watched": bool((rec or {}).get("watched")),
                })

        # ── merge with the durable sidecar (position is monotonic) ───────────────
        prior = self._load_viewer_positions(instance)
        merged: dict[str, dict] = {}      # account -> {str(sid): state}
        for (account, sid), pl in plays.items():
            state = merge_state(((prior.get(account) or {}).get(str(sid))),
                                account_facts(pl, cfg=cfg))
            if state:
                merged.setdefault(account, {})[str(sid)] = state
        # Accounts/series present ONLY in the sidecar (history aged out) keep their
        # remembered resume point — the whole reason the sidecar exists.
        _carried = 0
        for account, by_sid in (prior or {}).items():
            if str(account).strip().lower() in ignored or not isinstance(by_sid, dict):
                continue
            for sid_key, state in by_sid.items():
                if not isinstance(state, dict):
                    continue
                if sid_key in merged.get(account, {}):
                    continue
                merged.setdefault(account, {})[sid_key] = state
                _carried += 1

        # ── per-series ordinal universe from the parquet ─────────────────────────
        _sn = pd.to_numeric(df["season_number"], errors="coerce")
        _en = pd.to_numeric(df["episode_number"], errors="coerce")
        row_ordinal = _sn * 10_000 + _en
        order_by_sid: dict[int, set] = defaultdict(set)
        for _i in df.index:
            _s, _o = _sid_num.at[_i], row_ordinal.at[_i]
            if pd.notna(_s) and pd.notna(_o):
                order_by_sid[int(_s)].add(int(_o))

        # ── the rule ─────────────────────────────────────────────────────────────
        states_by_sid: dict[int, dict] = defaultdict(dict)
        for account, by_sid in merged.items():
            for sid_key, state in by_sid.items():
                try:
                    states_by_sid[int(sid_key)][account] = state
                except (TypeError, ValueError):
                    continue

        holds_by_sid: dict[int, dict] = {}
        all_intervals: list[dict] = []
        for sid, states in states_by_sid.items():
            holds, intervals = series_protected_from_states(
                states, order_by_sid.get(sid, ()), now, cfg=cfg)
            if holds:
                holds_by_sid[sid] = holds
            for rec in intervals:
                rec["series_id"] = sid
            all_intervals.extend(intervals)

        # ── stamp the frame ──────────────────────────────────────────────────────
        held = 0
        for _i in df.index:
            _s, _o = _sid_num.at[_i], row_ordinal.at[_i]
            if pd.isna(_s) or pd.isna(_o):
                continue
            by = holds_by_sid.get(int(_s), {}).get(int(_o))
            if not by:
                continue
            df.at[_i, "retention_hold"] = True
            df.at[_i, "retention_hold_by"] = ", ".join(by)
            held += 1

        # ── persist the sidecar ──────────────────────────────────────────────────
        if self.global_cache:
            _pkey = self._VIEWER_POSITIONS_KEY.format(inst=instance)
            try:
                self.global_cache.set(_pkey, {a: dict(b) for a, b in merged.items()})
            except Exception as e:
                # Non-fatal: this run's holds already used the merged state; only the
                # NEXT run loses the memory, and it will rebuild from live history.
                self.logger.log_warning(
                    f"[Retention] could not persist viewer positions ({_pkey}): {e}")

        stats.update({
            "accounts": len(merged),
            "series": len(holds_by_sid),
            "held_rows": held,
            "intervals": len(all_intervals),
            "dormant": sum(1 for r in all_intervals if r.get("dormant")),
            "carried": _carried,
        })
        self._log_viewer_retention(instance, cfg, all_intervals, stats, df)
        return df, stats

    def _log_viewer_retention(self, instance, cfg, intervals, stats, df) -> None:
        """One summary line + a per-(account, series) table on the run summary, so a
        hold is always attributable to a named viewer and a position."""
        self.logger.log_info(
            f"[Retention] per-viewer holds for '{instance}': {stats['held_rows']} row(s) "
            f"across {stats['series']} series from {stats['intervals']} (account, series) "
            f"interval(s) — {stats['dormant']} dormant (>{cfg['dormant_days']}d: position + "
            f"{cfg['backward_buffer']} back, no forward reach), {stats['accounts']} account(s), "
            f"{stats.get('carried', 0)} carried from the position sidecar. "
            f"back={cfg['backward_buffer']} horizon={cfg['horizon_days']}d "
            f"watched>={cfg['watched_percent']}%"
        )
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is None or not intervals:
            return
        titles: dict[int, str] = {}
        if "series_title" in df.columns:
            _s = pd.to_numeric(df["series_id"], errors="coerce")
            for _i in df.index:
                if pd.notna(_s.at[_i]):
                    titles.setdefault(int(_s.at[_i]), str(df.at[_i, "series_title"]))
        rows = []
        for rec in sorted(intervals, key=lambda r: (-r.get("episodes", 0), str(r["account"]))):
            rows.append([
                str(titles.get(rec.get("series_id"), rec.get("series_id"))),
                str(rec["account"]),
                rec["position_label"],
                f"{rec['pace']:.2f}/d" if rec.get("pace") is not None else "n/a",
                "dormant" if rec.get("dormant") else f"+{rec.get('forward', 0)}",
                (f"{rec.get('lo_label')}–{rec.get('hi_label')}"
                 if rec.get("lo_label") else "—"),
                str(rec.get("span", 0)),
            ])
        _rs.add_rows("sonarr", "Per-viewer retention", instance,
                     ["Series", "Viewer", "Position", "Pace", "Reach", "Held", "Eps"],
                     rows, order=35)

    # ── Public: pilot batch ─────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_pilot_batch")
    def _pilot_stale_cap(self) -> int:
        """Max stale pilot stubs re-checked per run (0/negative → unlimited).
        Config ``pilot_stale_recheck_cap``; default PILOT_STALE_RECHECK_CAP.
        Bounds the 48h cohort spike — deferred stubs stay due (FIFO)."""
        try:
            v = (self.config or {}).get("pilot_stale_recheck_cap",
                                        self.PILOT_STALE_RECHECK_CAP)
            v = int(v)
        except (TypeError, ValueError):
            v = self.PILOT_STALE_RECHECK_CAP
        return v if v and v > 0 else 0

    # ══════════════════════════════════════════════════════════════════════════════
    # §15  PILOT SEARCH — first-episode acquisition, the ladder, workers and offload
    #      LARGEST section, and `run_pilot_search` alone is ~900 lines. Cores already
    #      extracted: pilot_720_upgrade.py, pilot_interactive.py, acquisition/pilot_stepping
    #      (11 symbols). What remains here is orchestration — split it into phases
    #      (select → plan → offload → spawn) BEFORE moving it, or the move is unreviewable.
    #      → SonarrEpisodePilotManager
    # ══════════════════════════════════════════════════════════════════════════════

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
                # Per-series TTL jitter (deterministic in the series id) so a
                # cold-start cohort stops expiring as one block. A NaT/absent
                # timestamp yields NaN age → not fresh → stale, exactly as the
                # plain cutoff comparison did.
                _age_s = (now - _da).dt.total_seconds()
                _sid_f = pd.to_numeric(stub_rows["series_id"], errors="coerce").fillna(0)
                _ttl_s = self.CACHE_MAX_AGE * (
                    1.0 + (_sid_f.astype("int64").abs() % 1000) / 1000.0
                    * float(self.PILOT_STALE_TTL_JITTER_PCT)
                )
                _fresh = _age_s < _ttl_s
                fresh_stub_ids: set[int] = set(
                    stub_rows.loc[_fresh, "series_id"].dropna().astype(int)
                )
                stale_stub_ids: set[int] = set(
                    stub_rows.loc[~_fresh, "series_id"].dropna().astype(int)
                )
                # Cap the re-check batch, oldest (most overdue) first; the rest
                # keep their timestamps and remain due next run.
                _cap = self._pilot_stale_cap()
                if _cap and len(stale_stub_ids) > _cap:
                    _stale_rows = stub_rows.loc[~_fresh].assign(_age=_age_s[~_fresh])
                    _keep = set(
                        _stale_rows.sort_values("_age", ascending=False)
                        ["series_id"].dropna().astype(int).head(_cap)
                    )
                    deferred_stub_ids = stale_stub_ids - _keep
                    stale_stub_ids = _keep
                else:
                    deferred_stub_ids = set()
            else:
                fresh_stub_ids = set()
                deferred_stub_ids = set()
                stale_stub_ids = set(
                    stub_rows["series_id"].dropna().astype(int)
                ) if not stub_rows.empty else set()
        else:
            real_pilot_ids = fresh_stub_ids = stale_stub_ids = set()
            deferred_stub_ids = set()
            stub_rows = pd.DataFrame(columns=self.SCHEMA_COLUMNS)

        # Build a fast sid→row_index map for stale stubs so we can update
        # them in-place without a full DataFrame scan per series.
        stale_stub_idx: dict[int, int] = {}
        if not stub_rows.empty:
            for idx_s, row_s in stub_rows.iterrows():
                sid_s = row_s.get("series_id")
                if pd.notna(sid_s) and int(sid_s) in stale_stub_ids:
                    stale_stub_idx[int(sid_s)] = idx_s

        _total_stubs  = len(fresh_stub_ids) + len(stale_stub_ids) + len(deferred_stub_ids)
        self.logger.log_info(
            f"📂 Loaded episode_files.parquet for '{instance}': "
            f"{len(df)} rows — {len(real_pilot_ids)} real pilot(s), "
            f"{len(fresh_stub_ids)} fresh stub(s) (skipped), "
            f"{len(stale_stub_ids)} stale stub(s) (will re-check)"
            + (f", {len(deferred_stub_ids)} deferred past the "
               f"{self._pilot_stale_cap()}/run cap (oldest first; next run picks them up)"
               if deferred_stub_ids else "")
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

        # skip_ids = real pilots + fresh stubs (+ stale stubs deferred past this
        # run's re-check cap).  Non-deferred stale stubs are NOT skipped so they
        # get re-queried and potentially upgraded to real pilot rows.
        skip_ids   = real_pilot_ids | fresh_stub_ids | deferred_stub_ids
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
                rep   = self._pick_representative_file(files or [])
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
        import pathlib

        import pandas as pd
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
        import pathlib

        import pandas as pd
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

        Dry-run contract
        ----------------
        A dry-run performs ZERO Sonarr writes (no command POST, no profile PUT, no
        worker spawn / daemon offload) and never drops a row. It DOES save the
        parquet, but the only columns it may write are the PLAN-only pair
        ``pilot_last_planned_at`` + ``pilot_planned_profile_id`` (the tier the run
        WOULD have grabbed at) — stamped by ``_mark_planned``. Persisting them lets
        back-to-back dry-runs skip the whole per-stub planning loop.

        The LIVE-state columns ``pilot_search_attempts`` / ``pilot_last_searched_at``
        / ``pilot_last_profile_id`` are written ONLY by a real search
        (``_mark_searched``). This split is the anti-influence invariant: the plan
        columns are read exclusively under a ``self.dry_run`` guard, so a dry-run can
        neither suppress the next live search (interval guard) nor re-aim it (the
        ladder reads ``pilot_last_profile_id`` as ``last_pid``).
        """
        PILOT_SEARCH_INTERVAL_H = 24
        instance = self._resolve_instance(instance)
        stats = {
            "checked": 0, "searched": 0, "stepped_down": 0,
            "at_floor": 0, "skipped_recent": 0, "skipped_space": 0, "failed": 0,
            "skipped_unacquirable": 0, "offloaded": 0, "removed_grabbing": 0,
        }

        df = self.load(instance)
        if df.empty:
            return stats

        # Two disjoint column families:
        #   LIVE state   — pilot_search_attempts / pilot_last_searched_at / pilot_last_profile_id.
        #                  Written ONLY by a real search (_mark_searched); read by the backoff +
        #                  interval guard and by the ladder (last_pid → next_pilot_profile*).
        #   PLAN-only    — pilot_last_planned_at / pilot_planned_profile_id. Written ONLY by a
        #                  dry-run (_mark_planned); read ONLY under a `self.dry_run` guard, so a
        #                  dry-run can never steer or suppress the next LIVE run.
        for col in ("pilot_search_attempts", "pilot_last_searched_at", "pilot_last_profile_id",
                    "pilot_last_planned_at", "pilot_planned_profile_id"):
            if col not in df.columns:
                df[col] = None

        stub_mask = (
            df["is_pilot"].infer_objects(copy=False).fillna(False).astype(bool)
            & df["episode_file_id"].isna()
        )
        # ── Watchability gate ─────────────────────────────────────────────────────────────────
        # Only INTERACTIVE-SEARCH stub pilots the household is plausibly interested in: keep a stub
        # whose persisted watchability_score (affinity for an unwatched show — cast/crew/studio/genre)
        # is >= the floor, OR is not yet graded (NaN → sample once). A graded low-affinity stub is held
        # back from the expensive indexer search but its row + score are UNTOUCHED, so refresh_scores
        # keeps re-grading it every run and it returns the moment its affinity climbs back over the
        # floor (no dead-zone). Inert when the column is absent (first post-reset run) or the floor is 0.
        try:
            _pilot_floor = float(((self.config or {}).get("pilot_interactive", {}) or {})
                                  .get("min_watchability", self.PILOT_MIN_WATCHABILITY))
        except (TypeError, ValueError):
            _pilot_floor = self.PILOT_MIN_WATCHABILITY
        _pilot_floor = get_threshold("pilot_min_watchability", self.config,
                                     _pilot_floor, logger=getattr(self, "logger", None))
        if _pilot_floor > 0 and "watchability_score" in df.columns:
            _keep = pilot_watchability_keep(df["watchability_score"], _pilot_floor)
            _gated_out = int((stub_mask & ~_keep).sum())
            if _gated_out:
                stub_mask = stub_mask & _keep
                self.logger.log_info(
                    f"[PilotSearch] watchability gate: holding back {_gated_out} low-affinity stub "
                    f"pilot(s) (score < {_pilot_floor:.0f}) from search — re-graded each run, sampled "
                    f"once affinity climbs."
                )
        if not stub_mask.any():
            self.logger.log_info(
                f"[PilotSearch] No stub pilots due for '{instance}' — nothing to search."
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

        def _is_anime_profile(p):
            return str(p.get("name") or "").strip().lower().startswith("[anime]")

        def _build_climb_ladder(profiles):
            """Ascending floor→widest ladder: one rung per resolution tier (dedupe profiles sharing a
            max resolution), dropping profiles that allow nothing."""
            ladder, seen = [], set()
            for _p in profiles:
                _r = profile_max_resolution(_p)
                if _r <= 0 or _r in seen or _p.get("id") is None:
                    continue
                seen.add(_r)
                ladder.append((int(_p["id"]), int(_r)))
            return ladder

        # The NON-anime ladder EXCLUDES the [Anime] profiles so a live-action stub is never flipped onto
        # an anime profile; anime stubs get their OWN ladder so they never land on an x265-penalising
        # live-action profile (the root cause of anime never grabbing — see the [Anime] tier profiles).
        _climb_ladder = _build_climb_ladder([p for p in ranked if not _is_anime_profile(p)])
        if not _climb_ladder:   # degenerate profile set → fall back to every usable ranked id, ascending
            _climb_ladder = [(int(p["id"]), int(profile_max_resolution(p)))
                             for p in ranked
                             if p.get("id") is not None and profile_max_resolution(p) > 0]
        _floor_pid = _climb_ladder[0][0] if _climb_ladder else None

        # Anime ladder (default ON; self-degrades to the regular ladder when no [Anime] profiles exist,
        # so no behaviour change until those profiles are created). Anime STUBS are collected in the
        # loop below by seriesType and routed onto this ladder.
        _anime_on = bool(((self.config or {}).get("pilot_interactive") or {}).get("anime_ladder", True))
        _anime_ladder = _build_climb_ladder([p for p in ranked if _is_anime_profile(p)]) if _anime_on else []
        _anime_sids: set = set()

        # Stubs whose S01E01 is ALREADY downloading (a committed grab from a prior pass) are dropped
        # from the df below rather than re-searched — a committed grab needs no pilot search. One queue
        # fetch per run. When the file imports it becomes a real (non-stub) pilot row; a download that
        # fails leaves the series file-less, so the pilot cache rebuild re-creates the stub to re-probe.
        _downloading_eids: set = set()
        try:
            _q = self.sonarr_api._make_request(instance, "queue/details", fallback=[]) or []
            for _rec in _q:
                if not isinstance(_rec, dict):
                    continue
                _e = _rec.get("episodeId")
                if _e is None:
                    _e = (_rec.get("episode") or {}).get("id")
                if _e is not None:
                    _downloading_eids.add(int(_e))
        except Exception:
            _downloading_eids = set()
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

        # Interactive-search model (default ON when climbing): ONE manual search per stub reveals
        # all availability — grab the lowest available resolution, or flag UNACQUIRABLE when the
        # indexers return nothing at any resolution. An UNACQUIRABLE stub stays dead until a NEW
        # indexer is added OR recheck_days has elapsed (read here; the gate is in the loop below).
        _pi = ((self.config or {}).get("pilot_interactive") or {})
        interactive = pilot_climb and (bool(_pi.get("enabled", True)) if isinstance(_pi, dict) else bool(_pi))
        try:
            recheck_cooldown = timedelta(days=float(_pi.get("recheck_days", 7) or 7))
        except (TypeError, ValueError):
            recheck_cooldown = timedelta(days=7)
        try:
            pilot_floor_res = max(0, int(_pi.get("floor_res", 0) or 0))
        except (TypeError, ValueError):
            pilot_floor_res = 0
        current_indexers: list = []
        _unacq_ledger: dict = {}
        if interactive:
            current_indexers = indexer_fingerprint(
                self.sonarr_api._make_request(instance, "indexer", fallback=[]) or [])
            _unacq_ledger = (self.global_cache.get(self._pilot_unacq_key(instance))
                             if self.global_cache else None) or {}

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
        drop_idxs: list       = []   # stub rows whose grab is already committed (in the download queue) → remove from the df

        # STRING stamps into columns that a reloaded Parquet hands back as float64.
        # Same failure as the JIT pass guards at the `pre_upgrade_quality` coercion:
        # when every row of a column is null, Parquet round-trips it as float64, and a
        # strict-dtype pandas rejects assigning an ISO string into a float cell with
        # "Invalid value '2026-08-06T02:04:43+00:00' for dtype 'float64'". That exception
        # escapes run_pilot_search and takes the WHOLE pass with it -- and, because
        # run_episode_file_enrichment and run_full_series_enrichment sit above it, all
        # three report the identical error and the identical timestamp.
        #
        # The JIT pass already coerces its three ledger columns for exactly this reason;
        # these two stamps were simply never given the same treatment. Coerce once, up
        # front, rather than in each of the two writers below.
        for _c in ("pilot_last_searched_at", "pilot_last_planned_at"):
            if _c not in df.columns:
                df[_c] = None
            elif df[_c].dtype != object:
                df[_c] = df[_c].astype(object)

        def _mark_searched(idx: int, pid) -> None:
            prev = df.at[idx, "pilot_search_attempts"]
            df.at[idx, "pilot_search_attempts"]  = (int(prev) + 1) if prev and pd.notna(prev) else 1
            df.at[idx, "pilot_last_searched_at"] = now_utc.isoformat()
            df.at[idx, "pilot_last_profile_id"]  = pid

        def _mark_planned(idx: int, pid) -> None:
            # Dry-run bookkeeping ONLY: never touch pilot_search_attempts /
            # pilot_last_searched_at / pilot_last_profile_id — those drive the
            # live backoff + ladder state and must reflect REAL searches (a
            # dry-run that wrote pilot_last_profile_id would be fed back in as
            # `last_pid` and steer the NEXT LIVE run's tier).
            # ``pid`` is the tier this run WOULD have grabbed, recorded on the
            # PLAN-only mirror column so the dry-run preview/memo can still show
            # (and assert) the tier decision. Both plan stamps are persisted (see
            # the dry-run save below) so back-to-back dry-runs skip the full
            # per-stub planning loop, while live code reads NEITHER of them (a
            # dry-run can never suppress, nor re-aim, a real search).
            df.at[idx, "pilot_last_planned_at"]    = now_utc.isoformat()
            df.at[idx, "pilot_planned_profile_id"] = pid

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
                if self.dry_run and not pilot_search_due(
                    df.at[_i, "pilot_last_planned_at"], now_utc, _wiv
                ):
                    continue  # dry-run-planned recently → will be skipped below too
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

            # ── UNACQUIRABLE gate: a stub the interactive search proved has NO release at any
            #    resolution stays dead until a NEW indexer appears OR the re-check cooldown elapses
            #    (the only two ways an empty search can newly succeed). Supersedes the 24 h interval. ──
            if interactive:
                _e = _unacq_ledger.get(str(sid))
                if _e and not pilot_recheck_due(_e.get("flagged_at"), _e.get("indexers"),
                                                now_utc, current_indexers, cooldown=recheck_cooldown):
                    stats["skipped_unacquirable"] += 1
                    continue

            # ── Interval guard (with optional attempts-based backoff) ─────────
            _att_raw = df.at[idx, "pilot_search_attempts"]
            _eff_interval = pilot_backoff_interval(
                interval, int(_att_raw) if _att_raw and pd.notna(_att_raw) else 0,
                backoff=_pilot_backoff,
            )
            if not pilot_search_due(df.at[idx, "pilot_last_searched_at"], now_utc, _eff_interval):
                stats["skipped_recent"] += 1
                continue
            # Dry-run throttle: a stub planned by a recent dry-run is skipped in
            # dry-run only — LIVE searches deliberately ignore the planned stamp.
            if self.dry_run and not pilot_search_due(
                df.at[idx, "pilot_last_planned_at"], now_utc, _eff_interval
            ):
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
                # Anime stubs route onto the [Anime] ladder so they're never flipped to an
                # x265-penalising live-action profile (which is why anime never grabbed).
                _is_anime = str((series or {}).get("seriesType") or "").lower() == "anime"
                _stub_floor = _floor_pid
                if _is_anime and _anime_ladder:
                    _anime_sids.add(sid)
                    _stub_floor = _anime_ladder[0][0]
                ep_id = self._get_episode_id(
                    instance, sid, 1, 1, series_ep_cache=_ep_cache,
                    log_cache_miss=not use_tqdm, log_expired=not use_tqdm,
                    allow_live=(not self.dry_run) and not use_tqdm,
                )
                # Grab already committed (S01E01 is in the download queue) → drop the stub from the
                # df. A committed grab needs no further pilot search, and removing it shrinks every
                # subsequent run + daemon batch. Safe to remove: the pilot cache rebuild re-creates the
                # stub for any series still file-less (so a failed/abandoned download re-probes), and an
                # imported file becomes a real (non-stub) pilot row directly.
                if ep_id and int(ep_id) in _downloading_eids:
                    drop_idxs.append(idx)
                    stats["removed_grabbing"] += 1
                    changed = True
                    continue
                if self.dry_run:
                    # plan stamps only (incl. the tier this run WOULD grab at) —
                    # live ladder/backoff state untouched
                    _mark_planned(idx, _stub_floor)
                else:
                    _mark_searched(idx, _stub_floor)
                changed = True
                if ep_id:
                    climb_items.append((sid, int(ep_id)))
                else:
                    series_fallback.append((idx, sid, title))
                if self.dry_run:
                    _what = "S01E01" if ep_id else "SeriesSearch (S01E01 id n/a)"
                    _how = ("interactive-search" if interactive else "climb") + (
                        " (grab lowest available resolution, or flag UNACQUIRABLE)")
                    _log(
                        f"  [dry_run] PilotSearch would {_how} '{title}' {_what} "
                        f"(≤{_climb_ladder[0][1]}p → ≤{_climb_ladder[-1][1]}p) "
                        f"| why: stub pilot, no file"
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
                    # GLD-ACQ-30 - the space floor the CLIMB path never had.
                    #
                    # The floor at the top of this method sits inside
                    # `if pilot_best_tier:`, which is `(not pilot_climb) and ...` -
                    # i.e. the LEGACY escape hatch only. `pilot_climb` defaults to
                    # TRUE, so the default path reached here with `pilot_free_gb`
                    # and `pilot_reserve_gb` still None and dispatched searches with
                    # no disk check at all. The comment further up claiming "disk
                    # safety on grab is owned by the space-pressure coordinator +
                    # the JIT reserve" is inside that same legacy branch and does
                    # not describe this one.
                    #
                    # Pilot search is named in the 2026-08-07 incident. It is also
                    # the WORST lane to leave open, because it dispatches to a
                    # background worker (or the standalone daemon) that keeps
                    # searching after the run process has moved on - so a run that
                    # decided to grab at 98% full keeps grabbing even once the
                    # pressure pass notices.
                    #
                    # Gated HERE rather than inside the worker: one check, before
                    # dispatch, covering the interactive, in-process and daemon
                    # routes identically. Checking inside three workers would be
                    # three chances to diverge.
                    _cl_total = self._get_total_space_gb(instance)
                    _, _cl_floor = space_targets(
                        self.config, fallback_gb=self.MIN_FREE_SPACE_GB, total_gb=_cl_total,
                    )
                    _cl_free = self._get_free_space_gb(instance)
                    if _cl_free is not None and _cl_free < _cl_floor:
                        stats["skipped_space"] = len(climb_items)
                        self.logger.log_info(
                            f"[PilotSearch] PAUSED on '{instance}': {_cl_free:,.1f} GB free is "
                            f"below the {_cl_floor:,.0f} GB acquisition floor "
                            f"({_cl_floor - _cl_free:,.1f} GB short). {len(climb_items)} pilot "
                            f"search(es) held - a pilot is never deleted, only re-probed when "
                            f"space frees (GLD-ACQ-30).")
                        climb_items = []
                if climb_items:
                    if interactive:
                        # One manual search per stub: grab the lowest available resolution, or flag
                        # UNACQUIRABLE. Pass title/tvdb so the worker labels logs + the ledger.
                        _meta = {sid: {"title": (series_by_id.get(sid) or {}).get("title"),
                                       "tvdb": (series_by_id.get(sid) or {}).get("tvdbId")}
                                 for sid, _ in climb_items}
                        # LARGE batches (> threshold) spill to the standalone pilot-search daemon so
                        # the run process never blocks: the in-process worker is a NON-daemon thread,
                        # so a massive spree (thousands of stubs) would stall interpreter exit until
                        # every indexer search finished. Small batches stay in-process (no point
                        # spawning a process for a handful). A disabled daemon or any spawn failure
                        # falls back to the in-process thread, so searches are never dropped.
                        # Only the sids actually in this batch (anime stubs route onto the [Anime] ladder).
                        _batch_anime = {sid for sid, _ in climb_items if sid in _anime_sids}
                        if self._maybe_offload_pilot_search(
                            instance, climb_items, _climb_ladder, _meta,
                            current_indexers, pilot_floor_res, recheck_cooldown,
                            anime_ladder=_anime_ladder, anime_sids=sorted(_batch_anime),
                        ):
                            stats["offloaded"] = len(climb_items)
                        else:
                            self._spawn_pilot_interactive_worker(
                                instance, climb_items, _climb_ladder, _meta,
                                current_indexers, pilot_floor_res, recheck_cooldown,
                                anime_ladder=_anime_ladder, anime_sids=sorted(_batch_anime))
                    else:
                        self._spawn_pilot_climb_worker(instance, climb_items, _climb_ladder)
                stats["searched"] = len(climb_items)
            # Drop the committed-grab stubs in one shot, AFTER every idx-based write (mark_searched /
            # series_fallback) so positional indices stay valid through the loop. reset_index keeps the
            # saved frame contiguous. Dry-run plans the removal in the stats table but never persists.
            if drop_idxs and not self.dry_run:
                df = df.drop(index=drop_idxs).reset_index(drop=True)
            if changed:
                # Saved in dry-run too: the only dry-run mutations reaching the
                # frame are the PLAN-only columns pilot_last_planned_at +
                # pilot_planned_profile_id (via _mark_planned) — committed-grab
                # rows are dropped live-only above, and searched/attempt/last-
                # profile stamps are live-only by construction. Persisting the
                # plan stamps lets the next dry-run skip the whole planning loop.
                self.save(instance, df)
            _prefix = "[dry_run] " if self.dry_run else ""
            _mode = "interactive search" if interactive else "floor-first climb"
            self.logger.log_table(
                ["Outcome", "Count"],
                [
                    ["searched",            stats["searched"]],
                    ["offloaded to daemon", stats["offloaded"]],
                    ["removed (grab in flight)", stats["removed_grabbing"]],
                    ["id unresolved",       _unresolved],
                    ["skipped recent",      stats["skipped_recent"]],
                    ["skipped unacquirable", stats["skipped_unacquirable"]],
                    ["failed",              stats["failed"]],
                ],
                title=f"[PilotSearch] {_prefix}'{instance}' {_mode} "
                      f"(≤{_climb_ladder[0][1]}p → ≤{_climb_ladder[-1][1]}p)",
                caption="Pilot search: each pilot is grabbed at the LOWEST resolution actually "
                        "available, then left there for the watch-based upgrade path. With interactive "
                        "search ON, one manual search per stub reveals all availability — stubs with NO "
                        "release at any resolution are flagged UNACQUIRABLE and skipped until a new "
                        "indexer is added or the re-check cooldown elapses. Stubs whose S01E01 id can't "
                        "be resolved are deferred (never whole-series searched).",
                descriptions=[
                    "pilots handed to the background search worker (grab at lowest available tier)",
                    "of those, spilled to the standalone pilot-search daemon (batch > threshold) so the run never blocks",
                    "stubs skipped: S01E01 already downloading (committed grab in flight) — re-probes when it leaves the queue",
                    "stubs whose S01E01 id stayed unresolved (cache + live miss) → deferred to next run",
                    f"stubs skipped: searched within last {PILOT_SEARCH_INTERVAL_H}h",
                    "stubs skipped: flagged UNACQUIRABLE (no releases) — re-checks on new-indexer or cooldown",
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
                _mark_planned(idx, pid)
                changed = True
            for idx, _sid, pid, _t in series_queued:
                _mark_planned(idx, pid)
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

        if changed:
            # Dry-run persists ONLY the PLAN columns pilot_last_planned_at +
            # pilot_planned_profile_id (see _mark_planned).
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

    @staticmethod
    def _pilot_unacq_key(instance: str) -> str:
        """global_cache key for the per-instance UNACQUIRABLE ledger (the background interactive
        worker / daemon writes it; run_pilot_search reads it to gate re-searches). Delegates to
        the shared single source of truth so the in-process worker, the daemon, and the reader
        can never disagree about the key."""
        from scripts.managers.services.sonarr.cache.pilot_interactive import (
            unacquirable_key,
        )
        return unacquirable_key(instance)

    def _spawn_pilot_interactive_worker(self, instance: str, items: list, ladder: list,
                                        meta: dict, current_indexers: list, floor_res: int,
                                        recheck_cooldown, anime_ladder=None, anime_sids=None) -> None:
        """Spawn the interactive-search pilot worker on a NON-daemon background thread (mirrors
        :meth:`_spawn_pilot_climb_worker`): it never blocks the pipeline, but the interpreter waits
        for it on exit so every grab/flag lands. One manual search per stub — far lighter than the
        tier-by-tier climb it replaces. ``anime_sids`` route onto ``anime_ladder``."""
        import threading
        items  = [(int(s), int(e)) for s, e in items if s is not None and e is not None]
        ladder = [(int(p), int(r)) for p, r in ladder if p is not None]
        if not items or not ladder:
            return
        threading.Thread(
            target=self._pilot_interactive_worker,
            args=(instance, items, ladder, meta, list(current_indexers), int(floor_res),
                  recheck_cooldown),
            kwargs={"anime_ladder": anime_ladder, "anime_sids": anime_sids},
            name="pilot-interactive-search", daemon=False,
        ).start()
        self.logger.log_info(
            f"[PilotSearch] Interactive search started for {len(items)} pilot(s) across up to "
            f"{min(self.JIT_SEARCH_MAX_WORKERS, len(items))} parallel worker(s) — one manual search "
            f"each: grab the lowest available resolution, or flag UNACQUIRABLE if nothing is found."
        )

    def _maybe_offload_pilot_search(self, instance: str, items: list, ladder: list, meta: dict,
                                    current_indexers: list, floor_res: int, recheck_cooldown,
                                    anime_ladder=None, anime_sids=None) -> bool:
        """Spill a LARGE interactive-search batch to the standalone pilot-search daemon instead of
        the in-process NON-daemon worker thread, so the run process exits immediately rather than
        waiting out a thousands-of-stubs indexer spree (the thread blocks interpreter exit).

        Returns True when the batch was enqueued AND the daemon is running, so the caller skips the
        in-process worker. Returns False — caller falls back to the in-process thread — when:
          * the daemon is disabled (``daemons.pilot_search.enabled=false``),
          * the batch is at/below the spill threshold (``daemons.pilot_search.threshold``, default 10),
          * dry-run (never spawn / write in dry-run), or
          * anything goes wrong enqueueing / spawning (searches are NEVER silently dropped)."""
        if self.dry_run:
            return False
        try:
            import os

            from scripts.managers.factories.daemons.daemon_paths import (
                PILOT_SPILL_THRESHOLD,
            )
            cfg = ((self.config or {}).get("daemons", {}) or {}).get("pilot_search", {}) or {}
            if not cfg.get("enabled", True):
                return False
            try:
                threshold = int(cfg.get("threshold", PILOT_SPILL_THRESHOLD))
            except (TypeError, ValueError):
                threshold = PILOT_SPILL_THRESHOLD
            if len(items) <= max(0, threshold):
                return False

            from scripts.managers.factories.daemons import pilot_jobs
            from scripts.managers.factories.daemons.supervisor import (
                PilotSearchDaemonSupervisor,
            )

            try:
                recheck_days = float(recheck_cooldown.total_seconds()) / 86_400.0
            except Exception:
                recheck_days = 7.0
            _pi = ((self.config or {}).get("pilot_interactive") or {})
            job = {
                "version":          1,
                "mode":             "interactive",
                "instance":         instance,
                "items":            [[int(s), int(e)] for s, e in items],
                "ladder":           [[int(p), int(r)] for p, r in ladder],
                "meta":             {str(s): (meta.get(s) or {}) for s, _ in items},
                "current_indexers": list(current_indexers),
                "floor_res":        int(floor_res),
                "recheck_days":     recheck_days,
                "search_no_resolution": bool(_pi.get("search_no_resolution", True)),
                "skip_hard_rejects":    bool(_pi.get("skip_hard_rejects", True)),
                "soft_floor":           bool(_pi.get("soft_floor", True)),
                "anime_ladder":     [[int(p), int(r)] for p, r in (anime_ladder or [])],
                "anime_sids":       [int(s) for s in (anime_sids or [])],
                "run_pid":          os.getpid(),
            }
            path = pilot_jobs.enqueue(instance, job)
            try:
                PilotSearchDaemonSupervisor(logger=self.logger).ensure_running()
            except Exception:
                # Roll back the just-enqueued job so we don't BOTH leave an orphan AND run the
                # batch in-process (a double-search + dual ledger writer). The in-process fallback
                # then owns the batch.
                pilot_jobs.remove(path)
                raise
            self.logger.log_info(
                f"[PilotSearch] 🛰️ Spilled {len(items)} pilot(s) to the background search daemon "
                f"(batch > {threshold}); the run will NOT block on the search spree. "
                f"Job: {path.name}; daemon log: pilot_search_daemon.log."
            )
            return True
        except Exception as e:
            self.logger.log_warning(
                f"[PilotSearch] Could not offload to the search daemon ({e}); "
                f"falling back to the in-process worker for {len(items)} pilot(s)."
            )
            return False

    def _pilot_interactive_worker(self, instance: str, items: list, ladder: list, meta: dict,
                                  current_indexers: list, floor_res: int, recheck_cooldown,
                                  anime_ladder=None, anime_sids=None) -> None:
        """In-process (SMALL-batch) interactive pilot search: thin wrapper over the shared core
        :func:`pilot_interactive.interactive_pilot_search`, which both this worker and the
        out-of-process pilot-search daemon call so the two can never drift. ONE Sonarr interactive
        search per stub reveals all availability; set the series to the LOWEST tier with results +
        fire an ``EpisodeSearch`` (Sonarr's quality + custom-format scoring grabs the release), or
        flag UNACQUIRABLE when nothing is found.

        ``recheck_cooldown`` is accepted for signature parity with the spawn/daemon path but is
        unused here — the cooldown GATE lives in ``run_pilot_search`` (it reads the ledger this
        writes); the worker only records flags."""
        from scripts.managers.services.sonarr.cache.pilot_interactive import (
            interactive_pilot_search,
        )
        _pi = (getattr(self, "config", None) or {}).get("pilot_interactive") or {}
        interactive_pilot_search(
            make_request=self.sonarr_api._make_request,
            logger=self.logger,
            global_cache=self.global_cache,
            instance=instance, items=items, ladder=ladder, meta=meta,
            current_indexers=current_indexers, floor_res=floor_res,
            max_workers=self.JIT_SEARCH_MAX_WORKERS,
            search_no_resolution=bool(_pi.get("search_no_resolution", True)),
            skip_hard_rejects=bool(_pi.get("skip_hard_rejects", True)),
            soft_floor=bool(_pi.get("soft_floor", True)),
            anime_ladder=anime_ladder, anime_sids=anime_sids,
        )

    # ══════════════════════════════════════════════════════════════════════════════
    # §16  SIZING & ID HELPERS — total space, measured rates, profile ceiling, episode id
    #      Small shared utilities used by §11, §15 and §17. STAY on extraction.
    # ══════════════════════════════════════════════════════════════════════════════

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
                # Same key as _get_all_episodes — keep the freshness policy identical, or
                # whichever path ran first would decide the key's fate for the run (this one
                # previously served stale and never rewrote, re-freezing the key).
                cached = self.global_cache.get_or_generate_cache(
                    key=cache_key,
                    generator_function=lambda: self.sonarr_api._make_request(
                        instance, f"episode?seriesId={series_id}", fallback=None
                    ),
                    expiration_time=self._episodes_ttl_s(),
                    regenerate_on_expiry=True,
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

    # ══════════════════════════════════════════════════════════════════════════════
    # §17  JIT QUALITY UPGRADES — bump the profile just before the household watches
    #      Decision primitives already in the brain (space/jit_planner, 7 symbols;
    #      jit_row_skip / choose_jit_profile / pilot_floor_hold). Search payload already
    #      in sonarr/cache/jit_search.py. What remains is the selection loop.
    #      → SonarrEpisodeJitManager (merges with §18)
    # ══════════════════════════════════════════════════════════════════════════════

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
            "held_pilot": 0,                 # unwatched pilots left at the floor (pilot_hold_at_floor)
        }
        # GLD-DEL-10 — intents accumulate here and persist once, after the pass proves
        # it actually changed something.
        _upgrade_intents: list = []

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
        # QUEUE ORDER (GLD-SON-26). Series that came up empty on a previous run sort
        # to the BACK, so a run's finite search budget goes to candidates that can
        # actually grab. Measured 2026-08-21: five series that grabbed NOTHING consumed
        # 116 of 146 step-downs at ~112s each, then had their flags reset and were fully
        # re-eligible the next run — the same three hours, every night.
        #
        # ORDERING, NOT EXCLUSION, and the distinction is load-bearing. `legacy_regrab`
        # benches a file for 14 days; GLD-SON-02 records that going wrong at 814-of-881
        # scale, because a disabled or rate-limited indexer is indistinguishable from
        # "no release exists". Those five titles are old/obscure/anime — exactly what
        # lives on the torrent indexers currently disabled here. A bench would survive
        # re-enabling them; a demotion does not: the first grab calls record_success and
        # the series returns to full priority with no operator action.
        # Baked in, not configurable: see jit_backoff's policy-constants block. A knob
        # here would be a knob to re-enable the defect.
        _bo_led = {}
        _bo_deferred: list = []
        if self.global_cache:
            try:
                _bo_led = self.global_cache.get(jit_backoff.ledger_key(instance)) or {}
                _seq = int(self.global_cache.get(jit_backoff.run_seq_key(instance)) or 0) + 1
                self.global_cache.set(jit_backoff.run_seq_key(instance), _seq)
                _sids = list(dict.fromkeys(
                    int(s) for s in candidates.get("series_id", []) if pd.notna(s)))
                _now, _bo_deferred = jit_backoff.order_series(_sids, _bo_led, run_seq=_seq)
                if _bo_deferred or _now != _sids:
                    _rank = {s: i for i, s in enumerate(_now)}
                    _keep = candidates["series_id"].isin(_rank)
                    candidates = (candidates[_keep]
                                  .assign(_bo=candidates.loc[_keep, "series_id"].map(_rank))
                                  .sort_values("_bo", kind="stable")
                                  .drop(columns=["_bo"]))
                _sum = jit_backoff.summarise(_bo_led)
                if _sum["demoted"]:
                    self.logger.log_info(
                        f"[JIT] queue order: {_sum['demoted']} series demoted after coming up "
                        f"empty (worst: {_sum['worst_n']}x)"
                        + (f"; {len(_bo_deferred)} skipped this run (soft cooldown, never "
                           f"benched)" if _bo_deferred else "")
                        + " — fresh candidates searched first.")
            except Exception as e:
                # Ordering is an optimisation: on any failure fall back to the caller's
                # order, which is exactly the pre-GLD-SON-26 behaviour.
                self.logger.log_debug(f"[JIT] backoff ordering skipped: {e}")
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
        # PILOT FLOOR-HOLD (default ON): an UNWATCHED pilot (S01E01) is held at the resolution floor
        # — its next-up watch-likelihood is AFFINITY-ONLY, so taste alone must NOT lift a never-watched
        # pilot to 1080p just because the disk has room. It climbs only once its likelihood clears
        # ``upgrade_cutoff`` (very strong affinity) or, post-watch, the engagement floor earns it. The
        # floor follows the watch_likelihood floor_res (720). OFF → byte-identical (a pilot is treated
        # like any other next-up episode).
        _phf = (self.config or {}).get("pilot_hold_at_floor", {}) or {}
        pilot_hold_on = bool(_phf.get("enabled", True))
        try:
            pilot_upgrade_cutoff = float(_phf.get("upgrade_cutoff", 65) or 65)
        except (TypeError, ValueError):
            pilot_upgrade_cutoff = 65.0
        try:
            pilot_floor_res = int(
                ((self.config or {}).get("watch_likelihood") or {}).get("floor_res", 720) or 720
            )
        except (TypeError, ValueError):
            pilot_floor_res = 720
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
            _ll  = watch_likelihood(row, config=self.config)
            _cap = resolution_cap_for_likelihood(_ll, config=self.config)
            # PILOT FLOOR-HOLD: clamp an unwatched pilot to the floor unless its (affinity-only)
            # likelihood clears the high bar. A pilot already on disk at/below the floor is left
            # untouched (no no-op grab, not marked upgraded → still upgradeable once watched); a
            # missing pilot still ACQUIREs at the floor and an existing 1080p pilot DOWNGRADEs to it.
            if pilot_hold_on:
                _cur_res = row.get("resolution")
                try:
                    _cur_res = int(_cur_res) if (_cur_res is not None and pd.notna(_cur_res)) else None
                except (TypeError, ValueError):
                    _cur_res = None
                _cap, _pilot_settled = pilot_floor_hold(
                    _cap, is_pilot=bool(row.get("is_pilot")), likelihood=_ll,
                    upgrade_cutoff=pilot_upgrade_cutoff, floor_res=pilot_floor_res,
                    is_acquire=is_acquire, current_res=_cur_res,
                )
                if _pilot_settled:
                    stats["held_pilot"] += 1
                    self.logger.log_debug(
                        f"  🎚️  JIT hold pilot '{title}' S{sn:02d}E{en:02d}: at/below the "
                        f"{pilot_floor_res}p floor (likelihood {_ll:.0f} < {pilot_upgrade_cutoff:.0f}) "
                        f"— left as-is until watched."
                    )
                    continue
            if per_episode_tiers:
                chosen = choose_jit_profile(
                    best_first, cap=_cap, projected_free=projected_free,
                    reserve_gb=reserve_gb, runtime_min=runtime_min, measured=measured,
                    pressure_cap=jit_pressure_cap,
                )
            else:
                if sid not in series_choice:
                    # Best profile that fits the reserve within the earned tier (brain).
                    series_choice[sid] = choose_jit_profile(
                        best_first, cap=_cap, projected_free=projected_free,
                        reserve_gb=reserve_gb, runtime_min=runtime_min, measured=measured,
                        pressure_cap=jit_pressure_cap,
                    )
                chosen = series_choice[sid]

            # None now means ONLY a genuine reserve breach (choose_jit_profile floor-falls-back when
            # no profile is <= the earned cap), so the skip message is accurate.
            if chosen is None:
                stats["skipped_space"] += 1
                self.logger.log_info(
                    f"  ⏭️  JIT skip '{title}' S{sn:02d}E{en:02d}: even the lowest-resolution "
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
            # FLOOR FALLBACK: no profile sits at/below the earned cap (e.g. a 720p-earned episode on
            # an instance whose profiles floor at 1080p). choose_jit_profile grabbed the lowest
            # available instead of stranding it — log it distinctly from a true reserve skip.
            if target_res > _cap:
                self.logger.log_info(
                    f"  ⤵️  JIT '{title}' S{sn:02d}E{en:02d}: no profile at/below the earned "
                    f"{_cap}p cap — grabbing at the floor profile "
                    f"'{chosen.get('name', chosen.get('id'))}' ({target_res}p)"
                )
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
                # GLD-DEL-10 — record the file this episode owns RIGHT NOW. When the
                # upgrade lands, Sonarr's id will differ, and that difference is the
                # only available proof. Without it an upgrade is fire-and-forget and
                # the stale row it leaves behind becomes tomorrow's orphan.
                _upgrade_intents.append(upgrade_intent(
                    series_id=row.get("series_id"),
                    season=row.get("season_number"),
                    episode=row.get("episode_number"),
                    file_id=row.get("episode_file_id"),
                    size_bytes=row.get("size_bytes"),
                    quality_name=row.get("quality_name"),
                    title=row.get("series_title")))
            else:
                stats["acquired"] += 1

            projected_free -= est_gb

        if (changed or reconcile_changed) and not self.dry_run:
            # GLD-DEL-10 — persist the worklist alongside the rows it describes. Under
            # dry_run nothing was actually searched, so recording intents would create
            # a worklist for upgrades that never fired.
            self._persist_upgrade_intents(instance, _upgrade_intents)
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
            # LARGE batches spill to the daemon (the run won't block on per-search 180s polls);
            # small batches stay in-process. A disabled daemon / spawn failure falls back to the
            # in-process worker, so searches are never dropped.
            if not self._maybe_offload_jit_search(instance, queued):
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
                ["held-pilot (floor)",     stats["held_pilot"]],
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
                "unwatched pilots already at/below the 720p floor — held until watched",
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

        NOTE (``GLD-SON-26``): this clears ``jit/failed_upgrades`` ONLY. The per-series
        demotion ledger (``jit/backoff``) is a SEPARATE key and must survive — the
        episodes going back into the candidate pool is exactly the intent, and the
        backoff counter is what decides they are attempted LAST rather than first.
        Clearing both here would wipe the demotion every run and re-create the loop
        it exists to break. See ``_record_jit_backoff``.
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

    def _maybe_offload_jit_search(self, instance: str, work: dict) -> bool:
        """Spill a LARGE JIT step-down batch to the standalone pilot-search daemon (mode='jit')
        instead of the in-process NON-daemon thread, so the run exits immediately rather than
        waiting out per-search 180s command polls across many series. Mirrors
        :meth:`_maybe_offload_pilot_search` (same ``daemons.pilot_search`` gate + enqueue rollback);
        threshold here counts SERIES. The daemon reverts a crash-stranded bumped profile on start
        (the inflight-QP store), preserving the QP-restore guarantee the in-process thread gets for
        free. Returns True when offloaded (caller skips the in-process worker)."""
        if self.dry_run:
            return False
        try:
            import os

            from scripts.managers.factories.daemons.daemon_paths import (
                PILOT_SPILL_THRESHOLD,
            )
            cfg = ((self.config or {}).get("daemons", {}) or {}).get("pilot_search", {}) or {}
            if not cfg.get("enabled", True):
                return False
            try:
                threshold = int(cfg.get("threshold", PILOT_SPILL_THRESHOLD))
            except (TypeError, ValueError):
                threshold = PILOT_SPILL_THRESHOLD

            # Flatten {sid: {tier_res: {eps, step_pids}}} → JSON items [[sid, [[tier_res, eps, step_pids]]]].
            items = []
            for sid, tiers in (work or {}).items():
                groups = []
                for tier_res, g in (tiers or {}).items():
                    eps = [[int(e[0]), int(e[1]), int(e[2])]
                           for e in (g.get("eps") or []) if e and e[0]]
                    step_pids = [int(p) for p in (g.get("step_pids") or [])]
                    if eps and step_pids:
                        groups.append([int(tier_res), eps, step_pids])
                if groups:
                    items.append([int(sid), groups])
            if len(items) <= max(0, threshold):
                return False

            from scripts.managers.factories.daemons import pilot_jobs
            from scripts.managers.factories.daemons.supervisor import (
                PilotSearchDaemonSupervisor,
            )

            job = {"version": 1, "mode": "jit", "instance": instance,
                   "items": items, "run_pid": os.getpid()}
            path = pilot_jobs.enqueue(instance, job)
            try:
                PilotSearchDaemonSupervisor(logger=self.logger).ensure_running()
            except Exception:
                pilot_jobs.remove(path)
                raise
            _grp = sum(len(g) for _s, g in items)
            self.logger.log_info(
                f"[JIT] 🛰️ Spilled {len(items)} series ({_grp} tier-group(s)) to the background "
                f"search daemon (batch > {threshold}); the run will NOT block on the step-down polls. "
                f"Job: {path.name}; daemon log: pilot_search_daemon.log."
            )
            return True
        except Exception as e:
            self.logger.log_warning(
                f"[JIT] Could not offload to the search daemon ({e}); "
                f"falling back to the in-process worker."
            )
            return False

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
        # Thin wrapper over the shared core (which both this in-process worker and the out-of-process
        # pilot-search daemon's "jit" mode call, so the two can never drift). ``_episodes_in_queue``
        # is injected as the grabbed-episode probe; the core records each series' pre-flip profile in
        # a durable inflight store and reverts it, so a detached crash never strands a bumped tier.
        from scripts.managers.services.sonarr.cache.jit_search import (
            jit_step_down_search,
        )
        res = jit_step_down_search(
            make_request=self.sonarr_api._make_request,
            in_queue=self._episodes_in_queue,
            logger=self.logger,
            global_cache=self.global_cache,
            instance=instance, items=items,
            max_workers=self.JIT_SEARCH_MAX_WORKERS,
            max_consecutive_misses=jit_backoff.MAX_CONSECUTIVE_MISSES,
        )
        self._record_jit_backoff(instance, res)

    def _record_jit_backoff(self, instance: str, res: dict) -> None:
        """Fold one search pass's outcome into the demotion ledger (``GLD-SON-26``).

        DELIBERATELY SEPARATE FROM ``_reconcile_failed_jit``. That method reads
        ``jit/failed_upgrades``, resets the EPISODE flags so the rows are candidates
        again, and deletes its key — which is correct and stays. This writes
        ``jit/backoff``, a per-SERIES counter, and must NOT be cleared with it: the
        two answer different questions.

            failed_upgrades  — "is this EPISODE still owed a grab?"      (yes, retry)
            backoff          — "how often has this SERIES come up empty?" (sort it last)

        Together they produce the intended behaviour: the episodes stay fully
        eligible, and their series simply goes to the BACK of the queue. Merging the
        two — or clearing this key in the reconcile — would wipe the counters every
        run and restore the loop this exists to break: five series consuming 116 of
        146 step-downs, every night, forever.
        """
        if not self.global_cache or not isinstance(res, dict):
            return
        try:
            key = jit_backoff.ledger_key(instance)
            led = self.global_cache.get(key) or {}
            seq = int(self.global_cache.get(jit_backoff.run_seq_key(instance)) or 0)
            for sid in (res.get("grabbed_sids") or []):
                led = jit_backoff.record_success(led, sid)      # supply exists → full priority
            for sid in (res.get("exhausted_sids") or []):
                led = jit_backoff.record_exhaustion(led, sid, run_seq=seq)
            self.global_cache.set(key, led)
        except Exception as e:
            # A ledger write must never cost the run: worst case the series keeps its
            # current position, which is the pre-GLD-SON-26 behaviour.
            self.logger.log_debug(f"[JIT] backoff ledger not updated: {e}")

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

    # ══════════════════════════════════════════════════════════════════════════════
    # §18  JIT QUALITY RESTORE — roll a watched episode back to its pre-upgrade file
    #      The other half of §17: up before the watch, back after. Snapshot-backed, so a
    #      failed PUT must NOT clear pre_upgrade_quality (GLD-SON-20).
    #      → SonarrEpisodeJitManager (merges with §17)
    # ══════════════════════════════════════════════════════════════════════════════

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

        stats = {"checked": 0, "restored": 0, "failed": 0, "no_snapshot": 0,
                 "skipped_floor": 0, "would": 0}

        # Honours the run's dry_run AND the backup gate, like every other mutating pass
        # in this file. This method PUTs to Sonarr; before GLD-SON-20 it checked neither,
        # so a dry run issued live writes -- the one promise the flag exists to make.
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)

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
                # Sonarr rejects a non-integer resolution with a 400 ('The JSON value
                # could not be converted to System.Int32'). The snapshot is JSON off the
                # parquet, so the value can arrive as "720"/720.0/numpy int -- coerce, and
                # if it will not coerce leave the CURRENT value rather than sending junk.
                _res = original.get("resolution")
                try:
                    _res = int(float(_res))
                except (TypeError, ValueError):
                    _res = None
                q_inner["resolution"] = _res if _res is not None else q_inner.get("resolution")
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
                                stats["skipped_floor"] += 1   # NOT a restore: nothing was rolled back
                                continue
                    except Exception:
                        pass  # on any error, proceed with normal restore

                if eff_dry:
                    stats["would"] += 1
                    self.logger.log_info(
                        f"  [dry_run] would JIT restore '{title}' S{sn:02d}E{en:02d} -> "
                        f"{original.get('quality_name', '?')} (file {int(fid)} unchanged)"
                    )
                    continue      # nothing written, so the snapshot MUST survive

                # _make_request LOGS transport/HTTP failures and returns the fallback --
                # it does not raise -- so the `except` below can never see a 400. Before
                # GLD-SON-20 the result was ignored: four failed PUTs counted as four
                # restores, the parquet was rewritten to the pre-upgrade quality Sonarr
                # had rejected, and `pre_upgrade_quality` was cleared, destroying the only
                # record needed to retry. Falsy result now means FAILED, and on failure
                # the snapshot and the JIT flag are left INTACT so the next run retries.
                resp = self.sonarr_api._make_request(
                    instance, f"episodefile/{int(fid)}",
                    method="PUT", payload=current, fallback=None,
                )
                if not resp:
                    stats["failed"] += 1
                    self.logger.log_warning(
                        f"  JIT restore REJECTED by Sonarr for '{title}' "
                        f"S{sn:02d}E{en:02d} (file {int(fid)}) - snapshot kept, will retry"
                    )
                    continue
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
                ["checked",       stats["checked"]],
                ["restored",      stats["restored"]],
                ["would-restore", stats["would"]],
                ["skipped-floor", stats["skipped_floor"]],
                ["no-snapshot",   stats["no_snapshot"]],
                ["failed",        stats["failed"]],
            ],
            title=f"[JIT] Restore pass '{instance}'"
                  + (" [dry_run]" if eff_dry else ""),
            caption="Per-pass outcome of the JIT file-quality restore: how many upgraded "
                    "episodes were rolled back to their pre-upgrade file.",
            descriptions=[
                "watched episodes carrying a JIT upgrade, considered this pass",
                "episodes Sonarr ACCEPTED the rollback for",
                "rollbacks withheld (dry run / backup gate disarmed) - snapshots kept",
                "skipped: pre-upgrade quality sits below the pilot-successful floor",
                "episodes skipped: no pre-upgrade snapshot recorded",
                "PUTs Sonarr REJECTED - snapshot kept, retried next run",
            ],
        )
        return stats

    # ══════════════════════════════════════════════════════════════════════════════
    # §19  TAUTULLI SYNC — pull play history and stamp watched-state onto the frame
    #      Same subsystem as §13, separated only by where it landed in the file.
    #      → SonarrEpisodeHistoryManager (merges with §13)
    # ══════════════════════════════════════════════════════════════════════════════

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
        _hh_quorum: int | None = None
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
        # GLD-DEL-10 — reconcile BEFORE anything reads the frame. The parquet drives
        # every decision this pass makes, so a row still pointing at a file replaced
        # by last night's upgrade would mis-inform all of them. Re-pointing here is
        # also what stops that row becoming an orphan for GLD-DEL-09 to sweep up.
        df, _up_rec = self._reconcile_upgrade_intents(instance, df)

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
        _last_progress = 0.0        # last progress-line emission, seconds into the loop
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
                # ``is_watched`` is DERIVED, never asserted: this used to be a bare
                # ``True`` (having ANY history row made the file watched), which is the
                # bug the bar removes. Recomputed every sync, so a row whose only play
                # was a sample flips back to False on the first run after the change.
                df.at[row_idx, "is_watched"]      = bool(watch["watch_count"] > 0)
                # Recompute household watch state on every sync so a newly-added
                # watcher is reflected immediately rather than waiting for TTL. Fed the
                # WATCHED-only per-user view: "has the household watched this" must mean
                # the same thing as is_watched, or the gate holds files on samples.
                _all_hh, _hh_ts = self._resolve_household_watch_state(
                    self._watched_per_user(watch), household_members, quorum=_hh_quorum
                )
                df.at[row_idx, "all_household_watched"]     = _all_hh
                df.at[row_idx, "household_last_watched_at"] = _hh_ts
                # GLD-EPF-14 — re-read the FILE too, not just the watch stats. Without
                # this the branch below is the only place a file is ever resolved, so an
                # existing row's resolution/size/codec froze at insertion while the line
                # after this one went on stamping it as freshly synced.
                _fr, _fid, _ = self._resolve_episode_file(
                    instance, sid, season, episode, files_session_cache, season_ep_cache
                )
                if _fr and _fid and self._repoint_file_fields(df, row_idx, _fr, _fid):
                    stats["repointed"] = stats.get("repointed", 0) + 1
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
                        self._watched_per_user(watch), household_members, quorum=_hh_quorum
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

            # Progress checkpoint — TIME-gated, not count-gated. The pass used to
            # take 8-12s (four 25% lines earned their keep); with the O(1) lookup
            # index it usually finishes in ~1s, where "[104/417] ETA ~0s" ×4 is
            # pure noise. Emit only once the loop has actually been slow (>5s),
            # then at most every 5s — the frozen-screen guard stays for genuinely
            # slow passes (cold caches, live API misses).
            _elapsed = time.time() - _loop_start
            if _elapsed > 5 and (_elapsed - _last_progress) >= 5:
                _last_progress = _elapsed
                _rate = _loop_i / _elapsed if _elapsed > 0 else 0
                _eta  = (_total - _loop_i) / _rate if _rate > 0 else 0
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

        # GLD-ACQ-24: cold unwatched TV → flagged rows → the SAME mark/guard/coordinator
        # flow. Runs after keep-policies (keep tags gate the scan) and before retention/
        # grace/delete so fresh marks are visible this run. No-op unless
        # cold_tv_reclaim.enabled.
        df = self._ingest_cold_inventory(
            df, instance,
            season_ep_cache=season_ep_cache,
            files_session_cache=files_session_cache,
        )
        self.logger.log_info(f"[⏱️] cold_inventory — {time.time()-_ps:.1f}s")

        # GLD-INV-01: owned episodes for series NOTHING else enumerates. Runs directly
        # after the cold scan because it is the same question asked without the reclaim
        # gate — cold_inventory ingests only series below `score_floor` (delete
        # reachability), leaving every higher-scoring unwatched series represented by a
        # single pilot FINGERPRINT that the space planner then read as if it were the
        # whole series. Must land BEFORE _compute_next_episodes and the space passes so
        # the rows it writes are visible to them this run. No-op unless
        # inventory_scan.enabled; writes rows that can never be delete-marked.
        df = self._ingest_inventory_tv(
            df, instance,
            season_ep_cache=season_ep_cache,
            files_session_cache=files_session_cache,
        )
        self.logger.log_info(f"[⏱️] inventory_scan — {time.time()-_ps:.1f}s")

        df = self._compute_next_episodes(df, instance, files_session_cache, season_ep_cache=season_ep_cache)
        self.logger.log_info(f"[⏱️] compute_next_episodes — {time.time()-_ps:.1f}s")

        acquire_stats = self._do_acquire_next_episodes(
            instance, df, season_ep_cache=season_ep_cache
        )
        self.logger.log_info(f"[⏱️] acquire_next_episodes — {time.time()-_ps:.1f}s")

        # Per-viewer retention MUST run before the grace pass: it stamps the
        # retention_hold column the grace clear-guard (and the whole-file protected
        # set, and the delete-time defence-in-depth block) all read.
        df, retention_stats = self._apply_viewer_retention(df, history, instance)
        stats["retention_held"] = retention_stats.get("held_rows", 0)
        self.logger.log_info(f"[⏱️] viewer_retention — {time.time()-_ps:.1f}s")

        df = self._apply_grace_period(df)
        self.logger.log_info(f"[⏱️] apply_grace_period — {time.time()-_ps:.1f}s")

        # When the cross-service space coordinator owns deletion, keep MARKING
        # (above) so it has candidates, but defer the actual episode deletion +
        # purge to the coordinator's unified, space-driven, lowest-watchability pool.
        if coordinator_owns_deletion(self.config):
            delete_stats = {"deleted": 0, "bytes_freed": 0.0}
            self.logger.log_info(
                "[EpisodeFiles] deletion delegated to the space-pressure coordinator "
                "(grace marks applied)."
            )
            # GLD-DEL-09 — the PURGE is not part of that delegation and must still run.
            # Deleting is a policy decision the coordinator owns; purging is cache
            # HYGIENE — it removes rows for files Sonarr no longer has, and touches
            # nothing on disk. Skipping it alongside the delete pass meant that under
            # the standing config (`coordinator_owns_deletion` true) the purge NEVER
            # ran at all: the 2026-08-24 run measured 103 orphaned rows that a re-sync
            # could not clear (103->103, PERSISTENT) precisely because the one pass
            # that removes them was short-circuited here.
            df, purge_stats = self._do_purge_sonarr_deleted(instance, df)
            self.logger.log_info(f"[⏱️] purge_sonarr_deleted — {time.time()-_ps:.1f}s")
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
            # GLD-DEL-08 — audit AFTER the save, so both sides read the same state.
            # Timing is part of the measurement: a grab landing between the two reads
            # shows up as drift, which is why this sits at the end of the sync rather
            # than anywhere earlier.
            self._check_parquet_drift(instance, df)

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

    # ══════════════════════════════════════════════════════════════════════════════
    # §20  RUN SUMMARY — what this manager reports at the end of a run. STAYS.
    # ══════════════════════════════════════════════════════════════════════════════

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
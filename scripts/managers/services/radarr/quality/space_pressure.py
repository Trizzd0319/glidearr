"""
RadarrSpacePressureManager
==========================
Space-pressure quality manager for Radarr.

When free space falls below the pressure band (free < U, where the floor
T = ``free_space_limit`` or 25% of the total drive when that's unset — see
space.space_targets; PRESSURE_THRESHOLD_GB is only the last-resort floor when the
drive's total size is also unreadable) this manager runs a two-stage pipeline:

STAGE 1 — DOWNGRADE TO HD-720P
    Identifies low-priority movies and sets their Radarr quality profile to
    HD-720p, then triggers a MovieSearch so Radarr's cutoff-unmet logic
    fetches the 720p file and replaces the existing one.

    Low-priority candidates (all scored via the same affinity matrix used
    for Trakt auto-rating):
      a. Score below WATCHABILITY_PROTECT_THRESHOLD — genre/actor/director
         affinity + completion + collection bonus say the household is
         unlikely to watch this again.
      b. Unwatched movies (not keep-forever/keep-movie) — never seen at all.
      c. Movies in a collection where any member was watched in the last 30d
         (likely-to-be-watched-soon — keep them, but at 720p for now).
      d. Universe-tagged movies (quality-change only, never deleted).

    Excluded from downgrades:
      * keep_forever / keep_movie tagged
      * Already at or below HD-720p
      * Watched within the last 7 days
      * Score >= WATCHABILITY_PROTECT_THRESHOLD (household likely to re-watch)

STAGE 2 — DELETE (LAST RESORT)
    Only runs if still below threshold after downgrades are queued.
    Only targets watched + grace-expired + already-at-720p movies.
    Prioritises lowest-score movies first.

    NEVER deletes: universe, franchise entries, keep-forever, keep-movie,
    or anything watched within the last 30 days.

EXHAUSTIVE POLICY (``space_exhaustive_downgrade``, DEFAULT ON)
    "Deletion is the TRUE last resort": downgrade EVERYTHING that can still be
    downgraded before ANYTHING is deleted. 720p is the absolute floor.
      * Stage 1 plans every title above the floor (no score ceiling, no early stop
        at a partial need_gb), lowest watchability first, and stops only once free
        space NET of the re-grabs it queued this run reaches the band top U.
      * ``build_delete_candidates`` admits a title ONLY when it is AT or BELOW the
        720p floor — anything still shrinkable is excluded as ``skipped_downgradable``.
      * ``_pick_stepdown_release`` may fall BELOW 720 only for a title with literally
        no >=720 release available.
      * ``space_downgrade_max_regrabs_per_run`` (default 200) bounds the re-grab storm;
        deferred titles keep their files and re-qualify next run.
    Set the flag false to restore the previous behaviour byte-for-byte.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.managers.machine_learning.ledger.decision_ledger import stamp
from scripts.managers.machine_learning.lifecycle.restore_policy import (
    push_descriptor,
    release_record,
)
from scripts.managers.machine_learning.space.deletion_log import (
    deletion_record,
    merge_upgrade_intents,
    new_run_id,
    reconcile_upgrades,
    split_by_kind,
    to_jsonl,
    upgrade_events,
    upgrade_intent,
)
from scripts.managers.machine_learning.scoring.critic import critic_avg
from scripts.managers.machine_learning.space.delete_planner import (
    bare_universe_protected,
    build_movie_delete_candidates,
)
from scripts.managers.machine_learning.space.downgrade_planner import (
    DEFAULT_FLOOR_RESOLUTION as DOWNGRADE_FLOOR_RESOLUTION,
    plan_movie_downgrades,
    UNIVERSE_PROTECT_MIN,
)
from scripts.managers.machine_learning.space.upgrade_planner import (
    plan_movie_upgrades,
)
from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    movie_universe_credits,
)
from scripts.managers.machine_learning.playlists.models import PLACEHOLDER_AFFINITY
from scripts.managers.machine_learning.space.reclaim_ledger import (
    planned_reclaim_gb,
    record_planned_reclaim,
)
from scripts.managers.machine_learning.space.routing_targets import UHD_INSTANCE_LABELS
from scripts.managers.machine_learning.sizing import anomaly as size_anomaly
from scripts.managers.machine_learning.thresholds.registry import get_threshold
from scripts.support.utilities.backup_gate import effective_dry_run
from scripts.support.utilities.watch_likelihood import (
    affinity_boost as _affinity_boost,
)
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities import stepdown_cooldown
from scripts.support.utilities.space_targets import (
    coordinator_owns_deletion, deletions_disabled_reason, deletions_enabled,
    downgrade_regrab_cap, exhaustive_downgrade, space_targets,
)

# Plex parental-controls age tiers that count as a KID for movie_scorer's E1/E2
# cohort terms. Matches the vocabulary PlexUsersManager resolves onto
# ``restriction_profile`` (and the plex.playlists.profile_ages override), so the
# scorer and the playlist age-gate cannot disagree about who is a child.
_KID_AGE_TIERS = frozenset({"little_kid", "older_kid", "teen", "kid", "child"})


class RadarrSpacePressureManager(BaseManager, ComponentManagerMixin):

    PRESSURE_THRESHOLD_GB          = 0.0    # NO last-resort floor (config free_space_limit, else 25% of total, else none)
                                            # Was 25.0 -- and named differently from the other three managers'
                                            # PRESSURE_FALLBACK_GB, so a grep for the common name missed this site entirely.
    HD_720P_PROFILE_NAME           = "HD-720p"
    RECENT_WATCH_DAYS              = 7
    COLLECTION_WINDOW_DAYS         = 30
    WATCHABILITY_PROTECT_THRESHOLD = 6   # score >= this → protect from downgrade

    parent_name = "RadarrQualityManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)

        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _root = self.registry.get("manager", "RadarrManager")
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
                f"❌ {self.__class__.__name__} could not resolve dry_run. "
                f"Refusing to initialize without an explicit value from config.json."
            )
        self.dry_run = bool(_dry_run)
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (dry_run={self.dry_run})")

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_movie_files_manager(self):
        try:
            return self.registry.get("manager", "RadarrCacheMovieFilesManager")
        except Exception:
            return None

    @timeit("_get_free_space_gb")
    def _get_free_space_gb(self, instance: str) -> float:
        if self.radarr_api is None:
            return float("inf")
        # Mount-deduped — root folders sharing a disk must not be summed twice.
        return self.radarr_api.disk_free_gb(instance)

    def _space_targets(self, instance: str | None = None) -> tuple[float, float]:
        """(T, U) from the shared helper — T = ``free_space_limit`` floor, U = top of
        the pressure band. When ``free_space_limit`` is unset the floor defaults to
        25% of the total drive (mount-deduped via ``disk_total_gb``); PRESSURE_THRESHOLD_GB
        is only the last resort when the total drive size is also unreadable."""
        total_gb = None
        if instance is not None and self.radarr_api is not None:
            try:
                total_gb = self.radarr_api.disk_total_gb(instance)
            except Exception:
                total_gb = None
        alert_unconfigured_floor(self.config, self.logger, "Radarr", instance, total_gb)
        return space_targets(self.config, fallback_gb=self.PRESSURE_THRESHOLD_GB, total_gb=total_gb)

    def _coordinator_owns_deletion(self) -> bool:
        """When the cross-service space coordinator owns deletion, this manager keeps
        its upgrade + downgrade stages but skips its own delete loop (the coordinator
        deletes movies + TV together on one ranked pool)."""
        return coordinator_owns_deletion(self.config)

    def _universe_delete_age_days(self) -> "int | None":
        """On-disk dwell (days) a bare 'universe' title must reach before it becomes
        delete-eligible. None when unset / <= 0 → no ageing guard (byte-identical)."""
        try:
            v = int((self.config or {}).get("universe_delete_age_days", 0) or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

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

    # ── Decision-ledger helpers ──────────────────────────────────────────────────
    @staticmethod
    def _ensure_plan_cols(df) -> None:
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        # Reloaded all-null Parquet columns come back as float64; force the
        # string-plan columns to object so _stamp_plan's str assignments don't
        # trip pandas' incompatible-dtype FutureWarning.
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)

    def _stamp_plan(self, df, idx, action: str, reason: str, reclaim_gb) -> None:
        """Record a planned action on a row (preview-safe; persisted in dry_run).
        ``reclaim_gb`` is +GiB freed (delete/downgrade) or -GiB consumed (upgrade).
        Ensures the ledger columns exist, then delegates the write to the brain
        (ledger.decision_ledger.stamp)."""
        self._ensure_plan_cols(df)
        stamp(df, idx, action, reason, reclaim_gb)

    @timeit("_fetch_hd720p_profile")
    def _fetch_hd720p_profile(self, instance: str) -> dict | None:
        """Fetch the HD-720p quality profile from Radarr by exact name."""
        if self.radarr_api is None:
            return None
        profiles = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
        for p in profiles:
            if (p.get("name") or "").strip().lower() == self.HD_720P_PROFILE_NAME.lower():
                return p
        self.logger.log_warning(
            f"⚠️ Quality profile '{self.HD_720P_PROFILE_NAME}' not found in '{instance}'. "
            f"Available: {[p.get('name') for p in profiles]}"
        )
        return None

    def _downgrade_protect_threshold(self) -> int:
        """Watchability floor BELOW which movies are stepped down under space pressure.

        Default = WATCHABILITY_PROTECT_THRESHOLD (6): only the near-unwatched step down.
        When space_pressure_downgrade_before_delete is on, widen it to MATCH the delete
        ceiling (space_pressure_score_ceiling, default 17) so any title the coordinator
        could delete is shrunk to 720p FIRST — deletion becomes the last resort."""
        if self.config and self.config.get("space_pressure_downgrade_before_delete", False):
            try:
                widened = int(self.config.get("space_pressure_score_ceiling", 17))
            except (TypeError, ValueError):
                widened = 17
            # Same decision surface as the delete ceiling — route it identically
            # so a derived value can never widen one leg and not the other.
            return get_threshold("movie_delete_ceiling", self.config, widened,
                                 logger=getattr(self, "logger", None))
        return self.WATCHABILITY_PROTECT_THRESHOLD

    @staticmethod
    def _profile_max_resolution(profile: dict) -> int:
        """Highest resolution among a profile's *allowed* quality items (incl. nested
        group items); 0 if none. Used as the downgrade-floor resolution so titles already
        at/below it are never 'downgraded' upward."""
        best = 0
        for item in (profile.get("items") or []):
            if not item.get("allowed"):
                continue
            res = (item.get("quality") or {}).get("resolution", 0)
            if isinstance(res, (int, float)):
                best = max(best, int(res))
            for sub in (item.get("items") or []):
                if sub.get("allowed"):
                    sr = (sub.get("quality") or {}).get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
        return best

    def _fetch_ranked_profiles(self, instance: str) -> list[dict]:
        """All Radarr quality profiles sorted ascending by max allowed resolution — the
        ladder the step-down downgrade walks one rank at a time."""
        if self.radarr_api is None:
            return []
        raw = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
        return sorted(raw, key=self._profile_max_resolution)

    @timeit("_build_active_collection_set")
    def _build_active_collection_set(self, df: pd.DataFrame) -> set[str]:
        """
        Return collection_names where any member was watched within
        COLLECTION_WINDOW_DAYS.
        """
        if "collection_name" not in df.columns or "last_watched_at" not in df.columns:
            return set()

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.COLLECTION_WINDOW_DAYS)
        active: set[str] = set()

        watched_mask = df["is_watched"].infer_objects(copy=False).fillna(False).astype(bool)
        for _, row in df[watched_mask].iterrows():
            coll = row.get("collection_name")
            if not coll or pd.isna(coll):
                continue
            lw = row.get("last_watched_at")
            if not lw:
                continue
            try:
                if pd.to_datetime(lw, utc=True) >= cutoff:
                    active.add(str(coll))
            except Exception:
                pass

        return active

    # ── Affinity scoring ──────────────────────────────────────────────────────────

    @timeit("_build_affinity_inputs")
    def _build_affinity_inputs(self, instance: str) -> tuple[dict, set[int], dict[int, set[int]]]:
        """
        Pull the same affinity inputs used by run_movie_ratings() from global_cache.

        Returns (genre_affinity, watched_tmdb_ids, collection_members).
        """
        genre_affinity: dict = {}
        watched_tmdb_ids: set[int] = set()
        collection_members: dict[int, set[int]] = {}

        if not self.global_cache:
            return genre_affinity, watched_tmdb_ids, collection_members

        genre_affinity = self.global_cache.get("tautulli/affinity") or {}

        trakt_history = self.global_cache.get("trakt/history/movies") or []
        for entry in trakt_history:
            tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
            if tmdb_id:
                watched_tmdb_ids.add(int(tmdb_id))

        rating_groups_cfg = (self.config.get("rating_groups", {}) if self.config else {})
        for group_name in (rating_groups_cfg or {"household": {}}):
            raw = self.global_cache.get(f"tautulli/group/{group_name}/tmdb_completions") or {}
            for tmdb_str in raw:
                try:
                    watched_tmdb_ids.add(int(tmdb_str))
                except (ValueError, TypeError):
                    pass

        movies = self.global_cache.get(f"radarr.movies.{instance}.full") or []
        for m in movies:
            coll    = m.get("collection") or {}
            coll_id = coll.get("tmdbId")
            mid     = m.get("tmdbId")
            if coll_id and mid:
                collection_members.setdefault(int(coll_id), set()).add(int(mid))

        return genre_affinity, watched_tmdb_ids, collection_members

    def _build_user_movie_rating_map(self) -> "dict[int, float]":
        """{tmdbId: household Trakt movie rating 0-10} from the cached user ratings
        (best-effort, cache-only — no live Trakt call).

        THE EXACT MIRROR of ``sonarr/cache/episode_files._build_user_show_rating_map``:
        same cache namespace, same username fallback, same "id and rating must both be
        truthy" filter, same swallow-and-continue on a malformed row. The two feed the
        SAME Group-A4 term through the SAME shared formula
        (``scoring/_shared.user_rating_score``), so any divergence here would mean the
        household's own 7/10 counted differently for a film than for a series.

        PRECEDENCE: there is nothing to take precedence OVER. ``score_movie`` has always
        accepted ``user_rating``, but no caller ever supplied it — every movie in this
        library scored ``A4_user_rating: 0.0``. Trakt is therefore A4's FIRST and only
        movie source, not a competitor to one. (``plex/ratings`` produces per-member
        ``userRating`` maps and its own docstring already names the dedupe rule for the
        day it is wired — "OWNER-DEDUPE vs Trakt is mandatory downstream (or one verdict
        hits A4 twice)" — but it is default-off and no scoring path reads it today. The
        F-group critic ratings (imdb/tmdb/trakt/metacritic/RT) are a different question
        entirely: "what did the world think" vs "what did WE think".)"""
        out: "dict[int, float]" = {}
        gc = self.global_cache
        if not gc:
            return out
        # Fallback must match the WRITER (TraktRatingsManager uses .get("username",
        # "default") for the cache-key namespace) — every other Trakt cache key in the app
        # defaults to "default", so a blank username must too or the read silently misses.
        try:
            username = ((self.config.get("trakt", {}) if self.config else {}) or {}).get("username") or "default"
        except Exception:
            username = "default"
        for entry in (gc.get(f"trakt/{username}/ratings/movies") or []):
            try:
                tmdb = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
                rating = entry.get("rating")
                if tmdb and rating:
                    out[int(tmdb)] = float(rating)
            except Exception:
                continue
        return out

    @timeit("_score_row")
    def _score_row(self, row: "pd.Series | dict", genre_affinity: dict,
                   watched_tmdb_ids: set[int], collection_members: dict[int, set[int]],
                   people_manager=None,
                   platform_usage: dict | None = None,
                   transcode_stats: dict | None = None,
                   per_user_affinity: dict | None = None,
                   kids_users: list[str] | None = None,
                   adult_users: list[str] | None = None,
                   related_enabled: bool = False,
                   related_graph_cap: float = 4.0,
                   person_weights: dict | None = None,
                   person_affinity_cap: float = 0.0,
                   intent_index: dict | None = None,
                   intent_cap: float = 0.0,
                   intent_now=None,
                   intent_half_life_days: float | None = None,
                   intent_stale_floor: float | None = None,
                   user_movie_ratings: dict | None = None,
                   language_consumability: bool = False,
                   transcode_profile=None,
                   return_breakdown: bool = False) -> "int | tuple[int, dict]":
        """
        Score a single movie_files Parquet row using score_movie(). ``row`` is a
        pandas Series OR a plain dict (from df.to_dict("records")) — build_movie_feature_row
        reads it through row.get()+pd.notna() coercion, so both yield an identical score.
        Returns [0, 100], defaults to 30 on error. When ``return_breakdown`` is
        True, returns ``(score, breakdown)`` instead — the breakdown is the flat
        per-signal-group contribution dict the pure scorer already produces, so
        the returned score is byte-identical either way.
        """
        # ML Step 3c: the row->MovieFeatureRow marshalling + the score_movie call now
        # live in the brain boundary adapter (machine_learning.features.movie_features).
        # The service keeps only the I/O (credits + related set) and the config view.
        from scripts.managers.machine_learning.features.movie_features import (
            build_movie_feature_row, score_movie_features,
        )
        # Group-D device matrix: the shipped cold-start prior plus whatever the operator
        # added under scoring.device_capabilities (a device the shipped table has never
        # heard of). Returns the shipped table verbatim when the key is absent.
        from scripts.managers.machine_learning.scoring._shared import (
            resolve_device_capabilities,
        )

        try:
            tmdb_id = row.get("tmdb_id")

            credits: dict = {}
            if people_manager and pd.notna(tmdb_id):
                try:
                    credits = people_manager.get_people(int(tmdb_id)) or {}
                except Exception:
                    pass

            # GROUP C3 — collaborative related-graph affinity (daemon-cached neighbours).
            related_tmdb_ids = (
                self._load_related_tmdb_ids(int(tmdb_id))
                if (related_enabled and pd.notna(tmdb_id)) else None
            )

            # GROUP A4 — the household's own Trakt rating for this title (cache-only map
            # built once per pass). None when unrated → A4 stays 0.0.
            user_rating = (user_movie_ratings or {}).get(int(tmdb_id)) if pd.notna(tmdb_id) else None

            fr = build_movie_feature_row(row, credits=credits, related_tmdb_ids=related_tmdb_ids,
                                         user_rating=user_rating)
            return score_movie_features(
                fr,
                genre_affinity=genre_affinity,
                watched_tmdb_ids=watched_tmdb_ids,
                collection_members=collection_members,
                platform_usage=platform_usage,
                transcode_stats=transcode_stats,
                device_capabilities=resolve_device_capabilities(self.config),
                # GROUP D v2 — built once per pass in _build_score_map. None (feature
                # off, or no household transcode evidence at all) → score_movie takes
                # the legacy D1/D2/D3 path, byte-identical.
                transcode_profile=transcode_profile,
                per_user_affinity=per_user_affinity,
                kids_users=kids_users,
                adult_users=adult_users,
                completion_threshold=0.9,
                affinity_boost=_affinity_boost(self.config),
                related_graph_cap=related_graph_cap,
                person_weights=person_weights,
                person_affinity_cap=person_affinity_cap,
                intent_index=intent_index,
                intent_cap=intent_cap,
                intent_now=intent_now,
                intent_half_life_days=intent_half_life_days,
                intent_stale_floor=intent_stale_floor,
                language_consumability=language_consumability,
                return_breakdown=return_breakdown,
            )
        except Exception:
            return (30, {}) if return_breakdown else 30

    def _apply_watchlist_shield(self, df) -> int:
        """Stamp ``watchlist_hold`` / ``watchlist_hold_by`` onto the movie_files frame.

        Robert's decision: a watchlisted title is SHIELDED from deletion, not merely scored
        higher. A5's +4.8 cannot on its own lift a weak-taste title over the delete ceiling
        (17), and deleting something the household explicitly asked for is the one deletion
        that is never defensible.

        THE HOLD EXPIRES — otherwise one forgotten watchlist entry holds disk forever. It
        expires exactly the way ``lifecycle/saga_retention`` already expires watchlist
        intent (``watchlist_hold_policy: windowed``): on the WATCHLISTER'S OWN DORMANCY,
        anchored on that member's last play, over the same 90-day window
        (``scoring.watchlist_intent.shield.dormancy_window_days``, defaulting to
        ``saga_retention.dormancy_window_days``). A title added in 2020 by somebody who
        watched something last night is live intent; a title added last week by an account
        dormant for six months is not.

        Returns the number of held rows. All-False (byte-identical to the previous delete
        behaviour) when the shield is disabled, nothing is watchlisted, or no watchlister
        resolves to an active account."""
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
                                     logger=getattr(self, "logger", None)) or {}).get("movies") or {}
        if not index or "tmdb_id" not in df.columns:
            return 0
        now = datetime.now(tz=timezone.utc)
        held = 0
        _tm = pd.to_numeric(df["tmdb_id"], errors="coerce")
        for idx in df.index:
            t = _tm.at[idx]
            if pd.isna(t):
                continue
            entry = index.get(int(t))
            if not entry or not intent_hold_active(entry, now, dormancy_days=dormancy):
                continue
            df.at[idx, "watchlist_hold"] = True
            df.at[idx, "watchlist_hold_by"] = ", ".join(entry.get("members") or ()) or "watchlist"
            held += 1
        if held:
            self.logger.log_info(
                f"[Intent] watchlist shield: {held} movie(s) held from deletion "
                f"(released after {dormancy:.0f}d of watchlister dormancy).")
        return held

    def _build_transcode_profile(self, platform_usage: dict | None):
        """The household Group-D-v2 transcode profile, or None (→ the legacy D1/D2/D3
        terms). Built ONCE per scoring pass — it is a household-level object, not a
        per-title one.

        Reads the two Tautulli buckets the cause model is learned from:
          * ``tautulli/stream_decisions``     — per-stream ground truth (why a play
            transcoded), classified by the same function the operator-facing
            "transcode causes household-wide" report uses;
          * ``tautulli/transcode_fingerprint`` — direct/transcode counts per
            (device, network) cell, giving the base rate and the remote share.

        Returns None on ANY failure as well as on "no evidence", so a broken or empty
        cache degrades to the previously-shipped scoring path rather than to a new one
        running blind."""
        try:
            from scripts.managers.machine_learning.scoring.device_fit import (
                build_transcode_profile, resolve_device_fit,
            )
            from scripts.managers.machine_learning.scoring._shared import (
                resolve_device_capabilities,
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
            self.logger.log_debug(f"[SpacePressure] device_fit_v2 profile unavailable: {e}")
            return None

    def _report_group_d(self, breakdowns, instance: str, profile) -> None:
        """Log the pass's Group-D distribution. THE REGRESSION GUARD THIS WHOLE REDESIGN
        EXISTS FOR: Group D v1 silently collapsed to a near-constant (92% of movies at
        EXACTLY 12.0) and nothing in the run output said so, while every absolute
        threshold anchored on the score was quietly invalidated. A ``mode`` share back
        near 1.0, or ``distinct`` collapsing toward 1, is now visible in the run log the
        first time it happens."""
        try:
            from scripts.managers.machine_learning.scoring.device_fit import summarise_group_d
            vals = []
            for bd in breakdowns:
                if not isinstance(bd, dict):
                    continue
                vals.append(bd.get("D4_transcode_risk", 0.0)
                            + bd.get("D1_device_capability", 0.0)
                            + bd.get("D2_transcode_avoidance", 0.0)
                            + bd.get("D3_platform_ceiling", 0.0))
            s = summarise_group_d(vals)
            if not s["n"]:
                return
            mode = "v2 risk-penalty" if profile is not None else "v1 legacy bonus"
            extra = ""
            if profile is not None:
                extra = (f" | causes " +
                         ", ".join(f"{k}:{v:.0%}" for k, v in sorted(
                             profile.cause_weights.items(), key=lambda kv: -kv[1]))
                         + f" (n={profile.n_decisions} decisions, {profile.n_plays} plays,"
                           f" base rate {profile.base_rate:.1%})")
            self.logger.log_info(
                f"[SpacePressure] '{instance}' Group D ({mode}): mean {s['mean']:+.2f} "
                f"sd {s['sd']:.2f} range [{s['min']:+.2f}, {s['max']:+.2f}] "
                f"mode {s['mode']:+.2f} @ {s['share_at_mode']:.1%} of {s['n']}, "
                f"{s['distinct']} distinct value(s).{extra}")
            if s["share_at_mode"] >= 0.75 or s["distinct"] <= 2:
                self.logger.log_warning(
                    f"[SpacePressure] '{instance}' Group D is behaving as a CONSTANT "
                    f"({s['share_at_mode']:.0%} of titles share one value, {s['distinct']} "
                    f"distinct) — it is contributing no ranking information and every "
                    f"absolute threshold anchored on the score is drifting. This is the "
                    f"exact regression scoring/device_fit.py was written to prevent.")
        except Exception:
            pass

    @timeit("_build_score_map")
    def _build_score_map(self, df: pd.DataFrame, instance: str,
                         with_breakdown: bool = False) -> dict:
        """Return ``{df_index: watchability_score}`` for every row using affinity
        cache data. When ``with_breakdown`` is True, the value is instead
        ``(score, breakdown)`` — the score is identical, only the explanation dict
        is added (the persistence path uses this; decision paths don't)."""
        genre_affinity, watched_tmdb_ids, collection_members = self._build_affinity_inputs(instance)

        # Pull device/transcode/per-user context from global_cache
        platform_usage:   dict | None = None
        transcode_stats:  dict | None = None
        per_user_affinity: dict | None = None
        kids_users: list[str] = []
        adult_users: list[str] = []

        if self.global_cache:
            platform_usage  = self.global_cache.get("tautulli/platforms") or None
            transcode_stats = self.global_cache.get("tautulli/transcode") or None
            per_user_affinity = {}
            # Load per-user affinity from each user's cache key
            try:
                import re
                users_dir = "tautulli/users"
                # Iterate known users from config rating_groups
                cfg_groups = (self.config or {}).get("rating_groups", {})
                for group in cfg_groups.values():
                    for member in (group.get("members") or []):
                        safe = re.sub(r'[\\/:*?"<>|]', '_', member).strip()
                        ua   = self.global_cache.get(f"tautulli/users/{safe}/affinity")
                        if ua:
                            per_user_affinity[member] = ua
                    for member in (group.get("grace_members") or []):
                        kids_users.append(member)
                    for member in (group.get("members") or []):
                        if member not in kids_users:
                            adult_users.append(member)
                # ROSTER FALLBACK. ``rating_groups`` is read here but has no shipped
                # default and no fallback, so an install that never declared it scored
                # every title with per_user_affinity={} and BOTH cohort lists empty —
                # movie_scorer's E1 silently degrades to its flat +2 branch and E2 (+4)
                # is dead for every adult-certified title. _build_affinity_inputs already
                # guards the same key with a {"household": {}} default; this is the
                # matching guard for the per-user path.
                #
                # The cohort is NOT guessed: PlexUsersManager already resolves an age
                # tier per profile (Plex Home parental controls, or the
                # plex.playlists.profile_ages override) and that is exactly the split
                # the playlist builders run on. Reuse it rather than inventing a second
                # classifier that could disagree.
                if not per_user_affinity and not kids_users and not adult_users:
                    _roster = []
                    # PREFERRED source: ``plex.playlists.profile_ages`` is the
                    # operator's OWN explicit {username: age_tier} map, already
                    # driving the playlist age-gate. Being plain config it has NO
                    # ordering dependency - unlike the Plex roster below, it is
                    # available on the very first pass. ``ignored_users`` is honoured
                    # so an excluded profile cannot skew a cohort's affinity.
                    try:
                        _ages = (((self.config or {}).get("plex", {}) or {})
                                 .get("playlists", {}) or {}).get("profile_ages", {}) or {}
                        _ignored = {str(x).strip().lower()
                                    for x in ((self.config or {}).get("ignored_users") or [])}
                        _roster = [{"title": _n, "restriction_profile": _t}
                                   for _n, _t in _ages.items()
                                   if _n and str(_n).strip().lower() not in _ignored]
                    except Exception:
                        _roster = []
                    if not _roster:
                        try:
                            _pum = self.registry.get("manager", "PlexUsersManager") if self.registry else None
                            _roster = list(getattr(_pum, "tracked_users", None) or [])
                        except Exception:
                            _roster = []
                    # ORDERING. PlexUsersManager.run() lands AFTER the Radarr
                    # scoring phase (measured: 06:20:43 vs scores at 06:20:32-36),
                    # so ``tracked_users`` is EMPTY in memory on the very pass that
                    # needs it. Fall back to the roster it persisted last run - the
                    # age tier is stable between runs, so the cohorts self-heal
                    # after one pass instead of needing a pipeline reorder.
                    if not _roster and self.global_cache:
                        try:
                            _cached = self.global_cache.get("plex/users") or []
                            _ident = self.global_cache.get("plex/identity_map") or {}
                            _roster = [{
                                "title": u.get("title"),
                                "restriction_profile": u.get("restriction_profile"),
                                "tautulli_username": (_ident.get(u.get("uuid")) or {}).get("tautulli_username"),
                                "safe_user": (_ident.get(u.get("uuid")) or {}).get("safe_key"),
                            } for u in _cached if isinstance(u, dict)]
                        except Exception:
                            _roster = []
                    for u in _roster:
                        member = (u.get("tautulli_username") or u.get("title") or "").strip()
                        if not member:
                            continue
                        safe = (u.get("safe_user")
                                or re.sub(r'[\\/:*?"<>|]', '_', member).strip())
                        ua = self.global_cache.get(f"tautulli/users/{safe}/affinity")
                        if ua:
                            per_user_affinity[member] = ua
                        _tier = str(u.get("restriction_profile") or "").strip().lower()
                        if _tier in _KID_AGE_TIERS:
                            kids_users.append(member)
                        else:
                            adult_users.append(member)
                    if _roster:
                        self.logger.log_debug(
                            f"[SpacePressure] rating_groups unset - cohorts derived from the "
                            f"Plex roster: {len(adult_users)} adult, {len(kids_users)} kid, "
                            f"{len(per_user_affinity)} with cached affinity. Declare "
                            f"rating_groups (members / grace_members) to override."
                        )
            except Exception:
                pass

        people_manager = None
        try:
            trakt_movies   = self.registry.get("manager", "TraktMoviesManager")
            people_manager = getattr(trakt_movies, "people", None) if trakt_movies else None
        except Exception:
            pass

        # GROUP C3 — related-graph collaborative affinity (config.scoring.related_graph).
        _rg = ((self.config or {}).get("scoring", {}) or {}).get("related_graph", {}) or {}
        related_enabled = bool(_rg.get("enabled", True))
        try:
            related_graph_cap = float(_rg.get("cap", 4.0))
        except (TypeError, ValueError):
            related_graph_cap = 4.0
        # File-aware G1 language gate (oracle-mover, default OFF) — see episode_files.
        _lc = ((self.config or {}).get("scoring", {}) or {}).get("language_consumability", {}) or {}
        language_consumability = bool(_lc.get("enabled", False)) if isinstance(_lc, dict) else bool(_lc)

        # GROUP C4 — cast/crew taste overlap (config.scoring.person_affinity). Load the
        # household person-affinity ONCE per pass; the shared resolver forces cap=0.0
        # (byte-identical) when the term is disabled or the people-matrix has never been
        # built, so libraries without it score exactly as before.
        from scripts.managers.machine_learning.scoring._shared import resolve_person_affinity_inputs
        _aff_raw = self.global_cache.get("people_matrix/affinity") if self.global_cache else None
        person_weights, person_affinity_cap = resolve_person_affinity_inputs(self.config, _aff_raw)

        # GROUP A5 — explicit watchlist intent (config.scoring.watchlist_intent). Fold the
        # cached forward-intent feeds into ONE index per pass; the shared resolver forces
        # cap=0.0 (byte-identical) when the term is disabled or nothing is watchlisted.
        # ``intent_now`` is the run-stable clock the staleness decay measures against —
        # passed in rather than read per row so every title in a pass decays against the
        # same instant (and the memo cannot flip mid-run).
        from scripts.managers.machine_learning.scoring._shared import resolve_intent_inputs
        from scripts.managers.services._intent_index import (
            gather_intent_index, intent_memo_fingerprint,
        )
        _intent_all = gather_intent_index(self.global_cache, self.config,
                                          logger=getattr(self, "logger", None))
        (intent_index, intent_cap,
         intent_half_life, intent_floor) = resolve_intent_inputs(self.config,
                                                                 _intent_all.get("movies"))
        intent_now = datetime.now(tz=timezone.utc)

        # GROUP A4 — the household's own declared Trakt ratings, loaded ONCE per pass (the
        # show pass does the same with _build_user_show_rating_map). Empty map → A4 stays
        # 0.0 on every row, i.e. exactly how movies scored before this was threaded.
        user_movie_ratings = self._build_user_movie_rating_map()

        # GROUP D v2 (config.scoring.device_fit_v2, DEFAULT ON) — the household transcode
        # profile: observed per-stream decision CAUSES + the direct/transcode fingerprint,
        # blended with the shipped prior. Built ONCE per pass and reused for every row.
        # None → the legacy D1/D2/D3 bonus terms, byte-identical.
        transcode_profile = self._build_transcode_profile(platform_usage)

        # Iterate plain row dicts (one to_dict("records") pass) rather than building a
        # fresh pd.Series per row via df.loc[idx] — the classic per-row anti-pattern over
        # a few-thousand-row library. build_movie_feature_row reads every field through
        # row.get(col) + pd.notna() coercion, so a dict row yields a byte-identical
        # MovieFeatureRow (and thus an identical score) — see features/test_movie_features.
        # ── Score memo (mirrors the Sonarr show-score memo): rescore only rows
        # whose inputs changed. Context hash guards household-wide inputs; the
        # per-row key hashes the row dict + the DAY (bounds recency drift AND
        # daemon-credit arrival to <24h). Keyed by tmdb/file id, NOT df index
        # (indexes shift as the library changes). Sampled parity audit shares
        # the show memo's knob. Any miss/error → the identical scoring path.
        import hashlib as _hl
        import json as _json
        import random as _rnd
        from datetime import datetime as _dt, timezone as _tz

        def _h(o) -> str:
            try:
                return _hl.sha1(_json.dumps(o, sort_keys=True, default=str)
                                .encode("utf-8", "replace")).hexdigest()
            except Exception:
                return ""

        # SCORER_REVISION is part of the context on purpose: the memo is otherwise
        # keyed only on INPUTS, which do not change when the scoring CODE does — so a
        # wired-in signal group (or a device-table entry) would keep serving scores
        # computed by the previous revision until the 1% parity audit happened to catch
        # it. Bumping the constant forces exactly one full rescore.
        from scripts.managers.machine_learning.scoring._shared import SCORER_REVISION
        _ctx = _h([genre_affinity, sorted(watched_tmdb_ids or []),
                   {str(k): sorted(v) for k, v in (collection_members or {}).items()},
                   platform_usage, transcode_stats, per_user_affinity, kids_users,
                   adult_users, related_enabled, related_graph_cap, person_weights,
                   person_affinity_cap, language_consumability,
                   people_manager is not None,
                   _dt.now(tz=_tz.utc).date().isoformat(), bool(with_breakdown),
                   # The operator's Group-D device overrides. The SHIPPED matrix moves
                   # only with SCORER_REVISION, but an edit to scoring.device_capabilities
                   # changes D1/D2/D3 without changing any other input — so it has to be
                   # part of the key or the memo keeps serving scores from the old table.
                   ((self.config or {}).get("scoring", {}) or {}).get("device_capabilities"),
                   # Group-D v2: the household transcode profile is a per-PASS input that
                   # lives outside the per-row data, so it has to be in the CONTEXT hash
                   # or a household whose observed cause mix shifts keeps being served
                   # scores computed from the old weights.
                   (transcode_profile.memo_key() if transcode_profile is not None else None),
                   # Group-A5: the watchlist index is a per-PASS household input that is in
                   # NEITHER half of the per-row key — `_h(row)` hashes the parquet row, and
                   # the watchlist lives outside the parquet entirely. Without this line,
                   # adding a title to your watchlist would never invalidate its memoized
                   # score and A5 would silently do nothing. Only the score-relevant fields
                   # are digested (see intent_memo_fingerprint), so a cosmetic union change
                   # does not force a needless full rescore.
                   # The DECAY knobs ride with the cap for the same reason: they are
                   # config, not row data, and they move every DATED title's A5 without
                   # moving one input `_h(row)` can see. Editing half_life_days with only
                   # the cap in the hash would rescore nothing.
                   intent_cap, intent_half_life, intent_floor,
                   intent_memo_fingerprint(_intent_all),
                   # Group-A4: the household's Trakt movie ratings are a per-PASS input
                   # that lives OUTSIDE the parquet — `_h(row)` cannot see it. Without
                   # this line, changing your Trakt rating for a film you already own
                   # would never invalidate its memoized score and A4 would appear frozen.
                   # (The show memo has hashed its ratings map since it was written; this
                   # is the same line on the movie side.)
                   sorted((k, v) for k, v in (user_movie_ratings or {}).items()),
                   SCORER_REVISION])
        _MEMO_KEY = f"radarr/{instance}/movie_score_memo"
        _prev: dict = {}
        if self.global_cache and _ctx:
            try:
                _b = self.global_cache.get(_MEMO_KEY) or {}
                if _b.get("ctx") == _ctx and isinstance(_b.get("rows"), dict):
                    _prev = _b["rows"]
            except Exception:
                _prev = {}
        try:
            _audit_pct = float(((self.config or {}).get("scoring", {}) or {})
                               .get("show_score_memo_audit_pct", 0.01) or 0.0)
        except (TypeError, ValueError):
            _audit_pct = 0.01

        _next: dict = {}
        out: dict = {}
        _hits = _audited = _mismatches = 0
        for idx, row in zip(df.index, df.to_dict("records")):
            _rk = str(row.get("tmdb_id") or row.get("movie_file_id") or idx)
            _sk = _h(row)
            _hit = _prev.get(_rk) if _sk else None
            _expect = None
            if _hit and _hit.get("k") == _sk:
                if _audit_pct > 0 and _rnd.random() < _audit_pct:
                    _expect = _hit.get("v")     # fall through: rescore + compare
                else:
                    _v = _hit.get("v")
                    out[idx] = (_v[0], _v[1]) if (with_breakdown and isinstance(_v, list)) else _v
                    _next[_rk] = _hit
                    _hits += 1
                    continue
            _sv = self._score_row(
                row,
                genre_affinity=genre_affinity,
                watched_tmdb_ids=watched_tmdb_ids,
                collection_members=collection_members,
                people_manager=people_manager,
                platform_usage=platform_usage,
                transcode_stats=transcode_stats,
                per_user_affinity=per_user_affinity,
                kids_users=kids_users,
                adult_users=adult_users,
                related_enabled=related_enabled,
                related_graph_cap=related_graph_cap,
                person_weights=person_weights,
                person_affinity_cap=person_affinity_cap,
                intent_index=intent_index,
                intent_cap=intent_cap,
                intent_now=intent_now,
                intent_half_life_days=intent_half_life,
                intent_stale_floor=intent_floor,
                user_movie_ratings=user_movie_ratings,
                language_consumability=language_consumability,
                transcode_profile=transcode_profile,
                return_breakdown=with_breakdown,
            )
            out[idx] = _sv
            if _sk:
                _next[_rk] = {"k": _sk, "v": list(_sv) if isinstance(_sv, tuple) else _sv}
                if _expect is not None:
                    _audited += 1
                    _fresh = list(_sv) if isinstance(_sv, tuple) else _sv
                    if _fresh != _expect:
                        _mismatches += 1
                        self.logger.log_warning(
                            f"[SpacePressure] movie score memo parity MISMATCH for {_rk}: "
                            f"memo={_expect!r} fresh={_fresh!r} — fresh wins; an input is "
                            f"missing from the memo key (report this).")
        if self.global_cache and _ctx and _next:
            try:
                self.global_cache.set(_MEMO_KEY, {"ctx": _ctx, "rows": _next}, pretty=False)
            except Exception:
                pass
        if _hits or _audited:
            self.logger.log_info(
                f"[SpacePressure] movie score memo: {_hits}/{len(out)} unchanged — reused "
                f"(rescored {len(out) - _hits}; audited {_audited}, {_mismatches} mismatch(es)).")
        # Group-D distribution guard. Only the breakdown-bearing pass (the persistence
        # path) carries the per-signal dict, which is exactly the pass that writes the
        # scores every threshold is anchored on.
        if with_breakdown:
            self._report_group_d(
                [v[1] for v in out.values() if isinstance(v, tuple) and len(v) == 2],
                instance, transcode_profile)
        return out

    def _load_related_tmdb_ids(self, tmdb_id: int) -> set[int]:
        """Read this movie's daemon-cached Trakt related set (cache-only) and return
        the related neighbours' TMDb ids. Empty set when uncached / empty / unreadable
        — so the C3 term degrades gracefully to 0 until the daemon fills the bucket.

        The daemon writes ``movie_related/{tmdb_id}.json.gz`` as a bare list of movie
        objects, each ``{"ids": {"tmdb": ..., ...}, "title": ..., "year": ...}``.
        """
        import gzip
        import json
        from scripts.managers.factories.daemons.daemon_paths import MOVIE_BUCKETS

        try:
            path = MOVIE_BUCKETS["related"] / f"{int(tmdb_id)}.json.gz"
            if not path.exists():
                return set()
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return set()
        out: set[int] = set()
        for entry in (data or []):
            tid = ((entry or {}).get("ids") or {}).get("tmdb")
            if tid:
                try:
                    out.add(int(tid))
                except (TypeError, ValueError):
                    continue
        return out

    # ── Stage 1: downgrade to HD-720p ────────────────────────────────────────────

    def _is_uhd_instance(self, instance) -> bool:
        """Is *instance* the dedicated 4K/UHD Radarr session?

        Read from ``radarr_instances_categorized`` (the operator's own tier map, captured
        during onboarding) using the SAME alias list uhd_reconcile and radarr/repair/anomaly
        use -- the role map writes "4K" while the folder bucket is "4k", and operators
        reasonably use "uhd" or "2160p".

        Fails CLOSED to False: an unreadable map means this manager behaves exactly as it
        did before (an ordinary 720p-floor instance), rather than silently disabling the
        downgrade pass everywhere.
        """
        if not instance:
            return False
        try:
            cat = (self.config.get("radarr_instances_categorized", {}) or {}) if self.config else {}
        except Exception:
            return False
        if not isinstance(cat, dict):
            return False
        target = str(instance).strip().casefold()
        for label in UHD_INSTANCE_LABELS:
            got = cat.get(label)
            if got and str(got).strip().casefold() == target:
                return True
        return False

    @LoggerManager().log_function_entry
    @timeit("run_space_pressure_downgrades")
    def run_downgrades(self, instance: str, free_space_gb: float) -> dict:
        """
        Set low-priority/low-score movies to HD-720p and trigger MovieSearch.
        Movies with watchability score >= WATCHABILITY_PROTECT_THRESHOLD are protected.
        """
        # GLD-DEL-05 — step-down rows accumulate here and flush once after the loop.
        _archive: list = []
        if not getattr(self, "_deletion_run_id", None):
            self._deletion_run_id = new_run_id()
        stats = {
            "candidates_found":   0,
            "downgraded":         0,
            "already_at_720p":    0,
            "skipped_protected":  0,
            "skipped_high_score": 0,
            "skipped_recent":     0,
            "skipped_universe":   0,
            "failed":             0,
            # ── exhaustive-mode accounting (all 0 on the legacy path) ──
            "inflight_regrab_gb": 0.0,   # projected size of replacements queued THIS run
            "freed_now_gb":       0.0,   # bytes actually removed from disk this pass
            "deferred_cap":       0,     # over space_downgrade_max_regrabs_per_run → next run
            "stopped_at_target":  0,     # candidates left untouched once free (net) reached U
            "below_floor_picks":  0,     # stepped BELOW 720 — no >=720 release exists
        }

        mfm = self._get_movie_files_manager()
        if mfm is None:
            self.logger.log_warning("[SpacePressure] movie_files manager unavailable — skipping downgrades")
            return stats

        df = mfm.load(instance)
        if df.empty:
            return stats

        ranked_profiles = self._fetch_ranked_profiles(instance)
        if not ranked_profiles:
            self.logger.log_warning("[SpacePressure] Could not fetch quality profiles — skipping downgrades")
            return stats
        # Movies floor at the HD-720p resolution: they step DOWN toward it (4K → 1080p →
        # 720p) but never below (universe titles, which may reach SD, are handled by the
        # universe manager).
        #
        # EXCEPT ON THE DEDICATED 4K INSTANCE, where the floor is 2160p — i.e. nothing to
        # step down to, so this pass finds no candidates there. Two reasons:
        #
        #  1. IT FOUGHT uhd_reconcile. In the SAME run, uhd_reconcile plans to MOVE 2160p
        #     copies ONTO the 4K instance while this pass planned to SHRINK them once they
        #     arrived. uhd_reconcile already excludes the 4K instance from its move sources
        #     ("the guard that stops the sweep from dragging a real 4K library into the
        #     move"); this manager had no equivalent guard and treated every instance alike.
        #  2. SHRINKING A 4K COPY IN PLACE LOSES THE TIER WITH NO BASELINE. The purpose-built
        #     path (uhd_reconcile._demote_overqualified_4k) deletes the 4K FILE only once a
        #     ≤1080p baseline is confirmed surviving on standard, ledgers the shell, and
        #     re-acquires if the score recovers — make-before-break. Stepping 2160p → 720p in
        #     place gives up the 4K with nothing held anywhere.
        #
        # 4K space is therefore reclaimed by eviction, not by downgrade.
        hd720p = self._fetch_hd720p_profile(instance)
        floor_resolution = (self._profile_max_resolution(hd720p) or 720) if hd720p is not None else 720
        if self._is_uhd_instance(instance):
            floor_resolution = 2160
            self.logger.log_debug(
                f"[SpacePressure] '{instance}' is the dedicated 4K instance — step-down floor "
                f"is 2160p (no downgrade); its space is reclaimed by the 4K eviction path, "
                f"which keeps a 1080p baseline before removing anything.")

        now           = datetime.now(tz=timezone.utc)
        recent_cutoff = now - timedelta(days=self.RECENT_WATCH_DAYS)
        active_colls  = self._build_active_collection_set(df)
        score_map     = self._build_score_map(df, instance)

        _floor_gb, U = self._space_targets(instance)
        # TIERED, NOT CONCURRENT. Every space pass in the run reads the SAME free-space
        # figure from the SAME shared mount, so before this they each planned their full
        # need independently: standard planned 396 GB, ultra 205 GB and Sonarr TV 476 GB
        # against ONE 922 GB pool -- ~1077 GB of reclaim for a deficit of ~4575 GB, with no
        # pass aware that the others had already committed to part of it. Now each pass
        # ADDS what earlier passes have already planned this run to its effective free
        # space, so the tree drains a single shared deficit instead of three passes racing
        # the same number. Same idea as the in-run `inflight_regrab_gb` subtraction, lifted
        # from within one pass to across all of them.
        _planned = planned_reclaim_gb(self.global_cache)
        need_gb = max(0.0, U - (float(free_space_gb) + _planned))
        self.logger.log_info(
            f"[SpacePressure] '{instance}': {free_space_gb:.1f} GB free "
            f"(floor {_floor_gb:.0f} GB; need ~{need_gb:.0f} GB to band top {U:.0f} GB"
            + (f"; {_planned:.0f} GB already planned by earlier passes this run" if _planned else "")
            + f"). Active collections last {self.COLLECTION_WINDOW_DAYS}d: {len(active_colls)}"
        )

        # DECISION (ML Step 7c): the brain (space.downgrade_planner.plan_movie_downgrades)
        # steps the lowest-watchability movies DOWN the ranked ladder one rank at a time,
        # spread across the pool, until ~need_gb is reclaimed (no single title crushed to
        # the floor). The service APPLIES each per-title target (PUT + search + stamp).
        # Optional: widen the downgrade band to MATCH the delete band, so any title the
        # coordinator could delete is shrunk to 720p FIRST and only deleted if downgrades
        # can't free enough (make-before-break via Radarr's replace; deletion = last resort).
        # EXHAUSTIVE (space_exhaustive_downgrade, DEFAULT ON): plan EVERY movie above the
        # 720p floor down to it — no score ceiling, no early stop at a partial need_gb —
        # because the delete pools now only accept items already AT/BELOW that floor.
        # Deletion therefore cannot start while anything is still shrinkable. The apply
        # loop below is what stops at U, using a free-space figure NET of the re-grabs it
        # queued this run, and is bounded by space_downgrade_max_regrabs_per_run.
        _exhaustive = exhaustive_downgrade(self.config)
        # PASS-LEVEL RATE LIMIT. Exhaustive mode plans EVERY title above the floor,
        # so an unthrottled pass can re-admit the very files the previous one just
        # created. Radarr's deletion history for 2026-08-08 shows exactly that:
        # Edge of Tomorrow deleted six times in one day, and the 720p step-down
        # REPLACEMENTS deleted the following day. ~5.6 TiB went to the recycle bin
        # to satisfy a 1546 GB deficit that resolved itself when the bin cleared.
        #
        # Only exhaustive mode is throttled. The targeted path stops at need_gb and
        # cannot run away; exhaustive has no such bound by design.
        _pass_ledger = {}
        # GLD-RAD-36 - every other global_cache read in this file is guarded (see _affinity_inputs
        # and the device/transcode block); this one must be too. The throttle needs a
        # PERSISTED last-run stamp, so with no usable cache there is no record to read
        # - which pass_allowed() already treats as "first run, allow". Warn rather than
        # skip silently: a cacheless run is exactly when an unthrottled exhaustive pass
        # would go unnoticed.
        _gc = self.global_cache if hasattr(self.global_cache, "get") else None
        if _exhaustive:
            if _gc is None:
                self.logger.log_warning(
                    "  [SpacePressure] no usable global_cache, so the exhaustive "
                    "step-down rate limit cannot be enforced this run.")
            _pass_ledger = (_gc.get(
                stepdown_cooldown.ledger_key("radarr", instance)) if _gc else None) or {}
            _verdict = stepdown_cooldown.pass_allowed(
                _pass_ledger, free_gb=free_space_gb, config=self.config)
            if not _verdict["allowed"]:
                self.logger.log_info(
                    f"  [SpacePressure] exhaustive step-down SKIPPED on '{instance}': "
                    f"{_verdict['reason']}. Falling back to targeted downgrades "
                    f"(need {need_gb:.0f} GB).")
                _exhaustive = False
                stats["exhaustive_rate_limited"] = 1
            elif _verdict.get("extreme"):
                self.logger.log_warning(
                    f"  [SpacePressure] {_verdict['reason']} on '{instance}'.")
        protect = self._downgrade_protect_threshold()
        candidates, _pstats = plan_movie_downgrades(
            df, score_map, ranked_profiles,
            need_gb=need_gb,
            recent_cutoff=recent_cutoff,
            active_colls=active_colls,
            protect_threshold=protect,
            floor_resolution=floor_resolution,
            exhaustive=_exhaustive,
        )
        stats.update(_pstats)
        if _exhaustive and candidates:
            # Stamped on RUN, not on success: a pass that admitted titles and then
            # failed still churned the library, and spacing THAT out is the point.
            # Only stamped when it actually admitted something, so a no-op pass does
            # not burn the next 12 hours.
            if _gc is not None and hasattr(_gc, "set"):
                _gc.set(
                    stepdown_cooldown.ledger_key("radarr", instance),
                    stepdown_cooldown.stamp_pass(_pass_ledger, admitted=len(candidates)))
        # Publish this pass's projected reclaim so LATER passes (the other Radarr instance,
        # Sonarr TV, the universe pass, the coordinator) plan against the remaining deficit
        # rather than the same one. Projected, not realized: the whole point is that the
        # replacements have not landed yet, and a later pass must not re-plan the space this
        # one has already committed to freeing.
        record_planned_reclaim(self.global_cache, f"radarr:{instance}:downgrade",
                               float(_pstats.get("est_reclaim_gb", 0.0) or 0.0))

        if not candidates:
            self.logger.log_info("[SpacePressure] No downgrade candidates found.")
            return stats

        self.logger.log_info(
            f"[SpacePressure] {len(candidates)} step-down candidate(s) "
            f"(~{_pstats.get('est_reclaim_gb', 0):.0f} GB projected, "
            f"target {'met' if _pstats.get('target_met') else 'NOT met — deletions cover the rest'}):"
        )
        _regrab_cap = downgrade_regrab_cap(self.config) if _exhaustive else 0
        if _exhaustive:
            self.logger.log_info(
                f"[SpacePressure] exhaustive step-down: planning every title above the "
                f"{floor_resolution}p floor ({_pstats.get('over_ceiling_included', 0)} admitted over "
                f"the score ceiling); stopping at {U:.0f} GB free NET of in-flight re-grabs; "
                f"re-grab cap {_regrab_cap if _regrab_cap > 0 else 'off'}/run."
            )

        changed = False
        plan_changed = False
        movie_ids_to_search: list[int] = []
        # Cooldown ledger: loaded once, mutated in the loop, saved once at the end.
        from scripts.support.utilities.stepdown_cooldown import (
            clear as _clear, cooldown_left as _cooldown_left, entry_key as _ekey,
            stamp_failure as _stamp_failure, wait_days as _wait_days,
        )
        _ledger = self._stepdown_ledger(instance)

        for c in candidates:
            # ── IN-FLIGHT ACCOUNTING (exhaustive only) ────────────────────────────
            # A realized step-down deletes the file NOW and imports the smaller
            # replacement LATER, so free space SPIKES mid-pass. Deciding against that
            # spike would keep downgrading against phantom headroom, so the stop test
            # uses free + (bytes actually removed) − (projected size of every
            # replacement queued this run).
            if _exhaustive:
                _net_free = float(free_space_gb) + stats["freed_now_gb"] - stats["inflight_regrab_gb"]
                if _net_free >= U:
                    stats["stopped_at_target"] += 1
                    continue
                if _regrab_cap > 0 and stats["downgraded"] >= _regrab_cap:
                    # Bandwidth guard: the file is KEPT (still above the floor, so still
                    # excluded from deletion) and re-qualifies next run.
                    stats["deferred_cap"] += 1
                    continue
            idx         = c["idx"]
            movie_id    = c["movie_id"]
            target_id   = c["target_id"]
            target_name = c["target_name"]
            reason      = c["reason"]
            reclaim     = c["reclaim_gb"]
            cur_qp_name = c["cur_name"]
            title       = df.at[idx, "title"] or f"movie {movie_id}"
            _sz_raw     = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
            _sz_f       = float(_sz_raw) if pd.notna(_sz_raw) else 0.0

            # Decision ledger: record the step-down plan + its (cumulative) reclaim.
            # Persists in dry_run so the plan is previewable.
            self._stamp_plan(df, idx, "downgrade", f"{reason} → {target_name}", reclaim)
            plan_changed = True

            if self.dry_run:
                # debug: the per-title detail is stamped into the decision ledger
                # (line above) and rendered in the end-of-run "Change plan" grid —
                # the live log keeps only the pass summary table.
                self.logger.log_debug(
                    f"  📉 [dry_run] Would step down: '{title}' "
                    f"({self._fmt_bytes(_sz_f)}, {cur_qp_name} → {target_name}, ~{reclaim:.1f} GB) — {reason}"
                )
                stats["downgraded"] += 1
                # Model the same free/in-flight split in the PREVIEW so the dry-run plan
                # stops at U exactly where a live run would (no pick to size, so the
                # replacement is the planner's estimate: current size − cumulative reclaim).
                stats["freed_now_gb"] += _sz_f / (1024 ** 3)
                stats["inflight_regrab_gb"] += max(0.0, _sz_f / (1024 ** 3) - float(reclaim))
                continue

            try:
                # GLD-RAD-32 (operator ruling 2026-08-07): the space step-down NEVER runs on
                # the 4K instance — its whole purpose is to shrink below current, and on
                # ultra everything below current is sub-2160, which is a PLACEMENT event
                # (UhdReconcile demote/rehome + the coordinator's evict_uhd_first own it),
                # not a grab. Skip before the payload fetch, PUT, and indexer call.
                _uhd_inst = (self.config.get("radarr_instances_categorized") or {}).get("4K")
                if _uhd_inst and instance == _uhd_inst:
                    stats["uhd_deferred"] = stats.get("uhd_deferred", 0) + 1
                    self.logger.log_info(
                        f"  📦 '{title}': step-down on the 4K instance '{instance}' — deferred "
                        f"to the UHD demote/evict machinery (no profile change, no grab).")
                    continue
                payload = self.radarr_api._make_request(instance, f"movie/{movie_id}", fallback=None)
                if not payload or not isinstance(payload, dict):
                    self.logger.log_warning(f"  ⚠️ Could not fetch payload for '{title}' (id={movie_id})")
                    stats["failed"] += 1
                    continue

                payload["qualityProfileId"] = target_id
                self.radarr_api._make_request(instance, f"movie/{movie_id}", method="PUT", payload=payload)

                # ── Realize the downgrade NOW (verify → delete → grab) ────────
                # A profile flip + blind search NEVER reclaims: the existing file
                # exceeds the new cutoff, so Radarr rejects every release
                # ("cutoff met") — *arr does not downgrade files. So: one
                # interactive search FIRST; only if a smaller release actually
                # exists is the file deleted, then that release is grabbed by
                # guid (a blind search fallback also works post-delete, since the
                # cutoff-met blocker died with the file). No smaller release →
                # the file is KEPT (a title is never traded for an empty indexer
                # result) and the row re-probes next run.
                _fid_row = df.at[idx, "movie_file_id"] if "movie_file_id" in df.columns else None
                # COOLDOWN. A title that already failed to find a smaller release is not
                # re-probed until its backoff expires - skipped BEFORE the interactive
                # search, so it costs no indexer call at all. Escalates per attempt and
                # caps at 90d, so nothing is written off permanently.
                _ck = _ekey(df.at[idx, "tmdb_id"] if "tmdb_id" in df.columns else movie_id,
                            df.at[idx, "resolution"] if "resolution" in df.columns else None)
                _cd = _cooldown_left(_ledger, _ck, self.config)
                if _cd > 0:
                    stats["cooldown_skipped"] = stats.get("cooldown_skipped", 0) + 1
                    self.logger.log_debug(
                        f"  ⏭️ '{title}': step-down on cooldown, {_cd:.0f}d left — skipped.")
                    continue
                # GLD-RAD-33 — one downloadclient probe per pass, lazily memoised, so
                # the picker can refuse releases on protocols nothing can download.
                if not hasattr(self, "_grabbable_protocols_memo"):
                    self._grabbable_protocols_memo = {}
                if instance not in self._grabbable_protocols_memo:
                    self._grabbable_protocols_memo[instance] = self._enabled_protocols(
                        self.radarr_api, instance)
                _grabbable = self._grabbable_protocols_memo[instance]
                releases = self.radarr_api._make_request(
                    instance, f"release?movieId={int(movie_id)}", fallback=None) or []
                pick = self._pick_stepdown_release(
                    releases,
                    current_res=df.at[idx, "resolution"] if "resolution" in df.columns else None,
                    allow_below_floor=_exhaustive,
                    movie_title=title,
                    movie_year=(df.at[idx, "year"] if "year" in df.columns else None),
                    allowed_protocols=_grabbable,
                )
                if not pick:
                    # Back this title off instead of re-probing it every run. 31 titles on
                    # a real library have no smaller encode at any indexer at all.
                    _n = _stamp_failure(_ledger, _ck)
                    _wait = _wait_days(_ledger, _ck, self.config)
                    self.logger.log_info(
                        f"  ⏸️ '{title}': no smaller release available — file kept at "
                        f"{cur_qp_name} (profile now {target_name}; attempt {_n}, "
                        f"re-probes in {_wait:.0f}d).")
                    stats["no_release"] = stats.get("no_release", 0) + 1
                    df.at[idx, "quality_profile_id"]   = target_id
                    df.at[idx, "quality_profile_name"] = target_name
                    changed = True
                    continue
                if _fid_row is None or pd.isna(_fid_row):
                    # FID UNKNOWN ⇒ NO DELETE POSSIBLE ⇒ NO GRAB (GLD-RAD-31). Same
                    # hardening as the universe realize: 19 of 21 realize rows on the
                    # 2026-08-07 apply had an empty movie_file_id, so the delete was
                    # silently skipped and the grab left to die on cutoff-met.
                    stats["fid_missing"] = stats.get("fid_missing", 0) + 1
                    _stamp_failure(_ledger, _ck)
                    self.logger.log_warning(
                        f"  ⚠️ '{title}': no movie_file_id on the row — cannot "
                        f"delete-then-grab; grab SKIPPED, re-probes after backoff.")
                    df.at[idx, "quality_profile_id"]   = target_id
                    df.at[idx, "quality_profile_name"] = target_name
                    changed = True
                    continue
                if _fid_row is not None and pd.notna(_fid_row):
                    # GLD-DEL-05 — snapshot the ORIGINAL before anything mutates it. The
                    # rows below overwrite quality_profile_id/name in place, and the file
                    # itself is about to be destroyed, so this is the last moment the
                    # original release identity exists anywhere.
                    _orig_release = release_record(df.loc[idx])
                    _orig_qname = cur_qp_name
                    _orig_size = _sz_f
                    _grab_ok = True     # cleared by the grab branches below on failure
                    # CHECKED: DELETE success now returns True (base contract fix). On
                    # failure the old file is still on disk — grabbing anyway imports a
                    # second copy over a file Radarr cannot remove. Skip, stamp the
                    # ledger, re-probe after the operator fixes the delete path.
                    if not bool(self.radarr_api._make_request(
                            instance, f"moviefile/{int(_fid_row)}", method="DELETE")):
                        stats["delete_failed"] = stats.get("delete_failed", 0) + 1
                        _stamp_failure(_ledger, _ck)
                        self.logger.log_warning(
                            f"  ⚠️ '{title}': step-down delete FAILED (see instance-manager "
                            f"error above) — file kept, grab SKIPPED; re-probes after backoff.")
                        continue
                try:
                    # RETURN VALUE, not an exception: ``fallback=None`` makes _make_request
                    # swallow the HTTP error and return the fallback, so this ``except`` was
                    # unreachable. A 404 from POST /release left the file deleted with no
                    # replacement grabbed and no blind search queued. Same defect as the
                    # universe quality pass.
                    # SEND THE movieId. Without it Radarr re-parses the release TITLE to
                    # work out which movie this is, and a release whose name does not
                    # resemble the library title fails that parse: 7 grabs 404'd with
                    # "Unable to find matching movie, will need to be manually provided" on
                    # foreign-language and fansub releases (Schimpansen.2013.German,
                    # [DeadFish] Liz...). The id is right here - the search was
                    # release?movieId=N - so the parse is avoidable, not merely detectable.
                    # This is the fix for that whole class; the grab-result check below
                    # remains the backstop for everything else.
                    _res = self.radarr_api._make_request(
                        instance, "release", method="POST", fallback=None,
                        payload={"guid": pick.get("guid"), "indexerId": pick.get("indexerId"),
                                 "movieId": int(movie_id)})
                    if not _res:
                        movie_ids_to_search.append(movie_id)
                        stats["grab_failed"] = stats.get("grab_failed", 0) + 1
                        _grab_ok = False
                        _stamp_failure(_ledger, _ck)
                        self.logger.log_warning(
                            f"  ⚠️ '{title}': file DELETED but the guid grab FAILED — blind "
                            f"search queued. Restore from the recycle bin if it finds nothing."
                        )
                    else:
                        _clear(_ledger, _ck)                    # steppable again
                except Exception:
                    movie_ids_to_search.append(movie_id)   # file is gone → blind search now works
                    stats["grab_failed"] = stats.get("grab_failed", 0) + 1
                    _grab_ok = False

                df.at[idx, "quality_profile_id"]   = target_id
                df.at[idx, "quality_profile_name"] = target_name
                df.at[idx, "quality_action"]       = None
                changed = True
                stats["downgraded"] += 1

                _pick_gb = float(pick.get("size") or 0) / (1024 ** 3)
                # In-flight accounting: the file is GONE now, the replacement lands later.
                stats["freed_now_gb"] += _sz_f / (1024 ** 3)
                stats["inflight_regrab_gb"] += _pick_gb
                _below = ""
                if pick.get("stepped_below_floor"):
                    stats["below_floor_picks"] += 1
                    _below = " [stepped BELOW 720 — no >=720 release exists for this title]"
                self.logger.log_info(
                    f"  📉 Stepped down: '{title}' "
                    f"({self._fmt_bytes(_sz_f)}, {cur_qp_name} → {target_name}) — file deleted, "
                    f"grabbed '{pick.get('title')}' ({_pick_gb:.1f} GB) — {reason}{_below}"
                )
                # GLD-DEL-05. A step-down destroys quality IRREVERSIBLY: unlike a delete,
                # `match_release` cannot undo it, because the replacement is now the only
                # file. Recording the original's identity is the sole way a Remux master
                # can ever be re-acquired deliberately. Before this, the only trace was a
                # `default.log` line that rotates away in five runs.
                #
                # `replaced_by` ONLY on a confirmed grab (GLD-DEL-07). The grab-failure
                # branch above does not `continue` — it falls through to here — so the
                # first cut of this call asserted a replacement for releases that were
                # never grabbed, and the space ledger then counted bytes that never
                # landed. A POST that Radarr ACCEPTED is still only queued, not imported;
                # `replaced_by` therefore means "this is what was asked for", and only
                # reconciliation can promote it to what actually arrived.
                self._archive_movie_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    disposition="stepped-down",
                    reason=(f"{reason} | {_orig_qname} → {target_name}"
                            + ("" if _grab_ok else " | RE-GRAB FAILED, blind search queued")),
                    size_bytes=_orig_size, file_id=_fid_row, source="step_down",
                    replaced_by=({"title": pick.get("title"),
                                  "size_bytes": pick.get("size"),
                                  "quality_name": target_name,
                                  "state": "queued"} if _grab_ok else None),
                    release_override=_orig_release)
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Downgrade failed for '{title}' (id={movie_id}): {e}")
                stats["failed"] += 1

        if movie_ids_to_search:
            # Fallback pool: guid grabs that errored after their file was already
            # deleted. Blind search is EFFECTIVE for these (no file → no
            # cutoff-met rejection → best allowed release at the new profile).
            try:
                self.radarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "MoviesSearch", "movieIds": movie_ids_to_search},
                )
                self.logger.log_info(f"  🔍 MovieSearch triggered for {len(movie_ids_to_search)} movie(s)")
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ MovieSearch trigger failed: {e}")

        # Persist when real downgrades happened OR plan stamps were written
        # (the latter is the dry_run preview path).
        if changed or plan_changed:
            mfm.save(instance, df)
        # GLD-DEL-05 — a step-down destroys quality irreversibly, so its rows are EVENTS
        # and land in the append-only archive. Flushed once, after the loop.
        _archived = self._flush_movie_deletions(_archive)
        if _archived:
            self.logger.log_info(
                f"  \U0001f5c3\ufe0f  archived {_archived} step-down row(s) → "
                f"logs/deletions/deletions.jsonl (run {self._deletion_run_id})")
        # Ledger saves regardless: a cooldown stamped this run must survive even when
        # nothing else changed, or the backoff would reset every pass.
        self._save_stepdown_ledger(instance, _ledger)

        prefix = "[dry_run] " if self.dry_run else ""
        _rows = [
            ["stepped down",        stats['downgraded']],
            ["at/below floor",      stats['already_at_720p']],
            ["protected",           stats['skipped_protected']],
            ["hot-universe",        stats.get('skipped_universe', 0)],
            ["high-score protected", stats['skipped_high_score']],
            ["recently watched",    stats['skipped_recent']],
            ["no smaller release",  stats.get('no_release', 0)],
            ["on cooldown",         stats.get('cooldown_skipped', 0)],
            ["grab failed",         stats.get('grab_failed', 0)],
            ["failed",              stats['failed']],
        ]
        _descs = [
            "movies stepped down one quality rank",
            "movies already at or below the 720p floor",
            "movies protected from downgrade by keep policy",
            "movies protected — hot franchise/universe credit holds an untagged saga member at tier",
            "movies protected by a high watchability score",
            "movies skipped for a recent watch",
            "file KEPT: no release below the current resolution — profile lowered, re-probes next run",
            # These two were MISSING, so every description from here shifted up by two and the
            # last pair fell off the end entirely. The table then read, among others,
            # "over-ceiling included | 818 | projected size of the smaller replacements" and
            # "freed now GB | 171.2 | candidates over the per-run re-grab cap - files KEPT" --
            # i.e. an operator reading the delete-adjacent report was told the opposite of what
            # each number meant. The Sonarr TV twin has always carried both.
            "movies skipped without an indexer call: still inside their step-down backoff",
            "grab did not take; file already removed, blind MoviesSearch queued",
            "movies whose PUT/search call errored",
        ]
        if _exhaustive:
            _rows += [
                ["over-ceiling included", stats.get('over_ceiling_included', 0)],
                ["stepped below 720",     stats['below_floor_picks']],
                ["in-flight re-grab GB",  round(stats['inflight_regrab_gb'], 1)],
                ["freed now GB",          round(stats['freed_now_gb'], 1)],
                ["target reached",        stats['stopped_at_target']],
                [f"deferred (cap {_regrab_cap or 'off'})", stats['deferred_cap']],
            ]
            _descs += [
                "movies admitted despite a watchability score over the delete ceiling — "
                "EVERYTHING shrinks before anything is deleted",
                "no >=720 release exists for the title, so the pass fell below the 720p floor",
                "projected size of the smaller replacements queued THIS run — subtracted from "
                "free space so the pass never downgrades against phantom headroom",
                "bytes actually removed from disk this pass (the replacements land later)",
                "candidates left untouched: free space NET of in-flight re-grabs reached the band top",
                "candidates over the per-run re-grab cap — files KEPT (still above the floor, so "
                "still undeletable) and re-qualify next run",
            ]
        # rows and descriptions are PARALLEL lists with no structural link -- the same shape as
        # the cast_names/cast_characters/cast_order triple, and it failed the same way: two
        # missing entries silently re-paired every later row with the wrong text. Nothing in
        # log_table checks, so the mismatch is invisible until a human reads the grid and
        # notices the numbers contradict their labels. Pad + warn rather than raise: a
        # reporting bug must never take down the pass it is reporting on.
        if len(_descs) != len(_rows):
            self.logger.log_warning(
                f"[SpacePressure] table description mismatch: {len(_rows)} row(s) but "
                f"{len(_descs)} description(s) - rows beyond the shorter list would be "
                f"mislabelled. Padding; fix the two lists in run_downgrades.")
            _descs = (_descs + [""] * len(_rows))[:len(_rows)]
        self.logger.log_table(
            ["Outcome", "Count"], _rows,
            title=f"[SpacePressure] {prefix}step-down pass - '{instance}' "
                  f"(~{stats.get('est_reclaim_gb', 0):.0f} GB reclaimed"
                  f"{'; exhaustive' if _exhaustive else ''})",
            caption="Result of the HD-720p step-down pass that shrinks low-score movies to free space."
                    + (" EXHAUSTIVE: every title above the 720p floor is planned down to it (deletion "
                       "is the true last resort), stopping once free space net of the in-flight "
                       "re-grabs reaches the band top." if _exhaustive else ""),
            descriptions=_descs,
        )
        return stats

    # ── Stage 2: delete (last resort) ────────────────────────────────────────────

    _UPGRADE_PENDING_KEY = "radarr/{inst}/upgrade_pending"

    def _persist_upgrade_intents(self, instance, intents):
        """Merge this pass's upgrade intents into the pending worklist — GLD-DEL-12.

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
                f"  ↑ tracked {len(live)} movie upgrade intent(s) for the next pass")
            return len(live)
        except Exception as e:
            self.logger.log_debug(f"  upgrade intents not recorded ({e})")
            return 0

    def _observe_upgrade_targets(self, instance, ledger):
        """``{key: current_movie_file_id_or_None}`` for the pending worklist.

        ⚠️ Reads FRESH from `GET /movie/{id}`. Reconciliation asks "has this changed
        since we triggered it?", and a cache written BEFORE the trigger answers the
        wrong question — every landed upgrade would read as still-pending forever.

        A movie whose fetch fails is simply ABSENT from the result, which is not the
        same as "no file" (**P-C**): `reconcile_upgrades` leaves absent keys pending
        rather than declaring them orphaned, so one bad fetch cannot manufacture a
        phantom orphan.
        """
        obs = {}
        if not isinstance(ledger, dict) or not ledger:
            return obs
        for key, rec in ledger.items():
            if not isinstance(rec, dict) or rec.get("movie_id") is None:
                continue
            try:
                payload = self.radarr_api._make_request(
                    instance, f"movie/{int(rec['movie_id'])}", fallback=None)
            except Exception:
                payload = None
            if payload is None:                  # unknown — leave the key absent
                continue
            mf = payload.get("movieFile") if isinstance(payload, dict) else None
            try:
                obs[key] = int((mf or {}).get("id") or 0) or None
            except (TypeError, ValueError):
                continue
        return obs

    def _reconcile_upgrade_intents(self, instance, df):
        """Did the movie upgrades we triggered actually land? — GLD-DEL-12.

        The Radarr twin of the Sonarr pass. Radarr's parquet is rebuilt from a full
        library walk each refresh, so a stale row heals on its own — but the ARCHIVE
        does not: without this, a landed movie upgrade leaves no `upgraded` event, so
        churn detection sees only the step-down half and the space ledger counts
        reclaim without the spend that caused the pressure.

        The pure half (`upgrade_intent` / `reconcile_upgrades`) is media-agnostic, so
        this is two adapters rather than a second implementation (**P-E**).

        Returns ``(df, stats)``. Never raises.
        """
        stats = {"checked": 0, "fulfilled": 0, "orphaned": 0,
                 "abandoned": 0, "pending": 0}
        if not self.global_cache:
            return df, stats
        try:
            key = self._UPGRADE_PENDING_KEY.format(inst=instance)
            led = self.global_cache.get(key)
            led = led if isinstance(led, dict) else {}
            if not led:
                return df, stats
            stats["checked"] = len(led)

            res = reconcile_upgrades(led, self._observe_upgrade_targets(instance, led))
            stats["fulfilled"] = len(res["fulfilled"])
            stats["orphaned"] = len(res["orphaned"])
            stats["abandoned"] = len(res["abandoned"])
            stats["pending"] = len(res["pending"])

            # What actually landed, read off the same rows the refresh rebuilt.
            _observed = {}
            if df is not None and not df.empty and "movie_file_id" in df.columns:
                for hit in res["fulfilled"]:
                    try:
                        fid = int(hit["observed_file_id"])
                        row = df[df["movie_file_id"] == fid]
                        if row.empty:
                            continue
                        r0 = row.iloc[0]
                        _observed[fid] = {"size_bytes": r0.get("size_bytes"),
                                          "quality_name": r0.get("quality_name")}
                    except Exception:
                        continue

            if not getattr(self, "_deletion_run_id", None):
                self._deletion_run_id = new_run_id()
            _events = upgrade_events(res, run_id=self._deletion_run_id,
                                     instance=instance, observed_files=_observed)
            if _events:
                self._flush_movie_deletions(_events)

            self.global_cache.set(key, res["pending"])
            if stats["fulfilled"] or stats["orphaned"] or stats["abandoned"]:
                self.logger.log_info(
                    f"  ↑ movie upgrade reconcile '{instance}': {stats['fulfilled']} landed, "
                    f"{stats['orphaned']} now file-less, {stats['abandoned']} abandoned "
                    f"(>48h), {stats['pending']} still queued.")
            else:
                self.logger.log_debug(
                    f"  ↑ movie upgrade reconcile '{instance}': {stats['pending']} still queued.")
        except Exception as e:
            self.logger.log_debug(f"  movie upgrade reconcile skipped for '{instance}': {e}")
        return df, stats

    def _movie_grab_descriptor(self, instance, movie_id):
        """The redacted ``release/push`` descriptor for one movie, or ``{}``.

        Radarr's ``history/movie?movieId=`` returns a bare list rather than the paged
        envelope Sonarr uses on some routes; ``push_descriptor`` tolerates both, which
        is the reason it is pure and shared rather than duplicated per service (P-E).

        Radarr's grab history is FINITE, so this is captured at the moment of
        destruction rather than hoped for later. Best-effort: every failure returns
        ``{}`` and the caller proceeds, because losing the descriptor must never cost
        the deletion it documents.

        RUNS UNDER dry_run — a read-only ``GET`` destroys nothing, and skipping it made
        the descriptor path the one thing a disarmed run could not prove.
        """
        if movie_id is None:
            return {}
        try:
            raw = self.radarr_api._make_request(
                instance, f"history/movie?movieId={int(movie_id)}&eventType=1",
                fallback=None)
            return push_descriptor(raw) or {}
        except Exception:
            return {}

    def _archive_movie_deletion(self, sink, *, instance, row, title,
                                disposition, reason=None, size_bytes=None,
                                file_id=None, score=None, source=None,
                                fetch_descriptor=True, replaced_by=None,
                                release_override=None):
        """Append one movie deletion row to *sink* — GLD-RST-08.

        Radarr previously stored ``tmdb_id`` + a timestamp and nothing else, despite
        ``movie_files`` carrying every ``RELEASE_FIELDS`` column. Records WHAT was
        destroyed, WHY, FROM WHERE (path + derived library class) and HOW TO GET IT
        BACK (release identity + redacted grab descriptor).

        Never raises; a failed row is logged rather than silently dropped, because a
        missing row means a permanently unrecoverable file.
        """
        try:
            get = (lambda k: row.get(k)) if hasattr(row, "get") else (lambda k: None)
            # `release_override` carries a snapshot taken BEFORE the row was mutated —
            # the step-down path overwrites quality in place, so reading it here would
            # describe the replacement rather than what was destroyed.
            rel = release_override if release_override is not None else (
                release_record(row) if row is not None else None)
            push = (self._movie_grab_descriptor(instance, get("movie_id"))
                    if fetch_descriptor else {})
            sink.append(deletion_record(
                run_id=self._deletion_run_id,
                media="movie",
                instance=instance,
                title=title,
                year=get("year"),
                disposition=disposition,
                reason=reason,
                path=get("path"),
                file_id=file_id,
                tmdb_id=get("tmdb_id"),
                quality_profile_id=get("quality_profile_id"),
                quality_profile_name=get("quality_profile_name"),
                quality_name=get("quality_name"),
                resolution=get("resolution"),
                size_bytes=size_bytes,
                score=score,
                release=rel,
                push=push or None,
                source=source,
                replaced_by=replaced_by,
            ))
        except Exception as e:
            try:
                self.logger.log_warning(
                    f"  \u26a0\ufe0f deletion archive row FAILED for '{title}' ({e}) — the file "
                    f"is still being deleted, but this row will not be recorded.")
            except Exception:
                pass

    def _flush_movie_deletions(self, sink):
        """Persist this pass's movie rows, routed by KIND. One write per kind.

        Events append to the permanent archive; state rows replace the pending
        snapshot — see ``deletion_log.STATE_DISPOSITIONS``.
        """
        if not sink:
            return 0
        try:
            from scripts.support.utilities.logger.logger import (
                append_deletions, write_pending_deletions,
            )
            events, states = split_by_kind(sink)
            n = append_deletions(to_jsonl(events)) if events else 0
            n += write_pending_deletions(to_jsonl(states))
            return n
        except Exception:
            return 0

    @LoggerManager().log_function_entry
    @timeit("run_space_pressure_deletions")
    def run_deletions(self, instance: str, free_space_gb: float) -> dict:
        """
        Target-driven deletion: when free space is below the floor (free_space_limit),
        delete the lowest-rated owned movies — watchability score, then critic ratings
        (imdb/trakt/tmdb/rt/mc), then largest file — until projected free >= U (top of
        the pressure band, for hysteresis). Tiered: watched + grace-expired first, then
        (optionally) unwatched low-watchability. Every deletion is recorded so
        restore_recovered_deletions re-acquires it if its score later recovers. Guards:
        keep_forever/keep_movie/keep_universe, franchise entries/files, and
        recently-watched (within COLLECTION_WINDOW_DAYS) are never deleted.
        """
        stats = {"checked": 0, "deleted": 0, "failed": 0, "bytes_freed": 0.0,
                 "tier_watched": 0, "tier_unwatched": 0, "skipped_universe": 0,
                 "target_met": False}

        if self._coordinator_owns_deletion():
            # Cross-service coordinator deletes movies + TV together on one ranked
            # pool; this per-service loop defers (downgrades already ran).
            return stats
        if not deletions_enabled(self.config):
            # HARD SAFETY GATE: no operator-set free_space_limit → no deletions,
            # anywhere. main.py emits the loud end-of-run banner.
            self.logger.log_warning(
                f"[SpacePressure] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
                "skipping the movie delete pass."
            )
            return stats
        if not bool(self.config.get("space_pressure_delete_enabled", True) if self.config else True):
            return stats

        T, U = self._space_targets(instance)
        if free_space_gb >= T:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {free_space_gb:.1f} GB free — at/above floor "
                f"{T:.0f} GB, no space deletion."
            )
            return stats

        mfm = self._get_movie_files_manager()
        if mfm is None:
            return stats
        df = mfm.load(instance)
        if df.empty:
            return stats

        include_unwatched = bool(self.config.get("space_pressure_include_unwatched", True) if self.config else True)
        try:
            ceiling = int(self.config.get("space_pressure_score_ceiling", 17) if self.config else 17)
        except (TypeError, ValueError):
            ceiling = 17
        ceiling = get_threshold("movie_delete_ceiling", self.config, ceiling,
                                logger=getattr(self, "logger", None))

        score_map          = self._build_score_map(df, instance)
        franchise_file_ids = mfm._build_franchise_file_ids(df)
        now                = datetime.now(tz=timezone.utc)
        no_delete_cutoff   = now - timedelta(days=self.COLLECTION_WINDOW_DAYS)
        marked = (
            df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
            if "marked_for_deletion" in df.columns else pd.Series(False, index=df.index)
        )

        # ── Ranked, tiered candidate list (lowest-rated first) ────────────────────
        # The DECISION (which files, in what order) is the brain's; the target loop
        # below APPLIES it. Tuple: (tier, score, critic_or_None, -size, idx, fid, size);
        # a missing critic sorts NEUTRAL at 5.0 (not to the protected end). The
        # per-row critic blend is the shared scoring.critic.critic_avg (Step 2).
        # Same "deletion is the true last resort" invariant the coordinator pool enforces,
        # applied to this single-service fallback so the policy can't differ by which path
        # owns deletion: only titles already AT/BELOW the 720p floor are eligible.
        candidates = build_movie_delete_candidates(
            df, score_map, marked,
            franchise_file_ids=franchise_file_ids,
            no_delete_cutoff=no_delete_cutoff,
            include_unwatched=include_unwatched,
            ceiling=ceiling,
            universe_age_days=self._universe_delete_age_days(),
            now=now,
            stats=stats,
            floor_resolution=(DOWNGRADE_FLOOR_RESOLUTION
                              if exhaustive_downgrade(self.config) else None),
        )

        _uni = stats.get("skipped_universe", 0)
        _dg = stats.get("skipped_downgradable", 0)
        self.logger.log_info(
            f"[SpacePressure] '{instance}': {free_space_gb:.1f} GB free — target loop to {U:.0f} GB "
            f"({len(candidates)} candidate(s), lowest-rated first"
            f"{f'; {_uni} held by hot-universe credit' if _uni else ''}"
            f"{f'; {_dg} still ABOVE the {DOWNGRADE_FLOOR_RESOLUTION}p floor — must be downgraded first' if _dg else ''})."
        )

        freed_gb = 0.0
        changed  = False
        stamped  = False   # any planned_action='delete' written → persist for the ledger
        deleted_tmdbs: list[int] = []
        # GLD-RST-08 — the permanent record of what this pass destroys. Accumulated
        # here and flushed ONCE after the loop rather than appended per row.
        _archive: list = []
        if not getattr(self, "_deletion_run_id", None):
            self._deletion_run_id = new_run_id()
        for tier, score, critic, _neg, idx, fid, size in candidates:
            if free_space_gb + freed_gb >= U:
                stats["target_met"] = True
                break
            stats["checked"] += 1
            title      = df.at[idx, "title"] or f"movie {df.at[idx, 'movie_id']}"
            size_gb    = size / (1024 ** 3)
            tmdb_id    = df.at[idx, "tmdb_id"] if "tmdb_id" in df.columns else None
            critic_str = f"{critic:.1f}/10" if critic is not None else "n/a"
            reason     = (f"{'watched' if tier == 0 else 'unwatched'}, score={score}, critic={critic_str}, "
                          f"{self._fmt_bytes(size)}; space target {U:.0f} GB (free {free_space_gb:.0f} GB)")
            self._stamp_plan(df, idx, "delete", reason, size_gb)
            stamped = True

            if effective_dry_run(self.dry_run, self.global_cache):    # also dry when backup gate disarmed
                self.logger.log_info(f"  🗑️ [dry_run] Would delete: '{title}' ({self._fmt_bytes(size)}) — {reason}")
                freed_gb += size_gb
                stats["bytes_freed"] += size
                stats["deleted"] += 1
                stats["tier_watched" if tier == 0 else "tier_unwatched"] += 1
                self._archive_movie_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    disposition="would-delete", reason=reason, size_bytes=size,
                    file_id=fid, score=score, source="space_pressure")
                continue
            _del_ok = False
            try:
                # CHECKED (GLD-RST-20). `_make_request` swallows HTTP failures and
                # returns the fallback, so the old bare try/except was DEAD protection
                # for every 500 — the same P-A shape `delete_selected_movie_files`
                # already guards against a few hundred lines below. An unchecked DELETE
                # here credits freed GB, clears the mark, feeds `deleted_tmdbs` and
                # writes an archive row, all for a file still on disk.
                _del_ok = bool(self.radarr_api._make_request(
                    instance, f"moviefile/{fid}", method="DELETE"))
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Delete raised for '{title}' (movieFileId={fid}): {e}")
            if not _del_ok:
                self.logger.log_warning(
                    f"  ⚠️ Delete FAILED for '{title}' (movieFileId={fid}) — "
                    f"file KEPT, mark kept, retries next run.")
                self._archive_movie_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    disposition="failed", reason=f"{reason} | DELETE returned falsy",
                    size_bytes=size, file_id=fid, score=score,
                    source="space_pressure", fetch_descriptor=False)
                stats["failed"] += 1
                continue
            df.at[idx, "marked_for_deletion"] = False
            freed_gb += size_gb
            stats["bytes_freed"] += size
            stats["deleted"] += 1
            stats["tier_watched" if tier == 0 else "tier_unwatched"] += 1
            changed = True
            if tmdb_id is not None and pd.notna(tmdb_id):
                deleted_tmdbs.append(int(tmdb_id))
            self.logger.log_info(f"  🗑️ Deleted: '{title}' ({self._fmt_bytes(size)}) — {reason}")
            self._archive_movie_deletion(
                _archive, instance=instance, row=df.loc[idx], title=title,
                disposition="deleted", reason=reason, size_bytes=size,
                file_id=fid, score=score, source="space_pressure")

        _archived = self._flush_movie_deletions(_archive)
        if _archived:
            self.logger.log_info(
                f"  \U0001f5c3\ufe0f  archived {_archived} movie deletion row(s) → "
                f"logs/deletions/deletions.jsonl (run {self._deletion_run_id})")

        if free_space_gb + freed_gb >= U:
            stats["target_met"] = True

        # Record real deletions so restore_recovered_deletions can re-acquire them.
        if deleted_tmdbs and self.global_cache:
            try:
                from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager
                dkey = RadarrRepairAnomalyManager._DELETED_SET_KEY.format(inst=instance)
            except Exception:
                dkey = f"radarr/{instance}/demote_deleted"
            try:
                dset = self.global_cache.get(dkey)
                dset = dset if isinstance(dset, dict) else {}
                for t in deleted_tmdbs:
                    dset[str(t)] = now.isoformat()
                self.global_cache.set(dkey, dset)
            except Exception as e:
                # These files are already deleted on disk; if we can't record them
                # in the restore-set, restore_recovered_deletions can never re-grab
                # them. Make the loss LOUD instead of silent so it's recoverable.
                self.logger.log_error(
                    f"[SpacePressure] ⚠️ Failed to persist restore-set for {len(deleted_tmdbs)} "
                    f"deleted movie(s) ({dkey}): {e} — these deletions are NOT restorable."
                )

        # Persist the ledger annotations even in dry_run when we stamped a plan (i.e.
        # under pressure) so the dry-run ledger reflects what WOULD be deleted. In
        # dry_run only the plan columns change — no Radarr writes were issued.
        if (changed and not self.dry_run) or (self.dry_run and stamped):
            mfm.save(instance, df)

        prefix  = "[dry_run] " if self.dry_run else ""
        verb    = "would free" if self.dry_run else "freed"
        _target = 'met' if stats['target_met'] else 'NOT met (candidates exhausted)'
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["checked",          stats['checked']],
                ["deleted",          stats['deleted']],
                ["watched tier",     stats['tier_watched']],
                ["unwatched tier",   stats['tier_unwatched']],
                ["failed",           stats['failed']],
            ],
            title=f"[SpacePressure] {prefix}target-loop deletion - '{instance}' (target {U:.0f} GB {_target})",
            caption=f"Result of the last-resort delete loop that {verb} {self._fmt_bytes(stats['bytes_freed'])} toward the space target.",
            descriptions=[
                "movies examined as delete candidates",
                "movie files deleted from disk",
                "deletions from the watched + grace-expired tier",
                "deletions from the unwatched low-score tier",
                "movies whose DELETE call errored",
            ],
        )
        return stats


    # ── Cross-service coordinator hooks (Phase 4) ────────────────────────────────
    # build_delete_candidates + delete_selected_movie_files are the reusable
    # primitives the SpaceCoordinatorManager calls to merge movies and TV into ONE
    # ranked deletion pool. run_deletions stays the single-service fallback (used
    # only when space_coordinator_enabled is off).

    def _row_critic_avg(self, df, idx) -> "float | None":
        """Delegate the critic-consensus blend to the brain (scoring/critic).
        Service keeps only the column extraction (only present columns are passed)."""
        ratings = {c: df.at[idx, c]
                   for c in ("imdb_rating", "tmdb_rating", "trakt_rating",
                             "rotten_tomatoes_score", "metacritic_score")
                   if c in df.columns}
        return critic_avg(ratings)

    @timeit("build_movie_delete_candidates")
    def build_delete_candidates(self, instance: str, df, *,
                                ignore_score_ceiling: bool = False) -> list[dict]:
        """Return the ranked-but-unsorted list of MOVIE delete-candidates for the
        coordinator's combined pool. Same guards/tiers as run_deletions but it does
        NOT delete. Reads the persisted watchability_score column (refresh_scores)
        falling back to a live score_map. Each dict: service/idx/fid/tmdb_id/score/
        critic/size_bytes/size_gb/tier/title/resolution.

        ``ignore_score_ceiling`` (default False) keeps every keep/franchise/recently-watched
        guard but skips ONLY the watchability score ceiling — used to build the dual-version
        4K-copy reclaim pool, where each baseline-backed 4K copy is pure reclaim (no title lost)
        regardless of watchability, so it must be reclaimable before any whole title.

        DELETE-ELIGIBILITY INVARIANT (``space_exhaustive_downgrade``, DEFAULT ON): a whole title
        may enter the pool ONLY when its resolution is AT or BELOW the 720p floor — i.e. it has
        nothing left to shrink. Anything above the floor is excluded and counted as
        ``skipped_downgradable`` (also published on ``self.last_skipped_downgradable`` for the
        coordinator's log), so "the delete pool is small" is explained rather than mysterious.
        An unknown/missing resolution counts as at-floor: the downgrade planner also treats it as
        nothing-to-step, so it would otherwise be undeletable forever. The 4K-copy reclaim pool
        (``ignore_score_ceiling``) is EXEMPT for the same reason its score ceiling is relaxed —
        a baseline-backed 2160p bonus copy loses no title, so it is pure reclaim, not a deletion."""
        out: list[dict] = []
        self.last_skipped_downgradable = 0
        self.last_skipped_unscored = 0
        self.last_skipped_watchlist = 0
        if df is None or df.empty:
            return out
        include_unwatched = bool(self.config.get("space_pressure_include_unwatched", True) if self.config else True)
        try:
            ceiling = int(self.config.get("space_pressure_score_ceiling", 17) if self.config else 17)
        except (TypeError, ValueError):
            ceiling = 17
        ceiling = get_threshold("movie_delete_ceiling", self.config, ceiling,
                                logger=getattr(self, "logger", None))

        have_col = "watchability_score" in df.columns
        # If the persisted column exists but is entirely empty, refresh_scores didn't
        # populate it — the fallback would rank every movie as deletable. Defer rather
        # than delete on fallback scores. (Absent column → live _build_score_map below,
        # which computes real scores, so only guard the present-but-empty case.)
        if have_col and len(df) > 0 and \
                pd.to_numeric(df["watchability_score"], errors="coerce").notna().sum() == 0:
            self.logger.log_warning(
                f"[SpacePressure] '{instance}' watchability_score is empty — refresh_scores "
                f"likely didn't run; yielding NO movie delete candidates (won't delete on fallback scores)."
            )
            return out
        score_map = None if have_col else self._build_score_map(df, instance)
        mfm = self._get_movie_files_manager()
        franchise_file_ids = mfm._build_franchise_file_ids(df) if mfm else frozenset()
        now = datetime.now(tz=timezone.utc)
        no_delete_cutoff = now - timedelta(days=self.COLLECTION_WINDOW_DAYS)
        marked = (
            df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
            if "marked_for_deletion" in df.columns else pd.Series(False, index=df.index)
        )
        _univ_age = self._universe_delete_age_days()   # bare-universe ageing (default None = off)
        _held_universe = 0   # hot-saga rows the credit guard spared from the pool (observability)
        _held_watchlist = 0  # rows a still-active member's watchlist shielded (Group A5)
        # "Deletion is the true last resort": only at/below-floor titles may be deleted.
        _floor_gate = exhaustive_downgrade(self.config) and not ignore_score_ceiling
        _held_downgradable = 0
        _held_unscored = 0    # rows refresh_scores hasn't reached yet -> deferred, not deleted

        for idx in df.index:
            fid = df.at[idx, "movie_file_id"]
            if pd.isna(fid):
                continue
            keep_policy = df.at[idx, "keep_policy"] if "keep_policy" in df.columns else None
            is_fe = bool(df.at[idx, "is_franchise_entry"]) if "is_franchise_entry" in df.columns else False
            if is_fe or keep_policy in ("keep_forever", "keep_movie", "keep_universe"):
                continue
            if fid in franchise_file_ids:
                continue
            # Bare-universe ageing (default-off): mirror of the brain delete-planner guard
            # so the coordinated pool spares a still-ageing 'universe' title too.
            if bare_universe_protected(
                keep_policy, df.at[idx, "date_added"] if "date_added" in df.columns else None,
                now, age_days=_univ_age,
            ):
                continue
            # Borrowed franchise/universe credit (per-movie, recency-decayed by refresh_scores):
            # an UNTAGGED hot-saga member resists DELETION just as plan_movie_downgrades makes it
            # resist a step-down — mirror of the brain delete-planner guard (build_movie_delete_candidates)
            # so the coordinated pool spares it too (run_deletions, the single-service fallback, already
            # does). Skipped for the 4K-copy reclaim pool (ignore_score_ceiling): there the 1080p baseline
            # survives, so the bonus copy is pure reclaim and loses no title — the same reason the score
            # ceiling is relaxed there. Byte-identical when the column is absent / credit unset.
            if not ignore_score_ceiling and "universe_credit" in df.columns:
                _uc = df.at[idx, "universe_credit"]
                try:
                    if _uc is not None and pd.notna(_uc) and float(_uc) >= UNIVERSE_PROTECT_MIN:
                        _held_universe += 1
                        continue
                except (TypeError, ValueError):
                    pass
            # GROUP-A5 WATCHLIST SHIELD — mirror of the brain delete-planner guard
            # (space/delete_planner.build_movie_delete_candidates), because THIS method is a
            # SECOND implementation of the same pool for the coordinator, not a caller of it.
            # A guard added to only one of the two would mean the shield held under the
            # single-service fallback and silently did nothing under the coordinator — which
            # is the path that actually runs here. Column-driven for the same reason: the
            # coordinator arrives with a bare parquet, no cache handle and no manager graph.
            # EXEMPT under ``ignore_score_ceiling`` for exactly the reason the score ceiling
            # is: that pool only reclaims a 4K bonus copy whose 1080p baseline survives, so
            # the household keeps the watchlisted title either way.
            if not ignore_score_ceiling and "watchlist_hold" in df.columns:
                _wh = df.at[idx, "watchlist_hold"]
                if _wh is not None and pd.notna(_wh) and bool(_wh):
                    _held_watchlist += 1
                    continue
            lw = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
            if lw:
                try:
                    if pd.to_datetime(lw, utc=True) >= no_delete_cutoff:
                        continue
                except Exception:
                    pass
            # DELETE-ELIGIBILITY INVARIANT — deliberately LAST of the guards so the counter
            # means "would otherwise be deletable, but still has quality to shed" rather than
            # double-counting titles a keep tag already spared.
            if _floor_gate:
                _r = df.at[idx, "resolution"] if "resolution" in df.columns else None
                try:
                    if _r is not None and pd.notna(_r) and int(_r) > DOWNGRADE_FLOOR_RESOLUTION:
                        _held_downgradable += 1
                        continue   # still shrinkable → NOT delete-eligible (downgrade it first)
                except (TypeError, ValueError):
                    pass           # unreadable resolution → treated as at-floor (see docstring)
            # NOTE the deliberate ASYMMETRY with the unscored branch immediately below: an
            # unknown RESOLUTION admits the row (permissive), an unknown SCORE defers it
            # (conservative). Both are "missing data", and the difference is not an
            # oversight — a row with no resolution also has nothing the downgrade planner
            # can step, so deferring it would make it undeletable FOREVER, whereas a row
            # with no score gets one on the next refresh_scores pass and re-qualifies.
            # OPEN QUESTION worth revisiting with real numbers: this library currently has
            # 81 movies with a NULL resolution, 67 of which land in the delete pool
            # (~74 GB, ~1.3% of the pool's bytes). If that share ever grows, the honest fix
            # is to find out WHY the column is null, not to flip either branch.

            # UNSCORED -> DEFERRED. Not "score it 5 and rank it" — that literal was a point
            # on the score axis and quietly changed meaning (p1.3 -> p32.4) when Group D v2
            # translated the axis. This now agrees with the whole-column guard above
            # ("won't delete on fallback scores") for the partial case too. Counted +
            # logged, so a persistently-unscored row is a number rather than immortal.
            # Full reasoning: machine_learning/space/downgrade_planner.row_score.
            _sc = (df.at[idx, "watchability_score"] if have_col
                   else score_map.get(idx) if score_map else None)
            if _sc is None or not pd.notna(_sc):
                _held_unscored += 1
                continue
            score = int(_sc)
            size = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0

            if bool(marked.loc[idx]):
                tier = 0
            else:
                if not include_unwatched or (score >= ceiling and not ignore_score_ceiling):
                    continue
                da = df.at[idx, "date_added"] if "date_added" in df.columns else None
                if da:
                    try:
                        if pd.to_datetime(da, utc=True) >= no_delete_cutoff:
                            continue
                    except Exception:
                        pass
                tier = 1

            tmdb_id = df.at[idx, "tmdb_id"] if "tmdb_id" in df.columns else None
            _res = df.at[idx, "resolution"] if "resolution" in df.columns else None
            out.append({
                "service": "movie", "tier": tier, "score": score,
                "critic": self._row_critic_avg(df, idx), "size_bytes": size,
                "size_gb": size / (1024 ** 3), "idx": idx, "fid": int(fid),
                "tmdb_id": int(tmdb_id) if (tmdb_id is not None and pd.notna(tmdb_id)) else None,
                "resolution": int(_res) if (_res is not None and pd.notna(_res)) else None,
                "title": (df.at[idx, "title"] if "title" in df.columns else None) or f"movie {fid}",
            })
        if _held_universe:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {_held_universe} title(s) held by hot-universe credit "
                f"(coordinator delete pool)."
            )
        self.last_skipped_watchlist = _held_watchlist
        if _held_watchlist:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {_held_watchlist} title(s) held by the WATCHLIST shield "
                f"(coordinator delete pool) — a household member asked for these and is still active; "
                f"each releases after their dormancy window."
            )
        self.last_skipped_unscored = _held_unscored
        if _held_unscored:
            self.logger.log_warning(
                f"[SpacePressure] '{instance}': {_held_unscored} title(s) DEFERRED from the delete pool "
                f"— no watchability_score yet, and a title is never deleted on a guessed score. They "
                f"re-qualify as soon as refresh_scores reaches them; a count that persists across runs "
                f"means the scorer is skipping those rows."
            )
        self.last_skipped_downgradable = _held_downgradable
        if _held_downgradable:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {_held_downgradable} title(s) EXCLUDED from the delete "
                f"pool — still above the {DOWNGRADE_FLOOR_RESOLUTION}p floor, so there is quality left "
                f"to shrink (skipped_downgradable). Deletion is the true last resort: they must reach "
                f"the floor before they can ever be deleted."
                + ("" if out else " The pool is EMPTY for exactly this reason — nothing is at the "
                                  "floor yet, so nothing is deletable.")
            )
        return out

    @timeit("delete_selected_movie_files")
    def delete_selected_movie_files(self, instance: str, df, picks: list[dict]) -> dict:
        """Delete the chosen movie files (moviefile/{id} DELETE), record them in the
        restore-set, and persist df. ``picks`` are dicts from build_delete_candidates
        that the coordinator selected. dry_run stamps the plan but issues no DELETE."""
        stats = {"deleted": 0, "failed": 0, "bytes_freed": 0.0}
        if not picks:
            return stats
        if not deletions_enabled(self.config):
            # Belt-and-braces: the coordinator can't run without a floor, but never
            # delete through this APPLY primitive either when the gate is closed.
            self.logger.log_warning(
                f"[SpacePressure] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
                f"refusing coordinator delete of {len(picks)} movie pick(s)."
            )
            return stats
        now = datetime.now(tz=timezone.utc)
        changed = False
        deleted_tmdbs: list[int] = []
        # GLD-RST-08 — this path stamped the reason into the ledger but dropped it from
        # every log line, so a coordinator-driven delete was the least traceable of all.
        _archive: list = []
        if not getattr(self, "_deletion_run_id", None):
            self._deletion_run_id = new_run_id()
        for c in picks:
            idx, fid, size = c["idx"], c["fid"], float(c.get("size_bytes") or 0.0)
            title = c.get("title") or f"movie {fid}"
            _reason = c.get("reason") or "coordinator pool"
            self._stamp_plan(df, idx, "delete", _reason, size / (1024 ** 3))
            if effective_dry_run(self.dry_run, self.global_cache):    # also dry when backup gate disarmed
                # debug: 400+ per-title lines in selection order were live-log spam —
                # the decision ledger (stamped above) renders them sorted with GB +
                # reason in the end-of-run "Change plan" grid.
                self.logger.log_debug(f"  🗑️ [dry_run] Would delete movie: '{title}' ({self._fmt_bytes(size)})")
                stats["deleted"] += 1
                stats["bytes_freed"] += size
                self._archive_movie_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    disposition="would-delete", reason=_reason, size_bytes=size,
                    file_id=fid, source="coordinator")
                continue
            try:
                # CHECKED: DELETE success returns True (base contract fix). The old
                # try/except here was DEAD protection — _make_request swallows HTTP
                # errors and returns the fallback, so 30 failed deletes in one apply
                # counted as freed GB, cleared their marks, and fed deleted_tmdbs.
                if not bool(self.radarr_api._make_request(
                        instance, f"moviefile/{fid}", method="DELETE")):
                    self.logger.log_warning(
                        f"  ⚠️ Movie delete FAILED for '{title}' (movieFileId={fid}) — "
                        f"see instance-manager error above; mark kept, retries next run.")
                    self._archive_movie_deletion(
                        _archive, instance=instance, row=df.loc[idx], title=title,
                        disposition="failed", reason=f"{_reason} | DELETE returned falsy",
                        size_bytes=size, file_id=fid, source="coordinator",
                        fetch_descriptor=False)
                    stats["failed"] += 1
                    continue
                if "marked_for_deletion" in df.columns:
                    df.at[idx, "marked_for_deletion"] = False
                stats["deleted"] += 1
                stats["bytes_freed"] += size
                changed = True
                if c.get("tmdb_id") is not None:
                    deleted_tmdbs.append(int(c["tmdb_id"]))
                self.logger.log_info(f"  🗑️ Deleted movie: '{title}' ({self._fmt_bytes(size)})")
                self._archive_movie_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    disposition="deleted", reason=_reason, size_bytes=size,
                    file_id=fid, source="coordinator")
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Movie delete failed for '{title}' (movieFileId={fid}): {e}")
                self._archive_movie_deletion(
                    _archive, instance=instance, row=df.loc[idx], title=title,
                    disposition="failed", reason=f"{_reason} | DELETE failed: {e}",
                    size_bytes=size, file_id=fid, source="coordinator",
                    fetch_descriptor=False)
                stats["failed"] += 1

        _archived = self._flush_movie_deletions(_archive)
        if _archived:
            self.logger.log_info(
                f"  \U0001f5c3\ufe0f  archived {_archived} movie deletion row(s) → "
                f"logs/deletions/deletions.jsonl (run {self._deletion_run_id})")

        if deleted_tmdbs and self.global_cache:
            try:
                from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager
                dkey = RadarrRepairAnomalyManager._DELETED_SET_KEY.format(inst=instance)
            except Exception:
                dkey = f"radarr/{instance}/demote_deleted"
            try:
                dset = self.global_cache.get(dkey)
                dset = dset if isinstance(dset, dict) else {}
                for t in deleted_tmdbs:
                    dset[str(t)] = now.isoformat()
                self.global_cache.set(dkey, dset)
            except Exception as e:
                self.logger.log_error(
                    f"[SpacePressure] ⚠️ Failed to persist restore-set for {len(deleted_tmdbs)} "
                    f"deleted movie(s) ({dkey}): {e} — these deletions are NOT restorable."
                )

        # Persist plan stamps even in dry_run (coordinator preview) so the ledger
        # reflects what the unified pool would delete. dry_run touches only the
        # plan columns — no Radarr writes were issued.
        if (changed and not self.dry_run) or (self.dry_run and picks):
            mfm = self._get_movie_files_manager()
            if mfm:
                mfm.save(instance, df)
        return stats

    def load_movie_files(self, instance: str):
        """Coordinator helper: load the movie_files parquet (or None)."""
        mfm = self._get_movie_files_manager()
        return mfm.load(instance) if mfm else None

    # ── Stage 0: upgrade actively-watched movies ─────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_active_watcher_upgrades")
    def run_active_watcher_upgrades(self, instance: str, free_space_gb: float) -> dict:
        """
        When free space is comfortably above the upgrade threshold, upgrade
        non-kids movies that the household is actively watching to the best
        available quality profile.

        "Actively watching" = watched within ACTIVE_WATCH_DAYS (default 30d)
        and NOT in the kids library (certification G/PG excluded unless
        the household adults also watched it).

        NEVER touches keep_universe, keep_forever, keep_movie — the universe
        manager owns keep_universe upgrades.

        Only upgrades when free_space_gb >= U (top of the pressure band derived from
        free_space_limit) so we never upgrade into a space crunch.
        """
        ACTIVE_WATCH_DAYS   = 30
        KIDS_CERTS          = {"g", "pg", "tv-g", "tv-y", "tv-y7"}
        _, upgrade_min_free_gb = self._space_targets(instance)   # U = free_space_limit + headroom

        stats = {
            "checked": 0, "upgraded": 0, "already_best": 0,
            "skipped_kids": 0, "skipped_not_active": 0, "failed": 0,
        }
        # GLD-DEL-12 — intents accumulate here and persist once, after the pass.
        _upgrade_intents: list = []

        if free_space_gb < upgrade_min_free_gb:
            self.logger.log_debug(
                f"[SpacePressure] Active-watcher upgrades skipped: "
                f"{free_space_gb:.1f} GB < {upgrade_min_free_gb:.0f} GB threshold."
            )
            return stats

        mfm = self._get_movie_files_manager()
        if mfm is None:
            return stats

        df = mfm.load(instance)
        if df.empty:
            return stats

        # Fetch all quality profiles sorted best-last
        try:
            raw_profiles = (
                self.global_cache.get(f"radarr.quality.{instance}") or []
                if self.global_cache else []
            )
            if not raw_profiles:
                raw_profiles = self.radarr_api._make_request(
                    instance, "qualityprofile", fallback=[]
                ) or []
        except Exception:
            return stats

        if not raw_profiles:
            return stats

        # Rank profiles by max resolution — highest = best
        def _max_res(p: dict) -> int:
            best = 0
            for item in (p.get("items") or []):
                if not item.get("allowed"):
                    continue
                res = (item.get("quality") or {}).get("resolution", 0)
                if isinstance(res, (int, float)):
                    best = max(best, int(res))
                for sub in (item.get("items") or []):
                    if not sub.get("allowed"):
                        continue
                    sr = (sub.get("quality") or {}).get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
            return best

        ranked = sorted(raw_profiles, key=_max_res)
        if not ranked:
            return stats

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=ACTIVE_WATCH_DAYS)
        movie_ids_to_search: list[int] = []
        changed = False
        plan_changed = False

        # DECISION (which titles, to which profile, expected reclaim) is the
        # brain's; the loop below APPLIES it (stamp + dry-run preview or PUT).
        candidates, _pstats = plan_movie_upgrades(
            df, ranked, active_cutoff=cutoff, config=self.config
        )
        stats.update(_pstats)   # checked / already_best / skipped_kids / skipped_not_active

        for _cand in candidates:
            idx            = _cand["idx"]
            movie_id       = _cand["movie_id"]
            target_profile = _cand["target_profile"]
            target_id      = _cand["target_id"]
            target_name    = _cand["target_name"]
            likelihood     = _cand["likelihood"]
            title          = df.at[idx, "title"] or f"movie {movie_id}"
            cur_qp_name    = df.at[idx, "quality_profile_name"] if "quality_profile_name" in df.columns else "?"

            self._stamp_plan(df, idx, "upgrade", _cand["reason"], _cand["reclaim_gb"])
            plan_changed = True

            if self.dry_run:
                self.logger.log_info(
                    f"  📈 [dry_run] Would upgrade: '{title}' "
                    f"({cur_qp_name} → {target_name}) [L={likelihood:.0f}% → profile {target_id}]"
                )
                stats["upgraded"] += 1
                continue

            try:
                payload = self.radarr_api._make_request(
                    instance, f"movie/{int(movie_id)}", fallback=None
                )
                if payload and isinstance(payload, dict):
                    payload["qualityProfileId"] = target_id
                    self.radarr_api._make_request(
                        instance, f"movie/{int(movie_id)}", method="PUT", payload=payload
                    )
                    df.at[idx, "quality_profile_id"]   = target_id
                    df.at[idx, "quality_profile_name"] = target_name
                    movie_ids_to_search.append(int(movie_id))
                    stats["upgraded"] += 1
                    # GLD-DEL-12 — record the file this movie owns RIGHT NOW. When the
                    # upgrade lands Radarr's id will differ, and that difference is the
                    # only proof available; without it the upgrade is fire-and-forget
                    # and leaves no `upgraded` event for churn or the space ledger.
                    _upgrade_intents.append(upgrade_intent(
                        movie_id=movie_id,
                        file_id=df.at[idx, "movie_file_id"] if "movie_file_id" in df.columns else None,
                        size_bytes=df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None,
                        quality_name=cur_qp_name,
                        title=title))
                    changed = True
                    self.logger.log_info(
                        f"  📈 Upgraded: '{title}' "
                        f"({cur_qp_name} → {target_name})"
                    )
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Upgrade failed for '{title}': {e}"
                )
                stats["failed"] += 1

        if movie_ids_to_search and not self.dry_run:
            # GLD-DEL-12 — persist the worklist only when searches actually fired. A
            # dry_run triggers nothing, so recording intents would build a worklist for
            # upgrades that never happened.
            self._persist_upgrade_intents(instance, _upgrade_intents)
            try:
                self.radarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "MoviesSearch", "movieIds": movie_ids_to_search},
                )
                self.logger.log_info(
                    f"  🔍 Search triggered for {len(movie_ids_to_search)} upgraded movie(s)"
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Search trigger failed: {e}")

        if changed or plan_changed:
            mfm.save(instance, df)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["upgraded",      stats['upgraded']],
                ["already best",  stats['already_best']],
                ["skipped kids",  stats['skipped_kids']],
                ["not active",    stats['skipped_not_active']],
                ["failed",        stats['failed']],
            ],
            title=f"[SpacePressure] {prefix}active-watcher upgrades - '{instance}'",
            caption="Result of the pass that upgrades actively-watched movies to the best profile when space is plentiful.",
            descriptions=[
                "movies upgraded to a higher quality profile",
                "movies already at the best available profile",
                "kids-library movies skipped from upgrade",
                "movies not watched recently enough to upgrade",
                "movies whose PUT/search call errored",
            ],
        )
        return stats

    # ── Combined run ─────────────────────────────────────────────────────────────

    @timeit("refresh_scores")
    def refresh_scores(self, instance: str) -> int:
        """
        Compute watchability scores for every movie_files Parquet row and
        write them back to ``watchability_score``.

        Must be called before the universe quality manager runs so it has
        valid scores to gate 4K eligibility.
        """
        mfm = self._get_movie_files_manager()
        if mfm is None:
            return 0
        import json

        instance  = self._resolve_instance(instance)
        df        = mfm.load(instance)
        if df.empty:
            return 0
        # with_breakdown=True → {idx: (score, breakdown)}. The score is byte-identical
        # to the no-breakdown path; only the explanation dict is also returned, and it
        # is persisted alongside as a small flat JSON so the advise view can read back
        # WHICH signal groups raised/lowered each title's score.
        score_map = self._build_score_map(df, instance, with_breakdown=True)
        if not score_map:
            return 0
        if "watchability_score" not in df.columns:
            df["watchability_score"] = None
        if "watchability_breakdown" not in df.columns:
            df["watchability_breakdown"] = None
        elif df["watchability_breakdown"].dtype != object:
            # An all-null reloaded column comes back float64; force object so the
            # JSON-string assignments below don't trip the dtype FutureWarning.
            df["watchability_breakdown"] = df["watchability_breakdown"].astype(object)
        score_only: dict[int, int] = {}
        for idx, (score, breakdown) in score_map.items():
            score_only[idx] = score
            df.at[idx, "watchability_score"] = score
            df.at[idx, "watchability_breakdown"] = (
                json.dumps(breakdown, separators=(",", ":")) if breakdown else None
            )
        score_map = score_only   # downstream logging/percentile expects {idx: int}
        # Percentile rank of each score within the library — the rank-based input to
        # the watch-likelihood ladder (Option 1: spreads affinity across the tiers
        # instead of bunching at the low end of the 0-100 score).
        _sc = pd.to_numeric(df["watchability_score"], errors="coerce")
        df["watchability_percentile"] = (_sc.rank(pct=True, method="average") * 100).round(1)
        # Group-A5 DELETE SHIELD — stamped as a COLUMN, deliberately, not evaluated at
        # delete time. The space coordinator's delete path builds its pool from a bare
        # ``load_movie_files`` parquet with no manager graph and no cache handle, so it
        # cannot call a live predicate; the same three-layer shape ``retention_hold`` uses
        # (stamp a column here → read it in the guard → read it again at delete time) is
        # the only shape that reaches every path. Non-destructive; all-False when the term
        # is disabled or nothing is watchlisted.
        try:
            self._apply_watchlist_shield(df)
        except Exception as e:
            self.logger.log_debug(f"[Intent] watchlist shield skipped for '{instance}': {e}")
        # Franchise/universe credit: a hot saga lends borrowed effective-watch-count to its members,
        # so the likelihood-gated upgrade AND the space-pressure downgrade pass elevate a single-watch
        # member (and let it fall again as the saga's last watch recedes). Non-destructive; 0 when cold.
        try:
            self._apply_universe_credit(instance, df)
        except Exception as e:
            self.logger.log_debug(f"[Universe] credit pass skipped for '{instance}': {e}")
        # The score is a non-destructive annotation — persist it even in dry_run
        # so the Parquet is sortable by watchability ("least valuable first").
        mfm.save(instance, df)
        self.logger.log_info(
            f"[SpacePressure] Scored {len(score_map)} movies for '{instance}' "
            f"(range: {min(score_map.values())}–{max(score_map.values())})"
        )
        # ── ML snapshot append (Stage 1 — pure logging; ml.snapshots.enabled,
        #    DEFAULT ON). Reads the score/breakdown columns just persisted above.
        #    Fully wrapped: a snapshot failure can never affect the run.
        try:
            from scripts.managers.machine_learning.labels.snapshots import (
                maybe_snapshot_movies,
            )
            maybe_snapshot_movies(self.config, self.global_cache, self.logger,
                                  instance, df)
        except Exception as e:
            self.logger.log_debug(f"[MLSnapshot] radarr/{instance} snapshot hook failed: {e}")
        try:
            _rows = self.report_size_anomalies(instance, df)
            self.remediate_size_anomalies(instance, _rows)
        except Exception as e:
            self.logger.log_debug(f"[SizeAnomaly] report/remediate failed for '{instance}': {e}")
        try:
            self.report_codec_routing(instance, df)   # read-only preview; changes nothing
            self.report_transcode_causes()            # household-wide cause breakdown; logs once per run
        except Exception as e:
            self.logger.log_debug(f"[CodecRoute] preview failed for '{instance}': {e}")
        return len(score_map)

    def _apply_universe_credit(self, instance: str, df) -> None:
        """Broadcast a per-movie ``universe_credit`` column: borrowed effective-watch-count from a HOT
        saga (rewatched siblings), recency-decayed. Membership is the movie's TMDB COLLECTION
        (``collection_name`` — populated automatically from TMDB metadata, NO keep tag required, so it
        works for every user) unioned with any curated ``universe_name`` labels (pipe-sep) when present;
        a film keeps the credit of its HOTTEST group. Read by ``watch_likelihood`` so BOTH the
        space-pressure passes (untagged saga movies, via plan_movie_downgrades / plan_movie_upgrades)
        AND the universe manager (keep-tagged) elevate a single-watch member of a hot saga, and let it
        fall again as the saga's last watch recedes. 0 everywhere with no franchise heat → byte-identical."""
        has_coll = "collection_name" in df.columns
        has_uni  = "universe_name" in df.columns
        if "movie_id" not in df.columns or not (has_coll or has_uni):
            df["universe_credit"] = 0.0
            return
        _mid = pd.to_numeric(df["movie_id"], errors="coerce")
        _wc  = pd.to_numeric(df["watch_count"], errors="coerce") if "watch_count" in df.columns else None
        _lw  = pd.to_datetime(df["last_watched_at"], utc=True, errors="coerce") \
            if "last_watched_at" in df.columns else None
        now = pd.Timestamp.now(tz="UTC")
        universe_map: dict = {}
        stats: dict = {}
        for pos, idx in enumerate(df.index):
            m = _mid.iat[pos]
            if pd.isna(m):
                continue
            mid = int(m)
            labels: list[str] = []
            if has_coll:
                c = df.at[idx, "collection_name"]
                if c is not None and pd.notna(c) and str(c).strip():
                    labels.append(str(c).strip())
            if has_uni:
                u = df.at[idx, "universe_name"]
                if u is not None and pd.notna(u) and str(u).strip():
                    labels.append(str(u).strip())
            if labels:
                universe_map[mid] = "|".join(labels)
            wc = float(_wc.iat[pos]) if (_wc is not None and pd.notna(_wc.iat[pos])) else 0.0
            ds = (now - _lw.iat[pos]).days if (_lw is not None and pd.notna(_lw.iat[pos])) else 1e9
            stats[mid] = {"watch_count": wc, "days_since": ds}
        # Drop the junk placeholder group names ("universe"/"franchise"/"standalone"/…) — keep_policy
        # stamps bare-universe films with universe_name="universe", which must NOT fuse unrelated films.
        credits = movie_universe_credits(universe_map, stats, config=self.config,
                                         drop_labels=PLACEHOLDER_AFFINITY)
        # Saga CAUGHT-UP / DEPTH credit (household, cross-media, release-grace-decayed) — combined via
        # max with the rewatched-fraction credit above. Default-off: returns {} (no change) when
        # scoring.saga_credit.enabled is unset, so this is byte-identical inert until opted in.
        saga_cr = self._saga_quality_credits(df, _mid, instance)
        # SPLIT, not blended - see the matching note in the Sonarr episode cache.
        # ``universe_credit`` = rewatched-sibling heat only (a real reason to resist DELETION).
        # ``saga_credit`` = caught-up/depth, a forward-looking ACQUISITION/QUALITY signal that
        # must not hold old files on disk. Quality consumers take the max of the two; the
        # delete-pool guard below reads universe_credit alone.
        df["universe_credit"] = _mid.map(
            lambda m: credits.get(int(m), 0.0) if pd.notna(m) else 0.0)
        df["saga_credit"] = _mid.map(
            lambda m: saga_cr.get(int(m), 0.0) if pd.notna(m) else 0.0)
        if credits or saga_cr:
            _allc = dict(credits)
            for _k, _v in saga_cr.items():
                _allc[_k] = max(_allc.get(_k, 0.0), _v)
            _saga_note = f"; {len(saga_cr)} via saga caught-up/depth" if saga_cr else ""
            self.logger.log_info(
                f"[Universe] '{instance}': lent franchise/collection credit to {len(_allc)} movies "
                f"(max {max(_allc.values()):.2f} watch-counts{_saga_note})."
            )

    def _saga_quality_credits(self, df, _mid, instance) -> dict:
        """``{movie_id: saga caught-up/depth credit}`` for the QUALITY pre-pass — household,
        cross-media, release-grace-decayed (:func:`watch_likelihood.saga_credit`). Empty when
        ``scoring.saga_credit.enabled`` is off or the universe source / ``tmdb_id``+``date_added``
        columns are absent, so the caller's ``max`` is a no-op. ``_mid`` is the numeric ``movie_id``
        series (position-aligned with ``df``). Also surfaces the per-title detail to the run log + a
        global_cache snapshot (the GUI feed) via :func:`emit_saga_credit_preview`. Best-effort: logs +
        returns ``{}`` on any failure."""
        out: dict = {}
        try:
            from scripts.managers.machine_learning.likelihood.saga_engagement import (
                emit_saga_credit_preview,
                gather_saga_engagement,
                household_member_count,
            )
            from scripts.managers.machine_learning.likelihood.watch_likelihood import saga_credit
            from scripts.managers.services.plex.playlists.universe_order import saga_display_name
            eng = gather_saga_engagement(self.global_cache, self.config)
            if not eng or "tmdb_id" not in df.columns or "date_added" not in df.columns:
                return out
            members = household_member_count(self.config, self.global_cache) or None
            now = pd.Timestamp.now(tz="UTC")
            _tm = pd.to_numeric(df["tmdb_id"], errors="coerce")
            _da = pd.to_datetime(df["date_added"], utc=True, errors="coerce")
            _has_title = "title" in df.columns
            items: list = []
            for pos, _idx in enumerate(df.index):
                m, tmdb = _mid.iat[pos], _tm.iat[pos]
                if pd.isna(m) or pd.isna(tmdb) or pd.isna(_da.iat[pos]):
                    continue
                e = eng.get(("movie", int(tmdb)))
                if not e:
                    continue
                days = max(0.0, float((now - _da.iat[pos]).days))
                cr = saga_credit(caught_up_frac=e["caught_up_frac"],
                                 saga_watched_frac=e["saga_watched_frac"],
                                 days_since_available=days, household_members=members,
                                 config=self.config)
                if cr > 0:
                    out[int(m)] = cr
                    items.append({"id": int(m),
                                  "title": (df.at[_idx, "title"] if _has_title else int(m)),
                                  "saga": saga_display_name(e.get("saga", "")),
                                  "caught_up": e["caught_up_frac"], "depth": e["saga_watched_frac"],
                                  "days": days, "credit": cr})
            emit_saga_credit_preview(self.global_cache, self.logger, self.config, "radarr", instance, items)
        except Exception as e:
            self.logger.log_debug(f"[Universe] saga quality credit skipped: {e}")
        return out

    @timeit("report_size_anomalies")
    def report_size_anomalies(self, instance: str, df=None) -> list:
        """Flag movies whose file is WILDLY out of size profile for their graded quality (e.g.
        a 45 GiB file graded 720p ≈ 6x its expected size). Read-only diagnostic: logs a count
        and records a detail table in the end-of-run summary. Returns the anomaly rows (sorted
        biggest-reclaim first) so a space pass can act on them. Off via size_anomaly.enabled=false."""
        cfg = size_anomaly.config_for(self.config)
        if not cfg.get("enabled", True):
            return []
        instance = self._resolve_instance(instance)
        if df is None:
            mfm = self._get_movie_files_manager()
            df = mfm.load(instance) if mfm is not None else None
        if df is None or getattr(df, "empty", True):
            return []

        rows = size_anomaly.find_size_anomalies(
            df, id_cols=("title", "year", "movie_id", "movie_file_id"),
            size_col="size_bytes", runtime_col="runtime_minutes", runtime_unit="minutes",
            quality_col="quality_name", resolution_col="resolution",
            over_ratio=cfg["over_ratio"], under_ratio=cfg["under_ratio"],
            min_samples=cfg["min_samples"],
        )
        if not rows:
            return []
        over = [r for r in rows if r["verdict"] == "oversized"]
        reclaim = sum(r["reclaim_gb"] for r in over)
        self.logger.log_info(
            f"[SizeAnomaly] '{instance}': {len(rows)} file(s) wildly out of size profile — "
            f"{len(over)} oversized (~{reclaim:.0f} GB reclaimable at the in-profile size), "
            f"{len(rows) - len(over)} undersized."
        )
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            table = [[str(r.get("title"))[:30], r["quality_name"], r["looks_like"],
                      f"{r['size_gb']:.1f} GB", f"{r['expected_gb']:.1f} GB", f"x{r['ratio']:.1f}",
                      f"{r['reclaim_gb']:.1f} GB", r["verdict"]] for r in rows[:cfg["report_limit"]]]
            _rs.add_rows(
                "radarr", "Size anomalies", instance,
                ["Title", "Graded", "Looks like", "Size", "Expected", "Ratio", "Reclaim", "Verdict"],
                table, order=35,
            )
        return rows

    def report_codec_routing(self, instance: str, df=None) -> list:
        """READ-ONLY codec-routing preview. For each owned, WATCHED movie, show the codec the
        transcode-minimising policy WOULD pick for its actual viewers (profile_selector.
        choose_codec_profile over the per-user device→transcode matrix) vs the file's CURRENT
        codec. Changes NOTHING — logs a count and records a 'Codec routing preview' table in the
        end-of-run summary so the codec-aware-routing decisions are visible before any actuation
        is wired. Cheap no-op when there are no codec-variant profiles or no watch history.
        Off via ``scoring.codec_profiles.report=false``. (v1 covers WATCHED titles; extending the
        compute to all owned titles via affinity-predicted viewers is the Phase-2 follow-up.)"""
        if not (((self.config or {}).get("scoring") or {}).get("codec_profiles") or {}).get("report", True):
            return []
        instance = self._resolve_instance(instance)
        if df is None:
            mfm = self._get_movie_files_manager()
            df = mfm.load(instance) if mfm is not None else None
        if df is None or getattr(df, "empty", True):
            return []
        history = (self.global_cache.get("tautulli/history/all") if self.global_cache else None) or []
        if not history:
            self.logger.log_info(
                f"[CodecRoute] '{instance}': no Tautulli watch history cached yet — nothing to evaluate.")
            return []
        try:
            profiles = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
        except Exception:
            profiles = []
        if not profiles:
            self.logger.log_info(f"[CodecRoute] '{instance}': no quality profiles available — skipped.")
            return []
        from scripts.managers.machine_learning.quality_analytics.codec_report import (
            build_per_title_watchers, codec_report_rows, normalize_title,
            per_user_platform_usage_from_history,
        )
        from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
            per_user_source_fingerprint_matrix, per_user_transcode_fingerprint_matrix,
        )
        # Source-codec matrix (keyed by the FILE's codec via the metadata index, NOT Plex's streamed /
        # transcode-target codec) — this is what makes the prediction codec-aware. Falls back to the
        # streamed matrix (codec-blind) only when no metadata index is cached yet.
        metadata = (self.global_cache.get("tautulli/metadata/index") if self.global_cache else None) or {}
        matrix = (per_user_source_fingerprint_matrix(history, metadata) if metadata
                  else per_user_transcode_fingerprint_matrix(history))
        watchers = build_per_title_watchers(history)
        rows = codec_report_rows(
            df, profiles, matrix,
            per_user_platform_usage_from_history(history),
            watchers,
        )
        # TRANSPARENCY: always report what the pass evaluated — even when nothing qualifies or nothing
        # changes — so the run log shows it ran (vs. silently finding no rows). ``n_watched`` is owned
        # movies that appear in the watch history; ``len(rows)`` is the subset at a multi-codec tier.
        _titles = df["title"] if "title" in getattr(df, "columns", []) else []
        n_watched = sum(1 for t in _titles if normalize_title(t) in watchers)
        n_changed = sum(1 for r in rows if r["change"])
        self.logger.log_info(
            f"[CodecRoute] '{instance}': evaluated {n_watched} watched movie(s) "
            f"({len(rows)} at a multi-codec tier); {n_changed} would change codec to reduce "
            f"transcoding (read-only preview; nothing applied)."
        )
        if rows:
            headers = ["Title", "Viewers", "Current", "Recommend", "CurCost", "RecCost", "Change"]
            table = [[str(r["title"])[:28], ",".join(r["watchers"])[:16],
                      r["current_codec"], r["recommended_codec"],
                      f"{r['current_cost']:.2f}", f"{r['recommended_cost']:.2f}",
                      "YES" if r["change"] else "-"] for r in rows[:25]]
            # Log the table DIRECTLY so it's visible in the run log, not only the (mode-dependent)
            # end-of-run summary. CurCost/RecCost = the viewers' predicted P(transcode) for the current
            # vs recommended codec; a change is flagged only when RecCost is materially lower.
            self.logger.log_grid(
                headers, table,
                title=f"Codec routing preview - '{instance}' (read-only; cost = P(transcode))", cap=24,
            )
            _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
            if _rs is not None:
                _rs.add_rows("radarr", "Codec routing preview", instance, headers, table, order=37)
        return rows

    def report_transcode_causes(self) -> dict:
        """READ-ONLY household transcode-CAUSE breakdown: per viewer, WHY their plays transcode
        (subtitle / video codec / video bitrate-res / audio / remote bandwidth). Codec routing only
        fixes the 'video: codec' slice, so this answers whether it's worth pursuing for the household.
        Household-wide, so it logs ONCE per run (later per-instance calls are no-ops). Changes nothing.
        Off via ``scoring.codec_profiles.report=false``."""
        if getattr(self, "_transcode_causes_logged", False):
            return {}
        if not (((self.config or {}).get("scoring") or {}).get("codec_profiles") or {}).get("report", True):
            return {}
        history = (self.global_cache.get("tautulli/history/all") if self.global_cache else None) or []
        if not history:
            return {}
        self._transcode_causes_logged = True
        metadata = (self.global_cache.get("tautulli/metadata/index") if self.global_cache else None) or {}
        stream_decisions = (self.global_cache.get("tautulli/stream_decisions") if self.global_cache else None) or {}
        from scripts.managers.machine_learning.quality_analytics.transcode_causes import (
            transcode_cause_breakdown,
        )
        breakdown = transcode_cause_breakdown(history, stream_decisions, metadata)
        rows = []
        for user, b in sorted(breakdown.items(), key=lambda kv: -kv[1]["transcodes"]):
            if b["transcodes"] <= 0:
                continue
            top = ", ".join(f"{c}:{n}" for c, n in list(b["causes"].items())[:3])
            rows.append([str(user)[:18], str(b["transcodes"] + b["directs"]), str(b["transcodes"]),
                         f"{b['rate'] * 100:.0f}%", "y" if b["ground_truth"] else "~", top[:42]])
        if not rows:
            return breakdown
        self.logger.log_grid(
            ["Viewer", "Plays", "Transc", "Rate", "GT", "Top causes"], rows,
            title="Transcode causes by viewer (read-only; codec routing only fixes 'video: codec')",
            cap=20,
        )
        total_t = sum(b["transcodes"] for b in breakdown.values())
        codec_t = sum(b["causes"].get("video: codec", 0) for b in breakdown.values())
        self.logger.log_info(
            f"[CodecRoute] transcode causes household-wide: {total_t} transcode(s), of which "
            f"{codec_t} ({(codec_t / total_t * 100 if total_t else 0):.0f}%) are video-codec — the only "
            f"slice codec routing can fix; the rest are bitrate/audio/subtitle/remote-bandwidth."
        )
        return breakdown

    @timeit("remediate_size_anomalies")
    def remediate_size_anomalies(self, instance: str, rows: "list | None") -> dict:
        """ACT on the size anomalies (opt-in: ``size_anomaly.remediate=true``).

          * MIS-GRADED (junk/SD grade, really HD) → ``RefreshMovie`` rescans mediainfo to fix the
            grade. Non-destructive.
          * BLOATED (oversized at a real HD/UHD grade) → ACQUIRE-then-replace, never delete-first:
            an interactive search (``GET release?movieId=``) lists every candidate release with its
            size, we pick a SAME-RESOLUTION, in-profile-size one and grab it by guid (``POST release``),
            and Radarr's import removes the old bloated file on success. No window where the movie has
            no file; if no acceptable smaller release exists the bloated file is LEFT AS-IS. A blind
            ``MoviesSearch`` can't do this — the bloated file is already at the profile cutoff (Radarr
            grabs nothing), and even forced it would re-grab the same top-scored remux. Only for
            MONITORED movies, and the grab honours the run's dry_run AND the degrade-to-dry-run backup
            gate (otherwise logged as 'would …')."""
        cfg = size_anomaly.config_for(self.config)
        if not cfg.get("remediate", False) or not rows:
            return {}
        instance = self._resolve_instance(instance)
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        stats = {"rescanned": 0, "regrabbed": 0, "skipped_unmonitored": 0,
                 "skipped_no_release": 0, "failed": 0,
                 # ACCOUNTING, not a bug fix. VERDICT and ACTION are different
                 # populations and conflating them misreads a correct log as a
                 # defect - which happened on 2026-08-10 while auditing this very
                 # method:
                 #
                 #   report:  82 anomalies = 12 oversized + 70 undersized
                 #   summary: 80 rescanned, 0 re-grabbed, 2 skipped (unmonitored)
                 #
                 # "12 oversized but only 2 in the regrab counters" LOOKS like ten
                 # rows vanishing. They did not. `recommend_action` sends an
                 # oversized file at a JUNK/SD grade to `rescan`, because a 30 GB
                 # file graded SDTV is MIS-GRADED, not bloated - rescanning fixes
                 # the grade non-destructively. So 10 of the 12 were rescan rows:
                 # 70 undersized + 10 junk-graded = 80 rescanned, leaving exactly
                 # 2 genuine regrab rows, both unmonitored. 80 + 2 = 82. Exact.
                 #
                 # These three counters make that split visible so the arithmetic
                 # can be checked from the log instead of re-derived from source.
                 # `missing_ids` additionally catches a real fault if one ever
                 # occurs: a regrab-CLASSIFIED row arriving with no ids is a
                 # detector/remediator mismatch, not a policy skip.
                 "not_regrab_action": 0, "missing_ids": 0, "dry_deferred": 0}

        # ── rescan mis-graded (non-destructive) ──────────────────────────────────
        mids = [int(r["movie_id"]) for r in rows
                if r.get("action") == "rescan" and r.get("movie_id") is not None]
        if mids:
            if eff_dry:
                self.logger.log_info(f"[SizeAnomaly] [dry_run] would RefreshMovie (rescan) "
                                     f"{len(mids)} mis-graded movie(s) on '{instance}'.")
            else:
                try:
                    self.radarr_api._make_request(instance, "command", method="POST",
                                                  payload={"name": "RefreshMovie", "movieIds": mids})
                    stats["rescanned"] = len(mids)
                    self.logger.log_info(f"[SizeAnomaly] rescanned {len(mids)} mis-graded "
                                         f"movie(s) on '{instance}' to fix the grade.")
                except Exception as e:
                    stats["failed"] += 1
                    self.logger.log_warning(f"[SizeAnomaly] rescan batch failed on '{instance}': {e}")

        # ── re-grab bloated (acquire-then-replace: grab a right-sized release by guid) ─────
        # Dry-run budget: each candidate costs a movie GET + an interactive indexer
        # search (~2s of blocked wall each — profiler showed ~19s/run at 0.0 CPU)
        # just to NAME the would-grab release in the preview. Default 0 defers the
        # checks (the size-anomaly grid already lists every candidate); raise
        # size_anomaly.dry_run_search_budget to sample real releases in dry runs.
        _dry_search_budget = int(cfg.get("dry_run_search_budget", 0) or 0)
        _dry_searched = 0
        _dry_deferred = 0
        for r in rows:
            if r.get("action") != "regrab":
                # RESCAN rows (every undersized file, plus oversized-at-a-junk-grade)
                # and anything the classifier declined. Counted so the summary can show
                # that verdict-oversized and action-regrab are different populations.
                stats["not_regrab_action"] += 1
                continue
            mid, fid = r.get("movie_id"), r.get("movie_file_id")
            if mid is None or fid is None:
                # A regrab-classified row with no ids cannot be acted on. This IS a
                # real fault if it ever fires - the detector produced a row the
                # remediator cannot use - so it logs per item rather than only
                # incrementing a counter. Not observed to date.
                stats["missing_ids"] += 1
                self.logger.log_info(
                    f"[SizeAnomaly] cannot re-grab '{r.get('title') or '?'}' - row carries "
                    f"no movie_id/movie_file_id (detector/remediator mismatch).")
                continue
            if eff_dry:
                if _dry_searched >= _dry_search_budget:
                    _dry_deferred += 1
                    stats["dry_deferred"] += 1
                    continue
                _dry_searched += 1
            # Only re-grab MONITORED movies — replacing an unmonitored movie's file overrides a
            # deliberate opt-out (and Radarr won't keep monitoring the result).
            mv = self.radarr_api._make_request(instance, f"movie/{int(mid)}", fallback=None)
            if not (isinstance(mv, dict) and mv.get("monitored")):
                stats["skipped_unmonitored"] += 1
                self.logger.log_info(f"[SizeAnomaly] skip re-grab '{r.get('title')}' — not monitored.")
                continue
            cur_res = (((mv.get("movieFile") or {}).get("quality") or {}).get("quality") or {}).get("resolution")
            # No current resolution (the file was deleted/moved since the snapshot) → we can't keep the
            # resolution we own; a resolution-blind grab could route a 4K release into a 1080p instance.
            # Leave it for next run rather than risk a wrong-resolution pull.
            if not cur_res:
                stats["skipped_no_release"] += 1
                self.logger.log_info(
                    f"[SizeAnomaly] keep '{r.get('title')}' — current resolution unknown (no movie "
                    f"file); won't risk a wrong-resolution grab.")
                continue
            expected_gb = float(r.get("expected_gb") or 0)
            # ONE interactive search (read-only, but it does hit indexers) reveals every candidate
            # release + size; pick a same-resolution, in-profile-size one. Done in dry-run too so the
            # preview names the actual release that WOULD be grabbed.
            releases = self.radarr_api._make_request(instance, f"release?movieId={int(mid)}", fallback=None) or []
            pick = self._pick_replacement_release(
                releases, expected_gb=expected_gb, resolution=cur_res,
                under_ratio=cfg["under_ratio"], over_ratio=cfg["over_ratio"])
            if not pick:
                stats["skipped_no_release"] += 1
                self.logger.log_info(
                    f"[SizeAnomaly] keep '{r.get('title')}' — no in-profile same-resolution release "
                    f"found to replace the {r.get('size_gb')} GB {r.get('quality_name')} file (left as-is).")
                continue
            pick_gb = float(pick.get("size") or 0) / (1024 ** 3)
            if eff_dry:
                self.logger.log_info(
                    f"[SizeAnomaly] [dry_run] would re-grab '{r.get('title')}': grab "
                    f"'{pick.get('title')}' ({pick_gb:.1f} GB) to replace the {r.get('size_gb')} GB "
                    f"{r.get('quality_name')} file — Radarr import removes the old file "
                    f"(~{r.get('reclaim_gb')} GB reclaim). No file is deleted first.")
                continue
            try:
                # Confirm the grab was accepted — _make_request returns None on a soft rejection
                # (release no longer grabbable / indexer down) WITHOUT raising, so don't count those as
                # replaced (mirrors legacy_regrab). The old file is untouched, so it re-flags next run.
                res = self.radarr_api._make_request(
                    instance, "release", method="POST", fallback=None,
                    payload={"guid": pick.get("guid"), "indexerId": pick.get("indexerId"),
                             "movieId": int(mid)})
                if res is None:
                    stats["failed"] += 1
                    self.logger.log_warning(
                        f"[SizeAnomaly] re-grab not accepted for '{r.get('title')}': grab of "
                        f"'{pick.get('title')}' returned no result (left as-is, will re-flag next run).")
                    continue
                stats["regrabbed"] += 1
                self.logger.log_info(
                    f"[SizeAnomaly] re-grabbing '{r.get('title')}': grabbed '{pick.get('title')}' "
                    f"({pick_gb:.1f} GB) — Radarr will import and replace the {r.get('size_gb')} GB file.")
            except Exception as e:
                stats["failed"] += 1
                self.logger.log_warning(f"[SizeAnomaly] re-grab failed for '{r.get('title')}': {e}")

        if _dry_deferred:
            self.logger.log_info(
                f"[SizeAnomaly] [dry_run] '{instance}': {_dry_deferred} bloated file(s) queued "
                f"for re-grab — release checks deferred (candidates are in the size-anomaly "
                f"grid; set size_anomaly.dry_run_search_budget>0 to sample releases inline).")
        acted = stats["rescanned"] + stats["regrabbed"]
        if acted or stats["skipped_unmonitored"] or stats["skipped_no_release"] \
                or stats["missing_ids"] or stats["dry_deferred"]:
            # The regrab lane reports against the rows it was actually HANDED, not
            # against the oversized count - see the note at `stats`. `_cands` is the
            # regrab population; `not_regrab_action` is the rescan one, and the two
            # together are every row.
            _cands = (stats["regrabbed"] + stats["skipped_unmonitored"]
                      + stats["skipped_no_release"] + stats["missing_ids"]
                      + stats["dry_deferred"] + stats["failed"])
            self.logger.log_info(
                f"[SizeAnomaly] '{instance}' remediation: {stats['rescanned']} rescanned, "
                f"{stats['regrabbed']} re-grabbed, {stats['skipped_unmonitored']} skipped "
                f"(unmonitored), {stats['skipped_no_release']} kept (no right-sized release), "
                + (f"{stats['dry_deferred']} deferred (dry-run search budget), "
                   if stats["dry_deferred"] else "")
                + (f"{stats['missing_ids']} unusable (no ids), "
                   if stats["missing_ids"] else "")
                + f"{stats['failed']} failed — {_cands} regrab candidate(s) of "
                f"{_cands + stats['not_regrab_action']} anomaly row(s) "
                f"({stats['not_regrab_action']} were rescan-action)."
            )
        return stats

    # ── Step-down failure cooldown ────────────────────────────────────────
    # Implemented in support.utilities.stepdown_cooldown - the SAME ledger idiom
    # legacy_regrab uses, so there is one convention for "don't retry this yet" rather
    # than two. Keyed by TMDB id, not movie_file_id: a step-down deletes the file, so a
    # file-keyed entry would be orphaned on the grab_failed path - exactly when the
    # backoff matters most.

    def _stepdown_ledger(self, instance: str) -> dict:
        """Load this instance's cooldown ledger (mutated in place, saved once per run)."""
        from scripts.support.utilities.stepdown_cooldown import ledger_key
        if not self.global_cache:
            return {}
        return dict(self.global_cache.get(ledger_key("radarr", instance)) or {})

    def _save_stepdown_ledger(self, instance: str, ledger: dict) -> None:
        """Persist the ledger, pruning entries whose window has fully elapsed."""
        from scripts.support.utilities.stepdown_cooldown import ledger_key, prune
        if not self.global_cache or ledger is None:
            return
        prune(ledger, self.config)
        self.global_cache.set(ledger_key("radarr", instance), ledger)

    _ROMAN_NUM = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
                  "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
    _FOREIGN_AUDIO = {"french", "truefrench", "vff", "vfq", "vostfr", "german",
                      "ita", "italian", "spanish", "castellano", "latino", "hindi",
                      "russian", "rus", "korean", "mandarin", "cantonese", "polish",
                      "turkish", "nordic", "swedish", "norwegian", "danish"}
    _AUDIO_OK = {"english", "eng", "dual", "multi"}

    @staticmethod
    def _norm_title_tokens(text: str) -> list:
        """Lowercase, '&'→'and', punctuation→space, roman numerals→arabic per token.
        The shared normal form for release↔movie title matching (GLD-RAD-30)."""
        import re as _re
        s = (text or "").lower().replace("&", " and ")
        toks = [t for t in _re.split(r"[^a-z0-9]+", s) if t]
        return [RadarrSpacePressureManager._ROMAN_NUM.get(t, t) for t in toks]

    @staticmethod
    def _release_matches_movie(release_title: str, movie_title: str,
                               movie_year=None, alt_titles=()) -> bool:
        """GLD-RAD-30 — does this release NAME the movie we intend to grab?

        Born from a real apply pass that grabbed 'A.Business.Proposal.2025' for
        Demon Slayer: Infinity Castle, 'Snapdragon.1993' for DBZ: Broly,
        'Spiderman.2002' (film 1) for Spider-Man 2 and 'Scorpion.King.4' for The
        Scorpion King — the picker filtered on resolution/size/seeders and never
        asked WHICH movie the filename claims to be.

        Method: token-normalize both sides; drop standalone year tokens from the
        release (validated separately); require the movie's tokens (or any
        alternate title's) to appear as a CONTIGUOUS token subsequence, allowing
        adjacent-token joins in either direction ('spider man'≡'spiderman'); the
        token AFTER the match must not be a bare 1-2 digit sequel number the
        title itself doesn't end with (kills film-1-for-film-2 and sequel-4
        grabs); any year in the release must sit within ±1 of the movie's year
        (kills 'Superman.2025' for Superman '78, tolerates re-release cuts).
        A leading article (the/a/an) on the title is optional. Conservative by
        design: no title evidence ⇒ no match ⇒ the file is KEPT — a kept file
        beats a wrong grab on the destructive path."""
        _N = RadarrSpacePressureManager._norm_title_tokens
        rt_all = _N(release_title)
        rel_years = [int(t) for t in rt_all if len(t) == 4 and t.isdigit()
                     and 1900 <= int(t) <= 2099]
        rt = [t for t in rt_all if not (len(t) == 4 and t.isdigit()
                                        and 1900 <= int(t) <= 2099)]
        if movie_year is not None and rel_years:
            try:
                if min(abs(ry - int(movie_year)) for ry in rel_years) > 1:
                    return False
            except (TypeError, ValueError):
                pass

        def _flex(mt: list, start: int):
            i, j = start, 0
            while j < len(mt):
                if i >= len(rt):
                    return None
                a, b = mt[j], rt[i]
                if a == b:
                    i += 1; j += 1; continue
                if j + 1 < len(mt) and mt[j] + mt[j + 1] == b:
                    i += 1; j += 2; continue
                if i + 1 < len(rt) and a == rt[i] + rt[i + 1]:
                    i += 2; j += 1; continue
                return None
            return i

        candidates = [movie_title] + [t for t in (alt_titles or ()) if t]
        for cand in candidates:
            mt = _N(cand)
            variants = [mt]
            if mt and mt[0] in ("the", "a", "an"):
                variants.append(mt[1:])
            for v in variants:
                if not v:
                    continue
                for s in range(len(rt)):
                    end = _flex(v, s)
                    if end is None:
                        continue
                    nxt = rt[end] if end < len(rt) else None
                    if nxt and nxt.isdigit() and len(nxt) <= 2 and v[-1] != nxt:
                        continue   # sequel-number boundary: wrong film in franchise
                    return True
        return False

    @staticmethod
    def _release_language_ok(release: dict, allowed=("english",)) -> bool:
        """GLD-RAD-30 — audio-language gate. Radarr's per-release ``languages`` is
        authoritative when present: require an allowed language (or 'unknown').
        When absent, fall back to filename markers — a foreign-audio token
        (FRENCH/TRUEFRENCH/ITA/…) with no english/dual/multi marker rejects; subs
        tags (HebSubs, NL Subs) are subtitles, not audio, and stay eligible. Four
        of one apply pass's 21 grabs were FRENCH audio; this is that gate."""
        _allowed = {str(a).lower() for a in (allowed or ("english",))}
        langs = release.get("languages")
        if isinstance(langs, list) and langs:
            names = {str((l or {}).get("name", "")).lower()
                     for l in langs if isinstance(l, dict)}
            names.discard("")
            if names:
                return bool(names & _allowed) or "unknown" in names
        toks = set(RadarrSpacePressureManager._norm_title_tokens(release.get("title") or ""))
        if toks & RadarrSpacePressureManager._FOREIGN_AUDIO and not (
                toks & RadarrSpacePressureManager._AUDIO_OK):
            return False
        return True

    @staticmethod
    def _enabled_protocols(radarr_api, instance) -> "frozenset | None":
        """Protocols at least one ENABLED download client serves, or None when the
        answer is unavailable.

        WHY — GLD-RAD-33. 2026-08-15 live apply: Radarr 'standard' had NO torrent
        client, the picker chose torrent releases on seeders alone, and the realize
        flow is delete-THEN-grab — so 70 movies (~150 GB) were deleted and every grab
        500'd ("Torrent Download client isn't configured yet"), leaving blind-search
        fallbacks as the only thing between the library and the recycle bin's cleanup
        clock. A release nothing can download must never win the pick; with the pick
        empty the caller keeps the file and never reaches its delete.

        FAILURE SEMANTICS — the three-way distinction matters (P-C):
          list of clients  -> the frozenset of enabled protocols (may be EMPTY: no
                              enabled clients means nothing is grabbable, and an
                              empty set correctly rejects every candidate BEFORE any
                              delete can be reached).
          unreachable/odd  -> None = "unknown" — fail OPEN to the pre-guard
                              behaviour. Blocking every downgrade because one status
                              call blipped would be worse than the disease; the
                              delete-side contract (no pick ⇒ no delete) still holds.
        """
        try:
            clients = radarr_api._make_request(instance, "downloadclient", fallback=None)
            if not isinstance(clients, list):
                return None
            return frozenset(
                p for p in (str(c.get("protocol") or "").lower()
                            for c in clients if isinstance(c, dict) and c.get("enable"))
                if p)
        except Exception:
            return None

    @staticmethod
    def _pick_stepdown_release(releases: list, current_res=None,
                               min_size_bytes: int = 300 * 1024 * 1024,
                               allow_below_floor: bool = False,
                               target_res=None, movie_title=None, movie_year=None,
                               alt_titles=(), allowed_langs=("english",),
                               allowed_protocols=None) -> "dict | None":
        """Pick the release a step-down should grab: walk the resolution ladder
        UP from the floor (720 → 1080) and take the first non-empty rung strictly
        below the current file's resolution — 'no 720 found, take the next tier
        up' — never at/above the current resolution (that would re-grab what we
        are shrinking). Within a rung, the MEDIAN-sized release wins: the biggest
        is often a remux-grade outlier, the smallest a fake/undersized rip.
        ``min_size_bytes`` is the fake/undersized sanity floor — default 300 MiB
        (no 300MB "movies"); the Sonarr episode step-down passes a smaller floor
        (a legit 720p episode can be well under 300 MiB).
        Returns None when no rung has a candidate → caller keeps the file.

        ``target_res`` is the resolution the CALLER's profile change settled on. The picker
        prefers that tier and only descends when it is empty - see the note at the rung
        loop. None -> byte-identical to the previous behaviour.

        ``allow_below_floor`` (``space_exhaustive_downgrade``; DEFAULT False =
        byte-identical hard 720 floor): when set, and ONLY when no rung >= 720 exists
        strictly below the current resolution, fall back to the BEST available sub-720
        release (highest sub-720 rung, median within it, same fake/undersized size floor)
        rather than keeping the file. 720 stays the floor everywhere it can be honoured —
        this only fires for a title with literally no >=720 release. The returned dict is a
        SHALLOW COPY carrying ``stepped_below_floor=True`` so the caller can say "stepped
        below 720 — no >=720 release exists"; the normal >=720 path returns the release
        object itself, untouched."""
        try:
            cur = float(current_res) if current_res is not None and current_res == current_res else None
        except (TypeError, ValueError):
            cur = None
        by_rung: dict = {}
        sub_floor: dict = {}
        for r in releases or []:
            if not isinstance(r, dict) or not r.get("guid"):
                continue
            # GLD-RAD-33 — a release on a protocol NO enabled client serves is not a
            # candidate, however good it looks: the grab is a guaranteed 500 and the
            # caller's delete has already happened by then. None = unknown = allow
            # (fail-open); an ABSENT protocol field on the release is likewise not
            # treated as unserveable — absent is not "torrent" (P-C).
            if allowed_protocols is not None:
                _proto = str(r.get("protocol") or "").lower()
                if _proto and _proto not in allowed_protocols:
                    continue
            res = (((r.get("quality") or {}).get("quality") or {}).get("resolution"))
            try:
                res = int(res)
            except (TypeError, ValueError):
                continue
            if cur is not None and res >= cur:
                continue
            if float(r.get("size") or 0) < min_size_bytes:   # sanity floor (movies: no 300MB "movies")
                continue
            # VIABILITY. This picker deletes the existing file BEFORE grabbing, so a
            # release that can never download costs the household the copy it had. The
            # sibling picker (``_pick_movie_regrab_release``) already screens for this;
            # it guards a re-grab of a file that still exists, so the stricter check was
            # on the safer path and absent from the destructive one.
            #
            # Hard rejections only (sample / blocklist): a 'not an upgrade' or quality
            # rejection is EXPECTED here - a step-down is by definition not an upgrade,
            # and a manual grab by guid overrides those.
            _rj = [str(x).lower() for x in (r.get("rejections") or [])]
            if any(bad in x for x in _rj for bad in ("sample", "blocklist", "blacklist")):
                continue
            # WRONG-MOVIE / WRONG-LANGUAGE GATES (GLD-RAD-30). Radarr's own mapping
            # verdict first (an 'unknown movie' rejection is fatal here even though
            # quality rejections are expected), then the filename-parse identity
            # check, then the audio-language gate. movie_title=None (legacy caller
            # or tests) skips the identity check — byte-identical old behaviour.
            if any(bad in x for x in _rj for bad in
                   ("unknown movie", "unable to parse", "does not match", "not a match")):
                continue
            if movie_title and not RadarrSpacePressureManager._release_matches_movie(
                    r.get("title") or "", movie_title, movie_year, alt_titles):
                continue
            if not RadarrSpacePressureManager._release_language_ok(r, allowed_langs):
                continue
            # Torrent with nobody seeding it will never complete. Usenet reports no
            # seeders at all, so only a present-and-zero value disqualifies.
            _seed = r.get("seeders")
            if _seed is not None and _seed <= 0:
                continue
            if res < 720:
                if allow_below_floor:
                    sub_floor.setdefault(res, []).append(r)
                continue
            by_rung.setdefault(res, []).append(r)
        try:
            _target = int(target_res) if target_res is not None else None
        except (TypeError, ValueError):
            _target = None
        for rung in sorted(by_rung):
            # TARGET FLOOR. The caller's profile move decided a specific tier (e.g. 2160 ->
            # 1080, profile set to HD-1080p). Without this the loop takes the LOWEST rung
            # >= 720 and grabs 720p for a movie whose profile now demands 1080p - Radarr
            # then rejects the import or 404s the guid, and the file is already deleted.
            # Observed on 6 titles in one live pass (Deadpool 2, Eternals, Civil War,
            # Far From Home, GotG Vol. 2, ...).
            #
            # Rungs below the target are SKIPPED, not rejected outright: if nothing exists
            # at the target tier the loop still descends, which keeps the old "take what you
            # can get" behaviour rather than stranding a title. target_res=None (or a caller
            # that does not pass it) -> byte-identical to before.
            if _target is not None and rung < _target:
                continue
            cands = sorted(by_rung[rung], key=lambda r: float(r.get("size") or 0))
            return cands[len(cands) // 2]
        # Nothing at or above the target: fall back to the ladder as it was, lowest first.
        if _target is not None:
            for rung in sorted(by_rung):
                cands = sorted(by_rung[rung], key=lambda r: float(r.get("size") or 0))
                return cands[len(cands) // 2]
        # LAST RESORT: no >=720 rung below the current resolution exists at all. Take the
        # HIGHEST sub-720 rung (best of a bad lot) rather than strand the title above the
        # floor forever — an item that can never reach the floor could otherwise never
        # become delete-eligible either.
        for rung in sorted(sub_floor, reverse=True):
            cands = sorted(sub_floor[rung], key=lambda r: float(r.get("size") or 0))
            pick = dict(cands[len(cands) // 2])
            pick["stepped_below_floor"] = True
            return pick
        return None

    @staticmethod
    def _pick_replacement_release(releases, *, expected_gb, resolution, under_ratio, over_ratio):
        """Choose the release that should REPLACE a bloated file from Radarr interactive-search
        results. Returns the chosen release dict (carrying ``guid`` + ``indexerId``) or None.

        Eligibility: has a guid+indexerId, matches ``resolution`` (keep the resolution we own — when
        ``resolution`` is falsy the filter is skipped), and its size falls in the SAME in-profile band
        the detector uses (``under_ratio`` × expected < size < ``over_ratio`` × expected) so we skip
        both fake/tiny releases and still-bloated ones. Hard rejections (sample/blocklist) and torrents
        with no seeders are excluded; 'not an upgrade'/quality rejections are fine (a manual grab
        overrides them). Among the eligible, the one CLOSEST to the expected size wins (tie-break: most
        seeders) so the replacement lands at the profile target, not just under the bloat ceiling."""
        if not releases or expected_gb <= 0:
            return None
        lo, hi = under_ratio * expected_gb, over_ratio * expected_gb
        best, best_key = None, None
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            guid, indexer = rel.get("guid"), rel.get("indexerId")
            if not guid or indexer is None:
                continue
            res = (((rel.get("quality") or {}).get("quality") or {}).get("resolution"))
            if resolution and res != resolution:
                continue
            try:
                size_gb = float(rel.get("size") or 0) / (1024 ** 3)
            except (TypeError, ValueError):
                continue
            if not (lo < size_gb < hi):                       # skip undersized/fake AND still-bloated
                continue
            rejections = [str(x).lower() for x in (rel.get("rejections") or [])]
            if any(bad in rj for rj in rejections for bad in ("sample", "blocklist", "blacklist")):
                continue
            seeders = rel.get("seeders")
            if seeders is not None and seeders <= 0:          # dead torrent; usenet has no seeders → allow
                continue
            key = (abs(size_gb - expected_gb), -(seeders or 0))
            if best_key is None or key < best_key:
                best, best_key = rel, key
        return best

    @LoggerManager().log_function_entry
    @timeit("run_space_pressure")
    def run(self, instance: str) -> dict:
        """
        Full pipeline:
          1. Check free space — bail if above threshold.
          2. Stage 1: downgrade low-score movies to HD-720p + trigger search.
             (Deferred to the cross-service coordinator for the instance it owns,
             so the downgrade pass never runs twice on the shared mount.)
          3. Re-read free space.
          4. Stage 2: delete watched + grace-expired + already-720p if still tight
             (always deferred to the coordinator when it owns deletion).
        """
        instance = self._resolve_instance(instance)
        free_gb  = self._get_free_space_gb(instance)
        T, U     = self._space_targets(instance)

        if free_gb >= U:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {free_gb:.1f} GB free — "
                f"above {U:.0f} GB (free_space_limit {T:.0f} GB +headroom), no action needed."
            )
            return {"free_space_gb": free_gb, "action": "none"}

        self.logger.log_info(
            f"[SpacePressure] ⚠️ '{instance}': {free_gb:.1f} GB free — "
            f"below {U:.0f} GB pressure band (floor {T:.0f} GB). Starting pipeline."
        )

        # Stage 0: upgrade actively-watched movies when space is plentiful
        upgrade_stats   = self.run_active_watcher_upgrades(instance, free_gb)

        # Stage 1: downgrade low-score movies to HD-720p. When the cross-service space
        # coordinator owns reclamation, defer the downgrade to it for EVERY instance —
        # the coordinator now runs the downgrade pass for all Radarr instances on the
        # shared mount, so running it here too would double-process and double-log.
        if self._coordinator_owns_deletion():
            downgrade_stats = {"deferred_to_coordinator": True}
            self.logger.log_info(
                f"[SpacePressure] '{instance}': downgrades delegated to the space "
                f"coordinator (single shared-mount reclamation pass)."
            )
        else:
            downgrade_stats = self.run_downgrades(instance, free_gb)

        free_gb_after   = self._get_free_space_gb(instance)
        deletion_stats  = self.run_deletions(instance, free_gb_after)

        return {
            "free_space_before_gb": round(free_gb, 2),
            "free_space_after_gb":  round(free_gb_after, 2),
            "active_upgrades":      upgrade_stats,
            "downgrades":           downgrade_stats,
            "deletions":            deletion_stats,
        }

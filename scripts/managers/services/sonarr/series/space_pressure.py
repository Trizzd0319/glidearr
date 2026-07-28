"""
SonarrSpacePressureManager — Stage-1 TV downgrade under space pressure
=====================================================================
The Sonarr twin of ``RadarrSpacePressureManager.run_downgrades`` (Phase 3 of the
cross-service space plan). When free space is in the pressure band (free < U),
downgrade the lowest-watchability SERIES to HD-720p and REALIZE the reclaim, freeing
space BEFORE anything is deleted. Non-destructive and reversible (you keep the
show, just at a lower quality).

*arr never downgrades an existing file (a file above the new profile's cutoff →
"cutoff met" → every release rejected), so the old live path — PUT the series
profile + SeriesSearch and hope — reclaimed NOTHING (TV step-down space was
phantom). The live branch now mirrors the shipped movie/universe realize at
EPISODE-FILE granularity: after the profile PUT, each owned file above the target
resolution gets ONE interactive ``release?episodeId=`` search (the shared
``_pick_stepdown_release`` ladder picker, imported from the Radarr manager, with an
episode-sized fake floor); a pick exists → DELETE ``episodefile/{fid}`` then POST
the guid grab; grab error → post-delete blind ``EpisodeSearch`` fallback (chunked —
effective once the file is gone, since the cutoff-met blocker died with it); no
pick → file KEPT (a title is never traded for an empty indexer result), profile
stays lowered, counted ``no_release``, re-probes next run. An inline cap
(``tv_downgrade_realize_cap``, default 15) bounds the slow interactive searches per
pass; files over the cap re-qualify next run while still oversized (the planner
re-picks their series from file resolutions, not the profile).

Differences from the Radarr movie template:
  * SERIES-level — episode_files.parquet is per-episode, but watchability_score is
    per-series (broadcast onto every row by refresh_scores). We group by series_id,
    score once per series, change the SERIES qualityProfileId (PUT series/{id}),
    and stamp the plan on every episode row of that series. The REALIZE step then
    walks the series' owned episode files individually.
  * Reads the already-broadcast ``watchability_score`` column (Phase 2) — it does
    NOT recompute scores.
  * Adds a recently-AIRED guard (no Radarr analog): never downgrade a series with an
    episode aired within RECENT_AIR_DAYS.
  * U-target loop — downgrades just enough (lowest score first) to project free ≥ U,
    rather than downgrading the whole low-value catalog at once (avoids a re-grab
    storm on a large TV library). Falls back to "all candidates" if U is unreachable.

Gating lives in the orchestration wrapper (run_space_pressure_downgrades): it only
calls run_downgrades when free < U and ``tv_downgrade_enabled`` is set. dry_run only
changes whether the PUT/search actually fire — the plan is always stamped + persisted
so it is previewable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.ledger.decision_ledger import stamp
from scripts.managers.machine_learning.space.downgrade_planner import plan_series_downgrades
from scripts.managers.machine_learning.thresholds.registry import get_threshold
from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import (
    downgrade_regrab_cap, exhaustive_downgrade, space_targets,
)

# The step-down release picker is SHARED with the movie/universe realize paths —
# imported, not copied (same ladder semantics: rungs >= 720 strictly below the current
# resolution, 720 -> 1080 climb, median size in a rung, undersized fakes rejected).
_pick_stepdown_release = RadarrSpacePressureManager._pick_stepdown_release


class SonarrSpacePressureManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    HD_720P_PROFILE_NAME = "HD-720p"
    PRESSURE_FALLBACK_GB = 25.0  # last-resort floor only (free_space_limit unset AND total drive unreadable)
    RECENT_WATCH_DAYS    = 7      # don't downgrade a series watched within this window
    RECENT_AIR_DAYS      = 30     # don't downgrade a series with a very recently aired ep
    # 17, not 20: re-anchored with the whole delete family when Group D v2 replaced a
    # near-constant +12 bonus with a transcode-risk penalty and translated the score axis
    # (file-owning series median 21 -> 8). See machine_learning/thresholds/registry.py.
    DEFAULT_SCORE_CEILING = 17    # tv_space_pressure_score_ceiling default (0-100 scale)
    DEFAULT_RUNTIME_MIN  = 45.0   # fallback per-episode runtime when unknown
    KEEP_TAGS = frozenset({"keep_series", "keep_season", "keep_universe", "keep_forever"})
    DEFAULT_REALIZE_CAP  = 15     # tv_downgrade_realize_cap default — episode files searched+replaced
                                  # inline per pass (interactive searches are slow); rest defers
    SEARCH_CHUNK         = 100    # episodeIds per blind EpisodeSearch fallback command (Sonarr accepts a list)
    STEPDOWN_MIN_RELEASE_BYTES = 50 * 1024 * 1024   # episode fake/undersized floor for the shared picker
                                  # (movies use 300 MiB; a legit 720p episode can be far smaller)

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)

        # Resolve dry_run robustly — this manager PUTs to Sonarr, so never default
        # to False silently (the dry_run-propagation footgun). Walk kwargs → parent
        # → SonarrManager → Main; raise if unresolvable.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(manager, "dry_run", None)
        for _root_name in ("SonarrManager", "Main"):
            if _dry_run is not None:
                break
            if self.registry:
                try:
                    _root = self.registry.get("manager", _root_name)
                    _dry_run = getattr(_root, "dry_run", None) if _root else None
                except Exception:
                    _dry_run = None
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"SonarrManager, or Main. Refusing to initialize without an explicit "
                f"value to prevent accidental live profile changes."
            )
        self.dry_run = bool(_dry_run)

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__}")

    def prepare(self):
        pass

    def run(self):
        # No-op for the SonarrSeriesManager component-iteration; the downgrade pass
        # is driven by the orchestration (run_space_pressure_downgrades) AFTER
        # refresh_scores so it operates on fresh watchability scores.
        return {}

    # ── Helpers ───────────────────────────────────────────────────────────────────

    def get_free_space_gb(self, instance: str) -> float:
        """Free space (GiB) across this instance's disks, mount-deduped."""
        if self.sonarr_api is None:
            return float("inf")
        return self.sonarr_api.disk_free_gb(instance)

    def _space_targets(self, instance: str | None = None) -> tuple[float, float]:
        """(T, U) from the shared helper. When ``free_space_limit`` is unset the floor
        defaults to 25% of the total drive (mount-deduped via ``disk_total_gb``);
        PRESSURE_FALLBACK_GB is the last resort only when the total is also unreadable."""
        total_gb = None
        if instance is not None and self.sonarr_api is not None:
            try:
                total_gb = self.sonarr_api.disk_total_gb(instance)
            except Exception:
                total_gb = None
        alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, total_gb)
        return space_targets(self.config, fallback_gb=self.PRESSURE_FALLBACK_GB, total_gb=total_gb)

    def _score_ceiling(self) -> float:
        try:
            ceiling = float((self.config or {}).get("tv_space_pressure_score_ceiling", self.DEFAULT_SCORE_CEILING))
        except (TypeError, ValueError):
            ceiling = float(self.DEFAULT_SCORE_CEILING)
        return get_threshold("tv_delete_ceiling", self.config, ceiling, logger=getattr(self, "logger", None))

    def _realize_cap(self) -> int:
        """Inline per-pass budget of episode files REALIZED (interactive-searched and, when
        a smaller release exists, deleted + re-grabbed). Interactive searches are slow
        (seconds each), so the pass bounds them; files over the cap are counted
        ``deferred`` and re-qualify next run while still oversized (the planner re-picks
        their series from file resolutions). 0 = profile flips only, nothing realized."""
        try:
            v = int((self.config or {}).get("tv_downgrade_realize_cap", self.DEFAULT_REALIZE_CAP))
        except (TypeError, ValueError):
            return int(self.DEFAULT_REALIZE_CAP)
        return max(0, v)

    def _get_episode_files_manager(self):
        try:
            return self.registry.get("manager", "SonarrCacheEpisodeFilesManager")
        except Exception:
            return None

    @timeit("_fetch_hd720p_profile")
    def _fetch_hd720p_profile(self, instance: str) -> dict | None:
        """Fetch the HD-720p quality profile from Sonarr by exact name (Sonarr's
        endpoint is camelCase ``qualityProfile``; Radarr's is lowercase)."""
        if self.sonarr_api is None:
            return None
        profiles = self.sonarr_api._make_request(instance, "qualityProfile", fallback=[]) or []
        for p in profiles:
            if (p.get("name") or "").strip().lower() == self.HD_720P_PROFILE_NAME.lower():
                return p
        self.logger.log_warning(
            f"⚠️ [SpacePressure-TV] quality profile '{self.HD_720P_PROFILE_NAME}' not found in "
            f"'{instance}'. Available: {[p.get('name') for p in profiles]}"
        )
        return None

    @staticmethod
    def _profile_max_resolution(profile: dict) -> int:
        """Max allowed resolution of a Sonarr quality profile (0 if none)."""
        from scripts.managers.machine_learning.sizing.size_model import profile_max_quality
        res, _ = profile_max_quality(profile) if profile else (-1, None)
        return res if isinstance(res, (int, float)) and res > 0 else 0

    def _fetch_ranked_profiles(self, instance: str) -> list[dict]:
        """All Sonarr quality profiles sorted ascending by max allowed resolution — the
        ladder the step-down downgrade walks one resolution tier at a time."""
        if self.sonarr_api is None:
            return []
        raw = self.sonarr_api._make_request(instance, "qualityProfile", fallback=[]) or []
        return sorted(raw, key=self._profile_max_resolution)

    @staticmethod
    def _ensure_plan_cols(df) -> None:
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)

    def _stamp_plan(self, df, idx, action: str, reason: str, reclaim_gb) -> None:
        # Delegates the ledger write to the brain (ledger.decision_ledger.stamp).
        stamp(df, idx, action, reason, reclaim_gb)

    # NOTE: the _max_ts helper moved to the brain (space.downgrade_planner) in ML Step 7c.

    # ── Realize helpers (episode-file granularity) ────────────────────────────────

    @staticmethod
    def _iter_stepdown_file_rows(df, cand, target_res: int) -> list:
        """The candidate series' owned episode files ABOVE the step-down target
        resolution — ``(idx, fid, res, size_bytes, season, episode)`` tuples, largest
        file first (the inline cap spends its budget on the biggest reclaim), one tuple
        per physical file (a multi-episode file yields only its first backing row)."""
        out, seen = [], set()
        for idx in (cand.get("indices") or []):
            if idx not in df.index:
                continue
            fid = df.at[idx, "episode_file_id"] if "episode_file_id" in df.columns else None
            if fid is None or pd.isna(fid):
                continue
            fid = int(fid)
            if fid in seen:
                continue
            try:
                res = int(df.at[idx, "resolution"]) if "resolution" in df.columns else 0
            except (TypeError, ValueError):
                continue
            if res <= int(target_res):
                continue
            size = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
            size = float(size) if size is not None and pd.notna(size) else 0.0
            if size <= 0:
                continue
            sn = df.at[idx, "season_number"] if "season_number" in df.columns else None
            en = df.at[idx, "episode_number"] if "episode_number" in df.columns else None
            seen.add(fid)
            out.append((idx, fid, res, size, sn, en))
        out.sort(key=lambda t: -t[3])
        return out

    def _realize_stepdown_files(self, instance: str, df, ef, cand, target_res: int,
                                fid_rowcount: dict, *, budget: int,
                                fallback_eids: list, stats: dict,
                                exhaustive: bool = False,
                                free_base_gb: "float | None" = None,
                                target_u_gb: "float | None" = None) -> int:
        """Realize ONE candidate series' step-down at episode-file granularity
        (verify → delete → guid-grab; the series profile was already PUT down).

        Per file above ``target_res``: resolve the Sonarr episode id (the episode-files
        manager's cached ``_get_episode_id``), run ONE interactive ``release?episodeId=``
        search, and pick with the shared ladder picker. A pick → DELETE the episode file
        then POST the guid grab; a grab error/soft-reject → the file is already gone, so
        the episode id joins the blind ``EpisodeSearch`` fallback pool (effective
        post-delete: the cutoff-met blocker died with the file). No pick → file KEPT,
        counted ``no_release`` (profile stays lowered; re-probes next run). Multi-episode
        files are never realized (a single-episode replacement would orphan the
        siblings). ``budget`` is the shared inline cap; files over it count ``deferred``.
        Mutates ``stats`` and ``fallback_eids``; returns the remaining budget.

        ``exhaustive`` + ``free_base_gb`` + ``target_u_gb`` (all default off/None → byte-identical):
        stop realizing as soon as free space NET of the re-grabs queued this run
        (``free_base_gb + realized_reclaim_gb − inflight_regrab_gb``) reaches ``target_u_gb``.
        Without that subtraction the pass would keep shrinking against the phantom headroom the
        just-deleted files created, since the smaller replacements have not imported yet."""
        sid = cand["sid"]
        for idx, fid, res, size, sn, en in self._iter_stepdown_file_rows(df, cand, target_res):
            if exhaustive and free_base_gb is not None and target_u_gb is not None:
                _net = (float(free_base_gb) + stats["realized_reclaim_gb"]
                        - stats.get("inflight_regrab_gb", 0.0))
                if _net >= float(target_u_gb):
                    stats["stopped_at_target"] = stats.get("stopped_at_target", 0) + 1
                    continue
            label = (f"'{cand['title']}' S{int(sn):02d}E{int(en):02d}"
                     if (sn is not None and pd.notna(sn) and en is not None and pd.notna(en))
                     else f"'{cand['title']}' fid={fid}")
            if fid_rowcount.get(fid, 1) > 1:
                stats["skipped_multi_ep"] += 1
                self.logger.log_info(
                    f"  ⏸️ {label}: file backs {fid_rowcount[fid]} episodes — kept (a "
                    f"single-episode replacement would orphan the siblings).")
                continue
            if budget <= 0:
                stats["deferred"] += 1
                continue
            if sn is None or en is None or pd.isna(sn) or pd.isna(en):
                stats["failed"] += 1
                continue
            try:
                eid = ef._get_episode_id(instance, int(sid), int(sn), int(en))
            except Exception:
                eid = None
            if not eid:
                self.logger.log_warning(
                    f"  ⚠️ {label}: could not resolve the Sonarr episode id — file kept.")
                stats["failed"] += 1
                continue
            budget -= 1
            releases = self.sonarr_api._make_request(
                instance, f"release?episodeId={int(eid)}", fallback=None) or []
            pick = _pick_stepdown_release(releases, current_res=res,
                                          min_size_bytes=self.STEPDOWN_MIN_RELEASE_BYTES,
                                          allow_below_floor=exhaustive)
            if not pick:
                stats["no_release"] += 1
                self.logger.log_info(
                    f"  ⏸️ {label}: no smaller release available — file kept at {res}p "
                    f"(profile now {cand['target_name']}; re-probes next run).")
                continue
            try:
                self.sonarr_api._make_request(instance, f"episodefile/{fid}", method="DELETE")
            except Exception as e:
                stats["failed"] += 1
                self.logger.log_warning(f"  ⚠️ {label}: episode-file delete failed — file kept: {e}")
                continue
            # The file is gone from disk from here on: the reclaim is REAL regardless of
            # which grab path (guid or blind fallback) restores the smaller copy.
            stats["realized"] += 1
            stats["realized_reclaim_gb"] += size / (1024 ** 3)
            # …but the replacement is IN FLIGHT: book its projected size against the
            # free-space figure so the pass can't chase the temporary spike.
            stats["inflight_regrab_gb"] = (stats.get("inflight_regrab_gb", 0.0)
                                           + float(pick.get("size") or 0) / (1024 ** 3))
            if pick.get("stepped_below_floor"):
                stats["below_floor_picks"] = stats.get("below_floor_picks", 0) + 1
                self.logger.log_info(
                    f"  ⤵️ {label}: stepped BELOW 720 — no >=720 release exists for this title.")
            grabbed = False
            try:
                _res = self.sonarr_api._make_request(
                    instance, "release", method="POST", fallback=None,
                    payload={"guid": pick.get("guid"), "indexerId": pick.get("indexerId")})
                # _make_request returns None on a soft rejection (release no longer
                # grabbable / indexer down) WITHOUT raising — the file is already gone,
                # so a soft-reject must fall back too (mirrors legacy_regrab's check).
                grabbed = _res is not None
            except Exception:
                grabbed = False
            if not grabbed:
                fallback_eids.append(int(eid))
                stats["grab_fallback"] += 1
                continue
            _pick_gb = float(pick.get("size") or 0) / (1024 ** 3)
            self.logger.log_info(
                f"  📉 {label}: file deleted ({size / (1024 ** 3):.2f}GB @{res}p), grabbed "
                f"'{pick.get('title')}' ({_pick_gb:.2f}GB) — step-down realized.")
        return budget

    # ── Stage 1: downgrade to HD-720p ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_tv_downgrades")
    def run_downgrades(self, instance: str, free_space_gb: float) -> dict:
        """Downgrade the lowest-watchability series to HD-720p until projected free
        space reaches U. Series-level; stamps the plan on every episode row.
        ``free_space_gb`` is the current free space (the orchestration already
        verified free < U before calling)."""
        stats = {
            "candidates":        0,
            "downgraded":        0,
            "est_reclaim_gb":    0.0,
            "skipped_protected": 0,
            "skipped_high_score": 0,
            "skipped_recent":    0,
            "skipped_already":   0,
            "failed":            0,
            # ── realize accounting (live only; dry_run leaves these at 0) ──
            "realized":            0,     # episode files actually deleted + replaced
            "realized_reclaim_gb": 0.0,   # REAL GB freed (sum of deleted file sizes)
            "no_release":          0,     # no smaller release existed → file KEPT
            "grab_fallback":       0,     # guid grab failed → blind EpisodeSearch (file already gone)
            "deferred":            0,     # over the inline cap → next run
            "skipped_multi_ep":    0,     # file backs several episodes → never single-grabbed
            # ── exhaustive-mode accounting (0 on the legacy path) ──
            "inflight_regrab_gb":  0.0,   # projected size of the replacements queued THIS run
            "stopped_at_target":   0,     # items left untouched once free (net of in-flight) hit U
            "below_floor_picks":   0,     # stepped BELOW 720 — no >=720 release exists
        }

        ef = self._get_episode_files_manager()
        if ef is None:
            self.logger.log_warning("[SpacePressure-TV] episode_files manager unavailable — skipping downgrades")
            return stats
        df = ef.load(instance)
        if df.empty or "series_id" not in df.columns:
            return stats
        if "watchability_score" not in df.columns:
            self.logger.log_warning("[SpacePressure-TV] no watchability_score column — run refresh_scores first")
            return stats

        ranked_profiles = self._fetch_ranked_profiles(instance)
        if not ranked_profiles:
            self.logger.log_warning("[SpacePressure-TV] Could not fetch quality profiles — skipping downgrades")
            return stats
        # Series floor at the HD-720p resolution: they step DOWN toward it (4K → 1080p →
        # 720p) but never below.
        hd720p = self._fetch_hd720p_profile(instance)
        floor_resolution = (self._profile_max_resolution(hd720p) or 720) if hd720p is not None else 720

        self._ensure_plan_cols(df)
        # Clear any stale downgrade plan from a prior run so the ledger reflects THIS
        # run's decision (leave delete/acquire/upgrade plans untouched).
        _stale = df["planned_action"] == "downgrade"
        if _stale.any():
            df.loc[_stale, ["planned_action", "plan_reason", "plan_reclaim_gb"]] = None

        _, U = self._space_targets(instance)
        need_gb = max(0.0, U - float(free_space_gb))
        ceiling = self._score_ceiling()
        now = datetime.now(tz=timezone.utc)
        watch_cutoff = now - timedelta(days=self.RECENT_WATCH_DAYS)
        air_cutoff   = now - timedelta(days=self.RECENT_AIR_DAYS)

        # DECISION: the brain (space.downgrade_planner.plan_series_downgrades) steps the
        # lowest-watchability series DOWN the resolution ladder one tier at a time, spread
        # across the pool, until ~need_gb is reclaimed (no series crushed straight to 720p).
        # The service APPLIES each per-series target (PUT + SeriesSearch + stamp) below.
        # EXHAUSTIVE (space_exhaustive_downgrade, DEFAULT ON): plan EVERY series above the
        # 720p floor down to it — no score ceiling, no early stop at a partial need_gb —
        # because the episode delete pool now only accepts files already AT/BELOW that floor.
        _exhaustive = exhaustive_downgrade(self.config)
        candidates, _pstats = plan_series_downgrades(
            df, ranked_profiles,
            need_gb=need_gb,
            ceiling=ceiling,
            watch_cutoff=watch_cutoff,
            air_cutoff=air_cutoff,
            keep_tags=self.KEEP_TAGS,
            default_runtime_min=self.DEFAULT_RUNTIME_MIN,
            floor_resolution=floor_resolution,
            exhaustive=_exhaustive,
        )
        stats.update(_pstats)
        if not candidates:
            _why = ("every series is keep-tagged / hot-universe / recent / already at the "
                    f"{floor_resolution}p floor — the downgrade pool is EXHAUSTED, so deletion "
                    "is now the only lever left" if _exhaustive
                    else f"score<{ceiling:.0f}, not keep/hot-universe/recent/at-floor")
            self.logger.log_info(
                f"[SpacePressure-TV] '{instance}': {free_space_gb:.0f}GB free (<{U:.0f}GB) but no "
                f"downgrade candidates ({_why})."
            )
            return stats

        # Apply each per-series step-down target (the planner already spread to ~need_gb).
        # fallback_eids collects episodes whose guid grab failed AFTER their file was
        # deleted — a blind EpisodeSearch is effective for exactly those (the cutoff-met
        # blocker died with the file). realize_budget is the shared inline cap across all
        # candidate series this pass.
        fallback_eids: list[int] = []
        realize_budget = self._realize_cap()
        # BANDWIDTH GUARD: in exhaustive mode the TIGHTER of the two caps wins — the TV
        # inline cap (tv_downgrade_realize_cap, bounds slow interactive searches) and the
        # cross-service per-run re-grab cap (space_downgrade_max_regrabs_per_run). Files
        # over it keep their copies, count `deferred`, and re-qualify next run — and since
        # they are still above the floor they stay excluded from deletion.
        _regrab_cap = downgrade_regrab_cap(self.config) if _exhaustive else 0
        if _regrab_cap > 0:
            realize_budget = min(realize_budget, _regrab_cap)
        realize_budget_start = realize_budget   # for the summary table's cap description
        if _exhaustive:
            self.logger.log_info(
                f"[SpacePressure-TV] '{instance}': exhaustive step-down — every series above the "
                f"{floor_resolution}p floor is planned down to it "
                f"({_pstats.get('over_ceiling_included', 0)} admitted over the score ceiling); "
                f"stopping at {U:.0f}GB free NET of in-flight re-grabs; realize budget "
                f"{realize_budget}/run."
            )
        # How many episode rows each file backs — a multi-episode file is never replaced
        # by a single-episode grab (it would orphan the siblings).
        fid_rowcount: dict = {}
        if "episode_file_id" in df.columns:
            try:
                fid_rowcount = {
                    int(k): int(v)
                    for k, v in df["episode_file_id"].dropna().value_counts().items()
                }
            except Exception:
                fid_rowcount = {}
        plan_changed = changed = False
        reclaimed = 0.0

        for c in candidates:
            # ── IN-FLIGHT ACCOUNTING (exhaustive only) ────────────────────────────
            # dry_run has no picks to size, so it models the planner's projected reclaim
            # (= deleted size − replacement size, i.e. already net); the live pass uses the
            # realized/in-flight split the realize helper maintains from the actual picks.
            if _exhaustive:
                _net_free = float(free_space_gb) + (
                    reclaimed if self.dry_run
                    else stats["realized_reclaim_gb"] - stats["inflight_regrab_gb"])
                if _net_free >= U:
                    stats["stopped_at_target"] += 1
                    continue
            reason = f"{c['reason']} → {c['target_name']}"
            # Stamp the series-level downgrade on ONE representative episode row with
            # the WHOLE-series reclaim. The plan ledger (plan_summary.py) counts rows
            # and sums plan_reclaim_gb per planned_action, so stamping every episode
            # row of the series would inflate BOTH the count (~n_eps) and the GB freed.
            self._stamp_plan(df, c["indices"][0], "downgrade", reason, c["reclaim"])
            plan_changed = True
            reclaimed += c["reclaim"]
            stats["est_reclaim_gb"] = round(reclaimed, 1)

            if self.dry_run:
                # debug: stamped into the decision ledger above → rendered in the
                # end-of-run "Change plan" grid; live log keeps the summary table.
                self.logger.log_debug(
                    f"  📉 [dry_run] Would step down '{c['title']}' ({c['n_eps']} ep, "
                    f"{c['cur_gib']:.1f}GB → {c['target_name']}, ~{c['reclaim']:.1f}GB reclaim) — {c['reason']}"
                )
                stats["downgraded"] += 1
                continue

            try:
                payload = self.sonarr_api._make_request(instance, f"series/{c['sid']}", fallback=None)
                if not payload or not isinstance(payload, dict):
                    self.logger.log_warning(f"  ⚠️ Could not fetch series payload for '{c['title']}' (id={c['sid']})")
                    stats["failed"] += 1
                    continue
                payload["qualityProfileId"] = c["target_id"]
                self.sonarr_api._make_request(instance, f"series/{c['sid']}", method="PUT", payload=payload)
                changed = True
                stats["downgraded"] += 1
                self.logger.log_info(
                    f"  📉 Stepped down '{c['title']}' ({c['n_eps']} ep, {c['cur_gib']:.1f}GB → "
                    f"{c['target_name']}, ~{c['reclaim']:.1f}GB projected) — {c['reason']}"
                )
                # REALIZE: the profile flip alone reclaims nothing (Sonarr will not
                # replace a file that already exceeds the new cutoff). Walk this
                # series' oversized files: verify a smaller release exists → delete →
                # grab it. Bounded by the shared inline budget.
                # Target resolution comes from the planner's chosen profile (the
                # candidate carries the profile dict, not a bare resolution); fall
                # back to the pass's 720p floor if the profile can't be read.
                _t_res = self._profile_max_resolution(c.get("target_profile")) or floor_resolution
                realize_budget = self._realize_stepdown_files(
                    instance, df, ef, c, int(_t_res),
                    fid_rowcount, budget=realize_budget,
                    fallback_eids=fallback_eids, stats=stats,
                    exhaustive=_exhaustive,
                    free_base_gb=float(free_space_gb) if _exhaustive else None,
                    target_u_gb=U if _exhaustive else None,
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Downgrade failed for '{c['title']}' (id={c['sid']}): {e}")
                stats["failed"] += 1

        # ── blind-search fallback pool (chunked) ──────────────────────────────────
        # ONLY for episodes whose guid grab failed after their file was deleted. The
        # old blanket per-series SeriesSearch is gone: with the file still present it
        # was rejected as cutoff-met (the phantom-reclaim bug), and once the file IS
        # deleted the targeted grab above already handles the replacement.
        for i in range(0, len(fallback_eids), self.SEARCH_CHUNK):
            _batch = fallback_eids[i:i + self.SEARCH_CHUNK]
            try:
                self.sonarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "EpisodeSearch", "episodeIds": _batch},
                )
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Blind EpisodeSearch fallback failed for {len(_batch)} episode(s): {e}")
        if fallback_eids:
            self.logger.log_info(
                f"  🔍 Blind EpisodeSearch fallback for {len(fallback_eids)} episode(s) "
                f"whose guid grab did not take (files already removed).")

        if plan_changed or changed:
            ef.save(instance, df)

        prefix = "[dry_run] " if self.dry_run else ""
        target_status = "met" if _pstats.get("target_met") else "NOT met"
        _rows = [
            ["stepped down",     stats["downgraded"]],
            ["projected GB",     stats["est_reclaim_gb"]],
            ["files realized",   stats["realized"]],
            ["realized GB",      round(stats["realized_reclaim_gb"], 2)],
            ["no smaller release", stats["no_release"]],
            ["grab fallback",    stats["grab_fallback"]],
            ["deferred (cap)",   stats["deferred"]],
            ["multi-episode file", stats["skipped_multi_ep"]],
            ["candidates",       stats["candidates"]],
            ["score over ceil",  stats["skipped_high_score"]],
            ["keep-tagged",      stats["skipped_protected"]],
            ["hot-universe",     stats.get("skipped_universe", 0)],
            ["recent",           stats["skipped_recent"]],
            ["at/below floor",   stats["skipped_already"]],
            ["failed",           stats["failed"]],
        ]
        _descs = [
            "series whose profile was stepped down a tier",
            "planner's projected reclaim once every oversized file is replaced",
            "episode files ACTUALLY deleted + re-grabbed smaller this pass",
            "REAL space freed now (sum of the deleted files) — the rest lands as files replace",
            "files KEPT: no release below the current resolution — re-probes next run",
            "grab did not take; file already removed, blind EpisodeSearch queued",
            f"files over the inline cap ({realize_budget_start}/pass) — next run picks them up",
            "files backing several episodes — never single-grabbed (would orphan siblings)",
            "series the planner picked as candidates",
            "series skipped for watchability score over the ceiling",
            "series skipped because keep-tagged",
            "series skipped — hot franchise/universe credit holds them at tier",
            "series skipped for a recent watch or air date",
            "series already at or below the 720p floor",
            "series whose PUT/search call errored",
        ]
        if _exhaustive:
            _rows += [
                ["over-ceiling included", stats.get("over_ceiling_included", 0)],
                ["stepped below 720",     stats["below_floor_picks"]],
                ["in-flight re-grab GB",  round(stats["inflight_regrab_gb"], 2)],
                ["target reached",        stats["stopped_at_target"]],
            ]
            _descs += [
                "series admitted despite a score over the delete ceiling — EVERYTHING shrinks "
                "before anything is deleted",
                "no >=720 release exists for the episode, so the pass fell below the 720p floor",
                "projected size of the smaller replacements queued THIS run — subtracted from free "
                "space so the pass never downgrades against phantom headroom",
                "items left untouched: free space NET of the in-flight re-grabs reached the band top",
            ]
        self.logger.log_table(
            ["Outcome", "Count"], _rows,
            title=f"[SpacePressure-TV] {prefix}'{instance}' "
                  f"(free {free_space_gb:.0f}GB, target {U:.0f}GB, need ~{need_gb:.0f}GB, "
                  f"target {target_status}{'; exhaustive' if _exhaustive else ''})",
            caption="Per-pass result of the TV space-pressure step-down: how many low-watchability "
                    "series were downgraded toward HD-720p, the space reclaimed, and what was skipped "
                    "and why."
                    + (" EXHAUSTIVE: every series above the 720p floor is planned down to it "
                       "(deletion is the true last resort); the pass stops once free space net of "
                       f"the in-flight re-grabs reaches {U:.0f}GB, and realizes at most "
                       f"{realize_budget_start} file(s)/run." if _exhaustive else ""),
            descriptions=_descs,
        )
        return stats

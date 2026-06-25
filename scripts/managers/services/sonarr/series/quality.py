from datetime import datetime, timedelta, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    resolution_cap_for_likelihood,
    watch_likelihood,
)
from scripts.managers.machine_learning.space.upgrade_planner import (
    active_series_candidates,
    aggregate_series_signals,
    decide_series_upgrade,
    series_fully_downloaded,
)
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.registry import RegistryHelper
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import space_targets


def _profile_max_resolution(profile: dict | None) -> int:
    """Highest resolution among a profile's *allowed* quality items (incl. nested groups)."""
    best = 0
    for item in ((profile or {}).get("items") or []):
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


def _is_anime_profile(profile: dict) -> bool:
    """A quality profile dedicated to anime — named like ``[Anime] Remux-1080p``."""
    return (profile.get("name") or "").strip().lower().startswith("[anime]")


_CODEC_VARIANT_SUFFIXES = (" (h264)", " (hevc)", " (hevc-dv)", " (av1)")


def _codec_variant(profile: dict) -> bool:
    """A device-audience codec variant, e.g. ``WEB-2160p (Combined) (AV1)``. NEVER an auto-upgrade
    target: the RESOLUTION ladder picks the agnostic tier; the device→codec stage assigns the codec."""
    return (profile.get("name") or "").strip().lower().endswith(_CODEC_VARIANT_SUFFIXES)


def select_upgrade_targets(profiles: list[dict]) -> tuple[dict | None, dict | None]:
    """Highest-resolution AGNOSTIC upgrade target *within each seriesType family* — codec variants
    excluded (they're device picks, not ladder rungs, so 'best by resolution' must never land on one).

    Returns ``(best_standard, best_anime)``; each family falls back to the other so a target exists
    whenever any agnostic profile does. (The per-series likelihood CAP is applied separately via
    ``capped_target`` — this is just the family ceiling.)"""
    pool = [p for p in profiles if not _codec_variant(p)]

    def _best(sub: list[dict]) -> dict | None:
        ranked = sorted(sub, key=_profile_max_resolution)
        return ranked[-1] if ranked else None

    best_standard = _best([p for p in pool if not _is_anime_profile(p)])
    best_anime    = _best([p for p in pool if _is_anime_profile(p)])
    return (best_standard or best_anime), (best_anime or best_standard)


def capped_target(profiles: list[dict], *, is_anime: bool, max_res: int) -> dict | None:
    """The agnostic upgrade target for a series given its likelihood resolution CAP ``max_res``:
    the highest-resolution agnostic profile in its family whose max resolution ≤ the cap. So a
    single-watch series caps at 1080p; a regularly-rewatched / universe-elevated one reaches 2160p.
    Codec variants excluded. Falls back to the lowest in-family profile when none sit at/under the cap."""
    agn = [p for p in profiles if not _codec_variant(p)]
    fam = [p for p in agn if _is_anime_profile(p) == is_anime] or agn
    if not fam:
        return None
    le = [p for p in fam if _profile_max_resolution(p) <= max_res]
    return max(le, key=_profile_max_resolution) if le else min(fam, key=_profile_max_resolution)


class SonarrSeriesQualityManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    # ── Active-watcher upgrade constants ─────────────────────────────────────────
    # Upgrades gate on the space band top U from space_targets (free_space_limit, or
    # 25% of the total drive when unset) — no standalone hardcoded GB floor. The
    # last-resort fallback (when free_space_limit AND the total drive are both unknown)
    # is the shared PRESSURE_FALLBACK_GB.
    ACTIVE_WATCH_DAYS    = 30      # episodes watched within this window = active
    # Kids-only certifications — skip upgrading unless adults also watch
    KIDS_CERTS           = {"g", "pg", "tv-g", "tv-y", "tv-y7"}
    # Tag labels that freeze quality — series tagged with any of these are never upgraded
    FREEZE_QUALITY_TAGS  = {"keep_quality", "keep-quality", "keepquality"}

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache support
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.orchestration = kwargs.get("orchestration") or getattr(manager, "orchestration", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("prepare")
    def prepare(self):
        self.logger.log_debug("🔧 SonarrSeriesQualityManager preparation complete (no subcomponents declared).")

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        self.logger.log_info("🚀 Running SonarrSeriesQualityManager components...")

    # ── Active-watcher upgrades ──────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_free_space_gb")
    def get_free_space_gb(self, instance: str) -> float:
        """Free space (GiB) across this instance's disks, deduped by mount."""
        return self.sonarr_api.disk_free_gb(instance)

    @LoggerManager().log_function_entry
    @timeit("run_active_watcher_upgrades")
    def run_active_watcher_upgrades(self, instance: str) -> dict:
        """
        Upgrade the quality profile of series that the household is actively
        watching, provided free space is above the space band top U (from
        space_targets: free_space_limit + headroom, or 25% of the total drive when
        unset).

        “Actively watching” = an episode was watched within ACTIVE_WATCH_DAYS.
        “Non-kids” = series not exclusively carrying G/PG certifications.

        Signal sources:
          - episode_files Parquet (series_id, last_watched_at, keep_policy,
            certification) aggregated per series
          - Sonarr qualityprofile list to determine the best available profile
          - Sonarr GET /series/{id} for current profile before PUT

        Mirrors the Radarr SpacePressure active-watcher upgrade logic but
        operates at the series level across Sonarr’s episode_files Parquet.
        """
        instance = self.instance_manager.resolve_instance(instance) \
            if self.instance_manager else instance

        stats = {
            "checked": 0, "upgraded": 0, "already_best": 0,
            "skipped_kids": 0, "skipped_not_active": 0,
            "skipped_keep": 0, "skipped_fully_downloaded": 0,
            "skipped_quality_frozen": 0, "skipped_no_episodes": 0, "failed": 0,
        }

        # ── Space check (upgrade only above the band top U) ──────────────────────
        # U = free_space_limit + headroom, or 25% of the total drive when unset
        # (mount-deduped via disk_total_gb) — never a hardcoded GB floor.
        free_gb = self.get_free_space_gb(instance)
        try:
            _total_gb = self.sonarr_api.disk_total_gb(instance) if self.sonarr_api else None
        except Exception:
            _total_gb = None
        alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, _total_gb)
        _, upgrade_floor = space_targets(self.config, total_gb=_total_gb)
        if free_gb < upgrade_floor:
            self.logger.log_info(
                f"[Quality] Active-watcher upgrades skipped for '{instance}': "
                f"{free_gb:.1f} GB free < {upgrade_floor:.0f} GB threshold."
            )
            return stats

        # ── Load episode_files Parquet ────────────────────────────────────────────
        try:
            ep_mgr = self.registry.get("manager", "SonarrCacheEpisodeFilesManager")
            df = ep_mgr.load(instance) if ep_mgr else None
        except Exception as e:
            self.logger.log_warning(f"[Quality] Could not load episode_files Parquet: {e}")
            return stats

        if df is None or df.empty:
            return stats

        import pandas as pd

        # ── Aggregate per series: latest watch time + certifications + keep_policy ──
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.ACTIVE_WATCH_DAYS)

        if "last_watched_at" not in df.columns or "series_id" not in df.columns:
            return stats

        # Per-series signal aggregation (brain: space.upgrade_planner.aggregate_series_signals).
        series_data = aggregate_series_signals(df)

        # ── Best quality profile ─────────────────────────────────────────────────
        try:
            raw_profiles = self.sonarr_api._make_request(
                instance, "qualityprofile", fallback=[]
            ) or []
        except Exception as e:
            self.logger.log_warning(f"[Quality] Could not fetch quality profiles: {e}")
            return stats

        if not raw_profiles:
            return stats

        # Pick the highest-resolution upgrade target *within each seriesType family*
        # (see select_upgrade_targets): anime series upgrade to the anime profile,
        # standard/daily series to the best non-anime profile. A global argmax would
        # tie-break onto whichever 2160p profile sorts last — the anime one — and land
        # every actively-watched series on it regardless of type.
        best_standard, best_anime = select_upgrade_targets(raw_profiles)
        if best_standard is None:
            return stats

        # --- Anticipated-space model (depends only on the target profile + df) ---
        # Resolve the target MiB/min exactly as JIT _est_gb does: measured per-quality
        # average → JIT fallback table → 25.0 default. The df-derived tables are
        # profile-independent (compute once); the per-family target name resolves below.
        _aw_measured = {}
        if ep_mgr is not None and df is not None and not df.empty:
            try:
                _aw_measured = ep_mgr._measured_mb_per_min(df) or {}
            except Exception:
                _aw_measured = {}
        _aw_fallback = getattr(ep_mgr, "JIT_FALLBACK_MB_PER_MIN", {}) if ep_mgr else {}

        def _target_mbpm(target: dict | None) -> float:
            try:
                _, tname = ep_mgr._profile_max_quality(target) if (ep_mgr and target) else (-1, None)
            except Exception:
                tname = None
            return _aw_measured.get(tname) or _aw_fallback.get(tname) or 25.0

        _aw_mbpm_standard = _target_mbpm(best_standard)
        _aw_mbpm_anime    = _target_mbpm(best_anime)

        # ── df-based guards (brain): keep_series/keep_season, recently-active, kids-only.
        # Run BEFORE any per-series fetch so a skipped series costs no API call.
        active, _df_stats = active_series_candidates(
            series_data, cutoff=cutoff, kids_certs=self.KIDS_CERTS,
        )
        stats["checked"]            = _df_stats["checked"]
        stats["skipped_keep"]       = _df_stats["skipped_keep"]
        stats["skipped_not_active"] = _df_stats["skipped_not_active"]
        stats["skipped_kids"]       = _df_stats["skipped_kids"]

        # ── Evaluate and upgrade each active series ──────────────────────────────────
        for sid, info in active:
            title         = info["title"]
            latest        = info["latest_watch"]
            watched_eps   = info.get("watched_eps", 0)
            household_eps = info.get("household_eps", 0)

            # Fetch current series record — needed for statistics, tags, current profile.
            try:
                series = self.manager.retrieval.fetch.get_series_by_id(sid, instance)
            except Exception:
                series = None
            if not series:
                stats["failed"] += 1
                continue

            # Fully-downloaded guard FIRST (record-only) — so a fully-downloaded series
            # never pays for the (API-touching) tag fetch below, matching pre-extraction.
            if series_fully_downloaded(series):
                _sb = series.get("statistics") or {}
                self.logger.log_debug(
                    f"  ⏭️  '{title}' fully downloaded "
                    f"({_sb.get('episodeFileCount', 0)}/{_sb.get('episodeCount', 0)} eps) — JIT handles upgrades"
                )
                stats["skipped_fully_downloaded"] += 1
                continue

            # Per-family target, CAPPED by the recalibrated likelihood curve. A single watch caps at
            # 1080p; only a regularly-rewatched (or hot-universe-credited) series reaches 2160p. The
            # agnostic tier is chosen here — the device→codec stage assigns the codec variant, never
            # this upgrade pass (so 'best by resolution' can't land on a `(AV1)` profile any more).
            is_anime_series = (series.get("seriesType") or "").strip().lower() == "anime"
            cur_profile = next((p for p in raw_profiles if p.get("id") == series.get("qualityProfileId")), None)
            cur_res     = _profile_max_resolution(cur_profile) if cur_profile else 0
            likelihood  = watch_likelihood({
                "watch_count":        info.get("watch_count", 0),
                "watchability_score": info.get("watchability_score", 0),
                "universe_credit":    info.get("universe_credit", 0),
            }, config=self.config)
            cap_res = resolution_cap_for_likelihood(likelihood, config=self.config)
            best    = capped_target(raw_profiles, is_anime=is_anime_series, max_res=cap_res)
            if best is None:
                stats["already_best"] += 1
                continue
            best_id   = best["id"]
            best_name = best.get("name", str(best_id))
            best_res  = _profile_max_resolution(best)
            _aw_mbpm  = _target_mbpm(best)
            # Upgrade-only: a likelihood-capped target at/under the current tier → leave the series be
            # (this pass never downgrades — that's the space-pressure path).
            if best_res <= cur_res:
                stats["already_best"] += 1
                continue

            # Resolve the series' tag labels (cache → API) for the freeze-tag guard —
            # only for series that survived the fully-downloaded skip.
            tag_labels: set[str] = set()
            try:
                tag_map: dict[int, str] = {}
                if self.global_cache:
                    raw_tags = self.global_cache.get(f"sonarr.tags.{instance}") or []
                    tag_map  = {t["id"]: t["label"].lower() for t in raw_tags if t.get("id")}
                if not tag_map:
                    raw_tags = self.sonarr_api._make_request(instance, "tag", fallback=[]) or []
                    tag_map  = {t["id"]: t["label"].lower() for t in raw_tags if t.get("id")}
                tag_labels = {tag_map.get(tid, "") for tid in (series.get("tags") or [])}
            except Exception:
                pass

            # Record-based decision (brain): quality-freeze / already-best guards + the
            # upgrade numbers (fully-downloaded already handled above).
            verdict = decide_series_upgrade(
                series, tag_labels, best_id=best_id,
                freeze_tags=self.FREEZE_QUALITY_TAGS, mbpm=_aw_mbpm,
            )
            skip = verdict["skip"]
            if skip == "quality_frozen":
                self.logger.log_debug(f"  ❄️  '{title}' has quality-freeze tag — skipping upgrade")
                stats["skipped_quality_frozen"] += 1
                continue
            if skip == "already_best":
                stats["already_best"] += 1
                continue
            if skip == "no_episodes":
                stats["skipped_no_episodes"] += 1
                continue

            # ── Upgrade: format the "why this qualifies" detail + apply ──────────────
            ep_total      = verdict["ep_total"]
            ep_file_count = verdict["ep_file_count"]
            remaining     = verdict["remaining"]
            est_gb        = verdict["est_gb"]

            cur_profile_id   = series.get("qualityProfileId")
            cur_profile      = next((p for p in raw_profiles if p.get("id") == cur_profile_id), None)
            cur_profile_name = (cur_profile or {}).get("name", str(cur_profile_id))
            cur_res          = _profile_max_resolution(cur_profile) if cur_profile else 0
            cur_res_s        = f"≤{cur_res}p"  if cur_res  else "?"
            best_res_s       = f"≤{best_res}p" if best_res else "?"

            try:
                days_ago = (pd.Timestamp.now(tz="UTC") - latest).days
                if pd.isna(days_ago):        # NaT latest -> nan days; don't render "nand"
                    days_ago = None
            except Exception:
                days_ago = None
            recency = (
                f"watched {days_ago}d ago (≤{self.ACTIVE_WATCH_DAYS}d active window)"
                if days_ago is not None else f"last watched {str(latest)[:10]}"
            )

            why_bits = [recency, f"likelihood {likelihood:.0f} → cap ≤{cap_res}p"]
            if watched_eps:
                hh = f", {household_eps} by full household" if household_eps else ""
                why_bits.append(f"{watched_eps} ep watched{hh}")
            why_bits.append(
                f"{ep_file_count}/{ep_total} ep on disk "
                f"(~{est_gb:.1f} GB to grab {remaining} remaining at {best_res_s})"
            )

            detail = (
                f"{cur_profile_name} ({cur_res_s}) → {best_name} ({best_res_s}) "
                f"| why: actively watched — " + " · ".join(why_bits)
            )

            if self.dry_run:
                self.logger.log_info(f"  [dry_run] Would upgrade '{title}': {detail}")
                stats["upgraded"] += 1
                continue

            try:
                series["qualityProfileId"] = best_id
                self.sonarr_api._make_request(
                    instance, f"series/{sid}", method="PUT", payload=series
                )
                self.logger.log_info(f"  📈 Upgraded '{title}': {detail}")
                stats["upgraded"] += 1
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Upgrade failed for '{title}': {e}")
                stats["failed"] += 1

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["upgraded",         stats["upgraded"]],
                ["already best",     stats["already_best"]],
                ["not active",       stats["skipped_not_active"]],
                ["kids-only",        stats["skipped_kids"]],
                ["keep-tagged",      stats["skipped_keep"]],
                ["fully downloaded", stats["skipped_fully_downloaded"]],
                ["quality-frozen",   stats["skipped_quality_frozen"]],
                ["failed",           stats["failed"]],
            ],
            title=f"[Quality] {prefix}Active-watcher upgrades - '{instance}' ({free_gb:.1f} GB free)",
            caption="Outcome of the active-watcher quality-profile upgrade pass for this instance.",
            descriptions=[
                "series whose profile was raised to the target",
                "series already on the best profile, left as-is",
                "series with no watch inside the active window",
                "series skipped as kids-only certification",
                "series skipped for a keep_series/keep_season tag",
                "fully-downloaded series left for JIT to upgrade",
                "series skipped for a quality-freeze tag",
                "series whose fetch or upgrade PUT errored",
            ],
        )
        return stats


    @LoggerManager().log_function_entry
    @timeit("get_series_profile_id")
    def get_series_profile_id(self, instance: str, series_id: int) -> int | None:
        resolved_instance = self.instance_manager.resolve_instance(instance)
        series_data = self._get_series_data(resolved_instance, series_id)
        return series_data.get("qualityProfileId") if series_data else None

    @LoggerManager().log_function_entry
    @timeit("update_series_profile")
    def update_series_profile(self, instance: str, series_id: int, profile_id: int) -> bool:
        resolved_instance = self.instance_manager.resolve_instance(instance)
        series_data = self._get_series_data(resolved_instance, series_id)
        if not series_data:
            self.logger.log_warning(f"⚠️ Failed to fetch series data for update in {resolved_instance}.")
            return False

        series_data["qualityProfileId"] = profile_id
        response = self.sonarr_api._make_request(resolved_instance, f"series/{series_id}", method="PUT", payload=series_data)

        if response:
            self.logger.log_info(f"✅ Updated quality profile for series {series_id} in {resolved_instance} to {profile_id}.")
        else:
            self.logger.log_warning(f"❌ Failed to update quality profile for series {series_id} in {resolved_instance}.")
        return bool(response)

    @LoggerManager().log_function_entry
    @timeit("assign_default_profile_if_missing")
    def assign_default_profile_if_missing(self, series_data: dict, instance: str) -> dict:
        resolved_instance = self.instance_manager.resolve_instance(instance)
        if not series_data.get("qualityProfileId"):
            default_id = self.get_default_quality_profile(resolved_instance)
            self.logger.log_info(f"⚠️ No profile found. Assigning default: {default_id}")
            series_data["qualityProfileId"] = default_id
        return series_data

    @LoggerManager().log_function_entry
    @timeit("get_default_quality_profile")
    def get_default_quality_profile(self, instance: str) -> int:
        resolved_instance = self.instance_manager.resolve_instance(instance)
        profiles = []

        if self.sonarr_cache:
            profiles = self.sonarr_cache.quality.get_profiles(resolved_instance)
        if not profiles:
            profiles = self.sonarr_api._make_request(resolved_instance, "qualityProfile") or []

        if not profiles:
            self.logger.log_warning(f"⚠️ No quality profiles found for {resolved_instance}.")
            return 1

        default_id = profiles[0].get("id", 1)
        self.logger.log_info(f"✅ Default profile ID for {resolved_instance} is {default_id}")
        return default_id

    @LoggerManager().log_function_entry
    @timeit("_get_series_data")
    def _get_series_data(self, instance: str, series_id: int) -> dict | None:
        return self.manager.retrieval.fetch.get_series_by_id(series_id, instance)

    @LoggerManager().log_function_entry
    @timeit("batch_update_profiles")
    def batch_update_profiles(self, instance: str, updates: list[tuple[int, int]]) -> dict:
        resolved_instance = self.instance_manager.resolve_instance(instance)
        results = {}

        for series_id, profile_id in updates:
            success = self.update_series_profile(resolved_instance, series_id, profile_id)
            results[series_id] = "✅" if success else "❌"

        self.logger.log_info(f"📊 Batch quality profile update results for {resolved_instance}: {results}")
        return results

    @LoggerManager().log_function_entry
    @timeit("bulk_assign_defaults_if_missing")
    def bulk_assign_defaults_if_missing(self, instance: str, series_list: list[dict]) -> list[dict]:
        resolved_instance = self.instance_manager.resolve_instance(instance)
        updated = []

        for series_data in series_list:
            original_id = series_data.get("qualityProfileId")
            updated_data = self.assign_default_profile_if_missing(series_data, resolved_instance)
            if updated_data.get("qualityProfileId") != original_id:
                self.logger.log_info(f"🔄 Assigned default profile to series {series_data.get('title', series_data.get('id'))}")
            updated.append(updated_data)

        return updated

    @LoggerManager().log_function_entry
    @timeit("refresh_all_series_profiles")
    def refresh_all_series_profiles(self, instance: str, use_default_if_missing: bool = True):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        all_series = self.sonarr_api.get_series(resolved_instance)
        summary = {"updated": 0, "skipped": 0, "defaulted": 0}

        for series in all_series:
            sid = series["id"]
            title = series.get("title", sid)
            original_pid = series.get("qualityProfileId")

            if not original_pid and use_default_if_missing:
                series = self.assign_default_profile_if_missing(series, resolved_instance)
                summary["defaulted"] += 1
                self.logger.log_info(f"🧩 Assigned default profile for '{title}'")

            updated = self.update_series_profile(resolved_instance, sid, series["qualityProfileId"])
            if updated:
                summary["updated"] += 1
            else:
                summary["skipped"] += 1

        self.logger.log_info(f"📊 Profile refresh summary for {resolved_instance}: {summary}")

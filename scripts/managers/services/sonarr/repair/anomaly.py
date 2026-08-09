from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairAnomalyManager(BaseManager, ComponentManagerMixin):
    """
    Handles anomaly detection and logging for inconsistencies found
    across Sonarr's data sources (e.g., mismatched metadata, orphaned episodes).
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrRepair"
        class_name = self.__class__.__name__

        self.manager = kwargs.get("manager")
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.global_cache = kwargs.get("global_cache") or getattr(self.manager, "global_cache", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    def _series_cache(self):
        """The letter-bucketed series cache (``sonarr_cache.series``), or None.

        Every full-library read through this manager is memoised in-process (a
        per-bucket memo plus an id/title/tvdb index, both invalidated by the same
        write hooks), so scanning the whole library costs one gunzip pass per
        bucket PER RUN — not per call. Reading the library in full is therefore
        the cheap option here, not the expensive one.
        """
        return getattr(self.sonarr_cache, "series", None)

    @LoggerManager().log_function_entry
    @timeit("scan_for_metadata_anomalies")
    def scan_for_metadata_anomalies(self):
        """
        Compare cached vs. live series to identify inconsistencies.

        Compares by SERIES ID and reports by title: ids survive a rename, titles
        do not, and a title-keyed diff reports every renamed series as both
        missing-in-cache and missing-in-live. Titles are carried alongside purely
        so the report is readable.
        """
        self.logger.log_info("🔍 Scanning for metadata anomalies...")
        anomalies = []

        series_cache = self._series_cache()
        if series_cache is None:
            self.logger.log_warning(
                "⚠️ Metadata-anomaly scan skipped: sonarr_cache.series is unavailable, so "
                "there is nothing to compare the live library against."
            )
            return anomalies

        all_instances = self.sonarr_api.get_all_sonarr_apis()
        for instance_name, client in all_instances.items():
            try:
                live = client.get_series()
                live_titles_by_id = {
                    str(getattr(s, "id", None)): getattr(s, "title", None)
                    for s in live
                    if getattr(s, "id", None) is not None
                }

                # ABSENT vs EMPTY. No bucket files at all means the cache has never
                # been written for this instance -- which is NOT the same as a
                # library of zero series. This previously read a
                # "sonarr::<instance>::series" key that nothing anywhere writes
                # (CacheKeyPaths is slash-separated, and the series cache is
                # letter-bucketed rather than flat), so the lookup always missed and
                # every live series was reported missing-in-cache, in one log line.
                if not series_cache.list_cached_letters(instance_name):
                    self.logger.log_warning(
                        f"⚠️ Skipping metadata-anomaly scan for '{instance_name}': the series "
                        f"cache has no letter buckets on disk yet. That is an unwritten "
                        f"cache, not an empty library -- comparing would report all "
                        f"{len(live_titles_by_id)} live series as missing."
                    )
                    continue

                cached_ids = series_cache.get_all_series_ids(instance_name)
                live_ids   = set(live_titles_by_id)

                missing_in_cache = live_ids - cached_ids
                missing_in_live  = cached_ids - live_ids

                def _titles(ids, from_cache: bool):
                    out = []
                    for sid in sorted(ids):
                        if from_cache:
                            rec = series_cache.get_cached_series_by_id(instance_name, sid)
                            out.append((rec or {}).get("title") or f"id={sid}")
                        else:
                            out.append(live_titles_by_id.get(sid) or f"id={sid}")
                    return out

                def _preview(names, cap=10):
                    head = ", ".join(names[:cap])
                    return head + (f" (+{len(names) - cap} more)" if len(names) > cap else "")

                if missing_in_cache:
                    names = _titles(missing_in_cache, from_cache=False)
                    self.logger.log_warning(
                        f"⚠️ [{instance_name}] {len(names)} series in Sonarr but not in cache: "
                        f"{_preview(names)}"
                    )
                    anomalies.append(("missing_in_cache", instance_name,
                                      sorted(missing_in_cache), names))

                if missing_in_live:
                    names = _titles(missing_in_live, from_cache=True)
                    self.logger.log_warning(
                        f"⚠️ [{instance_name}] {len(names)} series in cache but not in Sonarr: "
                        f"{_preview(names)}"
                    )
                    anomalies.append(("missing_in_live", instance_name,
                                      sorted(missing_in_live), names))

            except Exception as e:
                self.logger.log_error(f"❌ Failed to scan {instance_name}: {e}")

        return anomalies

    # An orphan set larger than this fraction of the file set is treated as a
    # DETECTION FAILURE rather than a finding. The two API calls below are made
    # without a seriesId; Sonarr's /episode and /episodefile endpoints are
    # series-scoped, so either can plausibly come back empty. An empty episode set
    # makes `known - defined` equal EVERY file, i.e. the whole library reads as
    # orphaned. A library in which >10% of files have genuinely lost their episode
    # is far less likely than a fetch that returned nothing, so above this line we
    # report the failure instead of the finding.
    _ORPHAN_SANITY_FRACTION = 0.10

    @LoggerManager().log_function_entry
    @timeit("identify_orphaned_episodes")
    def identify_orphaned_episodes(self):
        """Find episode FILES whose episodeId no longer matches any defined episode.

        Returns one record per instance:
            {"instance", "orphaned_ids" (episode ids, legacy shape),
             "orphaned_files" [{episode_file_id, episode_id, path}],
             "file_count", "episode_count", "timestamp"}

        ``orphaned_files`` carries the **episode_file_id**, which is what a delete
        would actually need -- the previous shape returned episode ids only, so
        nothing downstream could have acted on it even in principle.

        NOT VERIFIED AGAINST A LIVE SONARR. This method sits in a chain nothing
        calls (GLD-REP-07), so its two bare fetches have never returned anything
        observable. The guards below exist because the failure mode -- flagging the
        entire library -- is indistinguishable from a real finding at the call site.
        """
        self.logger.log_info("🔍 Identifying orphaned episodes...")

        results = []
        for instance_name, client in self.sonarr_api.get_all_sonarr_apis().items():
            try:
                episode_files = client.get_episode_files() or []
                all_episodes  = client.get_episodes() or []

                if not episode_files:
                    self.logger.log_debug(
                        f"[{instance_name}] no episode files returned - nothing to check."
                    )
                    continue

                # ABSENT vs EMPTY. Files exist but no episodes came back: that is a
                # failed/unscoped episode fetch, not a library of zero episodes.
                # Comparing would mark all {len(episode_files)} files orphaned.
                if not all_episodes:
                    self.logger.log_warning(
                        f"⚠️ [{instance_name}] orphan scan ABORTED: {len(episode_files)} episode "
                        f"file(s) returned but ZERO episodes. That is a failed episode fetch "
                        f"(Sonarr's /episode is series-scoped), not an empty library - "
                        f"proceeding would flag every file as orphaned."
                    )
                    continue

                defined_ids = {getattr(e, "id", None) for e in all_episodes}
                defined_ids.discard(None)

                orphans = []
                for f in episode_files:
                    eid = getattr(f, "episodeId", None)
                    if eid is None or eid in defined_ids:
                        continue
                    orphans.append({
                        "episode_file_id": getattr(f, "id", None),
                        "episode_id": eid,
                        "path": getattr(f, "path", None),
                    })

                if not orphans:
                    continue

                fraction = len(orphans) / len(episode_files)
                if fraction > self._ORPHAN_SANITY_FRACTION:
                    self.logger.log_warning(
                        f"⚠️ [{instance_name}] orphan scan REJECTED: {len(orphans)} of "
                        f"{len(episode_files)} file(s) ({fraction:.0%}) look orphaned against "
                        f"{len(all_episodes)} episode(s). Above {self._ORPHAN_SANITY_FRACTION:.0%} "
                        f"this is treated as a detection fault, not a finding - most likely the "
                        f"episode set is partial. Nothing is reported for this instance."
                    )
                    continue

                self.logger.log_warning(
                    f"⚠️ [{instance_name}] {len(orphans)} orphaned episode file(s) of "
                    f"{len(episode_files)} ({fraction:.1%}), against {len(all_episodes)} episode(s)."
                )
                results.append({
                    "instance": instance_name,
                    # Legacy shape kept so existing readers do not break.
                    "orphaned_ids": [o["episode_id"] for o in orphans],
                    "orphaned_files": orphans,
                    "file_count": len(episode_files),
                    "episode_count": len(all_episodes),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            except Exception as e:
                self.logger.log_error(f"❌ Error identifying orphans in {instance_name}: {e}")
        return results

    @LoggerManager().log_function_entry
    @timeit("generate_anomaly_report")
    def generate_anomaly_report(self):
        anomalies = {
            "metadata": self.scan_for_metadata_anomalies(),
            "orphans": self.identify_orphaned_episodes()
        }
        self.logger.log_info("📄 Generated anomaly report.")
        return anomalies

    @LoggerManager().log_function_entry
    @timeit("repair_anomalies")
    def repair_anomalies(self, report: dict | None = None) -> dict:
        """Act on the anomaly report. Honours ``dry_run``.

        WHAT THIS REPAIRS — cache divergence only, in both directions:

          missing_in_live   a series is in the cache but no longer in Sonarr.
                            Removed via ``series.remove_series(instance, id)``,
                            which rewrites only the one letter bucket holding it.
          missing_in_cache  a series is in Sonarr but not in the cache. The live
                            records here are API OBJECTS, not the dicts the bucket
                            files store, and fabricating a dict from them would
                            write a differently-shaped record than every other
                            writer produces. So instead the affected letter
                            bucket is CLEARED, which forces the next series sync
                            to rebuild it from the live payload through the normal
                            path. Slower, but it cannot invent a malformed entry.

        WHAT THIS DELIBERATELY DOES NOT REPAIR — orphaned episode FILES. Those are
        media files on disk; deleting them is exactly the class of operation this
        repo gates behind the backup pre-flight plus an explicit consent flag (see
        services/backup and routing/uhd_reconcile's seven-gate stack). Acting on
        them from an unattended repair pass, with none of those gates, is not a
        repair — it is an unreviewed deletion. They are counted and surfaced for
        the operator instead.

        Returns ``{"removed": n, "buckets_cleared": n, "orphans_flagged": n,
        "failed": n}``.
        """
        stats = {"removed": 0, "buckets_cleared": 0, "orphans_flagged": 0, "failed": 0}
        report = report if report is not None else self.generate_anomaly_report()

        series_cache = self._series_cache()
        if series_cache is None:
            self.logger.log_warning("⚠️ Cannot repair anomalies: sonarr_cache.series unavailable.")
            return stats

        prefix = "[dry_run] " if self.dry_run else ""

        for entry in (report.get("metadata") or []):
            try:
                kind, instance_name, ids, names = entry
            except (TypeError, ValueError):
                continue                     # unexpected shape — skip, do not guess

            if kind == "missing_in_live":
                for sid, name in zip(ids, names):
                    if self.dry_run:
                        self.logger.log_info(
                            f"  🗑️ {prefix}would drop stale cache entry '{name}' (id={sid}) "
                            f"from '{instance_name}'"
                        )
                        stats["removed"] += 1
                        continue
                    try:
                        if series_cache.remove_series(instance_name, sid):
                            stats["removed"] += 1
                        else:
                            stats["failed"] += 1
                    except Exception as e:
                        self.logger.log_warning(f"  ⚠️ remove_series({instance_name}, {sid}) failed: {e}")
                        stats["failed"] += 1

            elif kind == "missing_in_cache":
                # One bucket per distinct first letter, not one per series.
                letters = sorted({series_cache.get_series_bucket_letter(n or "") for n in names})
                for letter in letters:
                    if self.dry_run:
                        self.logger.log_info(
                            f"  🧹 {prefix}would clear letter bucket '{letter}' for "
                            f"'{instance_name}' so the next sync rebuilds it"
                        )
                        stats["buckets_cleared"] += 1
                        continue
                    try:
                        series_cache.clear_letter_cache(instance_name, letter)
                        stats["buckets_cleared"] += 1
                    except Exception as e:
                        self.logger.log_warning(f"  ⚠️ clear_letter_cache({instance_name}, {letter}) failed: {e}")
                        stats["failed"] += 1

        for orphan in (report.get("orphans") or []):
            files = orphan.get("orphaned_files") or []
            n = len(files) or len(orphan.get("orphaned_ids") or [])
            stats["orphans_flagged"] += n
            if not n:
                continue
            inst = orphan.get("instance")
            self.logger.log_warning(
                f"  🚩 [{inst}] {n} orphaned episode file(s) FLAGGED, NOT deleted — of "
                f"{orphan.get('file_count', '?')} file(s) against "
                f"{orphan.get('episode_count', '?')} episode(s). Removing media files "
                f"requires deletions consent AND the backup pre-flight, neither of which "
                f"this pass has. The detection itself is also UNVERIFIED against a live "
                f"Sonarr (GLD-REP-07/09) — review the paths below before acting on them."
            )
            for o in files[:10]:
                self.logger.log_info(
                    f"     · fileId={o.get('episode_file_id')} episodeId={o.get('episode_id')} "
                    f"{o.get('path') or '(no path)'}"
                )
            if len(files) > 10:
                self.logger.log_info(f"     · (+{len(files) - 10} more)")

        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["removed",         stats["removed"]],
                ["buckets_cleared", stats["buckets_cleared"]],
                ["orphans_flagged", stats["orphans_flagged"]],
                ["failed",          stats["failed"]],
            ],
            title=f"[Anomaly] {prefix}Anomaly repair",
            caption="Reconciles the letter-bucketed series cache against the live Sonarr library.",
            descriptions=[
                "stale cache entries dropped (in cache, gone from Sonarr)",
                "letter buckets cleared so the next sync rebuilds them",
                "orphaned episode files surfaced for review, NOT deleted",
                "operations that errored",
            ],
        )
        return stats

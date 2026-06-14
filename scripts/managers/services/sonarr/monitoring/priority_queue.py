from collections import defaultdict
from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrMonitoringPriorityQueueManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrMonitoring"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.tautulli = self.registry.get("manager", "TautulliManager")
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def get_threshold_for_instance(self, instance, default=10):
        thresholds = (self.config.get("sonarr_thresholds") or {}).get(instance, {})
        return thresholds.get("default", default)

    def classify_severity(self, percent_free):
        if percent_free < 5:
            return "critical"
        elif percent_free < 10:
            return "warning"
        return "ok"

    def compare_with_previous(self, instance_name, current_summary):
        cache_key = self.sonarr_cache.format_cache_key("sonarr/storage/summary", instance=instance_name)
        previous = self.sonarr_cache.get(cache_key) or {}
        deltas = []

        for entry in current_summary:
            prev = next((p for p in previous.get("evaluated", []) if p["path"] == entry["path"]), {})
            if prev:
                delta = entry["percentFree"] - prev.get("percentFree", entry["percentFree"])
                entry["deltaPercent"] = round(delta, 2)
                if delta < 0:
                    deltas.append((entry["path"], delta))

        return deltas

    @LoggerManager().log_function_entry
    @timeit("build_priority_queue")
    def build_priority_queue(self, instance: str, include_unmonitored: bool = True) -> list:
        watched_titles = set()
        if self.tautulli:
            self.logger.log_debug("📱 Pulling recent titles from Tautulli")
            tautulli_viewed = self.sonarr_cache.get(f"sonarr/{instance}/sync/tautulli_viewed") or {}
            watched_titles.update(tautulli_viewed.keys())
            tautulli_rewatches = self.sonarr_cache.get(f"sonarr/{instance}/sync/tautulli_rewatches") or {}
        else:
            tautulli_rewatches = {}

        monitored_ids = set(self.sonarr_cache.get(f"sonarr/{instance}/monitoring/monitoredSeries") or [])
        unmonitored_ids = set(self.sonarr_cache.get(f"sonarr/{instance}/monitoring/unmonitoredSeries") or [])
        free_percent = self.sonarr_cache.get(f"sonarr/{instance}/storage/free_percent") or 100
        all_series = self.sonarr_api.get_series(instance)

        priority_queue = []

        for series in all_series:
            sid = series.get("id")
            title = series.get("title", f"ID-{sid}")
            is_monitored = sid in monitored_ids
            is_unmonitored = sid in unmonitored_ids
            tags = [str(t).lower() for t in series.get("tags", [])]
            score = 0

            if title in watched_titles:
                score += 2
            if title in tautulli_rewatches:
                score += 2
            if is_unmonitored and include_unmonitored:
                score += 1
            if "keep" in tags:
                score -= 5
            if "archive" in tags:
                score -= 3
            if "active" in tags:
                score += 1
            if not series.get("tvdbId") or not series.get("images"):
                score -= 1
            if series.get("added") and "2024" in series["added"]:
                score += 1
            if series.get("episodeFileCount", 0) == 0 and series.get("episodeCount", 0) > 0:
                score -= 3
            if free_percent < self.get_threshold_for_instance(instance):
                score -= 2

            severity = self.classify_severity(free_percent)

            priority_queue.append({
                "id": sid,
                "title": title,
                "score": score,
                "monitored": is_monitored,
                "severity": severity
            })

        ranked = sorted(priority_queue, key=lambda x: x["score"], reverse=True)
        self.logger.log_info(f"📊 Built monitoring priority queue for {instance} with {len(ranked)} entries")
        return ranked

    @LoggerManager().log_function_entry
    @timeit("apply_monitoring_priority")
    def apply_monitoring_priority(self, instance: str, queue: list, dry_run: bool = False):
        dry_run = dry_run or self.dry_run
        updated = 0

        for entry in queue:
            sid = entry["id"]
            title = entry["title"]
            current_status = entry["monitored"]
            target_status = True if entry["score"] >= 2 else False

            if current_status != target_status:
                if dry_run:
                    self.logger.log_info(f"[Dry Run] Would change monitoring for {title} → {target_status}")
                else:
                    self.sonarr_api.update_series_monitoring(sid, monitored=target_status)
                    self.logger.log_info(f"🔁 Changed monitoring for {title} → {target_status}")
                updated += 1

        self.logger.log_info(f"✅ Applied {updated} monitoring changes for {instance}")

    @LoggerManager().log_function_entry
    @timeit("run_across_all_instances")
    def run_across_all_instances(self, include_unmonitored=True):
        for instance in self.sonarr_api.get_all_sonarr_apis():
            queue = self.build_priority_queue(instance, include_unmonitored=include_unmonitored)
            self.apply_monitoring_priority(instance, queue)

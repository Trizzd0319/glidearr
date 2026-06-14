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

    @LoggerManager().log_function_entry
    @timeit("scan_for_metadata_anomalies")
    def scan_for_metadata_anomalies(self):
        """
        Compare cached vs. live metadata to identify inconsistencies.
        """
        self.logger.log_info("🔍 Scanning for metadata anomalies...")
        anomalies = []

        all_instances = self.sonarr_api.get_all_sonarr_apis()
        for instance_name, client in all_instances.items():
            try:
                series = client.get_series()
                cache_key = f"sonarr::{instance_name}::series"
                cached_series = self.sonarr_cache.get(cache_key) or []

                live_titles = {s.title for s in series}
                cached_titles = {s.get("title") for s in cached_series}

                missing_in_cache = live_titles - cached_titles
                missing_in_live = cached_titles - live_titles

                if missing_in_cache:
                    self.logger.log_warning(f"⚠️ {len(missing_in_cache)} series missing in cache: {missing_in_cache}")
                    anomalies.append(("missing_in_cache", instance_name, list(missing_in_cache)))

                if missing_in_live:
                    self.logger.log_warning(f"⚠️ {len(missing_in_live)} series missing in live API: {missing_in_live}")
                    anomalies.append(("missing_in_live", instance_name, list(missing_in_live)))

            except Exception as e:
                self.logger.log_error(f"❌ Failed to scan {instance_name}: {e}")

        return anomalies

    @LoggerManager().log_function_entry
    @timeit("identify_orphaned_episodes")
    def identify_orphaned_episodes(self):
        """
        Compare Sonarr API vs filesystem or disk scan results to find orphaned files.
        """
        self.logger.log_info("🔍 Identifying orphaned episodes...")

        results = []
        for instance_name, client in self.sonarr_api.get_all_sonarr_apis().items():
            try:
                episode_files = client.get_episode_files()
                known_episode_ids = {f.episodeId for f in episode_files}
                all_episodes = client.get_episodes()
                defined_ids = {e.id for e in all_episodes}

                orphan_ids = known_episode_ids - defined_ids
                if orphan_ids:
                    self.logger.log_warning(f"⚠️ Found {len(orphan_ids)} orphaned episode files in {instance_name}")
                    results.append({
                        "instance": instance_name,
                        "orphaned_ids": list(orphan_ids),
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

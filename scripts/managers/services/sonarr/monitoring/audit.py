from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrMonitoringAuditManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesRetrieval"

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.manager = kwargs.get("manager") or registry.get("manager", self.parent_name)

        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(self.manager, "sonarr_cache", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        self.logger.log_debug("📋 SonarrMonitoringAuditManager initialized")

    @LoggerManager().log_function_entry
    @timeit("audit_empty_episode_files")
    def audit_empty_episode_files(self, instance: str) -> list[dict]:
        resolved = self.instance_manager.resolve_instance(instance)
        empty_files = []
        all_series = self.sonarr_api.get_series(resolved)

        for series in all_series:
            sid = series.get("id")
            eps = self.sonarr_api._make_request(resolved, f"episode?seriesId={sid}") or []
            for ep in eps:
                if ep.get("hasFile") and (ep.get("episodeFile") or {}).get("size") == 0:
                    empty_files.append(ep)

        self.logger.log_info(f"🧼 Found {len(empty_files)} episodes with empty files in {instance}")
        return empty_files

    @LoggerManager().log_function_entry
    @timeit("audit_duplicate_episode_titles")
    def audit_duplicate_episode_titles(self, instance: str) -> dict:
        resolved = self.instance_manager.resolve_instance(instance)
        all_episodes = self.sonarr_cache.episodes.get_all(resolved)
        title_map = {}
        duplicates = {}

        for ep in all_episodes:
            title = ep.get("title")
            if not title:
                continue
            eid = ep.get("id")
            if title in title_map:
                duplicates.setdefault(title, [title_map[title]]).append(eid)
            else:
                title_map[title] = eid

        self.logger.log_info(f"🔍 Found {len(duplicates)} duplicate episode titles in {instance}")
        return duplicates

    @LoggerManager().log_function_entry
    @timeit("audit_missing_air_dates")
    def audit_missing_air_dates(self, instance: str) -> list[dict]:
        resolved = self.instance_manager.resolve_instance(instance)
        all_episodes = self.sonarr_cache.episodes.get_all(resolved)
        missing = [ep for ep in all_episodes if not ep.get("airDate")]

        self.logger.log_info(f"📅 Found {len(missing)} episodes missing air dates in {instance}")
        return missing

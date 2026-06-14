from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrEpisodesRetrievalFetchManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesRetrieval"

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.manager = kwargs.get("manager") or registry.get("manager", self.parent_name)

        # ✅ Renamed for consistency
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(self.manager, "sonarr_cache", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        self.logger.log_debug("🧲 SonarrEpisodesRetrievalFetchManager initialized")

    @LoggerManager().log_function_entry
    @timeit("get_episode_by_id")
    def get_episode_by_id(self, instance: str, episode_id: int) -> dict | None:
        resolved = self.instance_manager.resolve_instance(instance)
        all_episodes = self._get_cached_episodes_by_instance(resolved)
        for ep in all_episodes:
            if ep.get("id") == episode_id:
                return ep
        return None

    @LoggerManager().log_function_entry
    @timeit("get_episodes_by_title")
    def get_episodes_by_title(self, instance: str, title: str) -> list[dict]:
        resolved = self.instance_manager.resolve_instance(instance)
        all_episodes = self._get_cached_episodes_by_instance(resolved)
        return [ep for ep in all_episodes if ep.get("title", "").lower() == title.lower()]

    @LoggerManager().log_function_entry
    @timeit("get_episodes_by_slug")
    def get_episodes_by_slug(self, instance: str, slug: str) -> list[dict]:
        resolved = self.instance_manager.resolve_instance(instance)
        return self.sonarr_cache.episodes.get_by_slug(resolved, slug)

    @LoggerManager().log_function_entry
    @timeit("get_episodes_by_letter")
    def get_episodes_by_letter(self, instance: str, letter: str) -> list[dict]:
        resolved = self.instance_manager.resolve_instance(instance)
        return self.sonarr_cache.episodes.load_letter_cache(resolved, letter)

    @LoggerManager().log_function_entry
    @timeit("get_recent_episode_ids")
    def get_recent_episode_ids(self, instance: str, hours: int = 24) -> set[int]:
        resolved = self.instance_manager.resolve_instance(instance)
        history = self.sonarr_api._make_request(resolved, f"history?page=1&pageSize=1000&sortKey=date&sortDir=desc") or {}
        now = datetime.utcnow()
        recent = set()

        for record in history.get("records", []):
            dt_str = record.get("date")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if (now - dt).total_seconds() <= hours * 3600:
                    eid = record.get("episodeId")
                    if eid:
                        recent.add(eid)
            except Exception:
                continue

        self.logger.log_info(f"🕒 Fetched {len(recent)} recently active episode IDs from history.")
        return recent

    def _get_cached_episodes_by_instance(self, instance: str) -> list[dict]:
        try:
            return self.sonarr_cache.episodes.get_all(instance)
        except Exception as e:
            self.logger.log_warning(f"⚠️ Fallback triggered for {instance}: {e}")
            return self._fetch_fallback(instance)

    def _fetch_fallback(self, instance: str) -> list[dict]:
        all_series = self.sonarr_api.get_series(instance)
        episodes = []
        for s in all_series:
            sid = s.get("id")
            eps = self.sonarr_api._make_request(instance, f"episode?seriesId={sid}") or []
            episodes.extend(eps)
            self.sonarr_cache.episodes.set_series_timestamp(instance, sid, datetime.utcnow().isoformat())

        self.logger.log_info(f"🔄 Pulled {len(episodes)} episodes from API for fallback caching.")
        return episodes

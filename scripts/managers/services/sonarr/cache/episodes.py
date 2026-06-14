from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrCacheEpisodesManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr episode-level caches and monitoring.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrCacheEpisodes"
        class_name = self.__class__.__name__

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register(parent_name=self.parent_name)

        # 🧩 Fallback context linking
        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    def get_pilot_episode_id(self, series_id):
        episodes = self.sonarr_api.get_episodes(series_id)
        for episode in episodes:
            if episode.get("seasonNumber") == 1 and episode.get("episodeNumber") == 1:
                self.logger.log_debug(f"Found pilot episode ID {episode['id']} for series {series_id}")
                return episode.get("id")
        self.logger.log_warning(f"No pilot episode found for series {series_id}")
        return None

    def get_latest_episode_id(self, series_id):
        episodes = self.sonarr_api.get_episodes(series_id)
        if not episodes:
            self.logger.log_warning(f"No episodes found for series {series_id}")
            return None
        latest = max(episodes, key=lambda ep: ep.get("airDateUtc", ""))
        self.logger.log_debug(f"Latest episode ID {latest['id']} for series {series_id}")
        return latest.get("id")

    def build_episode_monitoring_map(self, series_id):
        episodes = self.sonarr_api.get_episodes(series_id)
        monitoring_map = {ep["id"]: ep["monitored"] for ep in episodes}
        self.logger.log_debug(f"Built monitoring map for series {series_id} with {len(monitoring_map)} entries")
        return monitoring_map

    def get_monitored_episodes(self, series_id):
        episodes = self.sonarr_api.get_episodes(series_id)
        monitored = [ep for ep in episodes if ep.get("monitored")]
        self.logger.log_debug(f"Found {len(monitored)} monitored episodes for series {series_id}")
        return monitored

    def get_unmonitored_episodes(self, series_id):
        episodes = self.sonarr_api.get_episodes(series_id)
        unmonitored = [ep for ep in episodes if not ep.get("monitored")]
        self.logger.log_debug(f"Found {len(unmonitored)} unmonitored episodes for series {series_id}")
        return unmonitored

    def update_episode_monitoring_state(self, series_id, updates):
        payload = [{"id": eid, "monitored": state} for eid, state in updates.items()]
        try:
            self.sonarr_api.bulk_update_episodes(series_id, payload)
            self.logger.log_info(f"✅ Updated monitoring state for {len(payload)} episodes in series {series_id}")
            return True
        except Exception as e:
            self.logger.log_error(f"❌ Failed to update episode monitoring for series {series_id}: {e}")
            return False

    def refresh_episode_cache(self, series_id):
        try:
            episodes = self.sonarr_api.get_episodes(series_id)
            self.logger.log_info(f"🔄 Refreshed episode cache for series {series_id} with {len(episodes)} episodes")
            return episodes
        except Exception as e:
            self.logger.log_error(f"❌ Failed to refresh episode cache for series {series_id}: {e}")
            return []

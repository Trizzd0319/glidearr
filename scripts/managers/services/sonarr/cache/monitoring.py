from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin

class SonarrCacheMonitoringManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr monitoring-related caches.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        # 🔗 Inherit metadata and API from parent if needed
        parent = self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def refresh_monitored_series(self, instance):
        series = self.sonarr_api.get_series(instance)
        monitored = [s for s in series if s.get("monitored")]
        self.sonarr_cache.set(f"sonarr/{instance}/monitoring_series.json", monitored)
        self.logger.log_info(f"✅ Refreshed monitored series cache for {instance}")

    def get_monitored_series(self, instance):
        data = self.sonarr_cache.get(f"sonarr/{instance}/monitoring_series.json")
        return data or []

    def sync_monitored_flags(self, series_id, desired_map):
        episodes = self.sonarr_api.get_episodes(series_id)
        for episode in episodes:
            ep_id = episode["id"]
            desired = desired_map.get(ep_id)
            if desired is not None and episode["monitored"] != desired:
                self.sonarr_api.update_episode_monitoring(ep_id, desired)
                self.logger.log_info(f"🔧 Updated monitoring for episode {ep_id} → {desired}")

    def detect_series_monitoring_discrepancies(self, series_id):
        episodes = self.sonarr_api.get_episodes(series_id)
        return [ep for ep in episodes if ep["monitored"] != self._should_be_monitored(ep)]

    def patch_series_monitoring_state(self, series_id, desired_state):
        episodes = self.sonarr_api.get_episodes(series_id)
        for episode in episodes:
            if episode["monitored"] != desired_state:
                self.sonarr_api.update_episode_monitoring(episode["id"], desired_state)
                self.logger.log_info(f"🔧 Patched monitoring → {episode['id']}: {desired_state}")

    def _should_be_monitored(self, episode):
        return episode.get("seasonNumber") == 1 and episode.get("episodeNumber") == 1

    def enforce_keep_tags(self, series_list):
        for series in series_list:
            if "keep" in series.get("tags", []):
                self.patch_series_monitoring_state(series["id"], True)
                self.logger.log_info(f"🔒 Enforced 'keep' monitoring for series {series['title']}")

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesMonitoringManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesMonitoringManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # 🗭 Parent + API Injection
        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)

        # 🧲 Mode Flag
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        # 🛠 Dual Cache Support
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def batch_unmonitor_downloaded_if_cutoff_met(self, instance):
        """
        Unmonitor episodes that are downloaded AND meet cutoff criteria,
        EXCEPT pilot episodes unless another instance already has a higher-quality download.
        """
        episodes = self.sonarr_api.get_episodes(instance)
        never_unmonitor = self.config.get("never_unmonitor_tags", [])
        to_unmonitor = []

        for episode in episodes:
            if not episode.get("monitored") or not episode.get("hasFile"):
                continue
            if any(tag in never_unmonitor for tag in episode.get("tags", [])):
                continue

            is_pilot = episode.get("seasonNumber") == 1 and episode.get("episodeNumber") == 1
            if is_pilot and not self._is_pilot_available_elsewhere(instance, episode):
                self.logger.log_debug(f"❌ Skipping unmonitor for pilot episode {episode['id']} — not replicated elsewhere.")
                continue

            if episode.get("cutoffMet", False):
                to_unmonitor.append({"id": episode["id"], "monitored": False})

        if to_unmonitor:
            try:
                self.sonarr_api.bulk_update_episodes(to_unmonitor)
                self.logger.log_info(f"🔻 Batch unmonitored {len(to_unmonitor)} episodes (cutoff met).")
            except Exception as e:
                self.logger.log_error(f"❌ Failed batch unmonitor: {e}")
        else:
            self.logger.log_info("✅ No episodes met cutoff for unmonitoring.")

    def batch_monitor_cutoff_unmet(self, instance):
        """
        Monitor episodes that are currently unmonitored and have not met cutoff.
        """
        episodes = self.sonarr_api.get_episodes(instance)
        to_monitor = []

        for episode in episodes:
            if episode.get("monitored"):
                continue
            if not episode.get("cutoffMet", True):
                to_monitor.append({"id": episode["id"], "monitored": True})

        if to_monitor:
            try:
                self.sonarr_api.bulk_update_episodes(to_monitor)
                self.logger.log_info(f"🔺 Batch monitored {len(to_monitor)} episodes (cutoff unmet).")
            except Exception as e:
                self.logger.log_error(f"❌ Failed batch monitor: {e}")
        else:
            self.logger.log_info("✅ No episodes needed to be monitored for cutoff.")

    def auto_unmonitor_downloaded(self, instance):
        """
        Automatically unmonitor episodes that are downloaded, meet cutoff,
        and are not tagged to retain monitoring. Never remove pilot unless it's covered.
        """
        episodes = self.sonarr_api.get_episodes(instance)
        never_unmonitor = self.config.get("never_unmonitor_tags", [])

        for episode in episodes:
            if not episode.get("monitored") or not episode.get("hasFile"):
                continue

            tags = episode.get("tags", [])
            if any(tag in never_unmonitor for tag in tags):
                self.logger.log_debug(f"⏩ Skipping unmonitor for episode ID {episode['id']} due to tag exemption.")
                continue

            is_pilot = episode.get("seasonNumber") == 1 and episode.get("episodeNumber") == 1
            if is_pilot and not self._is_pilot_available_elsewhere(instance, episode):
                self.logger.log_debug(f"❌ Skipping unmonitor for pilot episode {episode['id']} — not replicated elsewhere.")
                continue

            if episode.get("cutoffMet", False):
                try:
                    self.sonarr_api.update_episode_monitoring(instance, episode["id"], monitored=False)
                    self.logger.log_info(f"🔻 Auto-unmonitored episode ID {episode['id']} — downloaded and cutoff met.")
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Failed to unmonitor episode ID {episode['id']}: {e}")

    def monitor_episodes_with_unmet_cutoff(self, instance):
        """
        Monitor episodes where cutoff is unmet (e.g., manual removal or upgraded quality).
        """
        episodes = self.sonarr_api.get_episodes(instance)
        newly_monitored = 0

        for episode in episodes:
            if episode.get("monitored"):
                continue

            if not episode.get("cutoffMet", True):
                try:
                    self.sonarr_api.update_episode_monitoring(instance, episode["id"], monitored=True)
                    self.logger.log_info(f"🔺 Re-monitored episode ID {episode['id']} — cutoff unmet.")
                    newly_monitored += 1
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Failed to re-monitor episode ID {episode['id']}: {e}")

        self.logger.log_info(f"📈 Re-monitored {newly_monitored} episodes with unmet cutoff.")

    def _is_pilot_available_elsewhere(self, current_instance, episode):
        """
        Check if the pilot episode has already been downloaded in another Sonarr instance.
        """
        all_instances = self.config.get_sonarr_instances()
        series_id = episode.get("seriesId")
        season = episode.get("seasonNumber")
        episode_num = episode.get("episodeNumber")

        for name, config in all_instances.items():
            if name == current_instance:
                continue
            try:
                episodes = self.sonarr_api.get_episodes(name)
                for ep in episodes:
                    if (
                        ep.get("seriesId") == series_id
                        and ep.get("seasonNumber") == season
                        and ep.get("episodeNumber") == episode_num
                        and ep.get("hasFile")
                    ):
                        return True
            except Exception as e:
                self.logger.log_warning(f"⚠️ Failed to check pilot availability in {name}: {e}")
        return False

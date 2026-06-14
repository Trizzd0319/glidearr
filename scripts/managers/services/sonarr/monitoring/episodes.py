from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrMonitoringEpisodesManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesMonitoring"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.logger = self.logger or getattr(self.manager, "logger", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.tag_monitor = self.get_tag_monitor()

        if not self.logger:
            raise ValueError(f"❌ {self.__class__.__name__} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("get_all_episode_monitoring")
    def get_all_episode_monitoring(self, instance):
        return self.sonarr_api.get_all_episodes(instance)

    @LoggerManager().log_function_entry
    def update_monitoring_state(self, episode_id, instance, monitored):
        self.sonarr_api.update_episode_monitoring(instance, episode_id, monitored)

    @LoggerManager().log_function_entry
    def monitor_upcoming_episodes(self, series_id, last_ep, season, user, instance):
        upcoming = self.sonarr_api.get_upcoming_episodes(series_id)
        for ep in upcoming:
            if ep["seasonNumber"] == season and ep["episodeNumber"] > last_ep:
                self.sonarr_api.update_episode_monitoring(instance, ep["id"], monitored=True)

    @LoggerManager().log_function_entry
    def batch_monitor_episodes(self, episode_ids, instance):
        failed_updates = []
        for eid in episode_ids:
            try:
                self.sonarr_api.update_episode_monitoring(instance, eid, monitored=True)
            except Exception as e:
                self.logger.log_warning(f"⚠️ Failed to update episode {eid}: {e}")
                failed_updates.append(eid)

        if failed_updates:
            self.logger.log_error(f"❌ Rollback triggered due to failures in batch: {failed_updates}")
            for eid in episode_ids:
                if eid not in failed_updates:
                    try:
                        self.sonarr_api.update_episode_monitoring(instance, eid, monitored=False)
                        self.logger.log_info(f"🔄 Rolled back episode {eid} to unmonitored")
                    except Exception as e:
                        self.logger.log_warning(f"⚠️ Failed to rollback episode {eid}: {e}")

    @LoggerManager().log_function_entry
    def track_monitored_episodes(self, series_id, instance):
        episodes = self.sonarr_api.get_series_episodes(series_id)
        return [ep for ep in episodes if ep.get("monitored")]

    @LoggerManager().log_function_entry
    def auto_unmonitor_watched_episodes(self, monitored_episodes, instance, series_id):
        if self.tag_monitor and self.tag_monitor.is_series_tagged_keep(series_id):
            self.logger.log_debug(f"⏩ Skip unmonitoring for 'keep' series {series_id}")
            return

        config_exceptions = self.config.get("never_unmonitor_tags", [])
        for ep in monitored_episodes:
            tags = ep.get("tags", [])
            if ep.get("hasFile", False) and not any(tag in config_exceptions for tag in tags):
                self.sonarr_api.update_episode_monitoring(instance, ep["id"], monitored=False)

    @LoggerManager().log_function_entry
    def ensure_specials_are_unmonitored(self, series_id, instance):
        special_keep_tags = self.config.get("special_keep_tags", ["holiday", "finale"])
        holiday_dates = {
            "Halloween": (10, 31),
            "Christmas": (12, 25),
            "New Year's": (1, 1),
            "Valentine's Day": (2, 14),
            "Easter": (4, 9),
            "Thanksgiving": (11, 23),
            "Independence Day": (7, 4),
            "Labor Day": (9, 4),
            "Memorial Day": (5, 29),
            "Veterans Day": (11, 11),
            "Martin Luther King Jr. Day": (1, 16),
            "Presidents' Day": (2, 20)
        }

        episodes = self.sonarr_api.get_series_episodes(series_id)
        for ep in episodes:
            tags = ep.get("tags", [])
            air_date_str = ep.get("airDate")
            keep_due_to_date = False

            if air_date_str:
                try:
                    air_date = datetime.strptime(air_date_str, "%Y-%m-%d")
                    for _, (month, day) in holiday_dates.items():
                        if abs((air_date - datetime(air_date.year, month, day)).days) <= 7:
                            keep_due_to_date = True
                            break
                    if keep_due_to_date:
                        continue
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Could not parse air date for episode {ep['id']}: {air_date_str}")

            if ep.get("seasonNumber") == 0:
                if any(tag in special_keep_tags for tag in tags) or keep_due_to_date:
                    if not ep["monitored"]:
                        self.sonarr_api.update_episode_monitoring(instance, ep["id"], monitored=True)
                else:
                    if ep["monitored"]:
                        self.sonarr_api.update_episode_monitoring(instance, ep["id"], monitored=False)

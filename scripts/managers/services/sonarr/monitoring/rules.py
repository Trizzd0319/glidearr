from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrMonitoringRulesManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrMonitoring"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager") or self.registry.get("manager", self.parent_name)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.tag_monitor = self.get_tag_monitor()

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    # ─────────────────────────────────────────────
    # 🔁 Rule Application
    # ─────────────────────────────────────────────

    def apply_monitoring_rules(self, series_data):
        self.logger.log_info(f"⚙️ Applying monitoring rules to {len(series_data)} series...")
        for series in series_data:
            sid = series.get("id")
            title = series.get("title")

            if self.tag_monitor and self.tag_monitor.is_series_tagged_keep(sid):
                self.logger.log_info(f"🔒 Skipping unmonitoring for '{title}' (tagged 'keep')")
                continue

            self.ensure_pilot_episode_monitored(series)

            if self.should_auto_monitor(series):
                self.sonarr_api.update_series_monitoring(sid, monitored=True)

            elif self.should_auto_unmonitor(series):
                self.sonarr_api.update_series_monitoring(sid, monitored=False)

            if self.should_monitor_due_to_awards(series):
                self.sonarr_api.update_series_monitoring(sid, monitored=True)

            if self.should_monitor_due_to_high_rating(series):
                self.sonarr_api.update_series_monitoring(sid, monitored=True)

        self.logger.log_info("🎯 Monitoring rules applied successfully.")

    def ensure_pilot_episode_monitored(self, series):
        episodes = self.sonarr_api.get_series_episodes(series["id"])
        for ep in episodes:
            if ep.get("seasonNumber") == 1 and ep.get("episodeNumber") == 1 and not ep.get("monitored"):
                self.sonarr_api.update_episode_monitoring(series["id"], ep["id"], monitored=True)
                self.logger.log_info(f"✅ Pilot episode set monitored for '{series['title']}'")

    # ─────────────────────────────────────────────
    # 🔍 Analysis + Bulk Evaluation
    # ─────────────────────────────────────────────

    def evaluate_series_priority(self, series_list):
        return sorted(series_list, key=lambda s: (s.get("watchCount", 0), s.get("popularityScore", 0)), reverse=True)

    def auto_apply_user_rules(self, user_preferences):
        preferred = set(user_preferences.get("preferred_genres", []))
        excluded = set(user_preferences.get("excluded_titles", []))

        for series in self.sonarr_api.get_all_series():
            if series.get("genre") in preferred:
                if not self.dry_run:
                    self.sonarr_api.update_series_monitoring(series["id"], monitored=True)
                else:
                    self.logger.log_info(f"[DRY-RUN] Would monitor '{series.get('title')}' (preferred genre)")
            if series.get("title") in excluded:
                if not self.dry_run:
                    self.sonarr_api.update_series_monitoring(series["id"], monitored=False)
                else:
                    self.logger.log_info(f"[DRY-RUN] Would unmonitor '{series.get('title')}' (excluded title)")

    def prioritize_frequently_watched_series(self, watched_series):
        high_priority = [s for s in watched_series if s.get("watchCount", 0) > 10]
        for series in high_priority:
            if not self.dry_run:
                self.sonarr_api.update_series_monitoring(series["id"], monitored=True)
            else:
                self.logger.log_info(f"[DRY-RUN] Would monitor '{series.get('title')}' (high watch count)")

    def enforce_global_monitoring_settings(self):
        global_settings = self.config.get("global_monitoring_settings", {})
        force_all = global_settings.get("force_monitor_all", False)
        force_unmonitor = global_settings.get("force_unmonitor_archived", False)

        for series in self.sonarr_api.get_all_series():
            if force_all:
                if not self.dry_run:
                    self.sonarr_api.update_series_monitoring(series["id"], monitored=True)
                else:
                    self.logger.log_info(f"[DRY-RUN] Would monitor '{series.get('title')}' (force_all)")
            if force_unmonitor and series.get("status") == "archived":
                if not self.dry_run:
                    self.sonarr_api.update_series_monitoring(series["id"], monitored=False)
                else:
                    self.logger.log_info(f"[DRY-RUN] Would unmonitor '{series.get('title')}' (archived)")

    # ─────────────────────────────────────────────
    # 📊 Rule Conditions
    # ─────────────────────────────────────────────

    def should_auto_monitor(self, series):
        return series.get("popularityScore", 0) > 80

    def should_auto_unmonitor(self, series):
        return series.get("status") == "ended" and series.get("watchCount", 0) == 0

    def should_monitor_due_to_awards(self, series):
        awards = series.get("awards", [])
        return "Emmy" in awards or "Golden Globe" in awards

    def should_monitor_due_to_high_rating(self, series):
        return series.get("rating", 0) >= 8.5

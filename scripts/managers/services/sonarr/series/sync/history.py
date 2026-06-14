from datetime import datetime, timedelta
import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesSyncHistoryManager(BaseManager, ComponentManagerMixin):
    """
    Syncs Sonarr series based on recent history events from the API.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrSeries"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager") or self.registry.get("manager", self.parent_name)

        self.manager = parent
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False))
        self.orchestration = kwargs.get("orchestration", getattr(parent, "orchestration", None))
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)

        # ✅ Dual cache model
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(parent, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(parent, "global_cache", None)

        if not self.logger:
            raise ValueError("❌ SonarrSeriesSyncHistoryManager could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("get_recent_sonarr_series")
    def get_recent_sonarr_series(self, instance: str):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        now = datetime.utcnow()
        since_date = None
        ts_handler = getattr(self.global_cache, "timestamp_handler", None)

        try:
            if not ts_handler:
                raise ValueError("No timestamp handler on global_cache")

            # get_age_seconds returns None when no timestamp has been recorded yet
            age = ts_handler.get_age_seconds("sonarr", resolved_instance, "history")
            if age is None:
                raise ValueError("No history timestamp recorded yet — first run")

            last_synced_at = now - timedelta(seconds=int(age))
            if age > 7 * 86400:
                since_date = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
                self.logger.log_warning(
                    f"⚠️ History cache stale (age {age // 86400}d) for '{resolved_instance}'; "
                    f"clamped since-date to 7-day window: {since_date}"
                )
            else:
                since_date = last_synced_at.strftime("%Y-%m-%dT%H:%M:%S")
                age_h = int(age) // 3600
                age_m = (int(age) % 3600) // 60
                self.logger.log_info(
                    f"⏱ History since-date for '{resolved_instance}': "
                    f"{since_date} (age {age_h}h {age_m}m)"
                )

        except Exception as e:
            since_date = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
            self.logger.log_warning(
                f"⚠️ Using fallback since-date for '{resolved_instance}': "
                f"{since_date} ({e})"
            )

        history = self.sonarr_api._make_request(
            resolved_instance,
            f"history/since?date={since_date}&includeSeries=true&includeEpisode=true",
            method="GET",
            fallback={}
        )

        records = history.get("records", []) if isinstance(history, dict) else []
        self.logger.log_info(f"📜 Retrieved {len(records)} history records from '{resolved_instance}'")

        # Write the history timestamp so the next run uses *now* as its since-date
        # rather than falling back to the 7-day window every time.
        try:
            self.global_cache.timestamp_handler.update_timestamp("sonarr", resolved_instance, "history")
            self.logger.log_debug(f"⏱ Updated history sync timestamp for '{resolved_instance}'")
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to write history timestamp for '{resolved_instance}': {e}")

        return {record["seriesId"] for record in records if "seriesId" in record}

    @LoggerManager().log_function_entry
    @timeit("sync_series_from_history")
    def sync_series_from_history(self, instance: str, timestamp: str):
        instance_config = (self.config.get("sonarr_instances") or {}).get(instance)
        if not instance_config:
            self.logger.log_error(f"❌ No config found for instance '{instance}'")
            return

        url = f"{instance_config['base_url']}/api/v3/history/since"
        params = {
            "date": timestamp,
            "includeSeries": "true",
            "includeEpisode": "true"
        }

        try:
            response = requests.get(url, params=params, headers={"X-Api-Key": instance_config["api"]})
            response.raise_for_status()
            history_items = response.json()
        except Exception as e:
            self.logger.log_error(f"❌ Failed to fetch history from {timestamp}: {e}")
            return

        valid_event_types = {"downloadFolderImported", "seriesFolderImported", "episodeFileRenamed"}
        updates_made = 0

        for item in history_items:
            if item.get("eventType") in valid_event_types:
                self.logger.log_debug(
                    f"🔄 Event '{item.get('eventType')}' for series {item.get('seriesId')}"
                )
                updates_made += 1

        self.logger.log_info(f"✅ History sync complete — {updates_made} changes applied.")

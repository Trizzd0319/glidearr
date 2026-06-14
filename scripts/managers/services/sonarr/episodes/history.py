import time
import requests
from datetime import datetime, timedelta

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrEpisodesHistoryManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodes"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None,
                 registry=None, cache_manager=None, **kwargs):
        self.manager = kwargs.get("manager") or {}
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.sonarr_cache = cache_manager or getattr(self.manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    def get_recent_episode_events(self, instance_name, since_timestamp, event_types=None):
        instance_config = (self.config.get("sonarr_instances") or {}).get(instance_name)
        if not instance_config:
            self.logger.log_error(f"❌ No configuration found for instance '{instance_name}'")
            return []

        url = f"{instance_config['base_url']}/api/v3/history/since"
        params = {
            "date": since_timestamp,
            "includeSeries": "false",
            "includeEpisode": "true"
        }

        try:
            response = requests.get(url, params=params, headers={"X-Api-Key": instance_config['api']})
            self.logger.log_info(f"🌐 Full Sonarr episode history request URL: {str(response.url).split('?', 1)[0]}")
            response.raise_for_status()
            raw_items = response.json()
        except Exception as e:
            self.logger.log_error(f"❌ Failed to fetch episode history since {since_timestamp}: {e}")
            return []

        default_events = {"downloadFolderImported", "seriesFolderImported", "episodeFileRenamed"}
        valid_events = set(event_types or default_events)

        episode_list = [item.get("episode") for item in raw_items
                        if item.get("eventType") in valid_events and item.get("episode")]
        return episode_list

    @LoggerManager().log_function_entry
    def get_series_with_recent_history(self, instance_name, days_back=2, page_size=1000, max_retries=5):
        instance_config = (self.config.get("sonarr_instances") or {}).get(instance_name)
        if not instance_config:
            self.logger.log_error(f"❌ No configuration found for instance '{instance_name}'")
            return set()

        base_url = instance_config['base_url']
        api_key = instance_config['api']
        since = (datetime.utcnow() - timedelta(days=days_back)).isoformat() + "Z"

        updated_series_ids = set()
        page = 1
        total_processed = 0

        while True:
            url = f"{base_url}/api/v3/history"
            params = {
                "page": page,
                "pageSize": page_size,
                "sortDirection": "descending",
                "date": since
            }

            retries = 0
            while retries <= max_retries:
                try:
                    response = requests.get(url, params=params, headers={"X-Api-Key": api_key})
                    if response.status_code == 429:
                        wait = 2 ** retries
                        self.logger.log_warning(f"⏳ Rate limited (429) on page {page}, retrying in {wait}s...")
                        time.sleep(wait)
                        retries += 1
                        continue
                    response.raise_for_status()
                    break
                except requests.RequestException as e:
                    self.logger.log_error(f"❌ Failed request for page {page} (attempt {retries + 1}): {e}")
                    return updated_series_ids

            history_batch = response.json()
            if not history_batch:
                break

            for entry in history_batch:
                if entry.get("date", "") > since:
                    updated_series_ids.add(entry["seriesId"])
                    total_processed += 1

            self.logger.log_debug(
                f"📄 Page {page} — {len(history_batch)} entries, {len(updated_series_ids)} unique series")

            if len(history_batch) < page_size:
                break

            page += 1

        self.logger.log_info(
            f"🧠 {len(updated_series_ids)} unique series with updates across {page} pages "
            f"({total_processed} entries in last {days_back}d)")
        return updated_series_ids

    @LoggerManager().log_function_entry
    def build_episode_watch_counts(self, instance_name, days_back=30):
        instance_config = (self.config.get("sonarr_instances") or {}).get(instance_name)
        if not instance_config:
            self.logger.log_error(f"❌ No configuration found for instance '{instance_name}'")
            return {}

        since = (datetime.utcnow() - timedelta(days=days_back)).isoformat() + "Z"
        url = f"{instance_config['base_url']}/api/v3/history"
        params = {
            "pageSize": 1000,
            "sortDirection": "descending",
            "date": since
        }

        watch_counts = {}
        page = 1
        while True:
            params["page"] = page
            try:
                response = requests.get(url, params=params, headers={"X-Api-Key": instance_config['api']})
                response.raise_for_status()
                events = response.json()
            except Exception as e:
                self.logger.log_warning(f"⚠ Failed to fetch history page {page}: {e}")
                break

            if not events:
                break

            for event in events:
                eid = event.get("episodeId")
                if eid:
                    watch_counts[eid] = watch_counts.get(eid, 0) + 1

            if len(events) < 1000:
                break
            page += 1

        self.logger.log_info(f"📊 Compiled watch counts for {len(watch_counts)} episode IDs")
        return watch_counts

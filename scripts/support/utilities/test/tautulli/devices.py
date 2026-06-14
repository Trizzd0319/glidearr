# managers/tautulli/manager_devices.py

from collections import defaultdict, Counter
# Import statements
from datetime import datetime, timedelta

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.services.tautulli.watch_history import WatchHistoryManager
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class DeviceManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger: LoggerManager, cache: GlobalCacheManager, watch_history: WatchHistoryManager):
        self.logger = logger
        self.cache = cache
        self.watch_history = watch_history

    @LoggerManager().log_function_entry
    @timeit("_generate_preferred_devices")
    def _generate_preferred_devices(self):
        """
        Determines the most frequently used device for each user.
        """
        history_data = self.watch_history.get_history()
        if not history_data:
            self.logger.log_warning("⚠️ No history data found. Cannot track preferred devices.")
            return {}

        device_usage = {}
        for entry in history_data:
            user = entry.get("user", "Unknown User")
            device = entry.get("platform", "Unknown Device")

            if user not in device_usage:
                device_usage[user] = {}

            device_usage[user][device] = device_usage[user].get(device, 0) + 1

        # Identify the most frequently used device per user
        preferred_devices = {
            user: max(devices.items(), key=lambda x: x[1])[0]
            for user, devices in device_usage.items()
        }

        return preferred_devices

    @LoggerManager().log_function_entry
    @timeit("cache_preferred_devices")
    def cache_preferred_devices(self):
        """
        Caches preferred playback devices per user.
        Helps determine the best quality settings.
        """
        self.logger.log_info("🔍 Fetching and caching preferred playback devices.")

        return self.cache.get_or_generate_cache(
            "preferred_devices",
            lambda: self._generate_preferred_devices(),
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("generate_device_usage_trends")
    def generate_device_usage_trends(self, interval='weekly'):
        """
        Tracks device usage frequency trends over specified intervals.
        """
        history_data = self.watch_history.get_history()
        if not history_data:
            return {}

        trends = defaultdict(lambda: defaultdict(int))
        for entry in history_data:
            date = datetime.fromtimestamp(int(entry.get("date", 0)))
            if interval == 'weekly':
                key = date.strftime('%Y-%W')
            elif interval == 'monthly':
                key = date.strftime('%Y-%m')
            else:  # daily by default
                key = date.strftime('%Y-%m-%d')

            device = entry.get("platform", "Unknown Device")
            trends[key][device] += 1

        return trends

    @LoggerManager().log_function_entry
    @timeit("cache_device_usage_trends")
    def cache_device_usage_trends(self, interval='weekly'):
        """
        Caches device usage trends data.
        """
        cache_key = f"device_usage_trends_{interval}"
        self.logger.log_info(f"🔍 Fetching and caching device usage trends ({interval}).")

        return self.cache.get_or_generate_cache(
            cache_key,
            lambda: self.generate_device_usage_trends(interval),
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("generate_device_transcode_analysis")
    def generate_device_transcode_analysis(self):
        """
        Identifies devices most frequently requiring transcoding.
        """
        history_data = self.watch_history.get_history()
        if not history_data:
            return {}

        transcodes = Counter()
        for entry in history_data:
            if entry.get("transcode_decision", "direct_play").lower() == "transcode":
                device = entry.get("platform", "Unknown Device")
                transcodes[device] += 1

        return dict(transcodes.most_common())

    @LoggerManager().log_function_entry
    @timeit("cache_device_transcode_analysis")
    def cache_device_transcode_analysis(self):
        """
        Caches frequently transcoded device information.
        """
        self.logger.log_info("🔍 Fetching and caching device transcode analysis.")

        return self.cache.get_or_generate_cache(
            "device_transcode_analysis",
            self.generate_device_transcode_analysis,
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("detect_inactive_devices")
    def detect_inactive_devices(self, inactive_days=30):
        """
        Detects devices not used for a specified number of days.
        """
        cutoff_date = datetime.now() - timedelta(days=inactive_days)
        history_data = self.watch_history.get_history()
        if not history_data:
            return {}

        last_used = {}
        for entry in history_data:
            user = entry.get("user", "Unknown User")
            device = entry.get("platform", "Unknown Device")
            date = datetime.fromtimestamp(int(entry.get("date", 0)))

            key = (user, device)
            if key not in last_used or last_used[key] < date:
                last_used[key] = date

        inactive_devices = {
            f"{user} - {device}": last_date.strftime('%Y-%m-%d')
            for (user, device), last_date in last_used.items() if last_date < cutoff_date
        }

        return inactive_devices

    @LoggerManager().log_function_entry
    @timeit("cache_inactive_devices")
    def cache_inactive_devices(self, inactive_days=30):
        """
        Caches inactive device information.
        """
        cache_key = f"inactive_devices_{inactive_days}d"
        self.logger.log_info(f"🔍 Fetching and caching inactive devices ({inactive_days} days).")

        return self.cache.get_or_generate_cache(
            cache_key,
            lambda: self.detect_inactive_devices(inactive_days),
            instance="default"
        )

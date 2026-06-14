# managers/tautulli/transcode.py
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class TranscodeManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, cache, api, watch_history, metadata_manager):
        self.logger = logger
        self.cache = cache
        self.api = api
        self.watch_history = watch_history
        self.metadata_manager = metadata_manager

    @LoggerManager().log_function_entry
    @timeit("cache_transcode_history")
    def cache_transcode_history(self):
        """Caches transcode history pulled from Tautulli."""
        self.logger.log_info("🔍 Fetching and caching Tautulli transcode history.")
        try:
            return self.cache.get_or_generate_cache(
                "tautulli_transcode",
                lambda: self._fetch_transcode_history(),
                instance="default"
            )
        except Exception as e:
            self.logger.log_error(f"❌ Error caching transcode history: {e}")
            return {}

    @LoggerManager().log_function_entry
    @timeit("_fetch_transcode_history")
    def _fetch_transcode_history(self):
        """Fetches Tautulli history directly via API."""
        response = self.api._make_request("get_history", {"length": 1000})
        if not response or "records" not in response:
            self.logger.log_warning("⚠️ No valid transcode history data received from Tautulli.")
            return {}
        return response

    @LoggerManager().log_function_entry
    @timeit("get_transcode_frequency_by_user")
    def get_transcode_frequency_by_user(self, history_data):
        from collections import Counter
        counts = Counter()
        for entry in history_data:
            if entry.get("transcode_decision", "").lower() == "transcode":
                counts[entry.get("user", "Unknown User")] += 1
        return dict(counts.most_common())

    @LoggerManager().log_function_entry
    @timeit("get_transcode_frequency_by_device")
    def get_transcode_frequency_by_device(self, history_data):
        from collections import Counter
        counts = Counter()
        for entry in history_data:
            if entry.get("transcode_decision", "").lower() == "transcode":
                counts[entry.get("platform", "Unknown Device")] += 1
        return dict(counts.most_common())

    @LoggerManager().log_function_entry
    @timeit("get_transcode_frequency_by_resolution")
    def get_transcode_frequency_by_resolution(self, history_data):
        from collections import Counter
        counts = Counter()
        for entry in history_data:
            if entry.get("transcode_decision", "").lower() == "transcode":
                counts[entry.get("video_full_resolution", "Unknown")] += 1
        return dict(counts.most_common())

    @LoggerManager().log_function_entry
    @timeit("get_codec_transcode_analysis")
    def get_codec_transcode_analysis(self, history_data):
        from collections import Counter
        counts = Counter()
        for entry in history_data:
            if entry.get("transcode_decision", "").lower() == "transcode":
                key = f"{entry.get('video_codec', 'Unknown')} | {entry.get('audio_codec', 'Unknown')}"
                counts[key] += 1
        return dict(counts.most_common())

    @LoggerManager().log_function_entry
    @timeit("calculate_average_transcoded_bitrate")
    def calculate_average_transcoded_bitrate(self, history_data):
        bitrates = [
            entry.get("bitrate") for entry in history_data
            if entry.get("transcode_decision", "").lower() == "transcode" and entry.get("bitrate")
        ]
        return round(sum(bitrates) / len(bitrates), 2) if bitrates else 0

    @LoggerManager().log_function_entry
    @timeit("get_peak_transcoding_hours")
    def get_peak_transcoding_hours(self, history_data):
        from datetime import datetime
        from collections import Counter
        hours = Counter()
        for entry in history_data:
            if entry.get("transcode_decision", "").lower() == "transcode":
                try:
                    dt = datetime.fromtimestamp(entry.get("date"))
                    hours[dt.hour] += 1
                except Exception:
                    continue
        return hours.most_common()

    @LoggerManager().log_function_entry
    @timeit("get_average_transcode_throttle_duration")
    def get_average_transcode_throttle_duration(self, history_data):
        throttled = [
            entry.get("throttled_duration", 0)
            for entry in history_data if entry.get("throttled_duration")
        ]
        return round(sum(throttled) / len(throttled), 2) if throttled else 0

    @LoggerManager().log_function_entry
    @timeit("get_unusually_long_transcode_sessions")
    def get_unusually_long_transcode_sessions(self, history_data, threshold=0.9):
        long_sessions = []
        for entry in history_data:
            if entry.get("transcode_decision") != "transcode":
                continue
            metadata = self.metadata_manager.get_metadata(entry.get("rating_key")) if self.metadata_manager else None
            if metadata:
                media_duration = metadata.get("duration", 0)
                if media_duration and entry.get("duration", 0) / media_duration >= threshold:
                    long_sessions.append(entry)
        return long_sessions

    @LoggerManager().log_function_entry
    @timeit("generate_device_transcode_analysis")
    def generate_device_transcode_analysis(self, history_data):
        from collections import Counter
        counts = Counter()
        for entry in history_data:
            if entry.get("transcode_decision", "").lower() == "transcode":
                counts[entry.get("platform", "Unknown Device")] += 1
        return dict(counts.most_common())

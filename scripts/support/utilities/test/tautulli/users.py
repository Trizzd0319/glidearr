import os
from datetime import datetime

import pandas as pd
from prettytable import PrettyTable

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.services.tautulli.metadata import MetadataManager
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class UserPatternsManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger: LoggerManager, cache: GlobalCacheManager, watch_history, metadata_manager: MetadataManager):
        self.logger = logger
        self.cache = cache
        self.watch_history = watch_history
        self.metadata = metadata_manager
        self._history_loaded = False
        self._history_cache = []

        history_response = self.watch_history.get_history()
        self._history_cache = history_response.get("data", []) if isinstance(history_response, dict) else []
        self._history_loaded = True

    @LoggerManager().log_function_entry
    @timeit("log_user_module_summary")
    def log_user_module_summary(self):
        """Displays a table of all user behavior methods with brief descriptions."""

        summary_definitions = {
            "cache_user_viewing_patterns": "Analyze total watches, resolution, codec, container preferences",
            "cache_binge_watching_patterns": "Detect binge sessions and flag for future pre-monitoring",
            "cache_time_of_day_patterns": "Track hourly viewing activity per user",
            "cache_genre_affinity_scores": "Score favorite genres per user from watched titles",
            "cache_user_reengagement_predictions": "Predict when users might return based on gaps",
            "cache_quality_profile_recommendations": "Suggest Sonarr profiles based on playback compatibility",
            "cache_cross_device_continuity": "Track platform switching patterns mid-session",
            "cache_weekday_weekend_patterns": "Classify weekday vs weekend watch habits",
            "cache_pilot_episode_dropoffs": "Detect which users drop off after watching only pilot episodes",
            "cache_media_consumption_velocity": "Measure how quickly new content is consumed after release"
        }

        table = PrettyTable()
        table.field_names = ["Method", "Description"]
        table.align["Method"] = "l"
        table.align["Description"] = "l"

        for method_name, desc in summary_definitions.items():
            if hasattr(self, method_name):
                table.add_row([method_name, desc])

        self.logger.log_info("📊 User Behavior Module Overview:\n" + str(table))

    @LoggerManager().log_function_entry
    @timeit("_log_expected_tautulli_url")
    def _log_expected_tautulli_url(self, cmd="get_history", params=None):
        if hasattr(self.watch_history, "api_tautulli"):
            api = self.watch_history.api_tautulli
            if api is None:
                self.logger.log_warning("⚠️ watch_history.api_tautulli is None!")
                return
            if hasattr(api, "base_url") and hasattr(api, "api_key"):
                from urllib.parse import urlencode
                query = {
                    "apikey": api.api_key,
                    "cmd": cmd,
                    **(params or {})
                }
                url = f"{api.base_url}/api/v2?{urlencode(query)}"
                # self.logger.log_info(f"🌐 [PREVIEW] Tautulli API URL: {url}")
            else:
                self.logger.log_warning("⚠️ api_tautulli missing 'base_url' or 'api_key'")
        else:
            self.logger.log_warning("⚠️ self.watch_history has no attribute 'api_tautulli'")

    @LoggerManager().log_function_entry
    @timeit("cache_user_viewing_patterns")
    def cache_user_viewing_patterns(self):
        """
        Caches user viewing patterns including preferred genres, resolutions, and total watched counts.
        This cache will not expire — assumed to be refreshed once per session at startup.
        """
        self.logger.log_info("🔍 Generating user viewing patterns (non-expiring).")

        key = CacheKeyPaths.tautulli.USER_VIEWING_PATTERNS
        instance = "default"

        history_response = self.watch_history.get_history()

        records = []
        if isinstance(history_response, dict):
            if isinstance(history_response.get("data"), list):
                records = history_response["data"]
            elif isinstance(history_response.get("data"), dict) and "data" in history_response["data"]:
                records = history_response["data"]["data"]

        if not isinstance(records, list):
            self.logger.log_error(f"❌ Invalid history structure: Expected list, got {type(records).__name__}")
            records = []

        user_viewing_cache = {}
        missing_keys = []

        for entry in records:
            user = entry.get("user", "Unknown User")
            user_id = entry.get("user_id", "Unknown")
            user_thumb = entry.get("user_thumb", "")
            friendly_name = entry.get("friendly_name", user)

            rating_key = entry.get("rating_key")
            metadata = self.metadata.get_metadata(rating_key) if rating_key else {}

            if not metadata:
                missing_keys.append(rating_key)
                continue

            genres = metadata.get("genres", ["Unknown"])
            media_info = metadata.get("media_info", [{}])[0] if metadata else {}

            resolution = media_info.get("video_full_resolution", "Unknown")
            audio_codec = media_info.get("audio_codec", "Unknown")
            video_codec = media_info.get("video_codec", "Unknown")
            container = media_info.get("container", "Unknown")
            studio = metadata.get("studio", "Unknown")
            rating = metadata.get("content_rating", "Unknown")
            year = metadata.get("grandparent_year", "Unknown")

            transcode = entry.get("transcode_decision", "Unknown")
            platform = entry.get("platform", "Unknown")
            product = entry.get("product", "Unknown")
            player = entry.get("player", "Unknown")
            ip = entry.get("ip_address", "Unknown")
            location = entry.get("location", "Unknown")
            machine_id = entry.get("machine_id", "Unknown")
            relayed = entry.get("relayed", 0)
            secure = entry.get("secure", 0)
            live = entry.get("live", 0)

            u = user_viewing_cache.setdefault(user, {
                "user_id": user_id,
                "user_thumb": user_thumb,
                "alias": friendly_name,
                "total_watched": 0,
                "genres": set(),
                "resolutions": {},
                "audio_codecs": {},
                "video_codecs": {},
                "containers": {},
                "transcode_decisions": {},
                "platforms": {},
                "products": {},
                "studios": {},
                "ratings": {},
                "release_years": {},
                "players": {},
                "ip_addresses": {},
                "locations": {},
                "relayed_counts": {"relayed": 0, "secure": 0},
                "live_streams": 0,
                "machine_ids": set()
            })

            u["total_watched"] += 1
            u["genres"].update(genres)

            for field, value in {
                "resolutions": resolution,
                "audio_codecs": audio_codec,
                "video_codecs": video_codec,
                "containers": container,
                "transcode_decisions": transcode,
                "platforms": platform,
                "products": product,
                "studios": studio,
                "ratings": rating,
                "release_years": year,
                "players": player,
                "ip_addresses": ip,
                "locations": location,
            }.items():
                u[field][value] = u[field].get(value, 0) + 1

            if relayed:
                u["relayed_counts"]["relayed"] += 1
            if secure:
                u["relayed_counts"]["secure"] += 1
            if live:
                u["live_streams"] += 1

            u["machine_ids"].add(machine_id)

        # 🧠 Post-processing
        for user, data in user_viewing_cache.items():
            data["genres"] = list(data["genres"])
            data["machine_ids"] = list(data["machine_ids"])
            for category in [
                "resolutions", "audio_codecs", "video_codecs",
                "containers", "transcode_decisions",
                "platforms", "products", "studios", "ratings", "release_years",
                "players", "ip_addresses", "locations"
            ]:
                if data[category]:
                    preferred = max(data[category], key=data[category].get)
                    data[f"preferred_{category.rstrip('s')}"] = preferred

        self.cache.save_to_cache(
            key=key,
            data=user_viewing_cache,
            instance=instance
        )

        full_key = f"{key}.{instance}"
        path = self.cache._get_cache_path(full_key)
        if not path.endswith(".gz") and os.path.exists(path + ".gz"):
            path += ".gz"

        self.logger.log_info(f"📦 Viewing pattern cache saved to: {os.path.abspath(path)}")
        self.logger.log_info(f"✅ Extended viewing patterns generated for {len(user_viewing_cache)} users.")

        # 📉 Summary of missing metadata
        if missing_keys:
            from prettytable import PrettyTable
            table = PrettyTable()
            table.field_names = ["Missing Metadata Rating Keys"]
            for key in sorted(set(missing_keys)):
                table.add_row([key])
            self.logger.log_info("📉 Summary of missing metadata:\n" + str(table))
        else:
            self.logger.log_info("✅ No missing metadata entries detected.")

        return user_viewing_cache

    @LoggerManager().log_function_entry
    @timeit("_generate_user_viewing_patterns")
    def _generate_user_viewing_patterns(self):
        assert not hasattr(self.cache, 'load_cache'), "🚨 CacheManager should not call load_cache"

        self._log_expected_tautulli_url(cmd="get_history", params={"length": 1000})
        history_data = self.watch_history.get_history()

        # 🛡️ Defensive check
        if not isinstance(history_data, list):
            self.logger.log_error(f"❌ Invalid history structure: Expected list, got {type(history_data).__name__}")
            return {}

        if not history_data:
            self.logger.log_warning("⚠️ No user viewing history retrieved.")
            return {}

        user_viewing_cache = {}

        for entry in history_data:
            user = entry.get("user", "Unknown User")
            rating_key = entry.get("rating_key")
            if not rating_key:
                continue

            metadata = self.metadata.get_metadata(rating_key) or {}

            genres = metadata.get("genres", ["Unknown"])
            media_info = metadata.get("media_info", [{}])[0]

            resolution = media_info.get("video_full_resolution", "Unknown")
            audio_codec = media_info.get("audio_codec", "Unknown")
            video_codec = media_info.get("video_codec", "Unknown")
            container = media_info.get("container", "Unknown")
            studio = metadata.get("studio", "Unknown")
            rating = metadata.get("content_rating", "Unknown")
            year = metadata.get("grandparent_year", "Unknown")

            transcode = entry.get("transcode_decision", "Unknown")
            platform = entry.get("platform", "Unknown")
            product = entry.get("product", "Unknown")

            u = user_viewing_cache.setdefault(user, {
                "total_watched": 0,
                "genres": set(),
                "resolutions": {},
                "audio_codecs": {},
                "video_codecs": {},
                "containers": {},
                "transcode_decisions": {},
                "platforms": {},
                "products": {},
                "studios": {},
                "ratings": {},
                "release_years": {}
            })

            u["total_watched"] += 1
            u["genres"].update(genres)

            for field, value in {
                "resolutions": resolution,
                "audio_codecs": audio_codec,
                "video_codecs": video_codec,
                "containers": container,
                "transcode_decisions": transcode,
                "platforms": platform,
                "products": product,
                "studios": studio,
                "ratings": rating,
                "release_years": year
            }.items():
                u[field][value] = u[field].get(value, 0) + 1

        # 🧠 Post-processing: Convert sets to lists and select preferred fields
        for user, data in user_viewing_cache.items():
            data["genres"] = list(data["genres"])
            for category in [
                "resolutions", "audio_codecs", "video_codecs",
                "containers", "transcode_decisions",
                "platforms", "products", "studios", "ratings", "release_years"
            ]:
                if data[category]:
                    preferred = max(data[category], key=data[category].get)
                    data[f"preferred_{category.rstrip('s')}"] = preferred

        self.logger.log_info(f"✅ Extended viewing patterns generated for {len(user_viewing_cache)} users.")
        return user_viewing_cache

    @LoggerManager().log_function_entry
    @timeit("cache_binge_watching_patterns")
    def cache_binge_watching_patterns(self):
        """
        Caches binge-watching behavior to pre-monitor episodes for heavy viewers.
        """

        @LoggerManager().log_function_entry
        @timeit("_reverse_lookup_rating_key")
        def _reverse_lookup_rating_key(self, info: dict) -> str | None:
            """
            Attempts to find the rating_key for an episode using show/season/episode numbers.

            Args:
                info (dict): Must contain 'grandparent_title', 'parent_media_index', and 'media_index'.

            Returns:
                str | None: Matching rating_key if found.
            """

            @LoggerManager().log_function_entry
            @timeit("generate_history")
            def generate_history():
                self.logger.log_info("📦 Cache miss — regenerating watch history for reverse lookup.")
                return self.api._make_request("get_history", {"length": 1000}).get("response", {}).get("data", []) or []

            history = self.global_cache.get_or_generate_cache(
                CacheKeyPaths.tautulli.watch_history,
                generate_history,
                instance="default"
            )

            title = info.get("grandparent_title")
            season = int(info.get("parent_media_index", -1))
            episode = int(info.get("media_index", -1))

            for entry in history:
                if (
                        entry.get("grandparent_title") == title and
                        int(entry.get("parent_media_index", -2)) == season and
                        int(entry.get("media_index", -3)) == episode
                ):
                    return entry.get("rating_key")

            return None

    @LoggerManager().log_function_entry
    @timeit("cache_time_of_day_patterns")
    def cache_time_of_day_patterns(self):
        """
        Caches user peak viewing hours to optimize pre-download timing.
        """

        @LoggerManager().log_function_entry
        @timeit("generate_time_of_day_patterns")
        def generate_time_of_day_patterns():
            history_data = self.watch_history.get_history()
            if not history_data:
                self.logger.log_warning("⚠️ No history data found. Cannot analyze viewing times.")
                return {}

            viewing_times = {}
            for entry in history_data:
                user = entry.get("user", "Unknown User")
                timestamp = entry.get("date", 0)

                if user not in viewing_times:
                    viewing_times[user] = [0] * 24

                hour_of_day = pd.to_datetime(timestamp, unit='s').hour
                viewing_times[user][hour_of_day] += 1

            self.logger.log_info("✅ User viewing times generated successfully.")
            return viewing_times

        self.logger.log_info("🔍 Fetching and caching user viewing times.")
        return self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.TIME_OF_DAY_PATTERNS,
            generate_time_of_day_patterns,
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("generate_genre_affinity_scores")
    def generate_genre_affinity_scores(self):
        history = self.watch_history.get_history()

        genre_count_by_user = {}
        total_genre_counts = {}

        for entry in history:
            user = entry.get("user")
            rating_key = entry.get("rating_key")

            if not rating_key:
                self.logger.log_warning("⚠️ No rating_key found, skipping entry.")
                continue

            # Fetch metadata_manager using rating_key
            metadata_manager = self.metadata.get_metadata(rating_key)

            if not metadata_manager:
                self.logger.log_warning(f"⚠️ No metadata found for rating_key: {rating_key}, skipping entry.")
                continue

            genres = metadata_manager.get("genres", [])

            if not genres:
                self.logger.log_warning(f"⚠️ No genres found for rating_key: {rating_key}, skipping entry.")
                continue

            if user not in genre_count_by_user:
                genre_count_by_user[user] = {}

            for genre in genres:
                genre_count_by_user[user][genre] = genre_count_by_user[user].get(genre, 0) + 1
                total_genre_counts[user] = total_genre_counts.get(user, 0) + 1

        # Calculate affinity scores
        genre_affinity_scores = {}
        for user, genre_counts in genre_count_by_user.items():
            total = total_genre_counts[user]
            genre_affinity_scores[user] = {
                genre: round(count / total, 2) for genre, count in genre_counts.items()
            }

        return genre_affinity_scores

    @LoggerManager().log_function_entry
    @timeit("cache_genre_affinity_scores")
    def cache_genre_affinity_scores(self):
        genre_affinity = self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.GENRE_AFFINITY_SCORES,
            self.generate_genre_affinity_scores,
            instance="default"
        )
        return genre_affinity

    @LoggerManager().log_function_entry
    @timeit("cache_user_reengagement_predictions")
    def cache_user_reengagement_predictions(self):
        """Predict when users might return after inactivity."""

        @LoggerManager().log_function_entry
        @timeit("generate_reengagement")
        def generate_reengagement():
            history_data = self.watch_history.get_history()
            if not history_data:
                self.logger.log_warning("⚠️ No history data found.")
                return {}

            user_intervals = {}
            for entry in history_data:
                user = entry.get("user", "Unknown User")
                timestamp = entry.get("date", 0)

                user_intervals.setdefault(user, []).append(timestamp)

            reengagement_predictions = {}
            for user, timestamps in user_intervals.items():
                sorted_timestamps = sorted(timestamps)
                intervals = [j - i for i, j in zip(sorted_timestamps[:-1], sorted_timestamps[1:])]
                if intervals:
                    avg_interval = sum(intervals) / len(intervals)
                    last_watch = sorted_timestamps[-1]
                    predicted_next = last_watch + avg_interval
                    reengagement_predictions[user] = predicted_next

            self.logger.log_info("✅ User re-engagement predictions generated successfully.")
            return reengagement_predictions

        self.logger.log_info("🔍 Fetching and caching user re-engagement predictions.")
        return self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.USER_REENGAGEMENT_PREDICTIONS,
            generate_reengagement,
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("cache_quality_profile_recommendations")
    def cache_quality_profile_recommendations(self):
        """Recommend Sonarr quality profiles per user based on preferences and sensitivity."""
        user_patterns = self.cache_user_viewing_patterns()

        # ✅ Corrected: get_or_generate_cache for preferred_devices
        device_prefs = self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.PREFERRED_DEVICES,
            lambda: {},  # fallback
            instance="default"
        )

        recommendations = {}
        for user, patterns in user_patterns.items():
            preferred_res = patterns.get("preferred_resolution", "Unknown")
            preferred_device = device_prefs.get(user, "Unknown Device")

            recommendations[user] = {
                "quality_profile": preferred_res,
                "preferred_device": preferred_device
            }

        self.logger.log_info("✅ Quality profile recommendations cached successfully.")
        self.cache.save_to_cache(CacheKeyPaths.tautulli.QUALITY_PROFILE_RECOMMENDATIONS, recommendations)
        return recommendations

    @LoggerManager().log_function_entry
    @timeit("cache_cross_device_continuity")
    def cache_cross_device_continuity(self):
        """Track device switching during sessions."""

        @LoggerManager().log_function_entry
        @timeit("generate_continuity")
        def generate_continuity():
            history_data = self.watch_history.get_history()
            continuity = {}

            for entry in sorted(history_data, key=lambda x: (x["user"], x["date"])):
                user = entry["user"]
                device = entry["platform"]
                continuity.setdefault(user, []).append(device)

            device_switches = {}
            for user, devices in continuity.items():
                switches = sum(devices[i] != devices[i + 1] for i in range(len(devices) - 1))
                device_switches[user] = switches

            self.logger.log_info("✅ Cross-device continuity tracked.")
            return device_switches

        return self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.CROSS_DEVICE_CONTINUITY,
            generate_continuity,
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("cache_weekday_weekend_patterns")
    def cache_weekday_weekend_patterns(self):
        """Distinguish weekday vs weekend viewing habits."""

        @LoggerManager().log_function_entry
        @timeit("generate_weekday_weekend")
        def generate_weekday_weekend():
            history_data = self.watch_history.get_history()
            weekday_weekend = {}

            for entry in history_data:
                user = entry["user"]
                date = datetime.utcfromtimestamp(entry["date"])
                is_weekend = date.weekday() >= 5

                weekday_weekend.setdefault(user, {"weekday": 0, "weekend": 0})
                key = "weekend" if is_weekend else "weekday"
                weekday_weekend[user][key] += 1

            self.logger.log_info("✅ Weekday/weekend viewing patterns cached.")
            return weekday_weekend

        return self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.WEEKDAY_WEEKEND_PATTERNS,
            generate_weekday_weekend,
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("cache_pilot_episode_dropoffs")
    def cache_pilot_episode_dropoffs(self):
        """Detect pilot episode drop-offs to refine recommendations."""

        @LoggerManager().log_function_entry
        @timeit("generate_pilot_dropoffs")
        def generate_pilot_dropoffs():
            history_data = self.watch_history.get_history()
            pilot_dropoffs = {}

            for entry in history_data:
                user = entry.get("user", "Unknown User")
                series = entry.get("grandparent_title", "Unknown Series")

                try:
                    episode = int(entry.get("media_index", 0) or 0)
                except ValueError:
                    episode = 0  # Handle malformed values gracefully

                if episode == 1:
                    key = f"{user}.{series}"
                    pilot_dropoffs.setdefault(key, {"watched": False, "continued": False})["watched"] = True
                elif episode > 1:
                    key = f"{user}.{series}"
                    if key in pilot_dropoffs:
                        pilot_dropoffs[key]["continued"] = True

            dropoff_series = {k: v for k, v in pilot_dropoffs.items() if v["watched"] and not v["continued"]}
            self.logger.log_info("✅ Pilot episode drop-offs identified.")
            return dropoff_series

        return self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.PILOT_EPISODE_DROPOFFS,
            generate_pilot_dropoffs,
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("cache_media_consumption_velocity")
    def cache_media_consumption_velocity(self):
        """Track speed at which users consume new episodes."""

        @LoggerManager().log_function_entry
        @timeit("generate_velocity")
        def generate_velocity():
            history_data = self.watch_history.get_history()
            velocity = {}

            for entry in history_data:
                user = entry.get("user", "Unknown User")
                series = entry.get("grandparent_title", "Unknown Series")
                release_str = entry.get("originally_available_at")
                watch_time = entry.get("date", 0)

                if release_str:
                    try:
                        release_time = int(datetime.strptime(release_str, "%Y-%m-%d").timestamp())
                    except ValueError:
                        release_time = watch_time
                else:
                    release_time = watch_time

                key = f"{user}.{series}"
                velocity.setdefault(key, []).append(watch_time - release_time)

            avg_velocity = {k: sum(v) / len(v) for k, v in velocity.items() if v}
            self.logger.log_info("✅ Media consumption velocity calculated.")
            return avg_velocity

        return self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.MEDIA_CONSUMPTION_VELOCITY,
            generate_velocity,
            instance="default"
        )

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class MLTranscodeAnalyzer:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, cache, config, transcode_history):
        self.logger = logger
        self.cache = cache
        self.config = config
        self.history = transcode_history

    @LoggerManager().log_function_entry
    @timeit("get_transcode_penalty")
    def get_transcode_penalty(self, series_title, profile_name):
        key = f"{series_title.lower()}::{profile_name.lower()}"
        penalty_data = self.cache.get_or_generate_cache(
            "quality/profile/penalties",
            generator=lambda: self._generate_penalty_data(),
            expiration_time=24
        )

        if key in penalty_data:
            score = penalty_data[key]
            self.logger.log_debug(f"⚖️ Penalty for {series_title} with {profile_name}: {score}")
            return score

        self.logger.log_debug(f"🟡 No penalty data for {series_title} with {profile_name}. Defaulting to 0.")
        return 0

    @LoggerManager().log_function_entry
    @timeit("_generate_penalty_data")
    def _generate_penalty_data(self):
        data = self.history.get_transcode_history()
        penalty_scores = {}

        for entry in data:
            series = entry.get("series_title", "").lower()
            profile = entry.get("quality_profile", "").lower()
            transcode_reason = entry.get("transcode_decision", "")
            score = 1 if "transcode" in transcode_reason.lower() else 0

            if series and profile:
                key = f"{series}::{profile}"
                penalty_scores[key] = penalty_scores.get(key, 0) + score

        for key, value in penalty_scores.items():
            penalty_scores[key] = round(min(value, 5))

        self.logger.log_info(f"🧠 Cached {len(penalty_scores)} transcode penalty records.")
        return penalty_scores

    @LoggerManager().log_function_entry
    @timeit("get_codec_failure_rates")
    def get_codec_failure_rates(self):
        data = self.history.get_transcode_history()
        codec_totals = {}
        codec_transcodes = {}

        for entry in data:
            codec = entry.get("video_codec", "").lower()
            is_transcode = "transcode" in entry.get("video_decision", "").lower()
            if codec:
                codec_totals[codec] = codec_totals.get(codec, 0) + 1
                if is_transcode:
                    codec_transcodes[codec] = codec_transcodes.get(codec, 0) + 1

        failure_rates = {
            codec: round(codec_transcodes.get(codec, 0) / total, 2)
            for codec, total in codec_totals.items()
        }
        return failure_rates

    @LoggerManager().log_function_entry
    @timeit("get_transcode_score_by_user")
    def get_transcode_score_by_user(self):
        data = self.history.get_transcode_history()
        user_codec_stats = {}

        for entry in data:
            user = entry.get("user", "unknown")
            codec = entry.get("video_codec", "").lower()
            is_transcode = "transcode" in entry.get("video_decision", "").lower()
            if user and codec:
                user_codec_stats.setdefault(user, {}).setdefault(codec, {"total": 0, "transcoded": 0})
                user_codec_stats[user][codec]["total"] += 1
                if is_transcode:
                    user_codec_stats[user][codec]["transcoded"] += 1

        result = {}
        for user, codecs in user_codec_stats.items():
            result[user] = {
                codec: round(stats["transcoded"] / stats["total"], 2)
                for codec, stats in codecs.items()
            }
        return result

    @LoggerManager().log_function_entry
    @timeit("get_format_preference_matrix")
    def get_format_preference_matrix(self):
        data = self.history.get_transcode_history()
        format_scores = {}

        for entry in data:
            quality = entry.get("quality_profile", "").lower()
            if not quality:
                continue
            is_transcode = "transcode" in entry.get("video_decision", "").lower()
            format_scores.setdefault(quality, {"total": 0, "transcoded": 0})
            format_scores[quality]["total"] += 1
            if is_transcode:
                format_scores[quality]["transcoded"] += 1

        matrix = {
            fmt: round(stats["transcoded"] / stats["total"], 2)
            for fmt, stats in format_scores.items()
        }
        return matrix

    @LoggerManager().log_function_entry
    @timeit("get_problematic_series_list")
    def get_problematic_series_list(self, threshold=0.8):
        data = self.history.get_transcode_history()
        series_stats = {}

        for entry in data:
            series = entry.get("series_title", "").lower()
            is_transcode = "transcode" in entry.get("video_decision", "").lower()
            if series:
                series_stats.setdefault(series, {"total": 0, "transcoded": 0})
                series_stats[series]["total"] += 1
                if is_transcode:
                    series_stats[series]["transcoded"] += 1

        return [
            series for series, stats in series_stats.items()
            if stats["transcoded"] / stats["total"] >= threshold
        ]

    @LoggerManager().log_function_entry
    @timeit("get_common_transcode_causes")
    def get_common_transcode_causes(self):
        data = self.history.get_transcode_history()
        reasons = {}

        for entry in data:
            reason = entry.get("transcode_decision", "").strip()
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1

        return dict(sorted(reasons.items(), key=lambda item: item[1], reverse=True))

    @LoggerManager().log_function_entry
    @timeit("get_recent_transcode_activity")
    def get_recent_transcode_activity(self, days=7):
        from datetime import datetime, timedelta

        data = self.history.get_transcode_history()
        cutoff = datetime.utcnow() - timedelta(days=days)
        recent = []

        for entry in data:
            date_str = entry.get("date") or entry.get("started", "")
            try:
                entry_time = datetime.fromisoformat(date_str)
                if entry_time > cutoff and "transcode" in entry.get("video_decision", "").lower():
                    recent.append(entry)
            except Exception:
                continue

        return recent

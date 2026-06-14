from datetime import datetime, timezone

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities import size_model
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrQualityFileSizesManager(BaseManager, ComponentManagerMixin):
    # Library-calibrated MiB/min table (shared single source of truth).
    QUALITY_MB_PER_MIN = size_model.CALIBRATED_MB_PER_MIN

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrQuality"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(parent, "sonarr_cache", None)
        self.global_cache = kwargs.get("global_cache") or getattr(parent, "global_cache", None)
        self.key_builder = kwargs.get("key_builder") or getattr(parent, "key_builder", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧮 Initialized {class_name} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("compare_file_sizes")
    def compare_file_sizes(self, rating_key, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        actual_size = self.sonarr_api.get_episode_file_size(rating_key, resolved_instance)
        expected_size = self.estimate_expected_size(resolved_instance, rating_key)

        if actual_size == 0 or actual_size < expected_size * 0.6:
            return "upgrade"
        elif actual_size > expected_size * 1.4:
            return "downgrade"
        return "keep"

    @LoggerManager().log_function_entry
    @timeit("get_expected_file_size")
    def get_expected_file_size(self, instance, quality_profile_id, runtime):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        profile_name = self._get_profile_name(resolved_instance, quality_profile_id)
        mb_per_min = size_model.mb_per_min(profile_name)
        size_bytes = (mb_per_min * runtime) * 1024 ** 2
        return size_bytes

    @LoggerManager().log_function_entry
    @timeit("get_median_file_size")
    def get_median_file_size(self, instance, quality_profile_id, runtime):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        self.logger.log_info(f"📐 Estimating median file size for profile {quality_profile_id} in {resolved_instance}")
        episodes = self.sonarr_api._make_request(resolved_instance, f"episode?seriesId={quality_profile_id}")

        if not episodes:
            self.logger.log_warning("⚠️ No episode data. Falling back to estimate.")
            return self.get_expected_file_size(resolved_instance, quality_profile_id, runtime)

        file_sizes = [ep["size"] for ep in episodes if isinstance(ep.get("size", 0), (int, float)) and ep["size"] > 0]

        if not file_sizes:
            self.logger.log_info("⚠️ No file sizes found. Using fallback estimate.")
            return self.get_expected_file_size(resolved_instance, quality_profile_id, runtime)

        median = sorted(file_sizes)[len(file_sizes) // 2]
        self.logger.log_info(f"📦 Median size: {median / (1024 ** 2):.2f} MB")
        return median

    @LoggerManager().log_function_entry
    @timeit("get_predefined_file_size")
    def get_predefined_file_size(self, instance, quality_profile_id, runtime):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        profile_name = self._get_profile_name(resolved_instance, quality_profile_id)

        mb_per_min = size_model.mb_per_min(profile_name)
        size = mb_per_min * runtime * 1024 ** 2
        self.logger.log_info(
            f"📐 Estimated size for '{profile_name}' ({runtime} min) "
            f"@ {mb_per_min:.0f} MiB/min: {size / (1024 ** 2):.2f} MB"
        )
        return size

    @LoggerManager().log_function_entry
    @timeit("_get_profile_name")
    def _get_profile_name(self, instance, profile_id):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        profiles = self.sonarr_api._make_request(resolved_instance, "qualityprofile")
        for profile in profiles or []:
            if profile["id"] == profile_id:
                return profile.get("name", "Unknown")
        return "Unknown"

    @LoggerManager().log_function_entry
    @timeit("generate_quality_flags")
    def generate_quality_flags(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        episodes = self.sonarr_api._make_request(resolved_instance, "episodes", fallback=[])
        flags = {}
        for ep in episodes:
            issues = []
            if ep.get("hasFile") and ((ep.get("quality") or {}).get("quality") or {}).get("name") not in ["HD-1080p", "WEB-DL-1080p"]:
                issues.append("nonstandard_quality")
            if ep.get("monitored") and not ep.get("hasFile"):
                issues.append("missing_file")
            if issues:
                flags[ep["id"]] = {"issues": issues, "count": len(issues)}
        return flags

    @LoggerManager().log_function_entry
    @timeit("run_quality_definition_data_pull")
    def run_quality_definition_data_pull(self, instance):
        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())

        for instance_name, arrapi_client in all_instances:
            instance_config = (self.config.get("sonarr_instances") or {}).get(instance_name)
            if not instance_config:
                self.logger.log_error(f"❌ No configuration found for instance '{instance_name}'")
                continue

            api_base = instance_config['base_url']
            api_key = instance_config['api']
            url = f"{api_base}/api/v3/qualitydefinition"
            params = {}

            try:
                response = requests.get(url, params=params, headers={"X-Api-Key": api_key})
                response.raise_for_status()
                quality_defs = response.json()
            except Exception as e:
                self.logger.log_error(f"❌ Failed to fetch quality definitions for '{instance_name}': {e}")
                continue

            cache_key = self.key_builder.format_cache_key("sonarr", instance_name, "quality_definitions")
            updated_cache = {
                "qualityDefinitions": quality_defs,
                "meta": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "instance": instance_name,
                    "count": len(quality_defs)
                }
            }

            self.global_cache.set_with_pretty_output(cache_key, updated_cache)
            self.logger.log_info(
                f"✅ Quality definitions cached for {instance_name} ({len(quality_defs)} entries)"
            )

    @LoggerManager().log_function_entry
    @timeit("compare_file_sizes")
    def compare_file_sizes(self, rating_key, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        actual_size = self.sonarr_api.get_episode_file_size(rating_key, resolved_instance)
        expected_size = self.estimate_expected_size(resolved_instance, rating_key)

        if actual_size == 0 or actual_size < expected_size * 0.6:
            return "upgrade"
        elif actual_size > expected_size * 1.4:
            return "downgrade"
        return "keep"

    @LoggerManager().log_function_entry
    @timeit("compare_codecs")
    def compare_codecs(self, episode_data, expected_codec="x265"):
        actual_codec = (episode_data.get("mediaInfo") or {}).get("videoCodec", "").lower()
        if not actual_codec:
            return "unknown"
        if actual_codec != expected_codec.lower():
            return f"mismatch ({actual_codec})"
        return "match"

    @LoggerManager().log_function_entry
    @timeit("flag_size_anomalies")
    def flag_size_anomalies(self, instance, series_id, threshold_percent=50):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        episodes = self.sonarr_api._make_request(resolved_instance, f"episode?seriesId={series_id}") or []

        anomalies = []
        for ep in episodes:
            runtime = ep.get("runtime", 45)
            quality_profile_id = ((ep.get("quality") or {}).get("quality") or {}).get("id")
            if not quality_profile_id or not ep.get("hasFile"):
                continue

            expected = self.get_expected_file_size(instance, quality_profile_id, runtime)
            actual = ep.get("size", 0)
            if not actual:
                continue

            ratio = actual / expected if expected > 0 else 0
            if ratio < 1 - (threshold_percent / 100):
                anomalies.append({"episodeId": ep["id"], "type": "too_small", "ratio": round(ratio, 2)})
            elif ratio > 1 + (threshold_percent / 100):
                anomalies.append({"episodeId": ep["id"], "type": "too_large", "ratio": round(ratio, 2)})

        return anomalies

    @LoggerManager().log_function_entry
    @timeit("summarize_quality_distribution")
    def summarize_quality_distribution(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        episodes = self.sonarr_api._make_request(resolved_instance, "episode") or []

        stats = {}
        for ep in episodes:
            quality = ((ep.get("quality") or {}).get("quality") or {}).get("name", "Unknown")
            stats[quality] = stats.get(quality, 0) + 1

        sorted_stats = dict(sorted(stats.items(), key=lambda x: x[1], reverse=True))
        self.logger.log_info(f"📊 Quality distribution for {instance}: {sorted_stats}")
        return sorted_stats

    @LoggerManager().log_function_entry
    @timeit("get_average_size_by_quality")
    def get_average_size_by_quality(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        episodes = self.sonarr_api._make_request(resolved_instance, "episode") or []

        quality_sizes = {}
        for ep in episodes:
            quality = ((ep.get("quality") or {}).get("quality") or {}).get("name", "Unknown")
            size = ep.get("size", 0)
            if size:
                quality_sizes.setdefault(quality, []).append(size)

        averages = {
            quality: sum(sizes) / len(sizes)
            for quality, sizes in quality_sizes.items()
        }
        return {k: round(v / (1024 ** 2), 2) for k, v in averages.items()}  # Return in MB

    @LoggerManager().log_function_entry
    @timeit("get_flagged_upgrades_or_downgrades")
    def get_flagged_upgrades_or_downgrades(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        series_list = self.sonarr_api._make_request(resolved_instance, "series") or []

        review = {"upgrade": [], "downgrade": [], "keep": []}

        for series in series_list:
            sid = series.get("id")
            eps = self.sonarr_api._make_request(resolved_instance, f"episode?seriesId={sid}") or []
            for ep in eps:
                rating_key = ep.get("episodeFileId")
                if not rating_key:
                    continue
                action = self.compare_file_sizes(rating_key, instance)
                review[action].append(ep.get("id"))

        return {k: v for k, v in review.items() if v}

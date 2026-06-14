# sonarr/episodes/file.py

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from arrapi import SonarrAPI

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.progress.tqdm_wrapper import tqdm


class SonarrEpisodesFileManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodes"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError("❌ SonarrEpisodesFileManager could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def resolve_instance(self, instance):
        return self.instance_manager.resolve_instance(instance)

    @LoggerManager().log_function_entry
    @timeit("get_episode_file_size")
    def get_episode_file_size(self, episode_id, instance):
        resolved = self.resolve_instance(instance)
        files = self.sonarr_api._make_request(resolved, f"episodefile?episodeId={episode_id}")
        return sum(file.get("size", 0) for file in files if isinstance(file, dict))

    @LoggerManager().log_function_entry
    @timeit("get_episode_files_for_series")
    def get_episode_files_for_series(self, series_id, instance):
        resolved = self.resolve_instance(instance)
        return self.sonarr_api._make_request(resolved, f"episodefile?seriesId={series_id}") or []

    @LoggerManager().log_function_entry
    @timeit("get_episode_file_metadata")
    def get_episode_file_metadata(self, instance):
        resolved = self.resolve_instance(instance)
        return self.sonarr_api._make_request(resolved, "episodefile", fallback=[])

    @LoggerManager().log_function_entry
    @timeit("get_episode_format_data")
    def get_episode_format_data(self, instance):
        files = self.get_episode_file_metadata(instance)
        return [
            {
                "id": f.get("id"),
                "quality": ((f.get("quality") or {}).get("quality") or {}).get("name"),
                "codec": (f.get("mediaInfo") or {}).get("videoCodec"),
                "audio": (f.get("mediaInfo") or {}).get("audioCodec")
            } for f in files
        ]

    @LoggerManager().log_function_entry
    @timeit("get_codec_summary")
    def get_codec_summary(self, instance):
        data = self.get_episode_format_data(instance)
        codec_map = {}
        for item in data:
            key = (item["quality"], item["codec"], item["audio"])
            codec_map[key] = codec_map.get(key, 0) + 1
        return codec_map

    @LoggerManager().log_function_entry
    @timeit("get_format_counts_by_series")
    def get_format_counts_by_series(self, instance):
        resolved = self.resolve_instance(instance)
        all_series = self.sonarr_api.get_all_sonarr_apis()[resolved].all_series()
        summary = {}
        for s in all_series:
            sid = s.get("id")
            for f in self.get_episode_files_for_series(sid, resolved):
                key = (
                    sid,
                    ((f.get("quality") or {}).get("quality") or {}).get("name"),
                    (f.get("mediaInfo") or {}).get("videoCodec"),
                    (f.get("mediaInfo") or {}).get("audioCodec")
                )
                summary[key] = summary.get(key, 0) + 1
        return summary

    @LoggerManager().log_function_entry
    @timeit("warm_episode_file_cache_with_tqdm")
    def warm_episode_file_cache_with_tqdm(self, series_list, instance):
        resolved = self.resolve_instance(instance)
        self.logger.log_info(f"🧊 Warming cache for {len(series_list)} series in {resolved}...")
        episode_cache = {}
        valid_series = [s for s in series_list if s.get("id") or s.get("seriesId")]

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self.get_episode_files_for_series, s.get("id") or s.get("seriesId"), resolved): s
                for s in valid_series
            }

            for future in tqdm(as_completed(futures), total=len(futures), desc="📁 Caching Episode Files", file=sys.stderr):
                sid = futures[future].get("id") or futures[future].get("seriesId")
                try:
                    episode_cache[sid] = future.result()
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Failed to cache episode files for series {sid}: {e}")

        self.logger.log_info(f"✅ Warmed episode file cache for {len(episode_cache)} series.")
        return episode_cache

    def find_orphaned_episode_files(self, instance):
        resolved = self.resolve_instance(instance)
        all_files = self.get_episode_file_metadata(resolved)
        known_ids = {ep['id'] for ep in self.sonarr_api.get_all_sonarr_apis()[resolved].all_episodes()}
        return [f for f in all_files if f.get("episodeId") not in known_ids]

    def detect_codec_drift_by_season(self, instance, series_id):
        files = self.get_episode_files_for_series(series_id, instance)
        drift = {}
        for f in files:
            season = f.get("seasonNumber")
            codec = (f.get("mediaInfo") or {}).get("videoCodec")
            drift.setdefault(season, {}).setdefault(codec, 0)
            drift[season][codec] += 1
        return {s: c for s, c in drift.items() if len(c) > 1}

    def suggest_codec_standardization(self, instance):
        codec_summary = self.get_codec_summary(instance)
        if not codec_summary:
            return {}
        best = max(codec_summary.items(), key=lambda x: x[1])
        return {"suggested_format": best[0], "count": best[1]}

    def recommend_episode_deletion_candidates(self, instance, min_quality="SD"):
        return [
            f for f in self.get_episode_file_metadata(instance)
            if ((f.get("quality") or {}).get("quality") or {}).get("name", "") == min_quality
        ]

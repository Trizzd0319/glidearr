# sonarr/episodes/retrieval/enrich.py

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesRetrievalEnrichmentManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodes"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        self.logger.log_debug(f"🧩 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("enrich_with_series_title")
    def enrich_with_series_title(self, episodes, instance):
        resolved = self.instance_manager.resolve_instance(instance)
        series_map = {
            s["id"]: s["title"]
            for s in self.sonarr_api.get_all_sonarr_apis()[resolved].all_series()
        }

        for ep in episodes:
            sid = ep.get("seriesId")
            ep["seriesTitle"] = series_map.get(sid, "Unknown")

        return episodes

    @LoggerManager().log_function_entry
    @timeit("enrich_with_format_summary")
    def enrich_with_format_summary(self, episodes):
        for ep in episodes:
            quality = ((ep.get("quality") or {}).get("quality") or {}).get("name", "")
            audio = (ep.get("mediaInfo") or {}).get("audioCodec", "")
            video = (ep.get("mediaInfo") or {}).get("videoCodec", "")
            ep["format"] = f"{quality} / {video} / {audio}"
        return episodes

    @LoggerManager().log_function_entry
    @timeit("apply_standardized_keys")
    def apply_standardized_keys(self, episodes):
        for ep in episodes:
            ep["episode"] = ep.get("episodeNumber")
            ep["season"] = ep.get("seasonNumber")
            ep["series"] = ep.get("seriesId")
        return episodes

    @LoggerManager().log_function_entry
    @timeit("merge_enrichments")
    def merge_enrichments(self, episodes, instance):
        episodes = self.apply_standardized_keys(episodes)
        episodes = self.enrich_with_series_title(episodes, instance)
        episodes = self.enrich_with_format_summary(episodes)
        return episodes

    @LoggerManager().log_function_entry
    @timeit("enrich_episode_data")
    def enrich_episode_data(self, episodes, instance):
        resolved = self.instance_manager.resolve_instance(instance)
        enriched = []
        episode_files = self.sonarr_cache.episodes.get_all_episode_data(resolved)
        file_lookup = {f["id"]: f for f in episode_files if f.get("id")}

        for ep in episodes:
            file_id = ep.get("episodeFileId")
            file_info = file_lookup.get(file_id, {})

            enriched.append({
                **ep,
                "fileSize": file_info.get("size"),
                "quality": ((file_info.get("quality") or {}).get("quality") or {}).get("name"),
                "codec": (file_info.get("mediaInfo") or {}).get("videoCodec"),
                "audio": (file_info.get("mediaInfo") or {}).get("audioCodec"),
                "sceneName": file_info.get("sceneName"),
                "releaseGroup": file_info.get("releaseGroup"),
            })

        self.logger.log_info(f"✨ Enriched {len(enriched)} episodes for {resolved}")
        return enriched

    @LoggerManager().log_function_entry
    @timeit("summarize_quality_distribution")
    def summarize_quality_distribution(self, instance):
        resolved = self.instance_manager.resolve_instance(instance)
        episode_files = self.sonarr_cache.episodes.get_all_episode_data(resolved)

        quality_counts = {}
        for ep in episode_files:
            quality = ((ep.get("quality") or {}).get("quality") or {}).get("name", "Unknown")
            quality_counts[quality] = quality_counts.get(quality, 0) + 1

        return dict(sorted(quality_counts.items(), key=lambda x: x[1], reverse=True))

    @LoggerManager().log_function_entry
    @timeit("find_episodes_missing_file")
    def find_episodes_missing_file(self, episodes):
        return [ep for ep in episodes if not ep.get("hasFile") or not ep.get("episodeFileId")]

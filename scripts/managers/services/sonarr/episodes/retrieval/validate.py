from arrapi import SonarrAPI

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.quality import SonarrQualitySelectorManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesRetrievalValidationManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesRetrieval"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # 🔗 Parent + reference resolution
        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        # 🔁 Dual-cache structure
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        self.logger.log_debug("🧩 Initialized SonarrEpisodesRetrievalValidationManager")

    @LoggerManager().log_function_entry
    @timeit("identify_missing_episode_files")
    def identify_missing_episode_files(self, episode_list):
        """
        Checks all configured Sonarr instances for episode presence.
        If missing everywhere and blacklisted, suggests next viable quality upgrade.
        """
        sonarr_instances = self.config.get("sonarr_instances", {})
        if not sonarr_instances:
            self.logger.log_warning("⚠️ No Sonarr instances configured.")
            return []

        api = SonarrAPI(logger=self.logger, config=self.config, sonarr_instances=sonarr_instances)
        missing_final = []

        for ep in episode_list:
            eid = ep.get("id")
            sid = ep.get("seriesId")
            sn = ep.get("seasonNumber")
            en = ep.get("episodeNumber")

            # Skip if series is tagged "keep"
            if self._is_keep_series(sid):
                self.logger.log_debug(f"🔒 Skipping validation for 'keep' series ID {sid}")
                continue

            found = False
            for inst in sonarr_instances:
                all_eps = api._make_request(inst, "episodefile") or []
                if any(f.get("episodeId") == eid for f in all_eps):
                    found = True
                    break

            if found:
                continue  # Present somewhere, skip

            # Check blacklist for all instances
            all_blacklisted_qualities = []
            for inst in sonarr_instances:
                blacklist = api._make_request(inst, f"blacklist?seriesId={sid}&season={sn}&episode={en}") or []
                all_blacklisted_qualities.extend([b.get("quality") for b in blacklist if b.get("quality")])

            # Check fallback
            next_quality = SonarrQualitySelectorManager.get_next_quality(self.config, sid, all_blacklisted_qualities)
            if next_quality:
                self.logger.log_info(f"🔁 Suggest upgrading episode {eid} to: {next_quality}")
            else:
                self.logger.log_warning(f"❌ No viable quality fallback found for {eid} ({sn}x{en})")
                missing_final.append(ep)

        return missing_final

    def _is_keep_series(self, series_id):
        """Check if series has 'keep' tag."""
        for instance in self.instance_manager.get_all_instance_names():
            resolved = self.instance_manager.resolve_instance(instance)
            series = self.sonarr_api._make_request(resolved, f"series/{series_id}")
            tags = [t.lower() for t in (series.get("tags") or [])]
            if "keep" in tags:
                return True
        return False

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairFileManager(BaseManager, ComponentManagerMixin):
    """
    Responsible for identifying and correcting mismatched or corrupt
    file metadata records in Sonarr's episode storage.
    """
    parent_name = "SonarrRepair"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.global_cache = kwargs.get("global_cache") or getattr(self.manager, "global_cache", None)
        self.instance_manager = getattr(self.manager, "instance_manager", None)

        if not self.logger:
            raise ValueError("❌ SonarrRepairFileManager requires a logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_mismatched_file_metadata")
    def repair_mismatched_file_metadata(self, instance_name):
        """
        Finds episodes with metadata inconsistencies (e.g. missing quality, sceneName)
        and attempts to trigger Sonarr reindexing or refresh for the parent series.
        """
        resolved_instance = self.instance_manager.resolve_instance(instance_name)
        api_client = self.sonarr_api.get_all_sonarr_apis().get(resolved_instance)

        if not api_client:
            self.logger.log_error(f"❌ No API client available for {resolved_instance}")
            return

        self.logger.log_info(f"🩺 Scanning {resolved_instance} for corrupt or incomplete file metadata...")

        bad_files = []
        episode_files = api_client.episode_files() or []

        for ep in episode_files:
            ep_id = ep.get("id")
            series_id = ep.get("seriesId")
            quality = ep.get("quality")
            scene_name = ep.get("sceneName")

            if not quality or not scene_name:
                bad_files.append(ep)
                self.logger.log_warning(f"⚠️ Missing metadata for EpisodeFile ID {ep_id} (Series {series_id})")

                if not self.dry_run:
                    try:
                        api_client.refresh_series(series_id)
                        self.logger.log_info(f"🔁 Triggered metadata refresh for Series {series_id}")
                    except Exception as e:
                        self.logger.log_error(f"❌ Failed to refresh Series {series_id}: {e}")

        self.logger.log_info(f"✅ Repair scan complete for {resolved_instance}: {len(bad_files)} bad files found")
        return bad_files

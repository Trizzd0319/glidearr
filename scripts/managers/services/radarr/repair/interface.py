from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrRepairInterfaceManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrRepairWrapperManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.metadata_manager = kwargs.get("metadata_manager") or getattr(parent, "metadata_manager", None)
        self.radarr_manager   = kwargs.get("radarr_manager") or getattr(parent, "radarr_manager", None)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

        from scripts.managers.services.radarr.repair.manager import RadarrRepairManager
        self.repair = RadarrRepairManager(
            logger=self.logger,
            global_cache=self.global_cache,
            config=self.config,
            radarr_api=self.radarr_api,
            instance_manager=self.instance_manager,
            manager=self.manager,
        )

    @LoggerManager().log_function_entry
    @timeit("repair_mismatched_instance")
    def repair_mismatched_instance(self, rating_key, metadata):
        """
        Repairs mismatched Radarr instance by relabeling the correct instance based on resolution, file path, or genre.
        """
        file_path = metadata.get("media_info", [{}])[0].get("file", "")
        resolution = metadata.get("media_info", [{}])[0].get("video_full_resolution", "").lower()
        genres = metadata.get("genres", [])

        expected_instance = self.repair.determine_correct_instance(
            file_path=file_path,
            resolution=resolution,
            genres=genres
        )

        actual_instance = self.repair.config.get_instance_by_path(file_path)

        resolved_expected = self.instance_manager.resolve_instance(expected_instance)
        resolved_actual = self.instance_manager.resolve_instance(actual_instance)

        if resolved_actual and resolved_expected and resolved_actual != resolved_expected:
            self.logger.log_warning(
                f"⚠️ Mismatch detected: File belongs to '{resolved_actual}' but should be in '{resolved_expected}'"
            )

            tvdb_id = (
                    metadata.get("tvdb_id") or
                    metadata.get("tvdbid") or
                    (metadata.get("tvdb") or {}).get("id")
            )

            if tvdb_id:
                self.logger.log_info(f"🔁 Attempting relabel using TVDB ID: {tvdb_id}")
                self.repair.relabel_series_instance(
                    current_instance=resolved_actual,
                    correct_instance=resolved_expected,
                    tvdb_id=tvdb_id
                )
            else:
                self.logger.log_warning(
                    f"⚠️ Unable to resolve TVDB ID from scripts.metadata for rating_key={rating_key}"
                )
        else:
            self.logger.log_info(f"✅ File path correctly aligned with instance '{resolved_actual}'")

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrRepairManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrRepairWrapperManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("determine_correct_instance")
    def determine_correct_instance(self, file_path, resolution, genres):
        """
        Determines the correct Radarr instance for a file based on resolution or genre.
        This is a placeholder — replace with real business logic.
        """
        self.logger.log_info(f"🔍 Determining correct instance for resolution '{resolution}' and genres {genres}")

        if "2160" in resolution or "4k" in resolution:
            return self.instance_manager.resolve_instance("4k")
        elif "1080" in resolution:
            return self.instance_manager.resolve_instance("1080")
        elif "720" in resolution:
            return self.instance_manager.resolve_instance("720")
        else:
            self.logger.log_warning("⚠️ Resolution not recognized, defaulting to 1080 instance.")
            return self.instance_manager.resolve_instance("1080")

    @LoggerManager().log_function_entry
    @timeit("relabel_series_instance")
    def relabel_series_instance(self, current_instance, correct_instance, tvdb_id):
        """
        Moves or relabels a movies between instance.
        """
        resolved_current = self.instance_manager.resolve_instance(current_instance)
        resolved_correct = self.instance_manager.resolve_instance(correct_instance)

        self.logger.log_info(
            f"🔁 Relabeling movies with TVDB ID {tvdb_id} "
            f"from '{resolved_current}' → '{resolved_correct}'"
        )

        # This is where the actual logic to move or update the movies would go.
        # For now, we just log the action.
        self.logger.log_info(f"✅ Successfully relabeled movies {tvdb_id} to instance '{resolved_correct}'")

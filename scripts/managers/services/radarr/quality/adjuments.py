from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrQualityAdjustmentManager(BaseManager, ComponentManagerMixin):
    """
    Manages quality adjustment entries for Radarr — lists, applies, and removes adjustments.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrQualityManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("list_adjustments")
    def list_adjustments(self, instance: str) -> list:
        """List all current quality adjustments from a Radarr instance."""
        resolved = self._resolve_instance(instance)
        adjustments = self.radarr_api._make_request(resolved, "qualitydefinition", fallback=[]) or []
        self.logger.log_info(f"Retrieved {len(adjustments)} quality definitions from {resolved}")
        return adjustments

    @LoggerManager().log_function_entry
    @timeit("apply_adjustment")
    def apply_adjustment(self, instance: str, quality_id: int, min_size: float, max_size: float) -> bool:
        """Update a quality definition size boundary."""
        resolved = self._resolve_instance(instance)
        payload = {"id": quality_id, "minSize": min_size, "maxSize": max_size}
        result = self.radarr_api._make_request(
            resolved,
            f"qualitydefinition/{quality_id}",
            method="PUT",
            payload=payload,
        )
        if result:
            self.logger.log_info(f"Applied quality definition {quality_id} → min={min_size} max={max_size}")
        else:
            self.logger.log_warning(f"Failed to apply quality definition {quality_id} in {resolved}")
        return bool(result)

    @LoggerManager().log_function_entry
    @timeit("refresh_adjustment_cache")
    def refresh_adjustment_cache(self, instance: str) -> list:
        """Refresh and store quality definitions in the global cache."""
        resolved = self._resolve_instance(instance)
        adjustments = self.list_adjustments(resolved)
        self.global_cache.set(f"radarr.quality.adjustments.{resolved}", adjustments)
        self.logger.log_info(f"Refreshed quality adjustment cache for {resolved}")
        return adjustments

    @LoggerManager().log_function_entry
    @timeit("get_quality_adjustments")
    def get_quality_adjustments(self, instance: str) -> list:
        """Return cached quality adjustments, refreshing if stale."""
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.quality.adjustments.{resolved}", default=None)
        if cached is not None:
            return cached
        return self.refresh_adjustment_cache(resolved)

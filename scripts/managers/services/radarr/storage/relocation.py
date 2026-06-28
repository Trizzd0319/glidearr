"""
RadarrStorageRelocationManager
================================
Thin registered placeholder under ``RadarrStorageManager``. It used to carry a ``relocate_movie``
STUB (logged intent, never moved a file) plus a hard-coded ``determine_target_instance`` /
``get_relocation_candidates`` pair — all DEAD (no caller). That stub has been removed so nothing can
mistake it for a working relocation path.

The REAL actuation lives elsewhere and is fully wired:
  • same-instance root-folder moves — ``services/routing`` (RoutingManager), gated by
    ``routing_targets.relocation_enabled``.
  • cross-instance file moves + dedup — ``services/routing/uhd_reconcile`` driving
    ``radarr/storage/cross_instance_move`` (move) and ``radarr/storage/cross_instance_dedup_apply``
    (dedup, planned by ``machine_learning/space/cross_instance_dedup``), gated by
    ``routing_targets.cross_instance_move_enabled`` / ``cross_instance_dedup_enabled`` plus a
    shared-storage pre-flight and the backup gate.

This class is kept only so the storage component map (``radarr/storage/__init__.py``) still loads it;
it holds no behaviour.
"""

from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrStorageRelocationManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrStorageManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        **kwargs,
    ):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = (
            kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        )
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__} (placeholder; relocation is "
                              f"actuated by services/routing + radarr/storage/cross_instance_move)")

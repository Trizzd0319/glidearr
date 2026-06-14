"""
WritebackManager — push local state outward (Trakt collection/history, MAL list).
================================================================================
Runs in main.py's final phase. Each sub-sync is independently gated by config and
honours ``dry_run`` (logs "would …", writes nothing):
  * trakt_writeback.collection → mirror *arr library into Trakt collection
  * trakt_writeback.history    → push episode-level watched history from Tautulli
  * mal_writeback              → reflect watch progress to the user's MAL list
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.writeback.mal_list import MalListSync
from scripts.managers.services.writeback.trakt_collection import TraktCollectionSync
from scripts.managers.services.writeback.trakt_history import TraktHistorySync
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class WritebackManager(BaseManager, ComponentManagerMixin):
    parent_name = "WritebackManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "WritebackManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.trakt = kwargs.get("trakt")
        self.mal = kwargs.get("mal")
        self.sonarr = kwargs.get("sonarr")
        self.radarr = kwargs.get("radarr")
        self.tautulli = kwargs.get("tautulli")

    def prepare(self) -> None:
        pass

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self) -> None:
        tw = (self.config.get("trakt_writeback", {}) if self.config else {}) or {}
        mw = (self.config.get("mal_writeback", {}) if self.config else {}) or {}

        if tw.get("enabled"):
            if tw.get("collection", True):
                try:
                    TraktCollectionSync(self.trakt, self.sonarr, self.radarr,
                                        self.config, self.logger, self.dry_run).run()
                except Exception as e:
                    self.logger.log_warning(f"[writeback] collection sync failed: {e}")
            if tw.get("history", True):
                try:
                    TraktHistorySync(self.trakt, self.tautulli, self.global_cache,
                                     self.config, self.logger, self.dry_run).run()
                except Exception as e:
                    self.logger.log_warning(f"[writeback] history sync failed: {e}")
        else:
            self.logger.log_debug("[Writeback] trakt_writeback disabled — skipping.")

        if mw.get("enabled"):
            try:
                MalListSync(self.mal, self.tautulli, self.global_cache,
                            self.config, self.logger, self.dry_run).run()
            except Exception as e:
                self.logger.log_warning(f"[writeback] MAL list sync failed: {e}")
        else:
            self.logger.log_debug("[Writeback] mal_writeback disabled — skipping.")

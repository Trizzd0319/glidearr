# sonarr/series/sync/__init__.py

import asyncio

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.series.sync.async_tasks import SonarrSeriesSyncAsyncManager
from scripts.managers.services.sonarr.series.sync.history import SonarrSeriesSyncHistoryManager
from scripts.managers.services.sonarr.series.sync.payloads import SonarrSeriesSyncPayloadManager
from scripts.managers.services.sonarr.series.sync.synchronize import SonarrSeriesSyncSynchronizeManager
from scripts.managers.services.sonarr.series.sync.tautulli import SonarrSeriesSyncTautulliManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesSyncManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(self.manager, "logger", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.orchestration = kwargs.get("orchestration") or getattr(self.manager, "orchestration", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        # ✅ Dual-cache support
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        init_args = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "cache_manager": self.sonarr_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": self.sonarr_api,
            "instance_manager": self.instance_manager,
            "orchestration": self.orchestration,
            "dry_run": self.dry_run
        }

        self.components = self.load_components(
            component_map={
                "history": SonarrSeriesSyncHistoryManager,
                "payload": SonarrSeriesSyncPayloadManager,
                "tautulli": SonarrSeriesSyncTautulliManager,
                "synchronize": SonarrSeriesSyncSynchronizeManager,
                "async_": SonarrSeriesSyncAsyncManager,
            },
            registry_prefix="sonarr.series.sync",
            api_kwarg_name="sonarr_api",
        )

        self.logger.log_debug(f"✅ SonarrSeriesSyncManager subcomponents loaded: {', '.join(self.components)}")

    @timeit("composite_sync_workflow")
    def composite_sync_workflow(self, instance: str = None, use_tautulli: bool = False, dry_run: bool = None,
                                force_all: bool = False):
        dry_run = dry_run if dry_run is not None else self.dry_run
        resolved_instance = self.instance_manager.resolve_instance(instance)
        self.logger.log_info(f"🚀 Beginning composite series sync for: {resolved_instance} (dry_run={dry_run})")

        recent_series_ids = set()
        history_cold_start = False   # True only on a genuine first run (no history bookmark)

        if use_tautulli:
            self.logger.log_info(f"🎞️ Using Tautulli data to drive sync...")
            tautulli_titles = self.tautulli.get_recent_tautulli_series()
            for title in tautulli_titles:
                series = self.manager.retrieval.fetch.get_series_by_title(resolved_instance, title)
                if series:
                    recent_series_ids.add(series.get("id"))
        else:
            self.logger.log_info(f"📜 Fetching recent history-based series...")
            # Capture cold-start state BEFORE the fetch: get_recent_sonarr_series rewrites
            # the history bookmark to *now*, so afterwards a genuine first run is
            # indistinguishable from a recent one. age is None == no bookmark yet == cold start.
            _ts = getattr(self.global_cache, "timestamp_handler", None)
            try:
                history_cold_start = (_ts is None) or (
                    _ts.get_age_seconds("sonarr", resolved_instance, "history") is None
                )
            except Exception:
                history_cold_start = True
            recent_series_ids = self.history.get_recent_sonarr_series(resolved_instance)

        # ── Idle steady-state: a recent history bookmark + an empty window means Sonarr
        # simply had no activity since the last sync — there is nothing new to
        # incrementally sync. Do NOT re-seed from the entire Tautulli watch history here:
        # that is a heavy, mis-scoped pass that previously fired on EVERY quiet run (a
        # recent bookmark routinely yields 0 records), re-asserting tags/monitoring across
        # the whole ever-watched catalogue. Only a genuine cold start warrants it. ──
        if not recent_series_ids and not use_tautulli and not history_cold_start and not force_all:
            self.logger.log_info(
                f"💤 No Sonarr activity since the last sync for '{resolved_instance}' "
                f"— nothing to incrementally sync."
            )
            return

        # ── Cold start: no Sonarr history bookmark yet (genuine first run) → seed from the
        # full Tautulli watch history so tags/monitoring still apply on the first run. ──
        if not recent_series_ids and not use_tautulli and history_cold_start:
            self.logger.log_info(
                "📺 First run (no Sonarr history bookmark yet) — seeding from full "
                "Tautulli watch history so tags/monitoring apply..."
            )
            try:
                titles: list[str] = []
                tautulli_mgr = self.registry.get("manager", "TautulliManager")
                tautulli_series = getattr(tautulli_mgr, "series", None) if tautulli_mgr else None
                tautulli_history = getattr(tautulli_mgr, "watch_history", None) if tautulli_mgr else None
                if (tautulli_series and hasattr(tautulli_series, "get_series_completion_stats")
                        and tautulli_history and hasattr(tautulli_history, "get_all_history_cached")):
                    # Seed from the same cached watch history TautulliManager.run() uses;
                    # get_series_completion_stats() requires the pre-fetched entries.
                    history_entries = tautulli_history.get_all_history_cached() or []
                    stats = tautulli_series.get_series_completion_stats(history_entries) or {}
                    titles = [t for t in stats.keys() if t and t != "Unknown"]
                elif self.global_cache:
                    raw_history = self.global_cache.get("tautulli/history/all") or []
                    titles = list({
                        h.get("grandparent_title") or h.get("title")
                        for h in raw_history
                        if h.get("media_type") in ("episode", "show")
                        and (h.get("grandparent_title") or h.get("title"))
                    })

                resolved = 0
                for title in titles:
                    series = self.manager.retrieval.fetch.get_series_by_title(
                        resolved_instance, title
                    )
                    if series and series.get("id"):
                        recent_series_ids.add(series["id"])
                        resolved += 1

                self.logger.log_info(
                    f"📺 Tautulli fallback: {resolved}/{len(titles)} title(s) "
                    f"resolved to Sonarr series for sync"
                )
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Tautulli fallback failed: {e} — proceeding without series"
                )

        if not recent_series_ids and not force_all:
            self.logger.log_warning("📭 No series matched sync criteria. Skipping.")
            return

        if force_all:
            self.logger.log_info("⚠️ Force mode enabled — syncing entire library.")
            all_series = self.manager.retrieval.fetch.get_all_series(resolved_instance)
            recent_series_ids = {s.get("id") for s in all_series if s.get("id")}

        self.logger.log_info(f"🔢 Series selected for sync: {len(recent_series_ids)}")

        # Keep-tag monitor (resolved/created via BaseManager.get_tag_monitor);
        # the keep-tag check below tolerates None.
        tag_monitor = self.get_tag_monitor()
        sync_jobs = []

        for sid in recent_series_ids:
            series_data = self.manager.retrieval.fetch.get_series_by_id(sid, resolved_instance)
            if not series_data:
                continue

            updated_tags = set(series_data.get("tags", []))
            if tag_monitor and tag_monitor.is_series_tagged_keep(sid):
                updated_tags.add("keep")

            payload = {
                "id": sid,
                "tags": list(updated_tags),
                "monitored": series_data.get("monitored", True),
            }

            sync_jobs.append({
                "instance": resolved_instance,
                "title": series_data.get("title", f"ID-{sid}"),
                "payload": payload
            })

        if sync_jobs:
            self.logger.log_info(f"🔁 Dispatching {len(sync_jobs)} sync jobs to async processor...")
            asyncio.run(self.synchronize.run_sync_jobs(sync_jobs=sync_jobs, dry_run=dry_run))
        else:
            self.logger.log_info("📭 No valid sync jobs were generated.")

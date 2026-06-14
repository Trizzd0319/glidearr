"""
MALManager — MyAnimeList service (ingest + discovery + acquisition source).
================================================================================
Runs in Phase 2 (like Trakt) and READS only: the user's anime list (incl. scores
via list_status), suggestions, and the current season, cached under ``mal/...``.
It also exposes:
  * ``acquisition_candidates()`` — plan-to-watch + suggestions normalized into the
    acquisition candidate shape (anime → Sonarr), consumed by AcquisitionManager.
  * ``mal_api`` — the API handle used by WritebackManager for list updates.
  * ``enrich(title)`` — best-effort anime metadata lookup.

MAL is OPTIONAL: if it isn't configured/authorized, the manager disables itself
(``self.enabled = False``) and every method degrades to a no-op / empty list.
"""
from __future__ import annotations

from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.mal.api import MALAPIManager
from scripts.managers.services.mal.instances import MALInstanceManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_SEASONS = {1: "winter", 2: "winter", 3: "winter", 4: "spring", 5: "spring", 6: "spring",
            7: "summer", 8: "summer", 9: "summer", 10: "fall", 11: "fall", 12: "fall"}


class MALManager(BaseManager, ComponentManagerMixin):
    parent_name = "MALManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "MALManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        base = dict(logger=self.logger, config=self.config, global_cache=self.global_cache,
                    validator=self.validator, registry=self.registry, manager=self, dry_run=self.dry_run)
        self.instance_manager = MALInstanceManager(**base)
        self.enabled = self.instance_manager.register_and_validate()
        self.mal_api = MALAPIManager(config=self.config, logger=self.logger)

    @property
    def _user(self) -> str:
        return (self.config.get("mal", {}) if self.config else {}).get("username", "default") or "default"

    def prepare(self) -> None:
        pass

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self) -> None:
        if not self.enabled:
            self.logger.log_debug("[MAL] disabled — skipping ingest.")
            return
        user = self._user
        anime_list = self.mal_api.get_anime_list() or []
        plan = [i for i in anime_list
                if (i.get("list_status") or {}).get("status") == "plan_to_watch"]
        suggestions = self.mal_api.get_suggestions() or []
        now = datetime.now()
        seasonal = self.mal_api.get_seasonal(now.year, _SEASONS[now.month]) or []

        if self.global_cache:
            self.global_cache.set(f"mal/{user}/animelist", anime_list)
            self.global_cache.set(f"mal/{user}/plan_to_watch", plan)
            self.global_cache.set(f"mal/{user}/suggestions", suggestions)
            self.global_cache.set(f"mal/seasonal/{now.year}/{_SEASONS[now.month]}", seasonal)

        self.logger.log_info(
            f"[MALManager] ingest: {len(anime_list)} list · {len(plan)} plan-to-watch · "
            f"{len(suggestions)} suggestions · {len(seasonal)} seasonal."
        )

    # ── acquisition source ─────────────────────────────────────────────────────
    def acquisition_candidates(self) -> list:
        if not self.enabled:
            return []
        user = self._user
        plan = (self.global_cache.get(f"mal/{user}/plan_to_watch") if self.global_cache else None)
        if plan is None:
            plan = [i for i in (self.mal_api.get_anime_list(status="plan_to_watch") or [])]
        suggestions = (self.global_cache.get(f"mal/{user}/suggestions") if self.global_cache else None)
        if suggestions is None:
            suggestions = self.mal_api.get_suggestions() or []

        out = [self._norm(i, "mal_plantowatch") for i in (plan or [])]
        out += [self._norm(i, "mal_suggestions") for i in (suggestions or [])]
        return [c for c in out if c.get("title")]

    @staticmethod
    def _norm(item: dict, source: str) -> dict:
        node = item.get("node", item) if isinstance(item, dict) else {}
        start = node.get("start_season", {}) or {}
        genres = [g.get("name") for g in (node.get("genres") or []) if isinstance(g, dict) and g.get("name")]
        mean = node.get("mean")
        return {
            "title": node.get("title"),
            "year": start.get("year"),
            "type": "show",
            "ids": {"trakt": None, "tvdb": None, "tmdb": None, "imdb": None, "mal": node.get("id")},
            "genres": genres or ["anime"],
            "rating": mean,
            "votes": None,
            "runtime": None,
            "source": source,
            "is_anime": True,
        }

    # ── enrichment (best-effort) ───────────────────────────────────────────────
    def enrich(self, title: str) -> dict:
        if not self.enabled or not title:
            return {}
        matches = self.mal_api.search_anime(title, limit=1) or []
        return (matches[0].get("node", {}) if matches else {}) or {}

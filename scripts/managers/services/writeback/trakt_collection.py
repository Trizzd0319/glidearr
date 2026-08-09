"""
trakt_collection.py — mirror the *arr library into the Trakt collection.
================================================================================
Unions every Sonarr instance's series (by tvdbId) and Radarr's movies (by tmdbId),
diffs against the current Trakt collection, and POSTs only the missing items to
``/sync/collection`` (chunked). dry_run-gated.

SCOPE — ``trakt_writeback.collection_watched_only`` (onboarding: "Only mark WATCHED
titles as owned?"). Default False = mirror the WHOLE library, which is the literal
meaning of a Trakt collection. True narrows it to titles the household has actually
watched, for operators who use their collection as a viewing record rather than an
inventory. The watched set comes from the SAME cached Trakt history
(``trakt/history/movies`` / ``trakt/history/episodes``) that feeds the delete guard's
``watched_tmdb_ids``, so "watched" means one thing across the whole system.
"""
from __future__ import annotations

from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.writeback._util import chunked


class TraktCollectionSync:
    def __init__(self, trakt, sonarr, radarr, config, logger, dry_run: bool,
                 global_cache=None):
        self.trakt = trakt
        self.config = config
        self.logger = logger
        self.dry_run = dry_run
        self.global_cache = global_cache
        self.gw = {
            "sonarr": ArrGateway("sonarr", getattr(sonarr, "instance_manager", None), config, logger),
            "radarr": ArrGateway("radarr", getattr(radarr, "instance_manager", None), config, logger),
        }

    def _instance_names(self, service: str) -> list:
        insts = self.config.get(f"{service}_instances", {}) or {}
        return [k for k, v in insts.items() if k != "default_instance" and isinstance(v, dict)]

    def _library_ids(self, service: str, id_field: str) -> set:
        gw = self.gw[service]
        ids: set = set()
        if not gw.available:
            return ids
        for name in (self._instance_names(service) or [gw.default_instance()]):
            ids |= gw.library_ids(name, id_field)
        return ids

    def _watched_only(self) -> bool:
        return bool(((self.config.get("trakt_writeback", {}) if self.config else {}) or {})
                    .get("collection_watched_only", False))

    def _watched_ids(self):
        """``(watched_tvdbs, watched_tmdbs)`` from the cached Trakt history.

        Reads ``trakt/history/episodes`` (show tvdb) and ``trakt/history/movies`` (film
        tmdb) — the same 24h-cached feeds ``radarr/repair/anomaly`` folds into
        ``watched_tmdb_ids``, so "watched" means the same thing here as it does at the
        delete guard.

        Returns ``(None, None)`` when the history cannot be read. That is NOT "nothing was
        watched": the caller must skip the scoped push rather than infer an empty watched
        set, or an unreadable cache would silently mean "mark nothing as owned" — which is
        the harmless direction here, but only because it is checked for explicitly.
        """
        gc = self.global_cache
        if gc is None:
            return None, None
        try:
            eps = gc.get("trakt/history/episodes")
            movs = gc.get("trakt/history/movies")
        except Exception:
            return None, None
        if not isinstance(eps, list) and not isinstance(movs, list):
            return None, None
        tvdbs, tmdbs = set(), set()
        for row in (eps if isinstance(eps, list) else []):
            tv = (((row or {}).get("show") or {}).get("ids") or {}).get("tvdb")
            if tv:
                tvdbs.add(str(tv))
        for row in (movs if isinstance(movs, list) else []):
            tm = (((row or {}).get("movie") or {}).get("ids") or {}).get("tmdb")
            if tm:
                tmdbs.add(str(tm))
        return tvdbs, tmdbs

    def run(self) -> dict:
        api = getattr(self.trakt, "trakt_api", None)
        if not api:
            self.logger.log_warning("[writeback] Trakt API unavailable — skipping collection sync.")
            return {"ok": False}

        show_tvdbs = self._library_ids("sonarr", "tvdbId")
        movie_tmdbs = self._library_ids("radarr", "tmdbId")

        # WATCHED-ONLY scope: narrow the library to what the household has actually
        # watched before diffing. Skipped (with a warning) when the history is unreadable
        # -- pushing the WHOLE library because a cache was cold would be the opposite of
        # what the operator asked for, and a collection add cannot be un-done in bulk.
        if self._watched_only():
            w_tvdbs, w_tmdbs = self._watched_ids()
            if w_tvdbs is None:
                self.logger.log_warning(
                    "[writeback] collection_watched_only is set but the Trakt watch history "
                    "cache is unreadable — skipping the collection push rather than falling "
                    "back to the whole library.")
                return {"ok": False, "reason": "watched-set unavailable"}
            _before = (len(show_tvdbs), len(movie_tmdbs))
            show_tvdbs = {t for t in show_tvdbs if str(t) in w_tvdbs}
            movie_tmdbs = {t for t in movie_tmdbs if str(t) in w_tmdbs}
            self.logger.log_info(
                f"[writeback] collection scope = WATCHED ONLY: "
                f"{len(show_tvdbs)}/{_before[0]} show(s), {len(movie_tmdbs)}/{_before[1]} "
                f"movie(s) qualify.")

        existing_shows = {
            str(((i.get("show") or {}).get("ids") or {}).get("tvdb"))
            for i in (api._make_request("sync/collection/shows") or [])
        }
        existing_movies = {
            str(((i.get("movie") or {}).get("ids") or {}).get("tmdb"))
            for i in (api._make_request("sync/collection/movies") or [])
        }

        new_shows = [int(t) for t in show_tvdbs if str(t) not in existing_shows and str(t).isdigit()]
        new_movies = [int(t) for t in movie_tmdbs if str(t) not in existing_movies and str(t).isdigit()]

        if not new_shows and not new_movies:
            self.logger.log_info("[writeback] Trakt collection already up to date.")
            return {"ok": True, "shows": 0, "movies": 0}

        if self.dry_run:
            self.logger.log_info(
                f"[writeback] dry_run — would add {len(new_shows)} show(s) + "
                f"{len(new_movies)} movie(s) to Trakt collection.")
            return {"ok": True, "shows": len(new_shows), "movies": len(new_movies), "dry_run": True}

        added = {"shows": 0, "movies": 0}
        for batch in chunked(new_shows, 100):
            api._make_request("sync/collection", method="POST",
                              data={"shows": [{"ids": {"tvdb": t}} for t in batch]})
            added["shows"] += len(batch)
        for batch in chunked(new_movies, 100):
            api._make_request("sync/collection", method="POST",
                              data={"movies": [{"ids": {"tmdb": t}} for t in batch]})
            added["movies"] += len(batch)

        user = (self.config.get("trakt", {}) or {}).get("username", "default")
        for key in (f"trakt/{user}/collection/shows",):
            try:
                self.trakt.global_cache.invalidate_cache_key(key)
            except Exception:
                pass
        self.logger.log_success(
            f"[writeback] Trakt collection: +{added['shows']} shows, +{added['movies']} movies.")
        return {"ok": True, **added}

"""
trakt_collection.py — mirror the *arr library into the Trakt collection.
================================================================================
Unions every Sonarr instance's series (by tvdbId) and Radarr's movies (by tmdbId),
diffs against the current Trakt collection, and POSTs only the missing items to
``/sync/collection`` (chunked). dry_run-gated.
"""
from __future__ import annotations

from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.writeback._util import chunked


class TraktCollectionSync:
    def __init__(self, trakt, sonarr, radarr, config, logger, dry_run: bool):
        self.trakt = trakt
        self.config = config
        self.logger = logger
        self.dry_run = dry_run
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

    def run(self) -> dict:
        api = getattr(self.trakt, "trakt_api", None)
        if not api:
            self.logger.log_warning("[writeback] Trakt API unavailable — skipping collection sync.")
            return {"ok": False}

        show_tvdbs = self._library_ids("sonarr", "tvdbId")
        movie_tmdbs = self._library_ids("radarr", "tmdbId")

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

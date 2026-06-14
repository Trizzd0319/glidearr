"""
trakt_history.py — push full episode-level watched history to Trakt.
================================================================================
Reads raw Tautulli history (with season/episode + watched timestamp), resolves
external IDs from the metadata index, and POSTs to ``/sync/history`` (chunked).
Logs how many entries mapped vs. were skipped (no GUID / not watched). dry_run-gated.
"""
from __future__ import annotations

from scripts.managers.services.writeback._util import chunked, extract_id, fetch_history, iso_utc

_WATCHED_PCT = 85  # default "counts as watched" completion threshold


class TraktHistorySync:
    def __init__(self, trakt, tautulli, global_cache, config, logger, dry_run: bool):
        self.trakt = trakt
        self.tautulli = tautulli
        self.gc = global_cache
        self.config = config
        self.logger = logger
        self.dry_run = dry_run

    def run(self) -> dict:
        api = getattr(self.trakt, "trakt_api", None)
        tau = getattr(self.tautulli, "api", None)
        if not api or not tau:
            self.logger.log_warning("[writeback] Trakt/Tautulli unavailable — skipping history sync.")
            return {"ok": False}

        meta = (self.gc.get("tautulli/metadata/index") if self.gc else None) or {}
        wb = (self.config.get("trakt_writeback", {}) if self.config else {}) or {}
        threshold = int(wb.get("watched_threshold", _WATCHED_PCT))
        max_pages = int(wb.get("history_max_pages", 20))

        entries = fetch_history(tau, self.logger, max_pages=max_pages)
        movies: list = []
        shows: dict = {}            # tvdb -> {season -> {episode -> watched_at}}
        mapped = unmapped = 0

        for e in entries:
            pct = e.get("percent_complete") or 0
            if e.get("watched_status") != 1 and pct < threshold:
                continue
            watched_at = iso_utc(e.get("date"))
            mtype = e.get("media_type")
            if mtype == "movie":
                tmdb = extract_id(meta.get(str(e.get("rating_key"))) or {}, "tmdb")
                if tmdb:
                    movies.append({"ids": {"tmdb": int(tmdb)}, "watched_at": watched_at})
                    mapped += 1
                else:
                    unmapped += 1
            elif mtype == "episode":
                tvdb = extract_id(meta.get(str(e.get("grandparent_rating_key"))) or {}, "tvdb")
                season, ep = e.get("parent_media_index"), e.get("media_index")
                if tvdb and str(season).isdigit() and str(ep).isdigit():
                    shows.setdefault(int(tvdb), {}).setdefault(int(season), {})[int(ep)] = watched_at
                    mapped += 1
                else:
                    unmapped += 1

        show_payload = [
            {"ids": {"tvdb": tvdb},
             "seasons": [{"number": s, "episodes": [{"number": en, "watched_at": wa} for en, wa in eps.items()]}
                         for s, eps in seasons.items()]}
            for tvdb, seasons in shows.items()
        ]

        self.logger.log_info(
            f"[writeback] history: {mapped} mapped, {unmapped} unmapped "
            f"({len(movies)} movies, {len(show_payload)} shows).")
        if not movies and not show_payload:
            return {"ok": True, "movies": 0, "shows": 0, "unmapped": unmapped}

        if self.dry_run:
            self.logger.log_info(
                f"[writeback] dry_run — would push {len(movies)} movie play(s) + "
                f"{len(show_payload)} show(s) of episode history to Trakt.")
            return {"ok": True, "movies": len(movies), "shows": len(show_payload),
                    "unmapped": unmapped, "dry_run": True}

        for batch in chunked(movies, 100):
            api._make_request("sync/history", method="POST", data={"movies": batch})
        for batch in chunked(show_payload, 50):
            api._make_request("sync/history", method="POST", data={"shows": batch})
        self.logger.log_success(
            f"[writeback] pushed history: {len(movies)} movies, {len(show_payload)} shows.")
        return {"ok": True, "movies": len(movies), "shows": len(show_payload), "unmapped": unmapped}

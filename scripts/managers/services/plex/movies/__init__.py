"""
plex/movies — owned-movie tmdb→ratingKey map (the movie personal-playlist join + probe).
================================================================================
Builds ``plex/movies/owned_inventory`` = ``{"{tmdb}": {ratingKey, title, year, section}}`` so the
movie playlist resolver can turn each owned Radarr movie (``movie_files.parquet``, keyed
by ``tmdb_id``) into a playable Plex ratingKey. The movie analog of ``plex/episodes`` —
and SIMPLER, because a movie is a single item with no show→episode tree: scan the movie
section(s) (``plex_type=1``), resolve each movie's tmdb via the guid_map
(``PlexMetadataManager.resolve``), and key the inventory by tmdb.

Also the COVERAGE PROBE: ``plex/movies/resolution_stats`` reports the resolved % — the
make-or-break number for whether the tmdb join is viable on this server (Plex movie
items usually carry tmdb directly in ``Guid[]``, so coverage is typically high). Gated
default-off (``plex.movies.enabled``); local-PMS reads only; never writes to Plex.
Schema-tolerant (every UNSTABLE field via ``.get``).
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import (
    excluded_section_titles, metadata_items, parse_item, total_size)

_INVENTORY_KEY = "plex/movies/owned_inventory"
_STATS_KEY = "plex/movies/resolution_stats"
_SECTIONS_KEY = "plex/sections"
_PAGE_SIZE = 200
_MAX_PAGES = 200


class PlexMoviesManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def prepare(self):
        pass

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        meta = self.registry.get("manager", "PlexMetadataManager") if self.registry else None
        sections = self._sections()
        excluded = excluded_section_titles(self.config)
        movie_sections = {k: s for k, s in sections.items()
                          if str(s.get("type", "")).lower() == "movie"
                          and str(s.get("title", "")).strip().lower() not in excluded}
        skipped = [s.get("title") for s in sections.values()
                   if str(s.get("type", "")).lower() == "movie"
                   and str(s.get("title", "")).strip().lower() in excluded]
        if skipped:
            self.logger.log_info(f"[PlexMovies] excluding {len(skipped)} section(s) per "
                                 f"plex.exclude_sections: {', '.join(map(str, skipped))}")

        inventory: dict = {}
        stats = {
            "movie_sections": len(movie_sections), "movies_seen": 0, "movies_resolved": 0,
            "unresolved_no_tmdb": 0, "max_pages_hit": False,
        }
        for key in movie_sections:
            self._scan_movies(key, meta, inventory, stats)

        seen = stats["movies_seen"]
        stats["resolution_pct"] = round(100.0 * stats["movies_resolved"] / seen, 1) if seen else 0.0

        if self.global_cache:
            self.global_cache.set(_INVENTORY_KEY, inventory)
            self.global_cache.set(_STATS_KEY, stats)
        incomplete = " (TRUNCATED — hit page cap)" if stats["max_pages_hit"] else ""
        self.logger.log_info(
            f"[PlexMovies] {stats['movies_resolved']}/{seen} movie(s) resolved to a "
            f"ratingKey ({stats['resolution_pct']}% coverage) across "
            f"{stats['movie_sections']} movie section(s){incomplete}.")
        return stats

    # ── scan ─────────────────────────────────────────────────────────────────
    def _scan_movies(self, key, meta, inventory, stats):
        """Each movie → tmdb (via the guid_map) → {ratingKey, title, year, section}. The source
        ``section`` key lets per-user consumers (e.g. the anniversary shelf) scope an owned movie to
        the libraries that user was actually shared, instead of gating at the whole-medium level."""
        for raw in self._iter_section(key, plex_type=1, stats=stats):
            stats["movies_seen"] += 1
            p = parse_item(raw)
            tmdb = self._resolve_tmdb(meta, p)
            if tmdb is None or p["rating_key"] is None:
                stats["unresolved_no_tmdb"] += 1
                continue
            inventory[str(int(tmdb))] = {
                "rating_key": str(p["rating_key"]),
                "title": p["title"],
                "year": p["year"],
                "section": str(key),
            }
            stats["movies_resolved"] += 1

    def _iter_section(self, key, *, plex_type, stats):
        """Yield raw Metadata items across all pages; flag a page-cap truncation."""
        start = 0
        for _page in range(_MAX_PAGES):
            resp = self.plex_api.get_section_all(key, plex_type=plex_type,
                                                 start=start, size=_PAGE_SIZE)
            items = metadata_items(resp)
            if not items:
                return
            for raw in items:
                yield raw
            tot = total_size(resp)
            start += _PAGE_SIZE
            if tot and start >= tot:
                return
        stats["max_pages_hit"] = True          # loop exhausted without an early return

    # ── helpers ────────────────────────────────────────────────────────────────
    def _sections(self) -> dict:
        """Prefer the section index libraries.run() already cached; else fetch it."""
        if self.global_cache:
            cached = self.global_cache.get(_SECTIONS_KEY)
            if isinstance(cached, dict) and cached:
                return cached
        resp = self.plex_api.get_sections() if self.plex_api else None
        out = {}
        for d in metadata_items(resp):
            k = d.get("key")
            if k:
                out[str(k)] = {"title": d.get("title"), "type": str(d.get("type", "")).lower()}
        return out

    @staticmethod
    def _resolve_tmdb(meta, p):
        if not meta:
            return None
        try:
            ids = meta.resolve(p["guid"], p["guids"], rating_key=p["rating_key"],
                               allow_network=False) or {}
            return ids.get("tmdb")
        except Exception:
            return None

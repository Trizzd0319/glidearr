"""
plex/episodes — owned-episode tvdb→ratingKey map (the personal-playlist join + probe).
================================================================================
Builds ``plex/episodes/owned_inventory`` = ``{"{series_tvdb}:{season}:{episode}":
{ratingKey, title, series_title, grandparent_rating_key, section}}`` so the playlist resolver
can turn each owned Sonarr episode (which already carries the same join key, see
sonarr/cache/owned_episodes.py) into a playable Plex ratingKey.

WHY resolve the SERIES tvdb at the SHOW level: modern Plex agents frequently expose
NO tvdb on an episode's Guid[] (only ``plex://``), so keying off episode guids drops
most rows. Instead we resolve each SHOW's tvdb (which resolves reliably — the same
path libraries.py uses), then link every episode to its show via the structural
``grandparentRatingKey`` + ``parentIndex``/``index`` (season/episode), which Plex
always provides. Coverage therefore tracks SHOW-tvdb resolution, not episode guids.

This is also the COVERAGE PROBE: ``plex/episodes/resolution_stats`` reports the
resolved % — the make-or-break number for whether the simple join is viable on this
server. Gated default-off (``plex.episodes.enabled``); local-PMS reads only; never
writes to Plex. Schema-tolerant (every UNSTABLE field via ``.get``).
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import (
    excluded_section_titles, metadata_items, parse_item, total_size)
from scripts.managers.services.plex.playlists.readiness import diagnose_tv_readiness

_INVENTORY_KEY = "plex/episodes/owned_inventory"
_STATS_KEY = "plex/episodes/resolution_stats"
_SECTIONS_KEY = "plex/sections"
_PAGE_SIZE = 200
_MAX_PAGES = 200


class PlexEpisodesManager(BaseManager):
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
        show_sections = {k: s for k, s in sections.items()
                         if str(s.get("type", "")).lower() == "show"
                         and str(s.get("title", "")).strip().lower() not in excluded}
        skipped = [s.get("title") for s in sections.values()
                   if str(s.get("type", "")).lower() == "show"
                   and str(s.get("title", "")).strip().lower() in excluded]
        if skipped:
            self.logger.log_info(f"[PlexEpisodes] excluding {len(skipped)} section(s) per "
                                 f"plex.exclude_sections: {', '.join(map(str, skipped))}")

        inventory: dict = {}
        stats = {
            "show_sections": len(show_sections), "shows_resolved": 0, "shows_unresolved": 0,
            "episodes_seen": 0, "episodes_resolved": 0,
            "unresolved_no_series_tvdb": 0, "unresolved_missing_index": 0,
            "max_pages_hit": False,
        }
        for key in show_sections:
            show_tvdb = self._scan_shows(key, meta, stats)        # {show_rk: tvdb}
            self._scan_episodes(key, show_tvdb, inventory, stats)

        seen = stats["episodes_seen"]
        stats["resolution_pct"] = round(100.0 * stats["episodes_resolved"] / seen, 1) if seen else 0.0

        if self.global_cache:
            self.global_cache.set(_INVENTORY_KEY, inventory)
            self.global_cache.set(_STATS_KEY, stats)
        incomplete = " (TRUNCATED — hit page cap)" if stats["max_pages_hit"] else ""
        self.logger.log_info(
            f"[PlexEpisodes] {stats['episodes_resolved']}/{seen} episode(s) resolved to a "
            f"ratingKey ({stats['resolution_pct']}% coverage) across "
            f"{stats['show_sections']} show section(s){incomplete}.")

        # Surface COVERAGE guidance now (the probe is the first thing a shared install
        # runs) so a low-coverage server is never left guessing. The enrichment/per-user
        # readiness notes come later in the playlist builder, where watchability scores
        # converge — series_total=0 here cleanly skips that branch.
        for note in diagnose_tv_readiness(
                inventory_present=bool(inventory), resolution_pct=stats["resolution_pct"],
                max_pages_hit=stats["max_pages_hit"], series_total=0, series_scored=0,
                daemon_enabled=False, daemon_running=False)["notes"]:
            emit = self.logger.log_warning if note["level"] == "warn" else self.logger.log_info
            emit(f"[PlexEpisodes] {note['message']}")
        return stats

    # ── scans ──────────────────────────────────────────────────────────────────
    def _scan_shows(self, key, meta, stats) -> dict:
        """ratingKey → series tvdb for every show in a section (the reliable tvdb)."""
        show_tvdb: dict = {}
        for raw in self._iter_section(key, plex_type=2, stats=stats):
            p = parse_item(raw)
            tvdb = self._resolve_tvdb(meta, p)
            if tvdb is not None and p["rating_key"] is not None:
                show_tvdb[str(p["rating_key"])] = int(tvdb)
                stats["shows_resolved"] += 1
            else:
                stats["shows_unresolved"] += 1
        return show_tvdb

    def _scan_episodes(self, key, show_tvdb, inventory, stats):
        """Each episode → join key via its show's tvdb + (season, episode)."""
        for raw in self._iter_section(key, plex_type=4, stats=stats):
            stats["episodes_seen"] += 1
            ep = self._parse_episode(raw)
            tvdb = show_tvdb.get(str(ep["grandparent_rating_key"]))
            if tvdb is None:
                stats["unresolved_no_series_tvdb"] += 1
                continue
            if ep["season"] is None or ep["episode"] is None or ep["rating_key"] is None:
                stats["unresolved_missing_index"] += 1
                continue
            join_key = f"{tvdb}:{ep['season']}:{ep['episode']}"
            inventory[join_key] = {
                "rating_key": str(ep["rating_key"]),
                "title": ep["title"],
                "series_title": ep["series_title"],
                "grandparent_rating_key": str(ep["grandparent_rating_key"]),
                "section": str(key),
            }
            stats["episodes_resolved"] += 1

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
    def _parse_episode(item: dict) -> dict:
        """Episode-specific normalize (parse_item omits index/parentIndex/grandparent*)."""
        if not isinstance(item, dict):
            item = {}
        return {
            "rating_key": item.get("ratingKey") or item.get("ratingkey"),
            "season": item.get("parentIndex"),
            "episode": item.get("index"),
            "title": item.get("title") or "",
            "series_title": item.get("grandparentTitle") or "",
            "grandparent_rating_key": item.get("grandparentRatingKey"),
        }

    @staticmethod
    def _resolve_tvdb(meta, p):
        if not meta:
            return None
        try:
            ids = meta.resolve(p["guid"], p["guids"], rating_key=p["rating_key"],
                               allow_network=False) or {}
            return ids.get("tvdb")
        except Exception:
            return None

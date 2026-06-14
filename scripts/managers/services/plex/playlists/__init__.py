"""
plex/playlists — video playlists (DESIGN P4, default-off).
================================================================================
Subordinate to + noisier than the watchlist (queues/mixtapes ≠ intent), and the
smart-rule grammar re-derives the A–G scorer — so this is built strictly AFTER
watchlist forward-validates, default-off, and its acquisition source-score sits
below the watchlist. Smart-rule content is captured as OPAQUE metadata, never
executed. Video-only; deduped vs the watchlist union.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import metadata_items, parse_item

_INDEX_KEY = "plex/playlists/index"


class PlexPlaylistsManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def prepare(self):
        pass

    def run(self) -> dict:
        meta = self.registry.get("manager", "PlexMetadataManager") if self.registry else None
        union = (self.global_cache.get("plex/watchlist/union") if self.global_cache else None) or []
        watchlisted = {
            str((it.get("ids") or {}).get("tmdb") or (it.get("ids") or {}).get("tvdb")
                or (it.get("ids") or {}).get("imdb"))
            for it in union if isinstance(it, dict)
        }
        resp = self.plex_api.get_playlists()
        index = {}
        for d in metadata_items(resp):
            if str(d.get("playlistType", "")).lower() not in ("video", ""):
                continue  # video-only
            rk = d.get("ratingKey")
            if rk is None:
                continue
            members = self._members(rk, meta, watchlisted)
            index[str(rk)] = {
                "title": d.get("title") or "", "smart": bool(d.get("smart")),
                "smart_filter": d.get("content") if d.get("smart") else None,
                "items": members,
            }
        if self.global_cache:
            self.global_cache.set(_INDEX_KEY, index)
        self.logger.log_info(f"[PlexPlaylists] {len(index)} video playlist(s).")
        return {"playlists": len(index)}

    def _members(self, rating_key, meta, watchlisted: set) -> list:
        resp = self.plex_api.get_playlist_items(rating_key)
        out = []
        for raw in metadata_items(resp):
            p = parse_item(raw)
            ids = {}
            if meta:
                try:
                    ids = meta.resolve(p["guid"], p["guids"], rating_key=p["rating_key"],
                                       allow_network=False)
                except Exception:
                    ids = {}
            primary = str(ids.get("tmdb") or ids.get("tvdb") or ids.get("imdb") or "")
            if primary and primary in watchlisted:
                continue  # dedup vs watchlist union
            out.append({
                "title": p["title"], "type": p["type"],
                "ids": {k: ids.get(k) for k in ("tmdb", "tvdb", "imdb")},
            })
        return out

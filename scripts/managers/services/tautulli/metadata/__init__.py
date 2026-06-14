from scripts.managers.factories.base_manager import BaseManager

_METADATA_TTL = 604_800  # 7 days


class TautulliMetadataManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.tautulli_api = kwargs.get("tautulli_api")

    def build_metadata_index(self, rating_keys: list) -> dict:
        """Fetch and index metadata for each rating_key. One API call per key."""
        if not self.tautulli_api:
            return {}
        metadata = {}
        for rk in rating_keys:
            resp = self.tautulli_api.get_metadata(rating_key=rk)
            if not resp or (resp.get("response") or {}).get("result") != "success":
                self.logger.log_warning(f"[TautulliMeta] Metadata failed for rating_key={rk}")
                continue
            md = (resp.get("response") or {}).get("data", {})
            if not md:
                # Tautulli returns {"result": "success", "data": {}} for items that
                # no longer exist in Plex. Treat as missing so the rating_key ends
                # up in the not_in_metadata debug bucket rather than no_tmdb_guid.
                self.logger.log_debug(f"[TautulliMeta] Empty metadata for rating_key={rk} — item likely deleted from Plex.")
                continue
            media_info = md.get("media_info", [])
            fmt = media_info[0] if media_info else {}
            streams = fmt.get("streams", [])
            metadata[rk] = {
                "genres":       md.get("genres", []),
                "actors":       md.get("actors", []),
                "directors":    md.get("directors", []),
                "writers":      md.get("writers", []),
                "composers":    md.get("composers", []),
                "producers":    md.get("producers", []),
                "studios":      [md["studio"]] if md.get("studio") else [],
                "labels":       md.get("labels", []),
                "collections":  md.get("collections", []),
                "video_codec":  fmt.get("video_codec"),
                "audio_codec":  fmt.get("audio_codec"),
                "audio_language": self._extract_audio_language(streams),
                "view_time":    md.get("last_viewed_at"),
                "tmdb_id":      self._extract_tmdb_id(md),
                # Kept for tmdb_id debugging — lets callers see what GUIDs Plex
                # returned when tmdb_id is None (e.g. imdb-only items).
                "title":        md.get("title", ""),
                "year":         md.get("year"),
                "guids":        md.get("guids", []),
                "guid":         md.get("guid", ""),
            }
        self.logger.log_info(f"[TautulliMeta] Built metadata index: {len(metadata)} items.")
        return metadata

    def get_metadata_index_cached(self, rating_keys: list) -> dict:
        """Return metadata index, cached for 7 days.

        Auto-invalidates the cache when it pre-dates the tmdb_id field so that
        the first run after a schema change rebuilds immediately rather than
        waiting for the 7-day TTL to expire.
        """
        if not self.global_cache:
            return self.build_metadata_index(rating_keys)

        existing = self.global_cache.get("tautulli/metadata/index")
        if existing and isinstance(existing, dict):
            has_tmdb = any("tmdb_id" in v for v in existing.values())
            if not has_tmdb:
                self.logger.log_info(
                    "[TautulliMeta] Cached metadata index has no tmdb_id fields "
                    "(pre-schema cache) — invalidating and rebuilding."
                )
                self.global_cache.delete("tautulli/metadata/index")

        return self.global_cache.get_or_generate_cache(
            key="tautulli/metadata/index",
            generator_function=lambda: self.build_metadata_index(rating_keys),
            expiration_time=_METADATA_TTL,
            # Resolves rating_key → tmdb_id for the watched-set. Must refresh on
            # TTL or newly-watched items never resolve to a tmdbId and silently
            # drop out of the household watched-set.
            regenerate_on_expiry=True,
        )

    def get_library_index(self) -> dict:
        """Real-time library list from Tautulli."""
        if not self.tautulli_api:
            return {}
        resp = self.tautulli_api.get_libraries()
        libs = ((resp or {}).get("response") or {}).get("data", []) or []
        return {
            lib["section_id"]: {
                "name":   lib.get("section_name"),
                "type":   lib.get("section_type"),
                "count":  int(lib.get("count", 0)),
                "active": lib.get("is_active", 0) == 1,
            }
            for lib in libs if lib.get("section_id")
        }

    def _extract_audio_language(self, streams: list) -> list:
        return list({
            s.get("audio_language")
            for s in streams
            if s.get("type") == "2" and s.get("audio_language")
        })

    @staticmethod
    def _extract_tmdb_id(md: dict) -> int | None:
        """Extract TMDB ID from Plex guids in various formats."""
        guids = md.get("guids") or []
        for guid in guids:
            if isinstance(guid, dict):
                raw = guid.get("id", "")
            elif isinstance(guid, str):
                raw = guid
            else:
                continue
            if raw.startswith("tmdb://"):
                try:
                    return int(raw[7:])
                except (ValueError, TypeError):
                    pass
        # Fallback: single guid field
        single = md.get("guid", "")
        if isinstance(single, str) and single.startswith("tmdb://"):
            try:
                return int(single[7:])
            except (ValueError, TypeError):
                pass
        return None

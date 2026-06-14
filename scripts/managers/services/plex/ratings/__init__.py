"""
plex/ratings — per-member userRating (DESIGN P2, DEFER / fold into per-user pass).
================================================================================
Genuinely per-user (Trakt is single-account, Tautulli has none) and zero marginal
request cost when it rides the same per-user scan. Gated behind
``plex.ratings.enabled`` (default-off). Feeds ``scoring/_shared.user_rating_score``
via ``sonarr/cache/episode_files._build_user_show_rating_map`` once it proves out.

OWNER-DEDUPE vs Trakt is mandatory downstream (or one verdict hits A4 twice) — the
service only fetches; the brain dedupes. Ratings are normalized to integer 0–10
(Plex stores 0–10 in half-steps). Per-member id→rating maps are PII-minimized:
id-keyed, the rating value carries no identity.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import metadata_items, parse_item, total_size

_PAGE_SIZE = 200
_MAX_PAGES = 50


class PlexRatingsManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)
        self.user_tokens: dict = kwargs.get("user_tokens", {})

    def prepare(self):
        pass

    def run(self) -> dict:
        users_mgr = self.registry.get("manager", "PlexUsersManager") if self.registry else None
        users = list(getattr(users_mgr, "tracked_users", []) or [])
        meta = self.registry.get("manager", "PlexMetadataManager") if self.registry else None
        sections = self._rateable_sections()
        total_rated = 0
        for u in users:
            token = self.user_tokens.get(u["safe_user"])
            if not token:
                continue
            rated = self._scan_user(token, sections, meta)
            total_rated += len(rated)
            if self.global_cache:
                self.global_cache.set(f"plex/users/{u['safe_user']}/ratings", rated)
        self.logger.log_info(f"[PlexRatings] {total_rated} rating(s) across {len(users)} user(s).")
        return {"ratings": total_rated, "users": len(users)}

    def _rateable_sections(self) -> list:
        resp = self.plex_api.get_sections()
        out = []
        for d in metadata_items(resp):
            stype = str(d.get("type", "")).lower()
            key = d.get("key")
            if key and stype in ("movie", "show"):
                out.append({"key": key, "type": stype})
        return out

    def _scan_user(self, token, sections, meta) -> dict:
        """{resolved-id-or-ratingKey: int rating} for items this member rated.
        Filters ``userRating`` server-side where supported, client-side as a backstop."""
        rated: dict = {}
        for sec in sections:
            plex_type = 1 if sec["type"] == "movie" else 2
            start = 0
            for _page in range(_MAX_PAGES):
                resp = self.plex_api.get_section_all(
                    sec["key"], plex_type=plex_type, start=start, size=_PAGE_SIZE,
                    token=token, extra_params={"userRating>": "0"})
                items = metadata_items(resp)
                if not items:
                    break
                for raw in items:
                    p = parse_item(raw)
                    ur = self._norm_rating(p.get("user_rating"))
                    if ur is None:
                        continue
                    ids = self._resolve(meta, p, token)
                    key = ids.get("tmdb") or ids.get("tvdb") or ids.get("imdb") or p["rating_key"]
                    if key is not None:
                        rated[str(key)] = ur
                tot = total_size(resp)
                start += _PAGE_SIZE
                if tot and start >= tot:
                    break
        return rated

    @staticmethod
    def _norm_rating(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return int(round(f)) if f > 0 else None

    @staticmethod
    def _resolve(meta, p, token):
        if not meta:
            return {}
        try:
            return meta.resolve(p["guid"], p["guids"], rating_key=p["rating_key"], token=token)
        except Exception:
            return {}

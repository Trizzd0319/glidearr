"""
plex/collections — manual + smart collections (DESIGN P4, default-off).
================================================================================
C1 completeness math already exists; only operator-curated non-TMDB collections are
additive (often null), so this is default-off in scoring, deduped vs the existing
TMDB ``collection_members``, and must forward-validate before any weight. Stable
local-PMS endpoints → low API risk.

Byte-identical-to-golden contract: when there are no measured non-TMDB collections
the resolved membership map is empty and downstream scoring is unchanged.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import metadata_items, parse_item

_INDEX_KEY = "plex/collections/index"
_MEMBERSHIP_KEY = "plex/collections/membership_by_tmdb"
_COMPLETENESS_KEY = "plex/collections/completeness"


class PlexCollectionsManager(BaseManager):
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
        resp = self.plex_api.get_collections()
        index, membership = {}, {}
        for d in metadata_items(resp):
            rk = d.get("ratingKey")
            if rk is None:
                continue
            title = d.get("title") or ""
            smart = bool(d.get("smart"))
            members = self._members(rk, meta)
            index[str(rk)] = {
                "title": title, "smart": smart,
                "child_count": int(d.get("childCount", 0) or 0) if str(d.get("childCount", "")).isdigit() else len(members),
                "tmdb_members": members,
                # smart-rule grammar captured as OPAQUE metadata — never executed.
                "smart_filter": d.get("content") if smart else None,
            }
            for tmdb in members:
                membership.setdefault(str(tmdb), []).append(str(rk))
        if self.global_cache:
            self.global_cache.set(_INDEX_KEY, index)
            self.global_cache.set(_MEMBERSHIP_KEY, membership)
            self.global_cache.set(_COMPLETENESS_KEY, {
                rk: {"title": v["title"], "have": len(v["tmdb_members"]), "child_count": v["child_count"]}
                for rk, v in index.items()
            })
        self.logger.log_info(f"[PlexCollections] {len(index)} collection(s), "
                             f"{len(membership)} tmdb member(s).")
        return {"collections": len(index)}

    def _members(self, rating_key, meta) -> list:
        resp = self.plex_api.get_collection_children(rating_key)
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
            if ids.get("tmdb"):
                out.append(int(ids["tmdb"]))
        return out

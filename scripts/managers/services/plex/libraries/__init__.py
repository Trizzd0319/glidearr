"""
plex/libraries — section inventory + orphan/missing reconcile (DESIGN P3).
================================================================================
Reviews SPLIT this capability (DESIGN §2):
  * the **orphan/missing reconciler** is a true gap nothing fills (no Plex↔*arr
    reconcile exists) and is pure zero-API set-diff → KEEP (deferred, diagnostic).
  * the bare **section inventory** is near-redundant (Tautulli already fetches it) →
    we cache the cheap ``/library/sections`` index but build nothing on it.

DIAGNOSTIC ONLY: orphans (in Plex, not *arr-managed) must NEVER auto-feed deletion —
deletion stays pressure-gated on ``free_space_limit`` over *arr-owned files. Missing
reconcile UNIONs ALL Radarr/Sonarr instance caches (or it over-reports), and items
with UNRESOLVED GUIDs are excluded (else false-positive orphans).
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.plex._common import metadata_items, parse_item, total_size

_SECTIONS_KEY = "plex/sections"
_LIBIDS_KEY = "plex/library_ids"
_ORPHANS_KEY = "plex/reconcile/orphans"
_MISSING_KEY = "plex/reconcile/missing"
_PAGE_SIZE = 200
_MAX_PAGES = 200


class PlexLibrarySectionsManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def prepare(self):
        pass

    # ── PASS 1 (inventory; cheap section index always, id-scan only when enabled) ─
    def run(self) -> dict:
        resp = self.plex_api.get_sections()
        sections = {}
        for d in metadata_items(resp):
            key = d.get("key")
            if not key:
                continue
            locations = [loc.get("path") for loc in (d.get("Location") or [])
                         if isinstance(loc, dict) and loc.get("path")]
            sections[str(key)] = {
                "title": d.get("title"), "type": str(d.get("type", "")).lower(),
                "locations": locations,
                "item_count": int(d.get("count", 0) or 0) if str(d.get("count", "")).isdigit() else None,
            }
        if self.global_cache:
            self.global_cache.set(_SECTIONS_KEY, sections)

        if self._reconcile_enabled():
            self._scan_library_ids(sections)
        self.logger.log_debug(f"[PlexLibraries] {len(sections)} section(s) inventoried.")
        return {"sections": len(sections)}

    def _scan_library_ids(self, sections: dict):
        """Build the resolved Plex id-set (movie→tmdb, show→tvdb) for the diff. Heavy
        — gated behind reconcile.enabled. Items that don't resolve are counted, not
        guessed."""
        meta = self.registry.get("manager", "PlexMetadataManager") if self.registry else None
        movie_tmdb, show_tvdb, unresolved = set(), set(), 0
        for key, sec in sections.items():
            stype = sec["type"]
            if stype not in ("movie", "show"):
                continue
            plex_type = 1 if stype == "movie" else 2
            start = 0
            for _page in range(_MAX_PAGES):
                resp = self.plex_api.get_section_all(key, plex_type=plex_type,
                                                     start=start, size=_PAGE_SIZE)
                items = metadata_items(resp)
                if not items:
                    break
                for raw in items:
                    p = parse_item(raw)
                    ids = self._resolve(meta, p)
                    if stype == "movie" and ids.get("tmdb"):
                        movie_tmdb.add(int(ids["tmdb"]))
                    elif stype == "show" and ids.get("tvdb"):
                        show_tvdb.add(int(ids["tvdb"]))
                    else:
                        unresolved += 1
                tot = total_size(resp)
                start += _PAGE_SIZE
                if tot and start >= tot:
                    break
        if self.global_cache:
            self.global_cache.set(_LIBIDS_KEY, {
                "movie_tmdb": sorted(movie_tmdb), "show_tvdb": sorted(show_tvdb),
                "unresolved": unresolved,
            })
        self.logger.log_info(
            f"[PlexLibraries] id-scan: {len(movie_tmdb)} movies, {len(show_tvdb)} shows, "
            f"{unresolved} unresolved.")

    # ── PASS 2 (reconcile; pure set-diff, zero new API) ──────────────────────
    def run_reconcile(self) -> dict:
        libids = self.global_cache.get(_LIBIDS_KEY) if self.global_cache else None
        if not isinstance(libids, dict):
            self.logger.log_debug("[PlexLibraries] no plex/library_ids — enable plex.reconcile to build it.")
            return {}
        plex_movies = set(libids.get("movie_tmdb") or [])
        plex_shows = set(libids.get("show_tvdb") or [])
        radarr_tmdb = self._arr_ids("radarr", "tmdbId")
        sonarr_tvdb = self._arr_ids("sonarr", "tvdbId")

        orphans = {
            "movies_tmdb": sorted(plex_movies - radarr_tmdb),
            "shows_tvdb": sorted(plex_shows - sonarr_tvdb),
        }
        missing = {
            "movies_tmdb": sorted(radarr_tmdb - plex_movies),
            "shows_tvdb": sorted(sonarr_tvdb - plex_shows),
        }
        if self.global_cache:
            self.global_cache.set(_ORPHANS_KEY, orphans)
            self.global_cache.set(_MISSING_KEY, missing)
        self.logger.log_info(
            f"[PlexLibraries] reconcile — orphans: {len(orphans['movies_tmdb'])} movies / "
            f"{len(orphans['shows_tvdb'])} shows · missing: {len(missing['movies_tmdb'])} movies / "
            f"{len(missing['shows_tvdb'])} shows (DIAGNOSTIC — never auto-deletes).")
        return {"orphans": orphans, "missing": missing}

    def _arr_ids(self, service: str, id_field: str) -> set:
        """UNION the id_field across ALL configured instances of a service."""
        mgr = self.registry.get("manager", f"{service.capitalize()}Manager") if self.registry else None
        im = getattr(mgr, "instance_manager", None)
        gw = ArrGateway(service, im, self.config, self.logger)
        if not gw.available:
            return set()
        out: set = set()
        insts = [k for k, v in (self.config.get(f"{service}_instances", {}) or {}).items()
                 if k != "default_instance" and isinstance(v, dict)] or [gw.default_instance()]
        for inst in insts:
            for raw in gw.library_ids(inst, id_field):
                try:
                    out.add(int(raw))
                except (TypeError, ValueError):
                    pass
        return out

    # ── helpers ───────────────────────────────────────────────────────────────
    def _reconcile_enabled(self) -> bool:
        # `or {}` guards each level so a malformed non-dict plex/plex.reconcile config
        # can't raise AttributeError — matching PlexManager._cap_enabled's pattern.
        plex_cfg = (self.config.get("plex", {}) if self.config else {}) or {}
        return bool((plex_cfg.get("reconcile", {}) or {}).get("enabled", False))

    @staticmethod
    def _resolve(meta, p):
        if not meta:
            return {}
        try:
            # zero-network during a full library scan — bare plex:// fall through as
            # unresolved rather than firing a Discover hop per owned item.
            return meta.resolve(p["guid"], p["guids"], rating_key=p["rating_key"], allow_network=False)
        except Exception:
            return {}

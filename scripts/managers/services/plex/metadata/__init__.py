"""
plex/metadata — GUID → {tmdb, tvdb, imdb} resolver (DESIGN P0, build FIRST).
================================================================================
The join key without which every other Plex signal fails ``_dedup`` (acquisition
``candidates._dedup`` keys on ``tmdb‖tvdb‖imdb‖title``). A bare ``plex://`` item
would double-add against Trakt/MAL, so every per-user feed resolves ids HERE
(in the fetcher) before it reaches acquisition.

Two-tier resolution (DESIGN §6.1):
  * **FREE / regex** for ``tmdb://`` / ``imdb://`` / ``tvdb://`` and legacy
    ``com.plexapp.agents.*`` GUIDs, plus the external ``Guid[]`` array Discover/PMS
    return — zero network.
  * **PAID network** (``metadata.provider.plex.tv`` Discover hop, UNSTABLE) only for
    a bare ``plex://`` with no external ids — counted, memoised, and persisted so a
    given ``plex://`` resolves **at most once ever**.

The ``plex/guid_map`` cache is append-only (id mappings are immutable, so unlike a
TTL'd key it never needs ``regenerate_on_expiry`` — a resolved guid is resolved
forever). One shared map + bridge dicts are primed once per run and reused by every
fetcher, so the same ``ratingKey`` is never re-resolved per-user-per-run.
"""
from __future__ import annotations

import time

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.acquisition.gateway import ArrGateway

_GUID_MAP_KEY = "plex/guid_map"
_UNRESOLVED_KEY = "plex/debug/unresolved_guids"


class PlexMetadataManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)
        self.user_tokens = kwargs.get("user_tokens", {})

        self._guid_map: dict = {}
        self._dirty = False
        self._network_hops = 0
        self._unresolved: dict = {}
        self._imdb_to_tmdb: dict = {}
        self._tvdb_to_tmdb: dict = {}
        self._attempted_hops: set = set()   # rating_keys hopped THIS run (dedup the per-user fan-out)
        self._primed = False

    # ── run lifecycle ─────────────────────────────────────────────────────────
    def prepare(self):
        pass

    def prime(self):
        """Load the persistent guid_map + build the imdb→tmdb / tvdb→tmdb bridge
        dicts ONCE (DESIGN §6.1: the per-item linear ``next(...)`` scan is the
        hottest loop otherwise). Cache-only / zero-API — Radarr/Sonarr have not run
        yet during PASS-1, so this reads warm snapshot caches, never live."""
        if self._primed:
            return
        existing = self.global_cache.get(_GUID_MAP_KEY) if self.global_cache else None
        self._guid_map = existing if isinstance(existing, dict) else {}
        self._attempted_hops = set()
        self._build_bridges()
        self._primed = True
        self.logger.log_debug(
            f"[PlexMeta] primed: {len(self._guid_map)} cached guids · "
            f"{len(self._imdb_to_tmdb)} imdb→tmdb · {len(self._tvdb_to_tmdb)} tvdb→tmdb."
        )

    def _build_bridges(self):
        """Build imdb→tmdb (movies) + tvdb→tmdb (shows) from the *arr library caches.
        Best-effort: missing/cold caches just leave the bridge empty and we lean on
        the free-tier ``Guid[]`` parse (which carries tmdb/tvdb directly for the vast
        majority of Discover items)."""
        if not self.global_cache:
            return
        # Movies — the canonical key the watched-set uses, plus any per-instance variant.
        movie_keys = ["radarr.movies.standard.full"]
        try:
            for inst in (self.config.get("radarr_instances", {}) or {}):
                if inst != "default_instance":
                    movie_keys.append(f"radarr.movies.{inst}.full")
        except Exception:
            pass
        for key in dict.fromkeys(movie_keys):
            movies = self.global_cache.get(key)
            if not isinstance(movies, list):
                continue
            for m in movies:
                if not isinstance(m, dict):
                    continue
                imdb, tmdb = m.get("imdbId"), m.get("tmdbId")
                if imdb and tmdb and imdb not in self._imdb_to_tmdb:
                    self._imdb_to_tmdb[str(imdb)] = int(tmdb)
        # Shows — tvdb is the show join key; tvdb→tmdb only backfills tmdb for a show
        # that carries tvdb but not tmdb. Sonarr has NO global_cache `.full` key (unlike
        # Radarr's movies); it exposes series via its series cache, so resolve it through
        # the registered SonarrCacheSeriesManager — the same registry pattern ArrGateway
        # and sonarr.sync.tags use. Best-effort: a cold cache (first run / Sonarr not yet
        # run) just leaves the bridge empty and we lean on the free-tier Guid[] tvdb.
        try:
            series_cache = self.registry.get("manager", "SonarrCacheSeriesManager") if self.registry else None
            if series_cache and hasattr(series_cache, "get_all_series"):
                insts = [k for k, v in (self.config.get("sonarr_instances", {}) or {}).items()
                         if k != "default_instance" and isinstance(v, dict)]
                for inst in insts:
                    for s in (series_cache.get_all_series(inst) or []):
                        if not isinstance(s, dict):
                            continue
                        tvdb, tmdb = s.get("tvdbId"), s.get("tmdbId")
                        if tvdb and tmdb and str(tvdb) not in self._tvdb_to_tmdb:
                            self._tvdb_to_tmdb[str(tvdb)] = int(tmdb)
        except Exception:
            pass

    def flush(self) -> int:
        """Persist the merged guid_map (each plex:// resolves ≤ once ever) + the
        unresolved-debug bucket. Returns the count of network (Discover) hops."""
        if self.global_cache and self._dirty:
            try:
                self.global_cache.set(_GUID_MAP_KEY, self._guid_map)
            except Exception as e:
                self.logger.log_warning(f"[PlexMeta] guid_map persist failed: {e}")
        if self.global_cache and self._unresolved:
            try:
                self.global_cache.set(_UNRESOLVED_KEY, {
                    "total": len(self._unresolved),
                    "guids": self._unresolved,
                })
            except Exception:
                pass
        return self._network_hops

    # ── resolution ────────────────────────────────────────────────────────────
    def resolve(self, raw_guid: str, guids_list=None, rating_key=None,
                token: str | None = None, allow_network: bool = True) -> dict:
        """Resolve one Plex item to ``{tmdb, tvdb, imdb, resolved_via}`` (ints for
        tmdb/tvdb, ``tt…`` str for imdb; missing → None). Order: persistent memo →
        free Guid[] parse → free raw-guid parse → bridge → paid Discover hop."""
        # 1. persistent memo (never re-resolve)
        if raw_guid and raw_guid in self._guid_map:
            return dict(self._guid_map[raw_guid])

        ids = {"tmdb": None, "tvdb": None, "imdb": None, "resolved_via": None}

        # 2. free — external Guid[] array (Discover/PMS already carry these)
        for g in (guids_list or []):
            gid = g.get("id") if isinstance(g, dict) else g
            self._absorb(ids, gid, via="guid_array")

        # 3. free — the primary guid string itself (handles legacy agents:// too)
        if not _any_id(ids):
            self._absorb(ids, raw_guid, via="raw_guid")

        # 4. bridge — imdb/tvdb → tmdb (cheap dict lookups). tmdb is the canonical
        #    join id, so when the bridge supplies it we record THAT provenance.
        if not ids["tmdb"]:
            if ids["imdb"] and str(ids["imdb"]) in self._imdb_to_tmdb:
                ids["tmdb"] = self._imdb_to_tmdb[str(ids["imdb"])]
                ids["resolved_via"] = "bridge_imdb"
            elif ids["tvdb"] and str(ids["tvdb"]) in self._tvdb_to_tmdb:
                ids["tmdb"] = self._tvdb_to_tmdb[str(ids["tvdb"])]
                ids["resolved_via"] = "bridge_tvdb"

        # 5. paid — bare plex:// Discover hop. Fired at most ONCE per run per
        #    rating_key (the in-run guard kills the per-household-user N multiplier),
        #    and a CONFIRMED miss is memoized below so it never re-hops on future runs.
        if not _any_id(ids) and allow_network and rating_key and self.plex_api:
            attempted = getattr(self, "_attempted_hops", None)
            if attempted is None:
                attempted = self._attempted_hops = set()
            if rating_key not in attempted:
                attempted.add(rating_key)
                net = self._discover_hop(rating_key, token)
                if net is not None:   # Discover RESPONDED (ids may still be all-None)
                    ids.update({k: net.get(k) for k in ("tmdb", "tvdb", "imdb")})
                    ids["resolved_via"] = "discover_hop" if _any_id(ids) else "discover_miss"
                # net is None → TRANSIENT failure → leave retryable (not memoized)

        # Persist so each guid resolves at most once EVER: positive hits AND confirmed
        # Discover misses (a bare plex:// with genuinely no external id must not re-hop
        # every run). A transient/non-network empty stays in the retryable bucket.
        if raw_guid:
            if _any_id(ids):
                ids.setdefault("ts", int(time.time()))
                self._guid_map[raw_guid] = ids
                self._dirty = True
            elif ids.get("resolved_via") == "discover_miss":
                self._guid_map[raw_guid] = {"tmdb": None, "tvdb": None, "imdb": None,
                                            "resolved_via": "discover_miss", "ts": int(time.time())}
                self._dirty = True
            else:
                self._unresolved[str(raw_guid)] = {"rating_key": rating_key}
        return dict(ids)

    def _discover_hop(self, rating_key, token):
        """Returns the parsed ids dict when Discover RESPONDED (ids may be all-None — a
        confirmed miss), or None on a TRANSIENT failure (falsy response) so the caller
        keeps it retryable instead of memoizing a permanent miss."""
        self._network_hops += 1
        resp = self.plex_api.resolve_discover_metadata(rating_key, token=token)
        if not resp:
            return None  # transient — allow retry on a later run
        mc = resp.get("MediaContainer", {}) if isinstance(resp, dict) else {}
        items = mc.get("Metadata") or []
        ids = {"tmdb": None, "tvdb": None, "imdb": None}
        for g in ((items[0].get("Guid") if items else None) or []):
            self._absorb(ids, g.get("id") if isinstance(g, dict) else g)
        return ids  # responded; all-None means a confirmed miss

    def _absorb(self, ids: dict, guid_str, via: str | None = None):
        """Parse one guid string and fold its provider id into ``ids`` (first-win)."""
        provider, value = self._parse_guid(guid_str)
        if not provider or value is None:
            return
        if provider in ("tmdb", "tvdb"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                return
        if ids.get(provider) is None:
            ids[provider] = value
            if via and not ids.get("resolved_via"):
                ids["resolved_via"] = via

    @staticmethod
    def _parse_guid(s) -> tuple:
        """(provider, id) from any Plex guid string; ('','') if none.

        Handles ``tmdb://123``, ``imdb://tt1``, ``tvdb://9/1/2`` and legacy
        ``com.plexapp.agents.themoviedb://123?lang=en`` /``...thetvdb://9?...`` /
        ``...imdb://tt1?...``. A bare ``plex://`` resolves to nothing (network tier)."""
        if not isinstance(s, str) or "://" not in s:
            return "", ""
        core, _, _q = s.partition("?")
        scheme, _, rest = core.partition("://")
        first = rest.split("/", 1)[0].strip()
        scheme = scheme.lower()
        if scheme == "tmdb" or "themoviedb" in scheme:
            return "tmdb", first
        if scheme == "tvdb" or "thetvdb" in scheme:
            return "tvdb", first
        if scheme == "imdb" or scheme.endswith(".imdb"):
            return "imdb", first
        return "", ""


def _any_id(ids: dict) -> bool:
    return any(ids.get(k) is not None for k in ("tmdb", "tvdb", "imdb"))

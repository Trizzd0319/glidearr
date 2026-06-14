"""
plex/watchlist — multi-user account watchlist (DESIGN P1, the flagship).
================================================================================
The named blocker for the next-watch objective + top-tier acquisition feed, and
the one genuinely-additive top signal no other wired service can reproduce: the
user *literally said* "I want to watch this".

Per-user fetch (Discover ``watchlist/all``, UNSTABLE) → GUID-resolve (shared P0
resolver) → household union retaining per-user attribution → ``plex/watchlist/union``
(read warm by acquisition's last-phase ``_plex()`` branch) + a timestamped snapshot
(load-bearing for forward validation, DESIGN §5.4; retention-bounded since it is
per-member intent PII).

Resolution happens HERE, in the fetcher, so a ``plex://``-only item carries
``{tmdb,tvdb,imdb}`` before it reaches ``_dedup`` (which keys on those ids) and can't
double-add against Trakt/MAL.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import metadata_items, parse_item, total_size

_UNION_KEY = "plex/watchlist/union"
_SNAP_INDEX_KEY = "plex/watchlist/snapshots_index"
_PAGE_SIZE = 100
_MAX_PAGES = 50           # hard ceiling (≤ 5000 items/user) — bounds a pathological account
_DEFAULT_RETENTION = 12   # rolling snapshot window (per-member intent PII must not accumulate)


class PlexWatchlistManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)
        self.user_tokens: dict = kwargs.get("user_tokens", {})

    def prepare(self):
        pass

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        users = self._tracked_users()
        if not users:
            self.logger.log_debug("[PlexWatchlist] no tracked users — nothing to fetch.")
            return {"items": 0, "users": 0}

        per_user: dict[str, list] = {}
        for u in users:
            token = self.user_tokens.get(u["safe_user"])
            if not token:
                continue
            items = self._fetch_user_watchlist(token)
            if items is None:
                # transient failure — preserve the prior good per-user cache (don't zero it)
                self.logger.log_warning(
                    f"[PlexWatchlist] fetch failed for '{u['title']}' — keeping last-good.")
                continue
            per_user[u["safe_user"]] = items
            if self.global_cache:
                self.global_cache.set(f"plex/users/{u['safe_user']}/watchlist", items)

        union = self._build_union(per_user, users)
        if self.global_cache:
            if union or not self._has_prior_union():
                self.global_cache.set(_UNION_KEY, union)
                self._write_snapshot(union)
            else:
                # union empty but we had a prior good one → likely a transient all-fail;
                # don't overwrite good intent data with nothing (DESIGN §6.2 fail-closed).
                self.logger.log_warning("[PlexWatchlist] empty union with prior good cache — preserving prior.")

        self.logger.log_info(
            f"[PlexWatchlist] {len(union)} unique title(s) across {len(per_user)} user(s).")
        return {"items": len(union), "users": len(per_user)}

    # ── fetch ────────────────────────────────────────────────────────────────
    def _fetch_user_watchlist(self, token: str):
        """Paged Discover watchlist for one member → list of resolved items, or None
        on the first hard failure (so the caller preserves the prior good cache)."""
        meta = self.registry.get("manager", "PlexMetadataManager") if self.registry else None
        out: list = []
        start = 0
        for _page in range(_MAX_PAGES):
            resp = self.plex_api.get_watchlist(token, start=start, size=_PAGE_SIZE)
            if resp is None:
                # ANY page failing is a hard failure for this user → return None so the
                # caller preserves the prior COMPLETE cache rather than persisting a
                # truncated list as if it were whole (fail-closed; self-heals next run).
                return None
            items = metadata_items(resp)
            if not items:
                break
            for raw in items:
                parsed = parse_item(raw)
                ids = self._resolve(meta, parsed, token)
                out.append({
                    "title": parsed["title"],
                    "year": parsed["year"],
                    "type": "show" if parsed["type"] in ("show", "episode") else "movie",
                    "ids": {k: ids.get(k) for k in ("tmdb", "tvdb", "imdb")},
                    "rating_key": parsed["rating_key"],
                    "guid": parsed["guid"],
                })
            tot = total_size(resp)
            start += _PAGE_SIZE
            if tot and start >= tot:
                break
        return out

    @staticmethod
    def _resolve(meta, parsed: dict, token: str) -> dict:
        if not meta:
            return {"tmdb": None, "tvdb": None, "imdb": None}
        try:
            return meta.resolve(parsed["guid"], parsed["guids"],
                                rating_key=parsed["rating_key"], token=token)
        except Exception:
            return {"tmdb": None, "tvdb": None, "imdb": None}

    # ── union ────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_union(per_user: dict, users: list) -> list:
        """Household union keyed on ``tmdb‖tvdb‖imdb‖title`` (the acquisition _dedup
        key), merging per-user attribution onto duplicates."""
        title_of = {u["safe_user"]: u.get("title", u["safe_user"]) for u in users}
        by_key: dict = {}
        order: list = []
        for safe_user, items in per_user.items():
            who = title_of.get(safe_user, safe_user)
            for it in items:
                ids = it.get("ids", {}) or {}
                primary = ids.get("tmdb") or ids.get("tvdb") or ids.get("imdb") or it.get("title")
                key = (it.get("type"), str(primary))
                if key not in by_key:
                    entry = dict(it)
                    entry["watchlisted_by"] = []
                    entry["source"] = "plex_watchlist"
                    by_key[key] = entry
                    order.append(key)
                if who not in by_key[key]["watchlisted_by"]:
                    by_key[key]["watchlisted_by"].append(who)
        return [by_key[k] for k in order]

    def _has_prior_union(self) -> bool:
        return bool(self.global_cache and self.global_cache.get(_UNION_KEY))

    # ── snapshot (forward-validation; retention-bounded) ─────────────────────
    def _write_snapshot(self, union: list, ts: str | None = None):
        if not self.global_cache:
            return
        ts = ts or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"plex/watchlist/snapshot/{ts}"
        self.global_cache.set(key, {"ts": ts, "items": union})

        index = self.global_cache.get(_SNAP_INDEX_KEY)
        index = index if isinstance(index, list) else []
        index.append(ts)
        retention = self._retention()
        if len(index) > retention:
            for old in index[:-retention]:
                try:
                    self.global_cache.delete(f"plex/watchlist/snapshot/{old}")
                except Exception:
                    pass
            index = index[-retention:]
        self.global_cache.set(_SNAP_INDEX_KEY, index)

    def _retention(self) -> int:
        wl = (self.config.get("plex", {}) if self.config else {}).get("watchlist", {}) or {}
        try:
            return max(1, int(wl.get("snapshot_retention", _DEFAULT_RETENTION)))
        except (TypeError, ValueError):
            return _DEFAULT_RETENTION

    # ── helpers ───────────────────────────────────────────────────────────────
    def _tracked_users(self) -> list:
        users_mgr = self.registry.get("manager", "PlexUsersManager") if self.registry else None
        return list(getattr(users_mgr, "tracked_users", []) or [])

    # ── acquisition feed ──────────────────────────────────────────────────────
    def acquisition_candidates(self) -> list:
        """The union as acquisition candidates (read from cache so it works even if
        called in a later phase). Already id-resolved; the gatherer just maps shape."""
        union = (self.global_cache.get(_UNION_KEY) if self.global_cache else None) or []
        return union

"""
plex/on_deck — continue-watching (DESIGN P2, DEFER / A-B-gated).
================================================================================
DESIGN §0a treats on-deck as interchangeable with Tautulli ``percent_complete``,
and TV resume is already computed by ``next_episode_planner.last_watched_per_series``
with zero Plex calls. So this ships as ENRICHMENT only: it emits its own
``plex/on_deck/*`` key for forward A/B and is **not weighted in production** until the
eval harness shows lift. Gated behind ``plex.on_deck.enabled`` (default-off).

Reconciliation policy vs Tautulli (DESIGN Q3) is left to the consumer; this fetcher
just records the raw per-user ``resume_fraction`` so the A/B can decide prefer-max
vs prefer-server later.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import metadata_items, parse_item

_UNION_KEY = "plex/on_deck/union"


class PlexOnDeckManager(BaseManager):
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
        per_user: dict = {}
        for u in users:
            token = self.user_tokens.get(u["safe_user"])
            if not token:
                continue
            resp = self.plex_api.get_on_deck(token=token)
            items = []
            for raw in metadata_items(resp):
                p = parse_item(raw)
                off, dur = p.get("view_offset_ms") or 0, p.get("duration_ms") or 0
                ids = self._resolve(meta, p, token)
                items.append({
                    "id": p["rating_key"], "title": p["title"], "type": p["type"],
                    "ids": {k: ids.get(k) for k in ("tmdb", "tvdb", "imdb")},
                    "view_offset_ms": off, "duration_ms": dur,
                    "resume_fraction": round(off / dur, 4) if dur else None,
                })
            per_user[u["safe_user"]] = items
            if self.global_cache:
                self.global_cache.set(f"plex/users/{u['safe_user']}/on_deck", items)

        union = []
        for safe_user, items in per_user.items():
            for it in items:
                e = dict(it); e["user"] = safe_user
                union.append(e)
        if self.global_cache:
            # Only the rolling union key (what the next_watch consumer reads). NO
            # per-run timestamped snapshot: it would accumulate unbounded per-member
            # resume PII with no reader. If forward A/B ever needs history, mirror
            # watchlist._write_snapshot's bounded-index+retention discipline instead.
            self.global_cache.set(_UNION_KEY, union)
        self.logger.log_info(f"[PlexOnDeck] {len(union)} item(s) across {len(per_user)} user(s).")
        return {"items": len(union), "users": len(per_user)}

    @staticmethod
    def _resolve(meta, p, token):
        if not meta:
            return {}
        try:
            return meta.resolve(p["guid"], p["guids"], rating_key=p["rating_key"], token=token)
        except Exception:
            return {}

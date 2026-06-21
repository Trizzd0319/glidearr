"""
coordinator/saga_retention_producer.py — the catch-up retention PRODUCER (Phase 2).
================================================================================
A cross-service PRE-PASS (modelled on :class:`SpaceCoordinatorManager`) that turns
already-cached run artifacts into ONE de-identified ``lifecycle/saga_gates`` cache the
deletion guards (Phases 3-5) consume. It reads only; it never deletes.

Per run, when ``saga_retention.enabled``:
  1. build drift-proof title→id maps from owned Radarr movies + the Sonarr owned-episode
     inventory (the watch side has no tmdb/tvdb — only titles — and rating keys churn);
  2. bucket the raw per-user Tautulli history by ``user_id`` into WATCHED (≥threshold) vs
     STARTED (sub-threshold within the engagement grace) movie/show sets + last-watch ts;
  3. attach each user's WATCHLIST (already id-resolved) joined to the SAME ``user_id`` via
     the Plex identity bridge;
  4. call the pure brain :func:`compute_saga_gates`;
  5. write ``lifecycle/saga_gates`` (ids + saga keys + counts only — no names).

FAIL-OPEN: any missing/empty input → write EMPTY gates (nothing held). Deletions are armed,
so a fail-CLOSED "hold everything" bug would fill the disk; we never do that. OFF by default
→ no cache write churn beyond the (empty) key. See coordinator/catchup_retention.md.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.lifecycle.saga_retention import compute_saga_gates
from scripts.managers.services.plex.playlists.universe_order import saga_member_sets
from scripts.support.utilities.decorators.timing import timeit

_GATES_KEY = "lifecycle/saga_gates"
_EMPTY_GATES = {"movies": {}, "shows": {}, "gate_user_count": {}, "expiring_by_user": {}}
_YEAR_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def _norm(s) -> str:
    """Drift-proof title key: lowercase, keep only ``[a-z0-9]`` (same shape genre_affinity uses)."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _resolve_id(by_title: dict, raw):
    """Title → id with a trailing-``(YYYY)`` fallback so 'Bluey (2018)' owned matches 'Bluey' watched."""
    if not raw:
        return None
    key = _norm(raw)
    if key in by_title:
        return by_title[key]
    stripped = _norm(_YEAR_RE.sub("", str(raw)))
    return by_title.get(stripped) if stripped != key else None


def _epoch_dt(v):
    """Tautulli history ``date`` (unix seconds) → aware UTC datetime, or None."""
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _skeleton() -> dict:
    return {"watched": {"movies": {}, "shows": {}},
            "started": {"movies": {}, "shows": {}},
            "watchlist": {"movies": {}, "shows": {}}}


class SagaRetentionProducerManager(BaseManager, ComponentManagerMixin):
    parent_name = "SagaRetentionProducerManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "SagaRetentionProducerManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        parent = kwargs.get("manager")
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        self.dry_run = bool(_dry_run) if _dry_run is not None else False
        self.sonarr = kwargs.get("sonarr")
        self.radarr = kwargs.get("radarr")
        self.plex = kwargs.get("plex")
        self.tautulli = kwargs.get("tautulli")

    def prepare(self) -> None:
        pass

    # ── inputs ────────────────────────────────────────────────────────────────────
    def _title_id_maps(self) -> "tuple[dict, dict]":
        """``({norm_movie_title: tmdb}, {norm_series_title: tvdb})`` from OWNED inventory, unioned
        across all instances. Best-effort — a missing cache just yields a smaller map (fail-open)."""
        movie_map: dict = {}
        show_map: dict = {}
        cache = self.global_cache
        if not cache:
            return movie_map, show_map
        insts = [k for k in (self.config.get("radarr_instances", {}) or {})
                 if k != "default_instance"] or ["standard"]
        for inst in insts:
            try:
                rows = cache.get(f"radarr.movies.{inst}.full") or []
            except Exception:
                continue
            for v in (rows.values() if isinstance(rows, dict) else rows):
                if not isinstance(v, dict):
                    continue
                tmdb = v.get("tmdbId", v.get("tmdb_id"))
                title = v.get("title")
                if tmdb is not None and title:
                    try:
                        movie_map.setdefault(_norm(title), int(tmdb))
                    except (TypeError, ValueError):
                        pass
        # Sonarr owned-episode inventory parquet carries series_title + series_tvdb_id.
        base = getattr(getattr(cache, "key_builder", None), "base_dir", None)
        if base is not None:
            try:
                import glob
                import pandas as pd
                for p in glob.glob(str(base / "sonarr" / "**" / "owned_episodes.parquet"),
                                   recursive=True):
                    try:
                        df = pd.read_parquet(p, columns=["series_title", "series_tvdb_id"])
                    except Exception:
                        continue
                    for st, tv in zip(df["series_title"], df["series_tvdb_id"]):
                        if not st or tv is None or pd.isna(tv):
                            continue
                        try:
                            show_map.setdefault(_norm(st), int(tv))
                        except (TypeError, ValueError):
                            pass
            except Exception:
                pass
        return movie_map, show_map

    def _read_history(self) -> list:
        rows = self.global_cache.get("tautulli/history/all") if self.global_cache else None
        if rows:
            return rows
        wh = getattr(self.tautulli, "watch_history", None)
        if wh is not None and hasattr(wh, "get_all_history_cached"):
            try:
                return wh.get_all_history_cached(user_id=None) or []
            except Exception:
                return []
        return []

    def _tracked_users(self) -> list:
        """``[{safe_user, tautulli_user_id, title?}]`` — the watch-id ↔ watchlist-path bridge.
        Prefer the live PlexUsersManager (carries both jointly); fall back to plex/identity_map."""
        try:
            pum = self.registry.get("manager", "PlexUsersManager") if self.registry else None
            tracked = getattr(pum, "tracked_users", None)
            if tracked:
                return list(tracked)
        except Exception:
            pass
        idmap = (self.global_cache.get("plex/identity_map") if self.global_cache else None) or {}
        return [{"safe_user": e.get("safe_key"), "tautulli_user_id": e.get("tautulli_user_id"),
                 "title": e.get("tautulli_username")}
                for e in idmap.values() if isinstance(e, dict)]

    # ── pure-ish assembly (testable: all inputs injected) ───────────────────────────
    def _bucket_history(self, rows, movie_map, show_map, *, now, thr, grace):
        """Bucket raw history by user_id → per_user watched/started sets, + each user's last
        overall activity (the watchlist windowing anchor). PURE given its args."""
        grace_cut = now - timedelta(days=max(0, int(grace)))
        buckets: dict = {}                         # (uid, media, tid) → {"watched": bool, "ts": dt|None}
        last_activity: dict = {}                   # uid → latest watch dt (any title)
        for row in (rows or []):
            uid = str(row.get("user_id") or "").strip()
            if not uid:
                continue
            dt = _epoch_dt(row.get("date"))
            if dt is not None and (uid not in last_activity or dt > last_activity[uid]):
                last_activity[uid] = dt
            mt = row.get("media_type")
            if mt == "movie":
                tid, media = _resolve_id(movie_map, row.get("title")), "movies"
            elif mt == "episode":
                tid, media = _resolve_id(show_map, row.get("grandparent_title")), "shows"
            else:
                continue
            if tid is None:
                continue
            pct = (row.get("percent_complete") or 0) / 100.0
            watched = pct >= thr
            if not watched and not (dt is not None and dt >= grace_cut):
                continue                           # stale partial play → not engaged via this title
            k = (uid, media, int(tid))
            cur = buckets.get(k)
            if cur is None:
                buckets[k] = {"watched": watched, "ts": dt}
            else:
                cur["watched"] = cur["watched"] or watched
                if dt is not None and (cur["ts"] is None or dt > cur["ts"]):
                    cur["ts"] = dt

        per_user: dict = {}
        for (uid, media, tid), info in buckets.items():
            slot = per_user.setdefault(uid, _skeleton())
            bucket = "watched" if info["watched"] else "started"
            slot[bucket][media][tid] = info["ts"].isoformat() if info["ts"] else None
        return per_user, last_activity

    def _attach_watchlist(self, per_user, tracked, last_activity):
        """Attach each user's watchlist (already id-resolved) under str(user_id), anchored on their
        last activity so a fully-dormant member's watchlist intent expires with the dormancy window."""
        for entry in (tracked or []):
            safe = entry.get("safe_user") or entry.get("safe_key")
            uid = entry.get("tautulli_user_id")
            if uid is None or not safe:
                continue                           # unmatched profile → no watch bucket; never gate on it
            uid = str(uid)
            wl = (self.global_cache.get(f"plex/users/{safe}/watchlist") if self.global_cache else None) or []
            slot = per_user.setdefault(uid, _skeleton())
            anchor = last_activity.get(uid)
            anchor_iso = anchor.isoformat() if anchor else None
            for item in wl:
                if not isinstance(item, dict):
                    continue
                ids = item.get("ids") or {}
                t = item.get("type")
                if t == "movie" and ids.get("tmdb") is not None:
                    try:
                        slot["watchlist"]["movies"][int(ids["tmdb"])] = anchor_iso
                    except (TypeError, ValueError):
                        pass
                elif t == "show" and ids.get("tvdb") is not None:
                    try:
                        slot["watchlist"]["shows"][int(ids["tvdb"])] = anchor_iso
                    except (TypeError, ValueError):
                        pass
        return per_user

    def _exclude_set(self, raw, tracked) -> set:
        """Accept user_ids OR usernames/safe_users in exclude_users; translate names → user_id."""
        name_to_uid: dict = {}
        for entry in (tracked or []):
            uid = entry.get("tautulli_user_id")
            if uid is None:
                continue
            for nk in (entry.get("safe_user"), entry.get("safe_key"), entry.get("title")):
                if nk:
                    name_to_uid[str(nk)] = str(uid)
        out = set()
        for e in (raw or []):
            es = str(e)
            out.add(name_to_uid.get(es, es))
        return out

    # ── run ─────────────────────────────────────────────────────────────────────────
    @timeit("run")
    def run(self) -> dict:
        cfg = (self.config.get("saga_retention", {}) if self.config else {}) or {}
        if not cfg.get("enabled"):
            self.logger.log_debug("[SagaRetention] disabled (saga_retention.enabled=false) — skipping.")
            return {}

        src = (self.global_cache.get("plex/playlists/universe_source") if self.global_cache else None) or {}
        member_sets = saga_member_sets(src)
        if not member_sets:
            return self._write_empty("no universe source")     # no saga membership → nothing to gate

        now = datetime.now(timezone.utc)
        thr = float(cfg.get("completion_threshold", 0.8) or 0.0)
        grace = int(cfg.get("engagement_grace_days", 7) or 0)
        movie_map, show_map = self._title_id_maps()
        # Watch history may legitimately be empty (a watchlist-only household still gates); only a
        # missing universe source is a hard bail.
        per_user, last_activity = self._bucket_history(
            self._read_history(), movie_map, show_map, now=now, thr=thr, grace=grace)

        tracked = self._tracked_users()
        per_user = self._attach_watchlist(per_user, tracked, last_activity)
        if not per_user:
            return self._write_empty("no engaged users")

        gates = compute_saga_gates(
            member_sets, per_user, now=now,
            dormancy_days=cfg.get("dormancy_window_days", 90),
            expiry_boost_days=cfg.get("expiry_boost_days", 30),
            watchlist_hold_policy=cfg.get("watchlist_hold_policy", "windowed"),
            exclude_users=self._exclude_set(cfg.get("exclude_users"), tracked),
            quorum=cfg.get("quorum"),
        )
        if self.global_cache:
            self.global_cache.set(_GATES_KEY, gates)
        held = len(gates["movies"]) + len(gates["shows"])
        self.logger.log_info(
            f"[SagaRetention] {len(gates['gate_user_count'])} engaged saga(s) → {held} held title(s); "
            f"{len(gates['expiring_by_user'])} viewer(s) with use-it-or-lose-it items.")
        return {"sagas": len(gates["gate_user_count"]), "held": held}

    def _write_empty(self, why: str) -> dict:
        if self.global_cache:
            self.global_cache.set(_GATES_KEY, dict(_EMPTY_GATES))
        self.logger.log_debug(f"[SagaRetention] no holds ({why}) — wrote empty gates (fail-open).")
        return {"sagas": 0, "held": 0}

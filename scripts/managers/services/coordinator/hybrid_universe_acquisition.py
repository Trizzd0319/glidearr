"""
coordinator/hybrid_universe_acquisition.py — the hybrid (film + TV) universe acquisition capstone.
====================================================================================================
Phase-7 coordinator. Once the HOUSEHOLD has watched ≥1 member of a timeline saga (MCU, Star Trek,
One Chicago, …), it backfills the saga's UNOWNED members START-first — films via Radarr, shows via
Sonarr — bounded by ``acquisition.universe.max_per_run``, crossover-deduped, dry-run-safe.

Pure data layer reused (no new acquisition logic invented here):
  * unified_universe_order(source, owned_movie_tmdbs, owned_tvdb_to_sid, include_unowned=True)
  * universe_acquire_plan(unified, watched_movie_tmdbs, watched_show_tvdbs)
  * AcquisitionManager.ensure_owned_and_grab(tmdb) / .ensure_show_owned_and_grab(tvdb)  ← the grabs

EVERY grab is routed through those two primitives (never a raw gateway) so dry_run + the free-space
band + the deferred backlog are honoured. Gated by BOTH acquisition.universe.enabled AND
plex.playlists.universe_timeline.enabled (the membership source) — default OFF → byte-identical.
See services/coordinator/universe_acquisition.md.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.plex.playlists.universe_order import (
    unified_universe_order,
    universe_acquire_plan,
)
from scripts.support.utilities.decorators.timing import timeit


def _flatten_dedup_cap(plan, *, cap):
    """``{key: [{media,id,rank}…]}`` → ``(selected, dropped, flat)``. Flatten to ONE globally-ordered
    list of UNIQUE ``(media, id)`` — ordered by ``(rank, universe-key)`` so it's start-first and
    deterministic — then cap. A crossover member (a film in two universes) is deduped by the
    ``(media, id)`` TUPLE BEFORE the cap, so it never double-spends a slot or double-POSTs. PURE."""
    items = []
    for key, members in (plan or {}).items():
        for m in (members or []):
            items.append((m.get("rank", 0), str(key), m.get("media"), m.get("id")))
    items.sort(key=lambda t: (t[0], t[1]))
    flat, seen = [], set()
    for rank, key, media, mid in items:
        ident = (media, mid)
        if mid is None or ident in seen:
            continue
        seen.add(ident)
        flat.append({"media": media, "id": mid, "rank": rank, "universe": key})
    if cap and cap > 0:
        return flat[:cap], flat[cap:], flat
    return flat, [], flat


class HybridUniverseAcquisitionManager(BaseManager, ComponentManagerMixin):
    parent_name = "HybridUniverseAcquisitionManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "HybridUniverseAcquisitionManager"
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
        self.tautulli = kwargs.get("tautulli")

    def prepare(self) -> None:
        pass

    # ── owned inventories (for unowned-detection) ───────────────────────────────────
    def _owned_movie_tmdbs(self) -> set:
        out: set = set()
        cache = self.global_cache
        if not cache:
            return out
        insts = [k for k in (self.config.get("radarr_instances", {}) or {})
                 if k != "default_instance"] or ["standard"]
        for inst in insts:
            try:
                rows = cache.get(f"radarr.movies.{inst}.full") or []
            except Exception:
                continue
            for v in (rows.values() if isinstance(rows, dict) else rows):
                if isinstance(v, dict):
                    t = v.get("tmdbId", v.get("tmdb_id"))
                    if t is not None:
                        try:
                            out.add(int(t))
                        except (TypeError, ValueError):
                            pass
        return out

    def _sonarr_maps(self) -> "tuple[dict, dict, set]":
        """``({tvdb:int -> sid:int} owned, {sid -> tvdb}, watched_show_tvdbs: set[int])`` from the
        Sonarr parquets — owned_episodes for the tvdb↔sid bridge, episode_files for ``is_watched``.
        Best-effort (empty on missing). NO Sonarr API calls. The watched frontier is the ONLY correct
        TV signal (NOT WatchHistoryAggregator.get_all_watched_series, which mis-reads ratingKeys)."""
        tvdb_to_sid: dict = {}
        sid_to_tvdb: dict = {}
        watched: set = set()
        base = getattr(getattr(self.global_cache, "key_builder", None), "base_dir", None)
        if base is None:
            return tvdb_to_sid, sid_to_tvdb, watched
        try:
            import glob
            import pandas as pd
        except Exception:
            return tvdb_to_sid, sid_to_tvdb, watched
        for p in glob.glob(str(base / "sonarr" / "**" / "owned_episodes.parquet"), recursive=True):
            try:
                df = pd.read_parquet(p, columns=["series_id", "series_tvdb_id"])
            except Exception:
                continue
            for sid, tv in zip(df["series_id"], df["series_tvdb_id"]):
                if sid is None or tv is None or pd.isna(sid) or pd.isna(tv):
                    continue
                try:
                    sid_i, tv_i = int(sid), int(tv)
                except (TypeError, ValueError):
                    continue
                tvdb_to_sid.setdefault(tv_i, sid_i)
                sid_to_tvdb.setdefault(sid_i, tv_i)
        for p in glob.glob(str(base / "sonarr" / "**" / "episode_files.parquet"), recursive=True):
            try:
                df = pd.read_parquet(p, columns=["series_id", "is_watched"])
            except Exception:
                continue
            mask = df["is_watched"].fillna(False).astype(bool)
            for sid in df.loc[mask, "series_id"].dropna():
                try:
                    tv = sid_to_tvdb.get(int(sid))
                except (TypeError, ValueError):
                    continue
                if tv is not None:
                    watched.add(tv)
        return tvdb_to_sid, sid_to_tvdb, watched

    def _watched_movie_tmdbs(self) -> set:
        """Household-watched movie tmdbs from ``tautulli/group/<g>/tmdb_completions`` — STRING keys
        (JSON round-trip) so ``int(k)`` is mandatory; engaged PER-ENTRY at ``pct >= threshold``."""
        out: set = set()
        if not self.global_cache:
            return out
        groups = (self.config.get("rating_groups") or {}) or {"household": {}}
        for group in groups:
            raw = self.global_cache.get(f"tautulli/group/{group}/tmdb_completions") or {}
            if not isinstance(raw, dict):
                continue
            for k, v in raw.items():
                if isinstance(v, dict) and float(v.get("pct", 0.0) or 0.0) >= float(v.get("threshold", 0.9) or 0.9):
                    try:
                        out.add(int(k))
                    except (TypeError, ValueError):
                        pass
        return out

    # ── run ───────────────────────────────────────────────────────────────────────
    @timeit("run")
    def run(self) -> dict:
        uni = (((self.config or {}).get("acquisition", {}) or {}).get("universe", {}) or {})
        pl_uni = ((((self.config or {}).get("plex", {}) or {}).get("playlists", {}) or {})
                  .get("universe_timeline", {}) or {})
        if not (uni.get("enabled") and pl_uni.get("enabled")):
            self.logger.log_debug("[UniverseAcquire] disabled (needs acquisition.universe.enabled AND "
                                  "plex.playlists.universe_timeline.enabled) — skipping.")
            return {"enabled": False}

        source = (self.global_cache.get("plex/playlists/universe_source") if self.global_cache else None) or {}
        if not source.get("universes"):
            return self._noop("no universe source")

        owned_m = self._owned_movie_tmdbs()
        tvdb_to_sid, _sid_to_tvdb, watched_shows = self._sonarr_maps()
        watched_movies = self._watched_movie_tmdbs()

        unified = unified_universe_order(source, owned_m, tvdb_to_sid, include_unowned=True)
        plan = universe_acquire_plan(unified, watched_movies, watched_shows)
        cap = int(uni.get("max_per_run", 5) or 0)
        selected, dropped, flat = _flatten_dedup_cap(plan, cap=cap)
        if not flat:
            return self._noop("no engaged saga gaps")
        if dropped:
            self.logger.log_info(
                f"[UniverseAcquire] {len(flat)} unowned saga member(s) planned; capped to "
                f"{len(selected)} this run (max_per_run={cap}); {len(dropped)} deferred to next run.")

        acq = self.registry.get("manager", "AcquisitionManager") if self.registry else None
        gateways = {
            "radarr": ArrGateway("radarr", getattr(self.radarr, "instance_manager", None), self.config, self.logger),
            "sonarr": ArrGateway("sonarr", getattr(self.sonarr, "instance_manager", None), self.config, self.logger),
        }
        band_cache: dict = {}
        want_movies = bool(uni.get("movies", True))
        want_tv = bool(uni.get("tv", True))
        rows, by_action = [], {}
        for m in selected:
            if acq is None:
                res = {"action": "skipped", "reason": "no acquisition manager"}
            elif m["media"] == "movie":
                res = (acq.ensure_owned_and_grab(int(m["id"]), gateways=gateways, band_cache=band_cache)
                       if want_movies else {"action": "skipped", "reason": "movies off"})
            elif m["media"] == "show":
                res = (acq.ensure_show_owned_and_grab(int(m["id"]), gateways=gateways, band_cache=band_cache)
                       if want_tv else {"action": "skipped", "reason": "tv off"})
            else:
                res = {"action": "skipped", "reason": "unknown media"}
            a = res.get("action", "?")
            by_action[a] = by_action.get(a, 0) + 1
            rows.append((m["universe"], m["media"], m["id"], m["rank"], a, res.get("title", "")))

        self.logger.log_info(
            f"[UniverseAcquire] {'[dry-run] ' if self.dry_run else ''}{len(plan)} engaged saga(s) → "
            f"{len(selected)} member(s) this run: {by_action}")
        for u, media, mid, rank, a, title in rows:
            self.logger.log_debug(f"  {u} {media} {mid} rank={rank} → {a} {title}")
        return {"enabled": True, "action": "previewed" if self.dry_run else "grabbed",
                "planned": len(flat), "selected": len(selected), "dropped": len(dropped),
                "by_action": by_action, "universes": len(plan)}

    def _noop(self, why: str) -> dict:
        self.logger.log_debug(f"[UniverseAcquire] nothing to acquire ({why}).")
        return {"enabled": True, "action": "noop", "planned": 0, "selected": 0,
                "dropped": 0, "by_action": {}, "universes": 0}

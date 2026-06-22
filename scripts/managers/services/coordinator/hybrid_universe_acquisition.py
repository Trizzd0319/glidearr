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
    saga_display_name,
    unified_universe_order,
    universe_acquire_plan,
)
from scripts.support.utilities.decorators.timing import timeit


def _flatten_dedup_cap(plan, *, cap, tiers=None):
    """``{key: [{media,id,rank}…]}`` → ``(selected, dropped, flat)``. Flatten to ONE globally-ordered
    list of UNIQUE ``(media, id)`` — ordered by ``(tier, rank, universe-key)`` so CURATED/known sagas
    fill their gaps before auto-generated ones, start-first within each, deterministic on the key —
    then cap. ``tiers`` = ``{key: priority}`` (0 curated/known, 1 owned-stem-derived, 2 auto-generated;
    a key missing → 0). A crossover member (a film in two universes) is deduped by the ``(media, id)``
    TUPLE BEFORE the cap — keeping its lowest-tier (highest-priority) universe — so it never
    double-spends a slot or double-POSTs. PURE."""
    tiers = tiers or {}
    items = []
    for key, members in (plan or {}).items():
        tier = int(tiers.get(key, 0) or 0)
        for m in (members or []):
            items.append((tier, m.get("rank", 0), str(key), m.get("media"), m.get("id")))
    items.sort(key=lambda t: (t[0], t[1], t[2]))
    flat, seen = [], set()
    for tier, rank, key, media, mid in items:
        ident = (media, mid)
        if mid is None or ident in seen:
            continue
        seen.add(ident)
        flat.append({"media": media, "id": mid, "rank": rank, "universe": key, "tier": tier})
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
        tiers = {k: (v.get("tier", 0) if isinstance(v, dict) else 0)
                 for k, v in (source.get("universes") or {}).items()}      # curated < derived < generated
        selected, dropped, flat = _flatten_dedup_cap(plan, cap=cap, tiers=tiers)
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
        decided, by_action = {}, {}                          # (media,id) -> grab action; action tallies
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
            decided[(m["media"], m["id"])] = a

        # Per-saga PREVIEW (titled + comprehensive) — ALL engaged sagas, members resolved to real
        # titles, with the engagement reason, the start-first entry point and the per-member decision.
        # Cached for the future GUI; rendered as the decision table below.
        sagas = self._build_saga_preview(plan, unified, watched_movies, watched_shows, selected, decided)
        preview = {"dry_run": self.dry_run, "max_per_run": cap, "planned": len(flat),
                   "selected": len(selected), "deferred": len(dropped), "sagas": sagas}
        if self.global_cache:
            try:
                self.global_cache.set("plex/playlists/universe_acquire_preview", preview)
            except Exception:
                pass

        self.logger.log_info(
            f"[UniverseAcquire] {'[dry-run] ' if self.dry_run else ''}{len(plan)} engaged saga(s) → "
            f"{len(selected)} member(s) this run: {by_action}")
        self._log_saga_table(sagas)
        return {"enabled": True, "action": "previewed" if self.dry_run else "grabbed",
                "planned": len(flat), "selected": len(selected), "dropped": len(dropped),
                "by_action": by_action, "universes": len(plan), "sagas": len(sagas)}

    # ── per-saga preview (the GUI-facing structure + the decision table) ─────────────
    def _titles_map(self) -> dict:
        """``{(media, id): title}`` — TV-franchise members from ``plex/playlists/saga_member_titles``
        (the builder publishes owned + catalog titles there), MOVIE members from owned Radarr first,
        then the mdblist universe lists' own titles (``universe_source`` per-entry ``titles``) so an
        UNOWNED film still resolves to a real name. Falls back to ``"media id"`` only when nothing
        names it. Precedence: saga-titles / owned Radarr win; the list titles fill the remaining gaps."""
        out: dict = {}
        if not self.global_cache:
            return out
        raw = self.global_cache.get("plex/playlists/saga_member_titles") or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[("show", int(k))] = v
                except (TypeError, ValueError):
                    pass
        insts = [k for k in (self.config.get("radarr_instances", {}) or {})
                 if k != "default_instance"] or ["standard"]
        for inst in insts:
            rows = self.global_cache.get(f"radarr.movies.{inst}.full") or []
            for v in (rows.values() if isinstance(rows, dict) else rows):
                if isinstance(v, dict) and v.get("title"):
                    t = v.get("tmdbId", v.get("tmdb_id"))
                    if t is not None:
                        try:
                            out.setdefault(("movie", int(t)), v["title"])
                        except (TypeError, ValueError):
                            pass
        # mdblist list titles (fills UNOWNED films + any show the list itself named): each universe
        # entry carries ``titles`` = {"movie:<tmdb>"|"show:<tvdb>": name}. setdefault → owned wins.
        src = self.global_cache.get("plex/playlists/universe_source") or {}
        if isinstance(src, dict):
            for entry in (src.get("universes") or {}).values():
                if not isinstance(entry, dict):
                    continue
                for k, v in (entry.get("titles") or {}).items():
                    media, _, sid = str(k).partition(":")
                    if media in ("movie", "show") and sid.isdigit() and v:
                        out.setdefault((media, int(sid)), v)
        return out

    def _build_saga_preview(self, plan, unified, watched_movies, watched_shows, selected, decided) -> list:
        """One record per ENGAGED saga (every saga in ``plan``, not just the capped selection): the
        full member list with real titles, which members ENGAGED it (watched), where backfill STARTS,
        and each unowned member's decision (this run vs deferred). The decisive, GUI-ready structure."""
        titles = self._titles_map()

        def _name(media, mid):
            return titles.get((media, mid)) or f"{media} {mid}"

        watched = {("movie", t) for t in (watched_movies or ())} | {("show", t) for t in (watched_shows or ())}
        selected_ids = {(m["media"], m["id"]) for m in selected}
        out: list = []
        for key in plan:
            members = sorted(unified.get(key, []), key=lambda x: x.get("rank", 0))
            engaged_by = [_name(x["media"], x["id"]) for x in members if (x["media"], x["id"]) in watched]
            owned_ct = sum(1 for x in members if x.get("owned"))
            backfill = []
            for x in members:
                if x.get("owned"):
                    continue
                ident = (x["media"], x["id"])
                this_run = ident in selected_ids
                backfill.append({"media": x["media"], "id": x["id"], "title": _name(*ident),
                                 "rank": x.get("rank", 0), "this_run": this_run,
                                 "decision": (decided.get(ident, "?") if this_run else "deferred")})
            if not backfill:
                continue
            out.append({
                "saga": key, "display": saga_display_name(key),
                "total_members": len(members), "owned": owned_ct, "engaged_by": engaged_by,
                "start_at": backfill[0]["title"],
                "why": (f"household watched {', '.join(engaged_by[:2])}"
                        + (f" (+{len(engaged_by) - 2} more)" if len(engaged_by) > 2 else "")
                        + f" → acquire its {len(backfill)} unowned member(s) START-first"),
                "backfill": backfill,
            })
        out.sort(key=lambda s: (-len(s["backfill"]), s["display"]))
        return out

    def _log_saga_table(self, sagas) -> None:
        if not sagas:
            return
        rows = []
        for s in sagas:
            acts: dict = {}
            for b in s["backfill"]:
                if b["this_run"]:
                    acts[b["decision"]] = acts.get(b["decision"], 0) + 1
            deferred = sum(1 for b in s["backfill"] if not b["this_run"])
            dec = ", ".join(f"{n} {a}" for a, n in acts.items()) or "—"
            if deferred:
                dec += f"; {deferred} deferred"
            rows.append([
                s["display"],
                ", ".join(s["engaged_by"][:2]) or "—",
                s["start_at"],
                ", ".join(b["title"] for b in s["backfill"]),
                f"{s['owned']}/{s['total_members']}",
                dec,
            ])
        self.logger.log_table(
            ["saga", "engaged by (why)", "start at", "backfilling (unowned, start-first)", "have", "this run"],
            rows,
            title=f"{'[dry-run] ' if self.dry_run else ''}Universe acquisition",
            caption="Per saga: the watched member that engaged it, where backfill STARTS, the unowned "
                    "members to acquire (start-first, crossover-deduped), how much you already own, and "
                    "this run's decision — the rest deferred by acquisition.universe.max_per_run.",
        )

    def _noop(self, why: str) -> dict:
        self.logger.log_debug(f"[UniverseAcquire] nothing to acquire ({why}).")
        return {"enabled": True, "action": "noop", "planned": 0, "selected": 0,
                "dropped": 0, "by_action": {}, "universes": 0}

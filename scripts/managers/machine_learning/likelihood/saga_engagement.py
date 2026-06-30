"""
saga_engagement.py — gather the HOUSEHOLD, cross-media saga engagement that feeds the saga QUALITY
credit (:func:`watch_likelihood.saga_credit`).
================================================================================
The per-member math is PURE (``saga_order.saga_member_engagement`` + ``watch_likelihood.
saga_credit``); this module is the thin I/O layer that assembles their inputs from the caches the
universe-acquisition capstone already populates, so movie + TV engagement is read ONE way:

  * universe membership / timeline  → ``plex/playlists/universe_source`` (movies+shows, ranked)
  * owned movie tmdbs               → ``radarr.movies.<inst>.full``
  * owned tvdb→sid + watched shows  → the Sonarr ``owned_episodes`` / ``episode_files`` parquets
  * HOUSEHOLD-watched movie tmdbs   → ``tautulli/group/<group>/tmdb_completions`` (pct ≥ threshold)

Opt-in via ``scoring.saga_credit.enabled`` (default OFF → returns ``{}`` → the credit is byte-identical
inert). Every reader is best-effort: a missing cache / parquet degrades to empty, never raises — the
callers (``_apply_universe_credit`` in the Radarr + Sonarr pre-pass) further wrap this in try/except.
"""
from __future__ import annotations

from scripts.managers.machine_learning.likelihood.saga_order import (
    saga_member_engagement,
    unified_universe_order,
)


def saga_credit_enabled(config) -> bool:
    """Opt-in flag (default OFF). When off the whole feature is inert / byte-identical."""
    try:
        return bool((((config or {}).get("scoring") or {}).get("saga_credit") or {}).get("enabled", False))
    except Exception:
        return False


def household_member_count(config, global_cache=None) -> int:
    """Best-effort household member count for the √(ref/N) grace-window scaling. From ``rating_groups``
    member lists if present, else the distinct Tautulli users, else 0 → the caller passes None so
    :func:`watch_likelihood.saga_credit` falls back to the reference size (the base window)."""
    try:
        groups = (config or {}).get("rating_groups") or {}
        members: set = set()
        for _g, v in (groups.items() if isinstance(groups, dict) else []):
            mem = v.get("members") if isinstance(v, dict) else v
            for m in (mem or []):
                members.add(str(m))
        if members:
            return len(members)
    except Exception:
        pass
    if global_cache is not None:
        for key in ("tautulli/users", "tautulli/user_list", "tautulli/platforms"):
            try:
                raw = global_cache.get(key)
            except Exception:
                raw = None
            if isinstance(raw, (list, dict)) and len(raw):
                return len(raw)
    return 0


def _owned_movie_tmdbs(global_cache, config) -> set:
    out: set = set()
    insts = [k for k in ((config or {}).get("radarr_instances", {}) or {})
             if k != "default_instance"] or ["standard"]
    for inst in insts:
        try:
            rows = global_cache.get(f"radarr.movies.{inst}.full") or []
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


def _sonarr_maps(global_cache) -> "tuple[dict, set]":
    """``({tvdb -> sid} owned, watched_show_tvdbs)`` from the Sonarr parquets — owned_episodes for the
    tvdb↔sid bridge, episode_files for the household ``is_watched`` frontier. Best-effort (empty on
    missing); no Sonarr API calls. Mirrors HybridUniverseAcquisitionManager._sonarr_maps."""
    tvdb_to_sid: dict = {}
    sid_to_tvdb: dict = {}
    watched: set = set()
    base = getattr(getattr(global_cache, "key_builder", None), "base_dir", None)
    if base is None:
        return tvdb_to_sid, watched
    try:
        import glob
        import pandas as pd
    except Exception:
        return tvdb_to_sid, watched
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
    return tvdb_to_sid, watched


def _watched_movie_tmdbs(global_cache, config) -> set:
    """Household-watched movie tmdbs from ``tautulli/group/<g>/tmdb_completions`` (engaged per-entry at
    ``pct >= threshold``). STRING keys (JSON round-trip) → ``int(k)`` mandatory."""
    out: set = set()
    groups = ((config or {}).get("rating_groups") or {}) or {"household": {}}
    for group in groups:
        try:
            raw = global_cache.get(f"tautulli/group/{group}/tmdb_completions") or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            continue
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            try:                                          # a single malformed entry must skip itself,
                if float(v.get("pct", 0.0) or 0.0) >= float(v.get("threshold", 0.9) or 0.9):
                    out.add(int(k))                       # not abort the whole reader
            except (TypeError, ValueError):
                continue
    return out


def saga_credit_preview_key(service: str, instance: str) -> str:
    """global_cache key for the per-service/instance saga-credit preview snapshot the future web GUI
    reads — the SAME data the run-summary table shows. Single source of truth for writer + GUI."""
    return f"universe/saga_credit_preview/{service}/{instance}"


def emit_saga_credit_preview(global_cache, logger, config, service, instance, items) -> None:
    """Surface the per-title saga-credit detail BOTH in the run log (a run-summary table) AND offline
    (a ``global_cache`` snapshot for the future GUI) — so the data is captured the moment the credit is
    computed. ``items`` = ``[{id, title, saga, caught_up, depth, days, credit}…]`` (raw numbers). Each
    is enriched with ``floor_likelihood`` (the likelihood the credit ALONE yields for an unwatched
    title — the saga feature's direct contribution) + ``floor_resolution``. No-op on empty items;
    best-effort (never raises)."""
    if not items:
        return
    try:
        from scripts.managers.machine_learning.likelihood.watch_likelihood import (
            resolution_cap_for_likelihood,
            watch_likelihood,
        )
        enriched = []
        for it in items:
            cr = float(it.get("credit", 0.0) or 0.0)
            floor_l = watch_likelihood({"watch_count": 0, "universe_credit": cr}, config=config)
            enriched.append({**it, "floor_likelihood": round(float(floor_l), 1),
                             "floor_resolution": int(resolution_cap_for_likelihood(floor_l, config=config))})
        enriched.sort(key=lambda x: -x["credit"])
        rs = getattr(global_cache, "run_summary", None) if global_cache is not None else None
        if rs is not None:
            rows = [[str(it.get("title", it["id"]))[:34], str(it.get("saga", ""))[:18],
                     f"{it['caught_up'] * 100:.0f}%", f"{it['depth'] * 100:.0f}%",
                     f"{it['days']:.0f}d", f"+{it['credit']:.1f}",
                     f"{it['floor_likelihood']:.0f}", f"{it['floor_resolution']}p"]
                    for it in enriched[:50]]
            try:
                rs.add_rows(service, "Saga credit (caught-up/depth)", instance,
                            ["Title", "Saga", "Caught-up", "Depth", "Avail", "Credit", "Floor L", "Res"],
                            rows, order=33)
            except Exception:
                pass
        if global_cache is not None:
            try:
                global_cache.set(saga_credit_preview_key(service, instance),
                                 {"service": service, "instance": instance,
                                  "count": len(enriched), "items": enriched})
            except Exception:
                pass
        if logger is not None:
            _fourk = sum(1 for i in enriched if i["floor_resolution"] >= 2160)
            _remux = sum(1 for i in enriched if i["floor_likelihood"] >= 90)
            logger.log_info(
                f"[Universe] saga credit ({service}/{instance}): {len(enriched)} member(s) boosted "
                f"(max +{enriched[0]['credit']:.1f} watch-counts; {_fourk} reach 4K, "
                f"{_remux} reach Remux unwatched).")
    except Exception:
        pass


def gather_saga_engagement(global_cache, config) -> dict:
    """``{(media, id): {"caught_up_frac", "saga_watched_frac"}}`` — household, cross-media engagement
    for every saga member (movie→tmdb, show→tvdb). ``{}`` when the flag is off, ``global_cache`` is
    absent, the universe source is missing, or nothing is engaged — so callers ``max``-combine with the
    existing credit and stay byte-identical when inert."""
    if not saga_credit_enabled(config) or global_cache is None:
        return {}
    try:
        source = global_cache.get("plex/playlists/universe_source") or {}
        if not (isinstance(source, dict) and source.get("universes")):
            return {}
        owned_m = _owned_movie_tmdbs(global_cache, config)
        tvdb_to_sid, watched_shows = _sonarr_maps(global_cache)
        watched_movies = _watched_movie_tmdbs(global_cache, config)
        unified = unified_universe_order(source, owned_m, tvdb_to_sid, include_unowned=True)
        return saga_member_engagement(unified, watched_movies, watched_shows)
    except Exception:
        return {}                                         # best-effort: degrade to inert, never raise

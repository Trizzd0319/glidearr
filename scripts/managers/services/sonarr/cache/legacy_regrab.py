"""
legacy_regrab.py — the shared legacy-codec re-grab core.
========================================================
ONE Sonarr interactive search (``GET /release?episodeId=``) per owned legacy-codec episode file
confirms a modern-codec release at >= the file's current resolution; if one exists it grabs that
release (``POST /release`` by guid), and Sonarr replaces the file on IMPORT — nothing is deleted
first, so a file with no modern replacement is left untouched and never lost.

Mirrors ``pilot_interactive.interactive_pilot_search``: a core with Sonarr I/O injected as a
``make_request`` callable, shared by two callers —
  * ``SonarrCacheEpisodeFilesManager.regrab_legacy_codecs`` — small inline batches + dry-run preview.
  * ``pilot_search_daemon.py`` (mode ``"legacy_regrab"``) — large batches drained out-of-process so
    the run never blocks on the slow per-file interactive searches.

The cooldown ledger (``global_cache 'sonarr/legacy_regrab/{instance}'``) doubles as the resume
checkpoint: each grab / no-release decision is persisted AS IT HAPPENS, so a crash mid-drain leaves
the finished files recorded and the next run simply doesn't re-enqueue them (no double-grab).
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from scripts.managers.machine_learning.quality_analytics.legacy_codec import (
    best_modern_release,
    release_resolution,
)


def ledger_key(instance: str) -> str:
    """global_cache key for the per-instance cooldown/resume ledger ({episode_file_id: {at, result}})."""
    return f"sonarr/legacy_regrab/{instance}"


def run_legacy_regrab(*, make_request, logger, global_cache, instance, items,
                      max_workers: int = 3, dry_run: bool = False) -> dict:
    """Process ``items`` — a list of dicts ``{series_id, episode_file_id, resolution, series_title,
    season_number, episode_number, video_codec}`` (already cooldown-filtered + ordered by the caller).

    For each: resolve the S/E id, interactive-search, pick the best modern replacement, and (live)
    grab it by guid + record the decision in the ledger. Returns
    ``{checked, grabbed, previewed, no_release, failed, preview:[[label, current, release, res], ...]}``.
    Concurrency is bounded by ``max_workers`` (interactive searches are slow). Live writes the ledger
    incrementally; dry-run records NOTHING (a preview must not burn cooldowns)."""
    items = [i for i in (items or [])
             if i.get("series_id") is not None and i.get("episode_file_id") is not None]
    stats = {"checked": 0, "grabbed": 0, "previewed": 0, "no_release": 0, "failed": 0, "preview": []}
    if not items:
        return stats

    lock = threading.Lock()
    ep_cache: dict = {}
    lkey = ledger_key(instance)
    ledger = dict((global_cache.get(lkey) if global_cache else None) or {})

    def _episode(sid, fid):
        with lock:
            emap = ep_cache.get(sid)
        if emap is None:
            eps = make_request(instance, f"episode?seriesId={sid}", fallback=[]) or []
            emap = {}
            for e in eps:
                f = e.get("episodeFileId")
                if f is not None:
                    emap.setdefault(int(f), e)
            with lock:
                ep_cache[sid] = emap
        return emap.get(int(fid))

    def _persist(fid, result):
        if global_cache is None:
            return
        with lock:
            ledger[str(fid)] = {"at": datetime.now(tz=timezone.utc).isoformat(), "result": result}
            try:
                global_cache.set(lkey, dict(ledger))
            except Exception:
                pass

    def _one(item):
        sid, fid = int(item["series_id"]), int(item["episode_file_id"])
        ep = _episode(sid, fid)
        eid = ep.get("id") if ep else None
        if not eid:
            return
        releases = make_request(instance, f"release?episodeId={eid}", fallback=None)
        if releases is None:
            return  # transient search failure — DON'T record, retry next run
        best = best_modern_release(releases if isinstance(releases, list) else [],
                                   int(item.get("resolution") or 0))
        sn, en = ep.get("seasonNumber"), ep.get("episodeNumber")
        label = (f"{str(item.get('series_title') or '?')[:24]} S{sn:02d}E{en:02d}"
                 if isinstance(sn, int) and isinstance(en, int)
                 else str(item.get("series_title") or f"series {sid}"))
        cur = f"{item.get('video_codec')}@{item.get('resolution') or '?'}"
        if not best:
            with lock:
                stats["no_release"] += 1
            if not dry_run:
                _persist(fid, "no_release")
            return
        relt = (best.get("title") or "?")[:48]
        rres = release_resolution(best)
        if dry_run:
            with lock:
                stats["previewed"] += 1
                stats["preview"].append([label, cur, relt, f"{rres or '?'}p"])
            logger.log_info(f"  [LegacyRegrab] [dry_run] would re-grab {label}: {cur} -> {relt} [{rres or '?'}p]")
            return
        ok = False
        try:
            res = make_request(instance, "release", method="POST",
                               payload={"guid": best.get("guid"), "indexerId": best.get("indexerId")},
                               fallback=None)
            ok = res is not None
        except Exception as e:
            logger.log_warning(f"  [LegacyRegrab] grab error for {label}: {e}")
        if ok:
            with lock:
                stats["grabbed"] += 1
            _persist(fid, "grabbed")
            logger.log_info(f"  [LegacyRegrab] grabbed {label} -> {relt} [{rres or '?'}p]")
        else:
            with lock:
                stats["failed"] += 1

    workers = max(1, int(max_workers))
    if workers == 1 or len(items) == 1:
        for it in items:
            _one(it)
            stats["checked"] += 1
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(items)),
                                thread_name_prefix="legacy-regrab") as ex:
            futs = {ex.submit(_one, it): it for it in items}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    logger.log_warning(f"[LegacyRegrab] task crashed: {e}")
                with lock:
                    stats["checked"] += 1
    return stats

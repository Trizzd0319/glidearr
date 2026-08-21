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
                      max_workers: int = 3, dry_run: bool = False,
                      free_gb=None, acquire_floor_gb=None) -> dict:
    """Process ``items`` — a list of dicts ``{series_id, episode_file_id, resolution, series_title,
    season_number, episode_number, video_codec}`` (already cooldown-filtered + ordered by the caller).

    For each: resolve the S/E id, interactive-search, pick the best modern replacement, and (live)
    grab it by guid + record the decision in the ledger. Returns
    ``{checked, grabbed, previewed, no_release, failed, skipped_space, preview:[[label, current, release, res], ...]}``.
    Concurrency is bounded by ``max_workers`` (interactive searches are slow). Live writes the ledger
    incrementally; dry-run records NOTHING (a preview must not burn cooldowns).

    SPACE FLOOR (GLD-ACQ-30). ``free_gb`` / ``acquire_floor_gb`` are supplied by the
    caller, which already reads them for the pressure passes; below the floor this
    grabs NOTHING and says so with the figures.

    This lane is named in the 2026-08-07 incident. The next-episode and pilot lanes
    have since grown floors of their own (`_do_acquire_next_episodes`,
    `run_pilot_search`); this one had none, and it issues a DIRECT
    ``POST release`` by guid - the most immediate grab in the codebase, with no
    Sonarr-side queue check between the decision and the download.

    CHECKED ONCE, HERE, not per item: `_one` runs in a thread pool, so a per-item
    check would be both racy and N API calls. Once at the top is the honest
    granularity - free space cannot be meaningfully re-read between concurrent
    grabs anyway.

    UNKNOWN FREE SPACE DOES NOT BLOCK. Both parameters default to None, and a
    caller that supplies neither gets exactly today's behaviour. That is a
    deliberate difference from `acquire_gate.acquisition_space_ok`, which refuses
    on unknown: this function is called from several places and silently disabling
    a lane because a caller had not been updated would be a worse failure than the
    one being fixed. The caller is responsible for passing the numbers; not
    passing them is visible as an absent floor line in the log.
    """
    items = [i for i in (items or [])
             if i.get("series_id") is not None and i.get("episode_file_id") is not None]
    stats = {"checked": 0, "grabbed": 0, "previewed": 0, "no_release": 0, "failed": 0,
             "skipped_space": 0, "empty_search": 0, "preview": []}
    if not items:
        return stats

    if free_gb is not None and acquire_floor_gb is not None:
        try:
            _free, _floor = float(free_gb), float(acquire_floor_gb)
        except (TypeError, ValueError):
            _free = _floor = None
        if _free is not None and _free < _floor:
            stats["skipped_space"] = len(items)
            logger.log_info(
                f"  [LegacyRegrab] PAUSED on '{instance}': {_free:,.1f} GB free is below "
                f"the {_floor:,.0f} GB acquisition floor ({_floor - _free:,.1f} GB short). "
                f"{len(items)} re-grab(s) held — reclaim must run first (GLD-ACQ-30).")
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
        _rows = releases if isinstance(releases, list) else []
        if not _rows:
            # GLD-SON-02 - AN EMPTY LIST IS NOT A MISS. `releases is None` already
            # catches an errored request, but a search that succeeds and returns
            # ZERO rows is a different, ambiguous thing: a disabled indexer, an
            # unconfigured category, or a rate-limited provider all look exactly
            # like "no release exists for this episode".
            #
            # Recording it as `no_release` costs a 14-DAY COOLDOWN. Measured
            # 2026-08-10: 814 of 881 legacy files were sitting in that window. A
            # provider outage during one run would bench a large slice of the
            # backlog for a fortnight on evidence that was never gathered.
            #
            # So it is NOT persisted. It counts and it is named, and the file is
            # retried next run — the same conservative direction as the None branch
            # above, for the same reason *(P-C: absent conflated with empty)*.
            with lock:
                stats["empty_search"] = stats.get("empty_search", 0) + 1
            logger.log_info(
                f"  [LegacyRegrab] {str(item.get('series_title') or '?')[:28]}: indexer "
                f"returned ZERO releases — not recorded as 'no release' (could be a "
                f"disabled/rate-limited indexer); retrying next run (GLD-SON-02).")
            return
        best = best_modern_release(_rows, int(item.get("resolution") or 0))
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

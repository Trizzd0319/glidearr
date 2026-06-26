"""
jit_search.py — the shared Sonarr JIT step-down search core.
================================================================================
Per series, per target-tier GROUP: bump the series quality profile UP to the group's tier, run
``EpisodeSearch`` for ONLY that group's episodes, WAIT for the command to finish, check the queue,
and for episodes that grab nothing step DOWN one profile at a time until they grab or the group
ladder is exhausted — then REVERT the series to its original profile. Episodes that never grab are
recorded for next-run retry.

This is the SHARED payload behind two callers (mirrors ``pilot_interactive``):
  * ``SonarrCacheEpisodeFilesManager._jit_search_worker`` — the in-process non-daemon thread used for
    SMALL batches during a run (the interpreter waits for it, so the QP is always restored).
  * ``scripts/support/daemons/pilot_search_daemon.py`` (mode="jit") — the out-of-process daemon that
    drains LARGE batches so the run never blocks.

REVERT SAFETY (the reason this is trickier than pilot search): JIT temporarily BUMPS the profile then
reverts it. Detaching it to the daemon loses the "interpreter waits → QP restored" guarantee, so the
core records each series' pre-flip profile in a durable inflight store BEFORE the first flip and
clears it after the revert. ``revert_inflight_qp`` (run on daemon start, BEFORE re-processing a
resumed job) restores any series a crash left bumped — without it, a resumed job would re-read the
bumped profile as the "original" and revert to the wrong tier.

Sonarr I/O is injected: ``make_request`` (same signature as ``BaseInstanceManager._make_request``)
and ``in_queue`` (the grabbed-episode probe — injected so the in-process worker can pass its own
``_episodes_in_queue`` and tests can stub it).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time

_DONE_STATES = ("completed", "failed", "aborted", "cancelled")


def inflight_qp_key(instance: str) -> str:
    """global_cache key for the per-instance JIT inflight-profile store: ``{str(sid): original_pid}``
    of series currently flipped UP and not yet reverted — drained by ``revert_inflight_qp``."""
    return f"sonarr/jit/inflight_qp/{instance}"


def failed_upgrades_key(instance: str) -> str:
    """global_cache key for not-grabbed JIT episodes (re-enabled next run). Matches the key the
    in-process worker has always written."""
    return f"sonarr/{instance}/jit/failed_upgrades"


def episodes_in_queue(make_request, instance, ep_ids, attempts: int = 3, delay_s: float = 2.0) -> set:
    """Subset of ``ep_ids`` that currently have a download-queue item (a release was just grabbed).
    Sonarr's /queue/details wants REPEATED episodeIds params, NOT a comma-joined value. Retries
    briefly because the queue lags the EpisodeSearch command completing. Module-level so the daemon
    can build an ``in_queue`` callable from its own client."""
    wanted = {int(e) for e in ep_ids if e}
    if not wanted:
        return set()
    q = "&".join(f"episodeIds={e}" for e in wanted)
    for i in range(max(1, attempts)):
        found = set()
        try:
            resp = make_request(instance, f"queue/details?{q}", fallback=[]) or []
            for rec in resp:
                if not isinstance(rec, dict):
                    continue
                eid = rec.get("episodeId")
                if eid is None:
                    eid = (rec.get("episode") or {}).get("id")
                if eid is not None and int(eid) in wanted:
                    found.add(int(eid))
        except Exception:
            found = set()
        if found:
            return found
        if i < attempts - 1:
            time.sleep(delay_s)
    return set()


def jit_step_down_search(
    *,
    make_request,            # callable(instance, endpoint, method="GET", payload=None, fallback=None)
    in_queue,                # callable(instance, ep_ids) -> set of grabbed episodeIds
    logger,                  # exposes log_info / log_warning / log_debug
    global_cache,            # exposes get(key) / set(key, value); may be None
    instance: str,
    items: list,             # [(sid, [(tier_res, [(ep_id, season, episode), ...], [step_pid, ...]), ...]), ...]
    max_workers: int,
    poll_interval_s: float = 3.0,
    cmd_timeout_s: float = 180.0,
) -> dict:
    """Run the step-down ladder for every series CONCURRENTLY (each owns its own profile + episodeIds,
    so they're independent); the ladder WITHIN a series is strictly sequential (the shared series
    profile means a lower-target episode is never searched while flipped to a higher tier — the
    group-by-tier invariant that prevents over-grab). Returns ``{"failed": [...]}``."""
    lock = threading.Lock()   # guards the inflight-QP read-modify-write
    items = [(int(s), g) for s, g in items if g]
    if not items:
        return {"failed": []}

    def _label(sid, info=None):
        if isinstance(info, dict):
            title = (info.get("title") or "").strip()
            if title:
                tvdb = info.get("tvdbId")
                return f"sonarr/{instance} '{title}' ({'tvdb-' + str(tvdb) if tvdb else 'sid-' + str(sid)})"
        return f"sonarr/{instance} series {sid}"

    def _set_inflight(sid, original_pid):
        if global_cache is None or original_pid is None:
            return
        try:
            with lock:
                key = inflight_qp_key(instance)
                d = dict(global_cache.get(key) or {})
                d[str(sid)] = original_pid
                global_cache.set(key, d)
        except Exception:
            pass

    def _clear_inflight(sid):
        if global_cache is None:
            return
        try:
            with lock:
                key = inflight_qp_key(instance)
                d = dict(global_cache.get(key) or {})
                if str(sid) in d:
                    d.pop(str(sid), None)
                    global_cache.set(key, d)
        except Exception:
            pass

    def _wait_command(cid):
        if not cid:
            return
        start = time.time()
        while time.time() - start < cmd_timeout_s:
            cmd = make_request(instance, f"command/{cid}", fallback=None)
            if (cmd or {}).get("status") in _DONE_STATES:
                return
            time.sleep(poll_interval_s)

    def _revert(sid, original_pid):
        if original_pid is None:
            return
        fresh = make_request(instance, f"series/{sid}", fallback=None)
        if fresh and isinstance(fresh, dict) and fresh.get("qualityProfileId") != original_pid:
            fresh["qualityProfileId"] = original_pid
            make_request(instance, f"series/{sid}", method="PUT", payload=fresh)
            logger.log_info(f"  ↩️ JIT QP revert: {_label(sid, fresh)} → profile {original_pid}")

    def _process_series(sid, groups) -> list:
        failed: list = []
        original_pid = None
        label = _label(sid)
        try:
            base = make_request(instance, f"series/{sid}", fallback=None)
            if not (base and isinstance(base, dict)):
                return failed
            label = _label(sid, base)
            # Capture the pre-flip profile ONCE and record it durably BEFORE any flip, so a crash
            # mid-search can be reverted to the TRUE original (not an intermediate group's tier).
            original_pid = base.get("qualityProfileId")
            _set_inflight(sid, original_pid)

            for tier_res, eps, step_pids in groups:
                ep_meta   = {int(e[0]): (int(e[1]), int(e[2])) for e in eps if e and e[0]}
                remaining = set(ep_meta.keys())
                for pid in step_pids:
                    if not remaining:
                        break
                    s = make_request(instance, f"series/{sid}", fallback=None)
                    if not (s and isinstance(s, dict)):
                        break
                    if s.get("qualityProfileId") != pid:
                        s["qualityProfileId"] = pid
                        make_request(instance, f"series/{sid}", method="PUT", payload=s)
                    _cmd = make_request(
                        instance, "command", method="POST",
                        payload={"name": "EpisodeSearch", "episodeIds": list(remaining)},
                    )
                    _wait_command(_cmd.get("id") if isinstance(_cmd, dict) else None)
                    grabbed_now = in_queue(instance, list(remaining))
                    if grabbed_now:
                        remaining -= set(grabbed_now)
                        logger.log_info(
                            f"  ✅ JIT grab: {label} grabbed {len(grabbed_now)} ep(s) at profile "
                            f"{pid} (≤{tier_res}p tier, {len(remaining)} still searching)")
                    else:
                        logger.log_info(f"  ⏬ JIT step-down: {label} found nothing at profile {pid}")
                if remaining:
                    logger.log_info(
                        f"  ∅ JIT: {label} — {len(remaining)} ep(s) in the ≤{tier_res}p tier found no "
                        f"release across {len(step_pids)} profile(s); queued for retry next run")
                    for _eid in remaining:
                        _sn, _en = ep_meta[_eid]
                        failed.append({"series_id": sid, "season": _sn, "episode": _en})

            _revert(sid, original_pid)
            _clear_inflight(sid)
        except Exception as e:
            logger.log_warning(f"[JIT] Background step-down search failed for {label}: {e}")
            try:
                _revert(sid, original_pid)
            except Exception:
                pass
            try:
                _clear_inflight(sid)
            except Exception:
                pass
        return failed

    failed_all: list = []
    if len(items) <= 1:
        for sid, groups in items:
            failed_all.extend(_process_series(sid, groups))
    else:
        with ThreadPoolExecutor(
            max_workers=max(1, min(int(max_workers), len(items))), thread_name_prefix="jit-search"
        ) as ex:
            futures = [ex.submit(_process_series, sid, groups) for sid, groups in items]
            for fut in as_completed(futures):
                try:
                    failed_all.extend(fut.result() or [])
                except Exception as e:
                    logger.log_warning(f"[JIT] step-down series task crashed: {e}")

    if failed_all and global_cache is not None:
        try:
            key = failed_upgrades_key(instance)
            existing = global_cache.get(key) or []
            global_cache.set(key, list(existing) + failed_all)
        except Exception as e:
            logger.log_warning(f"[JIT] Could not persist failed upgrades for retry: {e}")
    return {"failed": failed_all}


def revert_inflight_qp(*, make_request, logger, global_cache, instance: str) -> int:
    """Restore any series a crashed JIT job left at a bumped profile — set each back to the TRUE
    pre-flip profile recorded in the inflight store, then clear it. Returns how many were reverted.
    MUST run on daemon start BEFORE re-processing a resumed jit job (else the re-run captures the
    bumped profile as 'original' and reverts to the wrong tier)."""
    if global_cache is None:
        return 0
    key = inflight_qp_key(instance)
    try:
        d = dict(global_cache.get(key) or {})
    except Exception:
        return 0
    if not d:
        return 0
    reverted = 0
    for sid_s, original_pid in list(d.items()):
        try:
            fresh = make_request(instance, f"series/{sid_s}", fallback=None)
            if (fresh and isinstance(fresh, dict)
                    and original_pid is not None
                    and fresh.get("qualityProfileId") != original_pid):
                fresh["qualityProfileId"] = original_pid
                make_request(instance, f"series/{sid_s}", method="PUT", payload=fresh)
                reverted += 1
                logger.log_info(
                    f"  ↩️ JIT resume-revert: sonarr/{instance} series {sid_s} → profile {original_pid}")
        except Exception as e:
            logger.log_warning(f"[JIT] resume-revert failed for series {sid_s}: {e}")
    try:
        global_cache.set(key, {})
    except Exception:
        pass
    return reverted

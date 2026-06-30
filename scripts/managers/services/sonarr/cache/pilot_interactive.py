"""
pilot_interactive.py — the per-stub Sonarr pilot interactive-search core.
================================================================================
ONE Sonarr interactive search (``GET /release?episodeId=``) per stub pilot reveals
every candidate release with its resolution, so a single call shows BOTH whether
anything exists AND every resolution that does. The search is used ONLY for
availability discovery: set the series to the LOWEST tier that has results (jumping
straight past tiers with none) and fire an ``EpisodeSearch`` so SONARR's OWN quality
+ custom-format scoring picks the actual release — we never grab a release by guid.
An EMPTY search → the stub has no release at any resolution → recorded UNACQUIRABLE
in the global-cache ledger.

This module is the SHARED payload behind two callers:
  * ``SonarrCacheEpisodeFilesManager._pilot_interactive_worker`` — the in-process
    background thread used for SMALL batches during a run.
  * ``scripts/support/daemons/pilot_search_daemon.py`` — the out-of-process daemon
    that drains LARGE batches so the run never blocks (a 9k-stub spree on a
    non-daemon thread would stall interpreter exit for the whole search).

It depends only on the pure tier brain + the stdlib, so importing it is cheap (the
daemon does not pull in the 300 KB episode-files manager). Sonarr I/O is injected as
a ``make_request`` callable with the SAME signature as
``BaseInstanceManager._make_request`` so the manager passes ``sonarr_api._make_request``
and the daemon passes its own thin HTTP client.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import threading
import time

from scripts.managers.machine_learning.acquisition.pilot_stepping import (
    choose_lowest_available_tier,
    classify_release_outcome,
)

# A long batch (thousands of interactive searches) must not look hung: emit a progress heartbeat
# (one log line + a snapshot the daemon's --status reads) at most this often.
PROGRESS_INTERVAL_S = 15.0
# How many completed stubs between cooperative-yield checks (only when a should_yield callback is
# supplied). Small enough to react within seconds; large enough that the queue glob is negligible.
_YIELD_CHECK_EVERY = 32


def unacquirable_key(instance: str) -> str:
    """global_cache key for the per-instance UNACQUIRABLE ledger. Single source of
    truth shared by the writer here and ``run_pilot_search``'s reader."""
    return f"sonarr/pilot/unacquirable/{instance}"


def progress_key(instance: str) -> str:
    """global_cache key for the live progress heartbeat of an in-flight pilot pass
    (``{total, done, reasons, updated_at}``); read by the daemon's --status so a long
    batch shows X/total done instead of appearing frozen."""
    return f"sonarr/pilot/progress/{instance}"


def diagnostics_key(instance: str) -> str:
    """global_cache key for the per-instance pilot ACQUISITION-REASON snapshot — the
    'why are pilots failing to acquire' breakdown (no_results / below_floor / rejected /
    available + top rejection reasons) written each pass and shown by the daemon's --status."""
    return f"sonarr/pilot/diagnostics/{instance}"


def checkpoint_key(instance: str) -> str:
    """global_cache key for the per-stub resume checkpoint (``{job_id, done: [sid, ...]}``).
    The core appends completed sids as it goes; on a daemon restart the SAME job (matched by
    ``job_id``) skips them instead of re-running the whole batch from zero."""
    return f"sonarr/pilot/checkpoint/{instance}"


def interactive_pilot_search(
    *,
    make_request,            # callable(instance, endpoint, method="GET", payload=None, fallback=None)
    logger,                  # exposes log_info / log_warning / log_debug
    global_cache,            # exposes get(key) / set(key, value); may be None
    instance: str,
    items: list,             # [(series_id, s01e01_episode_id), ...]
    ladder: list,            # ascending [(profile_id, max_resolution), ...] floor→widest
    meta: dict,              # {series_id: {"title": ..., "tvdb": ...}}
    current_indexers: list,  # indexer fingerprint captured at search time (for the re-check gate)
    floor_res: int,          # never set a series below this resolution
    max_workers: int,        # parallelism cap (series each own their profile, so they're independent)
    search_batch_size: int = 100,   # episodeIds per EpisodeSearch command (Sonarr accepts a list)
    search_no_resolution: bool = True,  # releases with no resolution (SD-only) → search at the floor tier
    skip_hard_rejects: bool = True,     # all releases rejected for profile-independent reasons → skip + flag
    soft_floor: bool = True,            # nothing at/above floor_res but sub-floor releases exist → grab the
                                        # best sub-floor (e.g. only 480 when floor is 720) instead of orphaning
    anime_ladder=None,              # [(profile_id, max_res)] ladder of [Anime] profiles
    anime_sids=None,                # series ids that should use anime_ladder (anime seriesType)
    job_id=None,                    # stable id of the offloaded job → enables per-stub resume
    skip_sids=None,                 # sids already completed by a prior interrupted run (skip them)
    should_yield=None,              # optional callable() -> bool; True → cooperatively stop early
                                    # (a higher-priority JIT grab is waiting) and resume via checkpoint
) -> dict:
    """Two phases per batch:

    PHASE 1 (concurrent) — for each stub pilot: ONE interactive search reveals availability; set
    the series to the LOWEST tier with results (or flag UNACQUIRABLE when nothing is found) and
    QUEUE its S01E01 id for the grab. No EpisodeSearch is fired yet.

    PHASE 2 (batched) — fire ``EpisodeSearch`` commands in chunks of ``search_batch_size`` episode
    ids. Sonarr's command takes a LIST, so a 9k-stub spree posts ~90 commands to Sonarr's task
    queue instead of 9k individual ones. Each series' profile was already set in phase 1, so the
    batched grab still picks each release at that series' own tier (Sonarr's quality + custom-format
    scoring chooses the release — we never grab by guid).

    Outcomes are collected under a lock and the UNACQUIRABLE ledger is MERGED once at the end so
    flags for stubs OUTSIDE this batch are preserved.

    Returns ``{"searched": [sid, ...], "flagged": {sid: entry, ...}}``.
    """
    lock = threading.Lock()
    flagged: dict = {}      # sid → ledger entry (UNACQUIRABLE this run)
    searched: set = set()   # sid whose batched search fired → clear any prior flag
    to_search: list = []    # [(sid, ep_id, res, pid)] profile set in phase 1, grabbed in phase 2
    reason_counts: Counter = Counter()    # why-it-acquired-or-not, aggregated for the diagnostics snapshot
    rejection_counts: Counter = Counter() # top Sonarr rejection reasons across the batch
    batch_only_count: Counter = Counter() # subset of 'rejected' available ONLY as season packs (SeasonSearch-grabbable)
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    items  = [(int(s), int(e)) for s, e in items if s is not None and e is not None]
    ladder = [(int(p), int(r)) for p, r in ladder if p is not None]
    anime_ladder = [(int(p), int(r)) for p, r in (anime_ladder or []) if p is not None]
    anime_sids = set(int(s) for s in (anime_sids or []))
    # Per-stub resume: skip stubs a prior interrupted run already completed (no restart-from-zero).
    _done: set = set(int(s) for s in (skip_sids or []))
    if _done:
        items = [(s, e) for s, e in items if s not in _done]
    if not items or not ladder:
        return {"searched": [], "flagged": {}}

    def _label(sid):
        m = meta.get(sid) or meta.get(str(sid)) or {}
        title = (m.get("title") or "").strip()
        return (f"sonarr/{instance} '{title}' (tvdb-{m.get('tvdb')})"
                if title else f"sonarr/{instance} series {sid}")

    def _set_profile(sid, pid) -> bool:
        """Re-fetch the series FRESH and PUT only its qualityProfileId, so the write lands
        against current Sonarr state (mirrors ``_pilot_set_profile``). False if the fetch came back
        empty OR the PUT failed (caller skips the search; the stub re-probes next run). Checking the
        PUT result is essential: a silently-failed flip would otherwise leave the series on its
        ORIGINAL (possibly 1080p-allowing) profile and the EpisodeSearch would over-grab above the
        floor — the exact bug this floor logic exists to prevent."""
        fresh = make_request(instance, f"series/{sid}", fallback=None)
        if not fresh or not isinstance(fresh, dict):
            return False
        if fresh.get("qualityProfileId") == pid:
            return True                                   # already on the target tier — nothing to do
        fresh = dict(fresh)
        fresh["qualityProfileId"] = pid
        # A failed write returns the None fallback; a successful PUT returns the updated series dict.
        return make_request(instance, f"series/{sid}", method="PUT", payload=fresh) is not None

    # ── Phase 1: discover availability + set each series' tier (concurrent) ──────────
    def _one(sid, ep_id):
        label = _label(sid)
        try:
            releases = make_request(
                instance, f"release?episodeId={ep_id}", fallback=None)
            if releases is None:
                # NO RESPONSE (timeout / rate-limit / HTTP error) — NOT a confirmed-empty result.
                # Coercing it to [] (the old behaviour) made a transient failure indistinguishable
                # from "indexers found nothing" → it got flagged UNACQUIRABLE and blacklisted for the
                # whole cooldown. Instead DEFER: re-probe next run, never flag on a failed search.
                with lock:
                    reason_counts["search_failed"] += 1
                logger.log_warning(
                    f"  ⏳ Pilot {label}: interactive search got no response (timeout / rate-limited "
                    f"/ error) — deferring, NOT flagged UNACQUIRABLE; re-probes next run")
                return
            if not isinstance(releases, list):
                releases = []
            # WHY-it-acquires-or-not: classify the indexer results (no_results / below_floor /
            # rejected / available) so the operator can see the reason, not just "UNACQUIRABLE".
            diag = classify_release_outcome(releases, floor_res=floor_res)
            rej = ", ".join(f"{m}×{c}" for m, c in diag["rejection_reasons"]) or "—"
            with lock:
                reason_counts[diag["reason"]] += 1
                if diag.get("batch_only"):
                    batch_only_count["n"] += 1
                for msg, c in diag["rejection_reasons"]:
                    rejection_counts[msg] += c
            # Anime series climb the [Anime] ladder (no x265 penalty); everything else the regular one.
            lad = anime_ladder if (anime_ladder and sid in anime_sids) else ladder
            pick = choose_lowest_available_tier(releases, lad, floor_res=floor_res)
            # Releases exist but report NO resolution (SD-only / odd) → search at the FLOOR tier so
            # Sonarr can still grab them, rather than skipping as UNACQUIRABLE.
            if pick is None and diag["reason"] == "no_resolution" and search_no_resolution and lad:
                pick = (lad[0][1], lad[0][0])
            # SOFT FLOOR: releases exist but EVERY one is BELOW floor_res (e.g. only 480/SD when the
            # floor is 720) → grab the best sub-floor release at the FLOOR tier rather than orphaning the
            # pilot UNACQUIRABLE. Setting the series to the floor profile and searching lets Sonarr grab
            # the best release that profile allows (the SD), so an SD-only show is still seeded; a show
            # that DOES have ≥floor releases never reaches here (choose_lowest_available_tier picked one).
            if pick is None and diag["reason"] == "below_floor" and soft_floor and lad:
                pick = (lad[0][1], lad[0][0])
            # Every usable release is rejected for a PROFILE-INDEPENDENT reason (size / blocklist /
            # incomplete) a flip can't fix → Sonarr would grab nothing, so skip the futile search and
            # flag with the reason (saves an indexer search + a queued command per stub).
            if pick is not None and diag["reason"] == "rejected_hard" and skip_hard_rejects:
                pick = None
            if pick is None:
                m = meta.get(sid) or meta.get(str(sid)) or {}
                entry = {"flagged_at": now_iso, "indexers": list(current_indexers),
                         "title": m.get("title"), "tvdb": m.get("tvdb"), "reason": diag["reason"]}
                if diag["rejection_reasons"]:
                    entry["rejections"] = diag["rejection_reasons"]
                with lock:
                    flagged[sid] = entry
                if diag["reason"] == "no_results":
                    why = "no S01E01 release at ANY resolution"
                elif diag["reason"] == "no_resolution":
                    why = (f"{diag['total']} release(s) found but NONE report a resolution "
                           f"(likely SD-only / odd releases — can't map to a quality tier)")
                elif diag["reason"] == "rejected_hard":
                    why = (f"{diag['total']} release(s) but ALL rejected for profile-independent "
                           f"reasons [{rej}] — a profile flip can't fix these (size/blocklist/"
                           f"incomplete persist), so the search is skipped")
                else:  # below_floor
                    why = (f"{diag['total']} release(s) but all below the ≥{floor_res}p floor "
                           f"(resolutions {diag['resolutions']})")
                logger.log_info(
                    f"  🚫 Pilot UNACQUIRABLE [{diag['reason']}]: {label} — {why} across "
                    f"{len(current_indexers)} indexer(s); blocked until an indexer is added or "
                    f"the re-check cooldown elapses")
                return
            res, pid = pick
            # Set the series to the lowest tier with results, then QUEUE the grab for the batched
            # EpisodeSearch in phase 2. The profile is set per-series HERE, so the later batched
            # search still grabs each episode at its own tier (Sonarr's CF picks the release).
            if not _set_profile(sid, pid):
                logger.log_warning(
                    f"  ⚠️ Pilot: {label} profile flip to tier ≤{res}p failed — skipping search")
                return
            with lock:
                to_search.append((sid, ep_id, res, pid))
            if diag["reason"] == "rejected":
                # A tier is available but EVERY release at/above the floor is currently rejected
                # (incomplete season pack / sample / size / quality-not-wanted). We still search —
                # a quality-profile rejection clears once we flip the profile — but a profile-
                # INDEPENDENT rejection persists and Sonarr will likely grab nothing. Surface it.
                logger.log_info(
                    f"  ⚠️ Pilot {label}: {diag['total']} release(s) at ≤{res}p but all REJECTED "
                    f"[{rej}] — searching anyway (profile flip clears quality rejections; "
                    f"incomplete/sample/size will persist)")
            else:
                # INFO (was debug) so EVERY stub's outcome is visible in the log, not just failures —
                # a batch where most stubs succeed quietly otherwise looks hung.
                logger.log_info(
                    f"  🎯 Pilot {label}: found a release at ≤{res}p (profile {pid}) — queued for "
                    f"batched search")
        except Exception as e:
            logger.log_warning(f"[PilotSearch] interactive search failed for {label}: {e}")

    # ── Progress heartbeat (so a long batch never looks frozen) + per-stub resume checkpoint ──
    done = 0
    completed_sids: set = set()   # sids finished THIS run (∪ skip_sids → the resume checkpoint)
    last_progress = time.monotonic()

    def _emit_progress(force=False):
        nonlocal last_progress
        now = time.monotonic()
        if not force and (now - last_progress) < PROGRESS_INTERVAL_S:
            return
        last_progress = now
        with lock:
            reasons = dict(reason_counts)
            done_snapshot = sorted(_done | completed_sids)
        if global_cache is not None:
            try:
                global_cache.set(progress_key(instance), {
                    "total": len(items), "done": done, "reasons": reasons,
                    "updated_at": datetime.now(tz=timezone.utc).isoformat()})
                if job_id is not None:   # resume checkpoint (only for offloaded jobs)
                    global_cache.set(checkpoint_key(instance),
                                     {"job_id": job_id, "done": done_snapshot})
            except Exception:
                pass
        logger.log_info(
            f"[PilotSearch] progress {done}/{len(items)} — reasons so far: {reasons}")

    yielded = False    # set True when we cooperatively stop early for a higher-priority JIT grab
    if len(items) <= 1:
        for sid, ep_id in items:
            _one(sid, ep_id)
            completed_sids.add(sid)
            done += 1
    else:
        with ThreadPoolExecutor(
            max_workers=max(1, min(int(max_workers), len(items))),
            thread_name_prefix="pilot-interactive",
        ) as ex:
            futures = {ex.submit(_one, sid, ep_id): sid for sid, ep_id in items}
            _since_yield_check = 0
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    logger.log_warning(f"[PilotSearch] interactive task crashed: {e}")
                with lock:
                    completed_sids.add(futures[fut])
                done += 1
                _emit_progress()
                # ── Cooperative yield: a long sweep must not block a time-sensitive JIT grab. Every
                # _YIELD_CHECK_EVERY completions, if a higher-priority job is waiting, cancel the
                # still-QUEUED (not-started) tasks and stop — the checkpoint (written by
                # _emit_progress) skips the done stubs when this batch resumes. In-flight tasks
                # finish as the pool unwinds; their sids may re-run on resume (a harmless re-search).
                _since_yield_check += 1
                if should_yield is not None and _since_yield_check >= _YIELD_CHECK_EVERY:
                    _since_yield_check = 0
                    try:
                        _do_yield = bool(should_yield())
                    except Exception:
                        _do_yield = False
                    if _do_yield:
                        for _f in futures:
                            if not _f.done():
                                _f.cancel()
                        yielded = True
                        logger.log_info(
                            f"[PilotSearch] yielding after {done}/{len(items)} stub(s) — a "
                            f"higher-priority JIT grab is queued; will resume from the checkpoint.")
                        break
    _emit_progress(force=True)

    # ── Phase 2: batched EpisodeSearch (one command per `search_batch_size` episodes) ──
    batch_size = max(1, int(search_batch_size or 1))
    fired_batches = 0
    total_batches = (len(to_search) + batch_size - 1) // batch_size
    for i in range(0, len(to_search), batch_size):
        chunk   = to_search[i:i + batch_size]
        ep_ids  = [e for _s, e, _r, _p in chunk]
        cmd = make_request(
            instance, "command", method="POST",
            payload={"name": "EpisodeSearch", "episodeIds": ep_ids})
        if cmd is None:
            logger.log_warning(
                f"  ⚠️ Pilot batched EpisodeSearch failed for {len(ep_ids)} episode(s) "
                f"(batch {fired_batches + 1}/{total_batches}); those stubs re-probe next run")
            continue
        fired_batches += 1
        for sid, _e, _r, _p in chunk:
            searched.add(sid)
        logger.log_info(
            f"  🔎 Pilot batched EpisodeSearch [{fired_batches}/{total_batches}]: {len(ep_ids)} "
            f"episode(s) at their lowest available tier — Sonarr's quality + custom-format scoring "
            f"grabs the best release for each")

    # Merge outcomes into the UNACQUIRABLE ledger (preserve entries for stubs not in this run):
    # drop the ones that had results (search fired), (re)flag the ones that came up empty.
    if global_cache is not None and (flagged or searched):
        try:
            key = unacquirable_key(instance)
            ledger = dict(global_cache.get(key) or {})
            for sid in searched:
                ledger.pop(str(sid), None)
            for sid, entry in flagged.items():
                ledger[str(sid)] = entry
            global_cache.set(key, ledger)
        except Exception as e:
            logger.log_debug(f"[PilotSearch] unacquirable ledger update skipped: {e}")
    # Persist the acquisition-reason snapshot (overwrite — it's a per-pass view) so the daemon's
    # --status can answer "why are pilots failing to acquire": no_results / below_floor / rejected
    # (incomplete blocks etc.) / available, plus the top Sonarr rejection reasons.
    if global_cache is not None and reason_counts:
        try:
            global_cache.set(diagnostics_key(instance), {
                "at": now_iso,
                "instance": instance,
                "stubs": len(items),
                "reasons": dict(reason_counts),
                "batch_only": batch_only_count["n"],   # of the 'rejected', how many are season-pack-only
                "top_rejections": [[m, c] for m, c in rejection_counts.most_common(10)],
            })
        except Exception as e:
            logger.log_debug(f"[PilotSearch] diagnostics snapshot skipped: {e}")
    if flagged or searched:
        logger.log_info(
            f"[PilotSearch] interactive pass: {len(searched)} searched at the lowest available tier "
            f"in {fired_batches} batch(es) of ≤{batch_size} (Sonarr CF picks the release), "
            f"{len(flagged)} flagged UNACQUIRABLE. Reasons: {dict(reason_counts)}"
            + (f"; top rejections: {rejection_counts.most_common(5)}" if rejection_counts else ""))
    return {"searched": sorted(searched), "flagged": flagged, "reasons": dict(reason_counts),
            "yielded": yielded}

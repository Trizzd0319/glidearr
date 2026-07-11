"""
pilot_720_upgrade.py — the shared "raise existing sub-720 pilots to 720" core.
==============================================================================
Reprofile a genuine pilot stub to its family 720 cap (so it physically cannot grab >720), then
EpisodeSearch ONLY the pilot episode so Sonarr upgrades a 480/576 file to 720 IN PLACE — no delete,
so an SD-only show with no 720 release keeps its file.

Mirrors ``legacy_regrab.run_legacy_regrab``: a core with Sonarr I/O injected as a ``make_request``
callable, shared by two callers —
  * the ``sonarr_upgrade_pilots_720`` tool — small inline batches + dry-run preview.
  * ``pilot_search_daemon.py`` (mode ``"pilot_720"``) — large batches drained OUT-OF-PROCESS, paced
    (chunk-by-chunk), cooperatively YIELDING to a time-sensitive JIT grab, and RESUMABLE — instead of
    blasting thousands of EpisodeSearch commands into Sonarr's queue at once.

The ledger (``global_cache 'sonarr/pilot720/{instance}'``) is BOTH a cooldown (don't re-search a pilot
within ``cooldown_days``) AND the resume checkpoint (each searched series is recorded AS IT HAPPENS, so
a crash / cooperative yield resumes without re-searching the finished ones).
"""
from __future__ import annotations

from datetime import datetime, timezone


def ledger_key(instance: str) -> str:
    """global_cache key for the per-instance cooldown/resume ledger ({series_id: {at, result}})."""
    return f"sonarr/pilot720/{instance}"


def _resolve_pilot_episode_id(make_request, instance, sid, season, episode):
    """Sonarr episode id for the series' pilot: match (season, episode); fall back to S01E01. None
    when unresolved — the caller then skips it (we NEVER SeriesSearch, which could grab a whole season)."""
    eps = make_request(instance, f"episode?seriesId={sid}", fallback=None)
    if not eps:
        return None
    want = []
    if season is not None and episode is not None:
        try:
            want.append((int(season), int(episode)))
        except (TypeError, ValueError):
            pass
    want.append((1, 1))
    for s, e in want:
        for ep in eps:
            if ep.get("seasonNumber") == s and ep.get("episodeNumber") == e:
                return ep.get("id")
    return None


def run_pilot_720_upgrade(*, make_request, logger, global_cache, instance, items,
                          search_batch: int = 100, cooldown_days: int = 7,
                          dry_run: bool = False, should_yield=None) -> dict:
    """Process ``items`` — dicts ``{series_id, season, episode, series_title, target_profile_id,
    current_profile_id}`` (already eligibility-filtered + ordered by the caller).

    (1) Reprofile every series whose current profile != its target to the family 720 cap (bulk
    ``series/editor`` PUT, grouped by target) so a later search can't grab >720. (2) Resolve each
    pilot's episode id and EpisodeSearch it in chunks of ``search_batch`` — Sonarr upgrades 480/576 to
    720 on import, and with no >=720 release the existing file is kept (no delete). Between chunks it
    checkpoints; when ``should_yield()`` is true it stops early (``{"yielded": True}``) so the daemon
    can service a higher-priority JIT grab and resume from the checkpoint. Live writes the ledger
    incrementally; dry-run records NOTHING.

    Returns ``{reprofiled, searched, skipped_cooldown, unresolved, yielded}``."""
    items = [i for i in (items or []) if i.get("series_id") is not None]
    stats = {"reprofiled": 0, "searched": 0, "skipped_cooldown": 0, "unresolved": 0, "yielded": False}
    if not items:
        return stats

    lkey = ledger_key(instance)
    ledger = dict((global_cache.get(lkey) if global_cache else None) or {})
    now = datetime.now(tz=timezone.utc)

    def _recent(sid) -> bool:
        ent = ledger.get(str(int(sid)))
        if not ent:
            return False
        try:
            return (now - datetime.fromisoformat(ent.get("at"))).days < max(0, cooldown_days)
        except Exception:
            return False

    def _persist(sid):
        if global_cache is None or dry_run:
            return
        ledger[str(int(sid))] = {"at": datetime.now(tz=timezone.utc).isoformat(), "result": "searched"}
        try:
            global_cache.set(lkey, dict(ledger))
        except Exception:
            pass

    # cooldown = resume: skip series already searched (recently). Old entries expire -> re-eligible.
    todo = [i for i in items if not _recent(i["series_id"])]
    stats["skipped_cooldown"] = len(items) - len(todo)
    if not todo:
        return stats

    # (1) REPROFILE (bulk, grouped by target) — only series whose current profile differs from target.
    if not dry_run:
        by_target: dict = {}
        for i in todo:
            tid = i.get("target_profile_id")
            if tid is not None and i.get("current_profile_id") != tid:
                by_target.setdefault(int(tid), []).append(int(i["series_id"]))
        for tid, sids in by_target.items():
            for j in range(0, len(sids), 200):
                chunk = sids[j:j + 200]
                try:
                    make_request(instance, "series/editor", method="PUT",
                                 payload={"seriesIds": chunk, "qualityProfileId": tid})
                    stats["reprofiled"] += len(chunk)
                except Exception as e:
                    logger.log_warning(f"[Pilot720] reprofile chunk failed on '{instance}': {e}")

    # (2) SEARCH in chunks, resolving each pilot's episode id, with cooperative yield + checkpoint.
    batch = max(1, int(search_batch))
    for j in range(0, len(todo), batch):
        if should_yield is not None and should_yield():
            stats["yielded"] = True
            return stats
        chunk = todo[j:j + batch]
        ep_ids, done_sids = [], []
        for i in chunk:
            sid = int(i["series_id"])
            eid = _resolve_pilot_episode_id(make_request, instance, sid, i.get("season"), i.get("episode"))
            if eid is None:
                stats["unresolved"] += 1
                continue
            ep_ids.append(eid)
            done_sids.append(sid)
        if dry_run:
            stats["searched"] += len(ep_ids)
            continue
        if ep_ids:
            try:
                make_request(instance, "command", method="POST",
                             payload={"name": "EpisodeSearch", "episodeIds": ep_ids})
                stats["searched"] += len(ep_ids)
            except Exception as e:
                logger.log_warning(f"[Pilot720] EpisodeSearch chunk failed on '{instance}': {e}")
                continue
        for sid in done_sids:
            _persist(sid)
    return stats
